from __future__ import annotations

import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import requests


TIMESTAMP_LINE = re.compile(
    r'^\s*(?:[-*]\s*)?\[?(?P<time>(?:\d{1,2}:)?\d{1,2}:\d{2})\]?'
    r'\s*(?:[-–—|:]\s*)?(?P<label>\S.*?)\s*$',
    re.MULTILINE,
)
LABEL_SPLIT = re.compile(r'\s+[-–—]\s+')
WINDOWS_RESERVED = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


@dataclass(slots=True)
class TrackDraft:
    index: int
    start: float
    end: float
    title: str
    artist: str = 'Unknown Artist'
    source: str = 'chapter'
    confidence: float = 1.0
    include: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackDraft:
        return cls(
            index=int(data['index']),
            start=float(data['start']),
            end=float(data['end']),
            title=str(data['title']).strip(),
            artist=str(data.get('artist') or 'Unknown Artist').strip(),
            source=str(data.get('source') or 'manual'),
            confidence=float(data.get('confidence', 1.0)),
            include=bool(data.get('include', True)),
        )


@dataclass(slots=True)
class CompilationDraft:
    source: str
    audio_path: str
    title: str
    album_artist: str
    year: str
    duration: float
    description: str
    cover_url: str
    segmentation: str
    tracks: list[TrackDraft]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, public: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if public:
            data.pop('audio_path', None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompilationDraft:
        return cls(
            source=str(data['source']),
            audio_path=str(data['audio_path']),
            title=str(data['title']),
            album_artist=str(data.get('album_artist') or 'Various Artists'),
            year=str(data.get('year') or ''),
            duration=float(data['duration']),
            description=str(data.get('description') or ''),
            cover_url=str(data.get('cover_url') or ''),
            segmentation=str(data.get('segmentation') or 'manual'),
            tracks=[TrackDraft.from_dict(track) for track in data.get('tracks') or []],
            warnings=[str(warning) for warning in data.get('warnings') or []],
        )


@dataclass(slots=True)
class PlexConfig:
    url: str
    token: str
    library: str
    music_root: str
    plex_music_root: str


def redact_error(error: Exception, *secrets: str) -> str:
    message = str(error) or error.__class__.__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, '[redacted]')
    return message


def clock_seconds(value: str) -> float:
    parts = [int(part) for part in value.split(':')]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
        invalid = seconds >= 60
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        invalid = minutes >= 60 or seconds >= 60
    else:
        raise ValueError(f'invalid timestamp: {value}')
    if invalid:
        raise ValueError(f'invalid timestamp: {value}')
    return float(hours * 3600 + minutes * 60 + seconds)


def split_label(label: str) -> tuple[str, str]:
    clean = re.sub(r'^\s*\d{1,3}\s*(?:[.)-]+|\s)\s*', '', label).strip()
    parts = LABEL_SPLIT.split(clean, maxsplit=1)
    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()
    return 'Unknown Artist', clean or 'Untitled'


def parse_description_tracklist(description: str, duration: float) -> list[TrackDraft]:
    entries: list[tuple[float, str]] = []
    seen: set[float] = set()
    for match in TIMESTAMP_LINE.finditer(description or ''):
        start = clock_seconds(match.group('time'))
        label = match.group('label').strip()
        if start >= duration or start in seen:
            continue
        seen.add(start)
        entries.append((start, label))
    entries.sort(key=lambda item: item[0])
    if len(entries) < 2:
        return []
    tracks: list[TrackDraft] = []
    for index, (start, label) in enumerate(entries, 1):
        end = entries[index][0] if index < len(entries) else duration
        artist, title = split_label(label)
        tracks.append(TrackDraft(index, start, end, title, artist, 'description', 1.0))
    return tracks


def tracks_from_chapters(chapters: list[dict[str, Any]], duration: float) -> list[TrackDraft]:
    tracks: list[TrackDraft] = []
    for index, chapter in enumerate(chapters, 1):
        start = float(chapter.get('start_time') or 0)
        end = float(chapter.get('end_time') or duration)
        if end <= start or start >= duration:
            continue
        artist, title = split_label(str(chapter.get('title') or f'Track {index:02d}'))
        tracks.append(TrackDraft(index, start, min(end, duration), title, artist, 'chapter', 1.0))
    return tracks if len(tracks) >= 2 else []


