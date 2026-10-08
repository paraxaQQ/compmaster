from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from compmaster import __version__
from compmaster.core import (
    CompilationDraft,
    PlexConfig,
    TrackDraft,
    draft_compilation,
    export_album,
    import_to_plex,
    redact_error,
    test_plex,
)


LOG = logging.getLogger('compmaster')
STATIC_DIR = Path(__file__).with_name('static')


def default_data_dir() -> Path:
    if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA']) / 'Compmaster'
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base / 'compmaster'


class TrackInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    index: int = Field(ge=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(default='Unknown Artist', max_length=300)
    source: str = Field(default='manual', max_length=30)
    confidence: float = Field(default=1.0, ge=0, le=1)
    include: bool = True


class DraftInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str = Field(min_length=1, max_length=300)
    album_artist: str = Field(min_length=1, max_length=300)
    year: str = Field(default='', max_length=10)
    tracks: list[TrackInput] = Field(min_length=1, max_length=500)


class CreateJobInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source: str = Field(min_length=1, max_length=2048)
    download_root: str = Field(default='', max_length=2048)


class ExportInput(DraftInput):
    output_root: str = Field(min_length=1, max_length=2048)


class PlexInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    url: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=1, max_length=2048)
    library: str = Field(min_length=1, max_length=300)
    music_root: str = Field(default='', max_length=2048)
    plex_music_root: str = Field(default='', max_length=2048)


class PlexImportInput(DraftInput):
    plex: PlexInput


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        for path in self.root.glob('*/job.json'):
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                LOG.exception('could not load job state from %s', path)
                continue
            job_id = str(job.get('id') or '')
            if job_id:
                if job.get('state') in {'analyzing', 'exporting', 'importing'}:
                    job.update(state='error', message='Compmaster stopped before this job finished.')
                self.jobs[job_id] = job

    def create(self, source: str, download_root: str = '') -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        job = {
            'id': job_id,
            'state': 'queued',
            'message': 'Waiting to analyze the source.',
            'source': source,
            'download_root': download_root,
            'draft': None,
            'result': None,
            'error': '',
        }
        with self.lock:
            self.jobs[job_id] = job
            self.save(job_id)
        return job

    def directory(self, job_id: str) -> Path:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        path = (self.root / job_id).resolve()
        if path.parent != self.root:
            raise ValueError('invalid job id')
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            job = self.get(job_id)
            job.update(changes)
            self.save(job_id)
            return job

    def save(self, job_id: str) -> None:
        job = self.jobs[job_id]
        directory = self.root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / 'job.json'
        temporary = directory / 'job.json.tmp'
        temporary.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)

    def public(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        public = {key: value for key, value in job.items() if key != 'draft'}
        if job.get('draft'):
            public['draft'] = CompilationDraft.from_dict(job['draft']).to_dict(public=True)
        return public


DATA_DIR = Path(os.environ.get('COMPMASTER_DATA_DIR') or default_data_dir())
STORE = JobStore(DATA_DIR / 'jobs')
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='compmaster')
app = FastAPI(title='Compmaster', version=__version__)
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


def _job_or_404(job_id: str) -> dict[str, Any]:
    try:
        return STORE.get(job_id)
    except KeyError as error:
        raise HTTPException(404, 'No such Compmaster job.') from error


def _draft_from_input(job: dict[str, Any], data: DraftInput) -> CompilationDraft:
    if not job.get('draft'):
        raise HTTPException(409, 'This source has not finished analysis.')
    draft = CompilationDraft.from_dict(job['draft'])
    draft.title = data.title.strip()
    draft.album_artist = data.album_artist.strip()
    draft.year = data.year.strip()
    draft.tracks = [TrackDraft.from_dict(track.model_dump()) for track in data.tracks]
    return draft


def _analyze(job_id: str) -> None:
    job = STORE.get(job_id)
    STORE.update(job_id, state='analyzing', message='Downloading and reading the compilation.', error='')
    try:
        download_root = str(job.get('download_root') or '').strip()
        workdir = Path(download_root).expanduser().resolve() if download_root else STORE.directory(job_id)
        workdir.mkdir(parents=True, exist_ok=True)
        draft = draft_compilation(str(job['source']), workdir)
        STORE.update(
            job_id,
            state='ready',
            message=f'Found {len(draft.tracks)} track boundaries from {draft.segmentation}.',
            draft=draft.to_dict(),
        )
    except Exception as error:
        LOG.exception('analysis failed for job %s', job_id)
        STORE.update(job_id, state='error', message='Analysis stopped.', error=str(error))


