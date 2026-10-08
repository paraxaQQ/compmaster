from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from compmaster import server
from compmaster.core import (
    CompilationDraft,
    PlexConfig,
    TrackDraft,
    clock_seconds,
    detect_boundaries,
    draft_compilation,
    export_album,
    import_to_plex,
    parse_description_tracklist,
    plex_path_join,
    redact_error,
    validate_tracks,
)


FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


class DescriptionTests(unittest.TestCase):
    def test_parses_artist_title_and_bare_titles(self) -> None:
        description = '\n'.join([
            'tracklist',
            '0:00 Artist One - First Track',
            '03:12 Bare Second Track',
            '1:02:03 Artist Three — Last Track',
        ])

        tracks = parse_description_tracklist(description, 4000)

        self.assertEqual(len(tracks), 3)
        self.assertEqual((tracks[0].artist, tracks[0].title), ('Artist One', 'First Track'))
        self.assertEqual((tracks[1].artist, tracks[1].title), ('Unknown Artist', 'Bare Second Track'))
        self.assertEqual(tracks[2].start, 3723)

    def test_accepts_minute_counts_above_an_hour(self) -> None:
        self.assertEqual(clock_seconds('90:01'), 5401)

    def test_warns_when_description_leaves_a_long_final_track(self) -> None:
        info = {
            'title': 'Long Tail',
            'duration': 3000,
            'description': '0:00 one\n4:00 two\n8:00 three',
            'chapters': [],
            'thumbnail': '',
            'upload_date': '',
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch('compmaster.core.fetch_source', return_value=('source.wav', info)),
            patch('compmaster.core.detect_boundaries', return_value=[]),
        ):
            draft = draft_compilation('source', Path(temporary))

        self.assertRegex(draft.warnings[0], r'final supplied track still runs 42:00')

    def test_fills_a_long_uncovered_tail_without_removing_supplied_tracks(self) -> None:
        info = {
            'title': 'Incomplete Tracklist',
            'duration': 2400,
            'description': '0:00 one\n5:00 two\n15:00 three',
            'chapters': [],
            'thumbnail': '',
            'upload_date': '',
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch('compmaster.core.fetch_source', return_value=('source.wav', info)),
            patch(
                'compmaster.core.detect_boundaries',
                return_value=[(1350.0, 0.8), (1800.0, 0.9)],
            ),
        ):
            draft = draft_compilation('source', Path(temporary))

        self.assertEqual([track.title for track in draft.tracks[:3]], ['one', 'two', 'three'])
        self.assertEqual(draft.tracks[2].end, 1350)
        self.assertEqual(draft.tracks[-1].end, 2400)
        self.assertRegex(draft.warnings[0], r'Audio analysis added 2 unlabeled tracks')

    def test_preserves_a_description_entry_at_a_similar_program_boundary(self) -> None:
        info = {
            'title': 'Repeated Mix With Boundary Marker',
            'duration': 3391,
            'description': (
                '0:00 one\n2:59 two\n6:00 three\n9:51 four\n13:39 five\n'
                '17:16 six\n20:39 seven\n24:49 eight\n28:15 duplicate marker'
            ),
            'chapters': [],
            'thumbnail': '',
            'upload_date': '',
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch('compmaster.core.fetch_source', return_value=('source.wav', info)),
            patch(
                'compmaster.core.detect_boundaries',
                return_value=[
                    (1879.0, 0.89), (2119.0, 0.72), (2331.0, 0.80), (2543.0, 0.88),
                    (2755.0, 0.84), (2967.0, 0.83), (3179.0, 0.78),
                ],
            ),
        ):
            draft = draft_compilation('source', Path(temporary))

        self.assertEqual(len(draft.tracks), 16)
        self.assertEqual(draft.tracks[8].title, 'duplicate marker')
        self.assertEqual(draft.tracks[8].end, 1879)
        self.assertEqual(draft.tracks[-1].title, 'Repeated Mix With Boundary Marker — 52:59')
        self.assertEqual(draft.tracks[-1].end, 3391)

    def test_merges_matching_description_and_chapter_evidence(self) -> None:
        info = {
            'title': 'Hybrid Mix',
            'duration': 600,
            'description': '0:00 one\n3:00 two\n6:00 three',
            'chapters': [
                {'start_time': 0, 'end_time': 180, 'title': 'one'},
                {'start_time': 180, 'end_time': 360, 'title': 'two'},
                {'start_time': 360, 'end_time': 600, 'title': 'three'},
            ],
            'thumbnail': '',
            'upload_date': '',
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch('compmaster.core.fetch_source', return_value=('source.wav', info)),
            patch('compmaster.core.detect_boundaries', return_value=[]),
        ):
            draft = draft_compilation('source', Path(temporary))

        self.assertEqual(draft.segmentation, 'description+chapters')
        self.assertEqual(len(draft.tracks), 3)
        self.assertTrue(all(track.source == 'description+chapter' for track in draft.tracks))
        self.assertEqual([track.title for track in draft.tracks], ['one', 'two', 'three'])

    def test_uses_uploader_only_for_tracks_without_an_artist(self) -> None:
        info = {
            'title': 'Uploader Fallback',
            'duration': 600,
            'description': '0:00 Named Artist - one\n3:00 two',
            'chapters': [],
            'thumbnail': '',
            'upload_date': '',
            'uploader': 'Compilation Channel',
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch('compmaster.core.fetch_source', return_value=('source.wav', info)),
            patch('compmaster.core.detect_boundaries', return_value=[]),
        ):
            draft = draft_compilation('source', Path(temporary))

        self.assertEqual(draft.tracks[0].artist, 'Named Artist')
        self.assertEqual(draft.tracks[1].artist, 'Compilation Channel')
        self.assertEqual(draft.album_artist, 'Various Artists')