def merge_supplied_tracks(
    description_tracks: list[TrackDraft],
    chapter_tracks: list[TrackDraft],
    duration: float,
) -> tuple[list[TrackDraft], str]:
    if not description_tracks:
        return chapter_tracks, 'chapters' if chapter_tracks else 'analysis'
    if not chapter_tracks:
        return description_tracks, 'description'
    merged: list[TrackDraft] = []
    matched_chapters: set[int] = set()
    for description_track in description_tracks:
        match = min(
            (
                (index, chapter_track)
                for index, chapter_track in enumerate(chapter_tracks)
                if index not in matched_chapters and abs(chapter_track.start - description_track.start) <= 2.0
            ),
            key=lambda item: abs(item[1].start - description_track.start),
            default=None,
        )
        if match is None:
            merged.append(description_track)
            continue
        chapter_index, chapter_track = match
        matched_chapters.add(chapter_index)
        artist = description_track.artist
        if artist == 'Unknown Artist' and chapter_track.artist != 'Unknown Artist':
            artist = chapter_track.artist
        merged.append(
            TrackDraft(
                index=0,
                start=chapter_track.start,
                end=chapter_track.end,
                title=description_track.title,
                artist=artist,
                source='description+chapter',
                confidence=1.0,
            )
        )
    merged.extend(
        chapter_track
        for index, chapter_track in enumerate(chapter_tracks)
        if index not in matched_chapters
    )
    merged.sort(key=lambda track: track.start)
    for index, track in enumerate(merged, 1):
        track.index = index
        track.end = merged[index].start if index < len(merged) else duration
    return merged, 'description+chapters'


def probe_duration(path: str) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def fetch_source(source: str, workdir: Path) -> tuple[str, dict[str, Any]]:
    local = Path(source).expanduser()
    if local.is_file():
        path = str(local.resolve())
        return path, {
            'title': local.stem,
            'duration': probe_duration(path),
            'description': '',
            'chapters': [],
            'thumbnail': '',
            'upload_date': '',
        }
    if not re.match(r'^https?://', source, re.I):
        raise ValueError('source must be an existing local file or an http(s) URL')
    import yt_dlp

    options = {
        'format': 'bestaudio/best',
        'outtmpl': str(workdir / '%(title).150B [%(id)s].%(ext)s'),
        'noplaylist': True,
        'windowsfilenames': os.name == 'nt',
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(source, download=True)
    downloads = info.get('requested_downloads') or []
    path = downloads[0].get('filepath') if downloads else None
    if not path:
        raise RuntimeError('yt-dlp downloaded the source but did not report its path')
    return str(path), info


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _feature_frames(path: str) -> list[dict[str, float]]:
    filters = (
        'aresample=8000,'
        'asetnsamples=n=4000:p=0,'
        'astats=metadata=1:reset=1:measure_overall=none:measure_perchannel=RMS_level,'
        'aspectralstats=win_size=4096:overlap=0:measure=centroid+flatness+flux,'
        'ametadata=mode=print:file=-'
    )
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-vn', '-af', filters, '-f', 'null', '-'],
        capture_output=True,
        text=True,
        check=True,
    )
    frames: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for line in result.stdout.splitlines():
        if line.startswith('frame:'):
            if {'time', 'rms', 'centroid', 'flatness', 'flux'} <= current.keys():
                frames.append(current)
            match = re.search(r'pts_time:([\d.]+)', line)
            current = {'time': float(match.group(1))} if match else {}
        elif '=' in line:
            key, raw = line.rsplit('=', 1)
            name = {
                'lavfi.astats.1.RMS_level': 'rms',
                'lavfi.aspectralstats.1.centroid': 'centroid',
                'lavfi.aspectralstats.1.flatness': 'flatness',
                'lavfi.aspectralstats.1.flux': 'flux',
            }.get(key)
            if name:
                try:
                    current[name] = float(raw)
                except ValueError:
                    current[name] = -120.0 if name == 'rms' else 0.0
    if {'time', 'rms', 'centroid', 'flatness', 'flux'} <= current.keys():
        frames.append(current)
    return frames