def _export(job_id: str, draft: CompilationDraft, output_root: str) -> None:
    STORE.update(job_id, state='exporting', message='Splitting and tagging the album.', error='', draft=draft.to_dict())
    try:
        result = export_album(draft, output_root)
        STORE.update(job_id, state='complete', message='Album exported.', result=result)
    except Exception as error:
        LOG.exception('export failed for job %s', job_id)
        STORE.update(job_id, state='error', message='Export stopped.', error=str(error))


def _plex_import(job_id: str, draft: CompilationDraft, config: PlexConfig) -> None:
    STORE.update(job_id, state='importing', message='Writing the album and asking Plex to scan it.', error='', draft=draft.to_dict())
    try:
        result = import_to_plex(draft, config)
        STORE.update(job_id, state='complete', message='Album written and Plex scan requested.', result=result)
    except Exception as error:
        safe_error = redact_error(error, config.token)
        LOG.error('plex import failed for job %s (%s): %s', job_id, error.__class__.__name__, safe_error)
        STORE.update(job_id, state='error', message='Plex import stopped.', error=safe_error)


@app.get('/')
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / 'index.html')


@app.get('/api/state')
def state() -> dict[str, Any]:
    return {
        'version': __version__,
        'source_cache': str((DATA_DIR / 'jobs').resolve()),
        'jobs': [STORE.public(job_id) for job_id in reversed(list(STORE.jobs)[-10:])],
    }


@app.post('/api/jobs', status_code=202)
def create_job(data: CreateJobInput) -> dict[str, Any]:
    job = STORE.create(data.source.strip(), data.download_root.strip())
    EXECUTOR.submit(_analyze, job['id'])
    return STORE.public(job['id'])


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str) -> dict[str, Any]:
    _job_or_404(job_id)
    return STORE.public(job_id)


@app.get('/api/jobs/{job_id}/audio')
def job_audio(job_id: str) -> FileResponse:
    job = _job_or_404(job_id)
    if not job.get('draft'):
        raise HTTPException(409, 'This source has not finished analysis.')
    path = Path(CompilationDraft.from_dict(job['draft']).audio_path)
    if not path.is_file():
        raise HTTPException(410, 'The analyzed source audio is no longer available.')
    return FileResponse(path)


@app.post('/api/jobs/{job_id}/export', status_code=202)
def export_job(job_id: str, data: ExportInput) -> dict[str, Any]:
    job = _job_or_404(job_id)
    if job['state'] in {'analyzing', 'exporting', 'importing'}:
        raise HTTPException(409, 'This job is already running.')
    draft = _draft_from_input(job, data)
    STORE.update(
        job_id,
        state='exporting',
        message='Splitting and tagging the album.',
        error='',
        draft=draft.to_dict(),
    )
    EXECUTOR.submit(_export, job_id, draft, data.output_root.strip())
    return STORE.public(job_id)


@app.post('/api/plex/test')
def plex_test(data: PlexInput) -> dict[str, str]:
    try:
        return test_plex(PlexConfig(**data.model_dump()))
    except Exception as error:
        safe_error = redact_error(error, data.token)
        LOG.error('plex connection test failed (%s): %s', error.__class__.__name__, safe_error)
        raise HTTPException(502, safe_error) from error


@app.post('/api/jobs/{job_id}/plex', status_code=202)
def plex_import(job_id: str, data: PlexImportInput) -> dict[str, Any]:
    job = _job_or_404(job_id)
    if job['state'] in {'analyzing', 'exporting', 'importing'}:
        raise HTTPException(409, 'This job is already running.')
    draft = _draft_from_input(job, data)
    config = PlexConfig(**data.plex.model_dump())
    STORE.update(
        job_id,
        state='importing',
        message='Writing the album and asking Plex to scan it.',
        error='',
        draft=draft.to_dict(),
    )
    EXECUTOR.submit(_plex_import, job_id, draft, config)
    return STORE.public(job_id)


def main() -> None:
    parser = argparse.ArgumentParser(prog='compmaster', description='Split compilations into tagged albums.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--version', action='version', version=__version__)
    args = parser.parse_args()
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise SystemExit(f'{tool} is not on PATH')
    url = f'http://127.0.0.1:{args.port}'
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    import uvicorn

    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='info')


if __name__ == '__main__':
    main()