class ValidationTests(unittest.TestCase):
    def test_redacts_every_occurrence_of_a_plex_token(self) -> None:
        error = RuntimeError('request token-value failed; retry token-value')

        message = redact_error(error, 'token-value')

        self.assertEqual(message, 'request [redacted] failed; retry [redacted]')

    def test_rejects_overlapping_tracks(self) -> None:
        tracks = [
            TrackDraft(1, 0, 10, 'one'),
            TrackDraft(2, 9, 20, 'two'),
        ]

        with self.assertRaisesRegex(ValueError, 'overlaps'):
            validate_tracks(tracks, 20)

    def test_maps_local_album_to_plex_path(self) -> None:
        self.assertEqual(plex_path_join('/music', 'Various Artists', 'Night Shift'), '/music/Various Artists/Night Shift')
        self.assertEqual(plex_path_join(r'Z:\Music', 'Various Artists', 'Night Shift'), r'Z:\Music\Various Artists\Night Shift')

    def test_plex_import_scans_only_the_exported_album_path(self) -> None:
        draft = CompilationDraft(
            source='source',
            audio_path='source.wav',
            title='Night Shift',
            album_artist='Various Artists',
            year='',
            duration=10,
            description='',
            cover_url='',
            segmentation='description',
            tracks=[TrackDraft(1, 0, 10, 'one')],
        )
        server = Mock()
        server.friendlyName = 'Test Plex'
        section = server.library.section.return_value
        section.title = 'Music'
        with tempfile.TemporaryDirectory() as temporary:
            album = Path(temporary) / 'Various Artists' / 'Night Shift'
            config = PlexConfig('http://plex:32400', 'token', 'Music', temporary, '/music')
            with (
                patch('plexapi.server.PlexServer', return_value=server),
                patch(
                    'compmaster.core.export_album',
                    return_value={'path': str(album), 'files': ['01 - one.m4a'], 'warnings': []},
                ),
            ):
                result = import_to_plex(draft, config)

        section.update.assert_called_once_with(path='/music/Various Artists/Night Shift')
        self.assertTrue(result['scan_requested'])


class ServerTests(unittest.TestCase):
    def test_analysis_uses_selected_download_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            download_root = root / 'source downloads'
            store = server.JobStore(root / 'jobs')
            job = store.create('https://example.test/compilation', str(download_root))
            draft = CompilationDraft(
                source=job['source'],
                audio_path=str(download_root / 'compilation.webm'),
                title='Compilation',
                album_artist='Various Artists',
                year='',
                duration=180,
                description='',
                cover_url='',
                segmentation='analysis',
                tracks=[TrackDraft(index=1, start=0, end=180, title='Track 01')],
            )

            with (
                patch.object(server, 'STORE', store),
                patch('compmaster.server.draft_compilation', return_value=draft) as analyze,
            ):
                server._analyze(job['id'])

            analyze.assert_called_once_with(job['source'], download_root.resolve())
            self.assertTrue(download_root.is_dir())
            self.assertEqual(store.get(job['id'])['state'], 'ready')


