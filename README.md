# compmaster

compmaster turns a long compilation into one tagged album. it takes a youtube URL or local media file, finds track boundaries, lets us correct the result in a browser, writes AAC files, and can request a targeted Plex library scan.

boundary sources are tried in this order:

1. timestamps in the source description
2. source chapters
3. audio-change analysis with ffmpeg

description entries without an `artist - title` split use the YouTube uploader as their artist fallback. supplied artist names always win, and the uploader is never used for track matching or boundary detection. sources without uploader metadata remain `Unknown Artist`.

description and chapter timestamps are merged into one supplied track plan. matching entries become one `description+chapter` row rather than duplicates. compmaster still analyzes the full recording and fills implausibly long uncovered spans. analysis-only rows receive deterministic `<album> — <timestamp>` working titles; supplied titles are never removed or replaced by similarity guesses.

## storage

the source download folder is set beside the source URL and remembered by the browser. when it is blank, downloaded source media stays under `%LOCALAPPDATA%\Compmaster\jobs\<job-id>\` on Windows. local source files are read in place and never copied.

the finished album folder is set in the bottom action bar. exported albums use `<finished album folder>/<album artist>/<album>/`.

the youtube thumbnail is saved as `cover.jpg` in the album folder and embedded as attached artwork in every exported m4a track.

## run

requirements: Python 3.11+, ffmpeg, and ffprobe on `PATH`.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\compmaster
```

the server listens only on `127.0.0.1:8765` and opens the workbench in the default browser. use `compmaster --no-browser` to suppress that.

## plex setup

the Plex form needs:

- server URL and access token
- music library name
- the local folder where compmaster writes albums
- the same folder as Plex sees it, which can differ across a container or network mount

compmaster tests the connection before writing anything. on import it writes the album under `<music root>/<album artist>/<album>/`, then requests a partial scan for that exact Plex-visible album path. the token exists only in the request and browser tab. it is redacted from errors before job state is written.

## license

MIT. see `LICENSE`.

live Plex access is intentionally unverified until the adapter is tested against a real server with both path roots configured.

## checks

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m pip check
```