def detect_boundaries(path: str, duration: float, min_track_seconds: float = 45.0) -> list[tuple[float, float]]:
    frames = _feature_frames(path)
    if len(frames) < 40:
        return []
    metrics = ('rms', 'centroid', 'flatness')
    scales: dict[str, float] = {}
    for metric in metrics:
        values = [frame[metric] for frame in frames if math.isfinite(frame[metric])]
        centre = _median(values)
        mad = _median([abs(value - centre) for value in values])
        floor = {'rms': 3.0, 'centroid': 180.0, 'flatness': 0.015}[metric]
        scales[metric] = max(mad * 3.0, floor)

    scores: list[tuple[float, float]] = []
    radius = 16
    gap = 4
    for index in range(radius + gap, len(frames) - radius - gap):
        at = frames[index]['time']
        if at < min_track_seconds or duration - at < min_track_seconds:
            continue
        left = frames[index - radius - gap:index - gap]
        right = frames[index + gap:index + radius + gap]
        score = 0.0
        for metric, weight in (('rms', 0.55), ('centroid', 1.15), ('flatness', 0.8)):
            delta = abs(_median([row[metric] for row in left]) - _median([row[metric] for row in right]))
            score += weight * delta / scales[metric]
        flux_base = _median([row['flux'] for row in frames])
        score += min(1.5, frames[index]['flux'] / max(flux_base * 8.0, 1e-7)) * 0.35
        scores.append((at, score))
    if not scores:
        return []

    baseline = _median([score for _, score in scores])
    spread = _median([abs(score - baseline) for _, score in scores])
    threshold = max(1.35, baseline + 3.5 * spread)
    candidates: list[tuple[float, float]] = []
    for index, item in enumerate(scores):
        at, score = item
        if score < threshold:
            continue
        neighbourhood = scores[max(0, index - 12):index + 13]
        if score >= max(value for _, value in neighbourhood):
            candidates.append((at, score))

    selected: list[tuple[float, float]] = []
    max_boundaries = max(0, int(duration // min_track_seconds) - 1)
    for at, score in sorted(candidates, key=lambda item: item[1], reverse=True):
        if all(abs(at - existing) >= min_track_seconds for existing, _ in selected):
            selected.append((at, score))
        if len(selected) >= max_boundaries:
            break
    if not selected:
        return []
    max_score = max(score for _, score in selected)
    return [(round(at, 3), round(score / max_score, 3)) for at, score in sorted(selected)]


def _boundary_path(
    start: float,
    end: float,
    candidates: list[tuple[float, float]],
    typical_duration: float,
) -> list[tuple[float, float]]:
    span = end - start
    desired_segments = max(2, round(span / typical_duration))
    available = sorted((at, confidence) for at, confidence in candidates if start < at < end)
    if not available:
        return []
    desired_segments = min(desired_segments, len(available) + 1)
    attempts = [desired_segments]
    for offset in range(1, 3):
        attempts.extend((desired_segments - offset, desired_segments + offset))
    for segment_count in attempts:
        if segment_count < 2 or segment_count > len(available) + 1:
            continue
        segment_target = span / segment_count
        minimum = max(60.0, segment_target * 0.35)
        maximum = segment_target * 2.2
        nodes = [*available, (end, 0.0)]
        states: dict[int, tuple[float, list[int]]] = {-1: (0.0, [])}
        for step in range(1, segment_count + 1):
            final_step = step == segment_count
            indexes = [len(nodes) - 1] if final_step else range(len(available))
            next_states: dict[int, tuple[float, list[int]]] = {}
            for index in indexes:
                at, confidence = nodes[index]
                for previous_index, (previous_score, path) in states.items():
                    if previous_index >= index:
                        continue
                    previous_at = start if previous_index == -1 else nodes[previous_index][0]
                    segment_duration = at - previous_at
                    if not minimum <= segment_duration <= maximum:
                        continue
                    duration_penalty = ((segment_duration - segment_target) / segment_target) ** 2
                    score = previous_score - duration_penalty + (0.45 * confidence if not final_step else 0.0)
                    existing = next_states.get(index)
                    if existing is None or score > existing[0]:
                        next_states[index] = (score, [*path, index])
            states = next_states
            if not states:
                break
        endpoint = len(nodes) - 1
        if endpoint in states:
            return [nodes[index] for index in states[endpoint][1][:-1]]
    return []


def fill_uncovered_spans(
    tracks: list[TrackDraft],
    boundaries: list[tuple[float, float]],
    duration: float,
    album_title: str,
) -> tuple[list[TrackDraft], int]:
    if not tracks:
        return tracks, 0
    observed = [
        track.end - track.start
        for track in tracks[:-1]
        if 60.0 <= track.end - track.start <= 600.0
    ]
    typical_duration = _median(observed) or 240.0
    suspicious_duration = max(480.0, typical_duration * 2.25)
    working_title_prefix = re.sub(r'\s*\([^)]*\)\s*$', '', album_title).strip() or 'Untitled'
    filled: list[TrackDraft] = []
    additions = 0
    for track in tracks:
        span = track.end - track.start
        cuts = (
            _boundary_path(track.start, track.end, boundaries, typical_duration)
            if span >= suspicious_duration
            else []
        )
        if not cuts:
            filled.append(track)
            continue
        original_end = track.end
        track.end = cuts[0][0]
        filled.append(track)
        for cut_index, (at, confidence) in enumerate(cuts):
            next_at = cuts[cut_index + 1][0] if cut_index + 1 < len(cuts) else original_end
            filled.append(
                TrackDraft(
                    index=0,
                    start=at,
                    end=next_at,
                    title=f'{working_title_prefix} — {_clock_label(at)}',
                    source='analysis',
                    confidence=confidence,
                )
            )
            additions += 1
    for index, track in enumerate(filled, 1):
        track.index = index
    return filled, additions


def _clock_label(seconds: float) -> str:
    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'


def draft_compilation(source: str, workdir: Path) -> CompilationDraft:
    path, info = fetch_source(source, workdir)
    duration = float(info.get('duration') or 0) or probe_duration(path)
    description = str(info.get('description') or '')
    title = str(info.get('title') or Path(path).stem).strip()
    working_title_prefix = re.sub(r'\s*\([^)]*\)\s*$', '', title).strip() or 'Untitled'
    description_tracks = parse_description_tracklist(description, duration)
    chapter_tracks = tracks_from_chapters(info.get('chapters') or [], duration)
    tracks, segmentation = merge_supplied_tracks(description_tracks, chapter_tracks, duration)
    boundaries = detect_boundaries(path, duration)
    warnings: list[str] = []
    if tracks:
        evidence = segmentation
        tracks, additions = fill_uncovered_spans(tracks, boundaries, duration, title)
        if additions:
            segmentation = f'{evidence}+analysis'
            noun = 'track' if additions == 1 else 'tracks'
            warnings.append(
                f'Audio analysis added {additions} unlabeled {noun} inside long uncovered spans. '
                'Working titles use the album name and timestamp; review those rows before export.'
            )
    else:
        starts = [0.0, *(at for at, _ in boundaries)]
        confidences = [1.0, *(confidence for _, confidence in boundaries)]
        tracks = [
            TrackDraft(
                index=index,
                start=start,
                end=starts[index] if index < len(starts) else duration,
                title=f'{working_title_prefix} — {_clock_label(start)}',
                source='analysis',
                confidence=confidences[index - 1],
            )
            for index, start in enumerate(starts, 1)
        ]
        segmentation = 'analysis'
    uploader = str(info.get('uploader') or info.get('channel') or '').strip()
    if uploader:
        for track in tracks:
            if track.artist == 'Unknown Artist':
                track.artist = uploader
    if '+analysis' not in segmentation and segmentation != 'analysis' and len(tracks) >= 2:
        earlier_durations = [track.end - track.start for track in tracks[:-1]]
        last_duration = tracks[-1].end - tracks[-1].start
        if last_duration > max(900.0, _median(earlier_durations) * 3.0):
            minutes, seconds = divmod(round(last_duration), 60)
            warnings.append(
                f'The final supplied track still runs {minutes}:{seconds:02d} to the source end. '
                'Audio analysis could not find a stable path through that span.'
            )
    artists = {track.artist for track in tracks if track.artist != 'Unknown Artist'}
    album_artist = next(iter(artists)) if len(artists) == 1 and len(artists) == len({t.artist for t in tracks}) else 'Various Artists'
    upload_date = str(info.get('upload_date') or '')
    return CompilationDraft(
        source=source,
        audio_path=path,
        title=title,
        album_artist=album_artist,
        year=upload_date[:4] if len(upload_date) >= 4 else '',
        duration=duration,
        description=description,
        cover_url=str(info.get('thumbnail') or ''),
        segmentation=segmentation,
        tracks=tracks,
        warnings=warnings,
    )


def safe_component(value: str, fallback: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', value).strip().rstrip('. ')
    if clean.upper() in WINDOWS_RESERVED:
        clean = f'_{clean}'
    return (clean or fallback)[:120]


def validate_tracks(tracks: list[TrackDraft], duration: float) -> list[TrackDraft]:
    included = sorted((track for track in tracks if track.include), key=lambda track: track.start)
    if not included:
        raise ValueError('include at least one track')
    previous_end = 0.0
    for track in included:
        if not track.title.strip():
            raise ValueError(f'track {track.index} needs a title')
        if track.start < 0 or track.end > duration + 0.5 or track.end <= track.start:
            raise ValueError(f'track {track.index} has an invalid start or end time')
        if track.start < previous_end - 0.05:
            raise ValueError(f'track {track.index} overlaps the previous included track')
        previous_end = track.end
    return included


def _unique_album_dir(root: Path, album_artist: str, album: str) -> Path:
    artist_dir = root / safe_component(album_artist, 'Various Artists')
    base = safe_component(album, 'Untitled Compilation')
    candidate = artist_dir / base
    suffix = 2
    while candidate.exists():
        candidate = artist_dir / f'{base} ({suffix})'
        suffix += 1
    return candidate


def _download_cover(url: str, directory: Path) -> tuple[Path | None, str]:
    if not url:
        return None, ''
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        return None, f'cover download failed: {error}'
    content_type = response.headers.get('Content-Type', '').split(';', 1)[0].lower()
    suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}.get(content_type)
    if not suffix:
        return None, f'cover download skipped: unsupported content type {content_type or "unknown"}'
    if len(response.content) > 15 * 1024 * 1024:
        return None, 'cover download skipped: image exceeds 15 MB'
    cover = directory / 'cover.jpg'
    if suffix == '.jpg':
        cover.write_bytes(response.content)
        return cover, ''
    source = directory / f'.cover-source{suffix}'
    source.write_bytes(response.content)
    try:
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-frames:v', '1', '-c:v', 'mjpeg', str(cover)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        source.unlink(missing_ok=True)
        detail = error.stderr.strip() or str(error)
        return None, f'cover conversion failed: {detail}'
    source.unlink()
    return cover, ''


def export_album(draft: CompilationDraft, output_root: str) -> dict[str, Any]:
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    tracks = validate_tracks(draft.tracks, draft.duration)
    final_dir = _unique_album_dir(root, draft.album_artist, draft.title)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.compmaster-', dir=final_dir.parent))
    files: list[str] = []
    warnings = list(draft.warnings)
    try:
        cover, cover_warning = _download_cover(draft.cover_url, stage)
        if cover_warning:
            warnings.append(cover_warning)
        total = len(tracks)
        for position, track in enumerate(tracks, 1):
            filename = f'{position:02d} - {safe_component(track.title, f"Track {position:02d}")}.m4a'
            destination = stage / filename
            artist = track.artist.strip() or 'Unknown Artist'
            command = [
                'ffmpeg', '-v', 'error', '-y', '-ss', f'{track.start:.3f}', '-i', draft.audio_path,
            ]
            if cover:
                command += ['-i', str(cover)]
            command += [
                '-t', f'{track.end - track.start:.3f}', '-map', '0:a:0',
            ]
            if cover:
                command += ['-map', '1:v:0', '-c:v', 'copy', '-disposition:v:0', 'attached_pic']
            else:
                command += ['-vn']
            command += [
                '-c:a', 'aac', '-b:a', '256k',
                '-movflags', '+faststart', '-metadata', f'title={track.title}', '-metadata', f'artist={artist}',
                '-metadata', f'album={draft.title}', '-metadata', f'album_artist={draft.album_artist}',
                '-metadata', f'track={position}/{total}', '-metadata', 'disc=1/1', '-metadata', 'compilation=1',
            ]
            if draft.year:
                command += ['-metadata', f'date={draft.year}']
            command.append(str(destination))
            subprocess.run(command, capture_output=True, check=True)
            files.append(filename)
        manifest = {
            'version': 1,
            'source': draft.source,
            'album': draft.title,
            'album_artist': draft.album_artist,
            'year': draft.year,
            'segmentation': draft.segmentation,
            'files': files,
            'tracks': [track.to_dict() for track in tracks],
            'warnings': warnings,
        }
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(stage, final_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {'path': str(final_dir), 'files': files, 'warnings': warnings}


def plex_path_join(root: str, *parts: str) -> str:
    path_type = PureWindowsPath if '\\' in root and '/' not in root else PurePosixPath
    return str(path_type(root).joinpath(*parts))


def test_plex(config: PlexConfig) -> dict[str, str]:
    if not config.url.strip() or not config.token.strip() or not config.library.strip():
        raise ValueError('plex url, token, and music library are required')
    from plexapi.server import PlexServer

    server = PlexServer(config.url.rstrip('/'), config.token)
    section = server.library.section(config.library)
    return {'server': str(server.friendlyName), 'library': str(section.title)}


def import_to_plex(draft: CompilationDraft, config: PlexConfig) -> dict[str, Any]:
    if not config.music_root.strip() or not config.plex_music_root.strip():
        raise ValueError('local music root and plex-visible music root are required')
    connection = test_plex(config)
    exported = export_album(draft, config.music_root)
    relative = Path(exported['path']).relative_to(Path(config.music_root).expanduser().resolve())
    plex_album_path = plex_path_join(config.plex_music_root, *relative.parts)
    from plexapi.server import PlexServer

    server = PlexServer(config.url.rstrip('/'), config.token)
    section = server.library.section(config.library)
    section.update(path=plex_album_path)
    return {**connection, **exported, 'plex_path': plex_album_path, 'scan_requested': True}