class StaticUiTests(unittest.TestCase):
    def test_track_boundaries_are_editable_and_plex_copy_is_public(self) -> None:
        static = Path(__file__).parents[1] / 'compmaster' / 'static'
        app = (static / 'app.js').read_text(encoding='utf-8')
        page = (static / 'index.html').read_text(encoding='utf-8')
        readme = (Path(__file__).parents[1] / 'README.md').read_text(encoding='utf-8')

        self.assertIn('time-input track-start', app)
        self.assertIn('time-input track-end', app)
        self.assertIn("querySelector('.track-start')", app)
        self.assertIn("querySelector('.track-start').addEventListener('click'", app)
        self.assertIn("querySelector('.track-end').addEventListener('click'", app)
        self.assertIn('Plex setup', page)
        self.assertNotIn('Void supplies', page)
        self.assertNotIn('Void', readme)
        self.assertIn('source-download-root', page)
        self.assertIn('download_root: ui.sourceDownloadRoot.value.trim()', app)
        self.assertIn('Finished album folder', page)


@unittest.skipUnless(FFMPEG and FFPROBE, 'ffmpeg and ffprobe are required')
class MediaTests(unittest.TestCase):
    def test_audio_change_detector_finds_tone_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'tones.wav'
            subprocess.run(
                [
                    FFMPEG,
                    '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', 'sine=frequency=330:duration=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=1760:duration=12',
                    '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1[out]',
                    '-map', '[out]', str(source),
                ],
                check=True,
            )

            boundaries = detect_boundaries(str(source), 24, min_track_seconds=5)

            self.assertTrue(any(abs(at - 12) < 2 for at, _ in boundaries), boundaries)

    def test_exports_tagged_album_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.wav'
            cover_source = root / 'source-cover.jpg'
            output = root / 'library'
            subprocess.run(
                [FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', str(source)],
                check=True,
            )
            subprocess.run(
                [FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64', '-frames:v', '1', str(cover_source)],
                check=True,
            )
            draft = CompilationDraft(
                source=str(source),
                audio_path=str(source),
                title='Test Compilation',
                album_artist='Various Artists',
                year='2026',
                duration=4,
                description='',
                cover_url='https://example.test/cover.jpg',
                segmentation='analysis',
                tracks=[
                    TrackDraft(1, 0, 2, 'Opening', 'First Artist'),
                    TrackDraft(2, 2, 4, 'Closing', 'Second Artist'),
                ],
            )

            def provide_cover(_url: str, directory: Path) -> tuple[Path, str]:
                destination = directory / 'cover.jpg'
                shutil.copy2(cover_source, destination)
                return destination, ''

            with patch('compmaster.core._download_cover', side_effect=provide_cover):
                result = export_album(draft, str(output))

            album = Path(result['path'])
            self.assertEqual(len(list(album.glob('*.m4a'))), 2)
            self.assertTrue((album / 'cover.jpg').is_file())
            manifest = json.loads((album / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['album'], 'Test Compilation')
            probe = subprocess.run(
                [FFPROBE, '-v', 'error', '-show_entries', 'format_tags', '-of', 'json', str(album / result['files'][0])],
                capture_output=True,
                text=True,
                check=True,
            )
            tags = json.loads(probe.stdout)['format']['tags']
            self.assertEqual(tags['album'], 'Test Compilation')
            self.assertEqual(tags['album_artist'], 'Various Artists')
            self.assertEqual(tags['artist'], 'First Artist')
            stream_probe = subprocess.run(
                [
                    FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type:stream_disposition',
                    '-of', 'json', str(album / result['files'][0]),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            streams = json.loads(stream_probe.stdout)['streams']
            pictures = [stream for stream in streams if stream.get('codec_type') == 'video']
            self.assertEqual(len(pictures), 1)
            self.assertEqual(pictures[0]['disposition']['attached_pic'], 1)


if __name__ == '__main__':
    unittest.main()
