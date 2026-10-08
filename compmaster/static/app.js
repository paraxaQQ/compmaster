const byId = (id) => document.getElementById(id);

const ui = {
  appShell: byId('app-shell'),
  intake: byId('intake'),
  sourceForm: byId('source-form'),
  source: byId('source'),
  sourceHelp: byId('source-help'),
  sourceDownloadRoot: byId('source-download-root'),
  sourceDownloadHelp: byId('source-download-help'),
  analyze: byId('analyze'),
  status: byId('status'),
  statusDot: byId('status-dot'),
  statusMessage: byId('status-message'),
  statusDetail: byId('status-detail'),
  skeleton: byId('skeleton'),
  workspace: byId('workspace'),
  actions: byId('actions'),
  cover: byId('cover'),
  sourceTitle: byId('source-title'),
  segmentation: byId('segmentation'),
  duration: byId('duration'),
  trackCount: byId('track-count'),
  audio: byId('audio'),
  playhead: byId('playhead'),
  albumTitle: byId('album-title'),
  albumArtist: byId('album-artist'),
  albumYear: byId('album-year'),
  tracks: byId('tracks'),
  splitHere: byId('split-here'),
  outputRoot: byId('output-root'),
  export: byId('export'),
  plexOpen: byId('plex-open'),
  plexImport: byId('plex-import'),
  plexDialog: byId('plex-dialog'),
  plexClose: byId('plex-close'),
  plexForm: byId('plex-form'),
  plexUrl: byId('plex-url'),
  plexToken: byId('plex-token'),
  plexLibrary: byId('plex-library'),
  musicRoot: byId('music-root'),
  plexMusicRoot: byId('plex-music-root'),
  plexStatus: byId('plex-status'),
  plexTest: byId('plex-test'),
  plexConfirm: byId('plex-confirm'),
  version: byId('version'),
};

let currentJobId = '';
let draft = null;
let pollTimer = null;
let defaultSourceCache = '';

const folderKeys = {
  source: 'compmaster.sourceDownloadRoot',
  output: 'compmaster.outputRoot',
};

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function formatTime(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value / 60) % 60;
  const rest = value % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
    : `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

function parseTime(value) {
  const parts = String(value).trim().split(':').map(Number);
  if (parts.some((part) => !Number.isFinite(part) || part < 0)) return NaN;
  if (parts.length === 2 && parts[1] < 60) return parts[0] * 60 + parts[1];
  if (parts.length === 3 && parts[1] < 60 && parts[2] < 60) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return NaN;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json', ...(options.headers || {}) } : options.headers,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : `Request failed with status ${response.status}.`;
    throw new Error(detail);
  }
  return body;
}

function setButton(button, state, label) {
  button.dataset.state = state || '';
  button.disabled = state === 'loading';
  button.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
  if (label) button.textContent = label;
}

function showStatus(message, detail = '', tone = 'default', running = false) {
  ui.status.hidden = false;
  ui.status.dataset.tone = tone;
  ui.statusMessage.textContent = message;
  ui.statusDetail.textContent = detail;
  ui.statusDot.dataset.state = running ? 'running' : 'idle';
}

function validateSource() {
  const valid = ui.source.value.trim().length > 0;
  ui.source.setAttribute('aria-invalid', valid ? 'false' : 'true');
  ui.sourceHelp.dataset.tone = valid ? '' : 'error';
  ui.sourceHelp.textContent = valid
    ? 'Local paths are read by the Compmaster process. Nothing is uploaded.'
    : 'The source is empty. Paste a URL or enter a full local media path.';
  return valid;
}

function updateSourceDownloadHelp() {
  ui.sourceDownloadHelp.textContent = ui.sourceDownloadRoot.value.trim()
    ? 'YouTube source media is kept in this folder. Local files are still read in place.'
    : `Blank uses ${defaultSourceCache || "Compmaster's private cache"}. Local files are read in place.`;
}

function restoreFolderPaths() {
  try {
    ui.sourceDownloadRoot.value = localStorage.getItem(folderKeys.source) || '';
    ui.outputRoot.value = localStorage.getItem(folderKeys.output) || '';
  } catch (error) {
    console.warn('Could not restore saved folder paths.', error);
  }
  updateSourceDownloadHelp();
}

function rememberFolderPath(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch (error) {
    console.warn('Could not save folder path.', error);
  }
}

function refreshTrackCount() {
  if (!draft) return;
  const count = draft.tracks.filter((track) => track.include).length;
  ui.trackCount.textContent = `${count} ${count === 1 ? 'track' : 'tracks'}`;
}

function seekAndPlay(seconds) {
  if (!Number.isFinite(seconds)) return;
  ui.audio.currentTime = Math.min(Math.max(seconds, 0), draft?.duration || seconds);
  const playback = ui.audio.play();
  if (playback) {
    playback.catch((error) => {
      showStatus('Playback could not start.', error.message, 'error');
    });
  }
}

function renderTracks() {
  if (!draft) return;
  draft.tracks.forEach((track, index) => { track.index = index + 1; });
  ui.tracks.innerHTML = draft.tracks.map((track, index) => `
    <tr class="track-row" data-index="${index}" data-included="${track.include}">
      <td data-label="Use"><input class="track-include" type="checkbox" ${track.include ? 'checked' : ''} aria-label="Include track ${index + 1}"></td>
      <td data-label="No."><span class="track-number"><strong>${index + 1}</strong><span class="track-source">${escapeHtml(track.source)}</span></span></td>
      <td data-label="Start"><input class="time-input track-start" type="text" inputmode="numeric" value="${formatTime(track.start)}" aria-label="Start time for track ${index + 1}"></td>
      <td data-label="End"><input class="time-input track-end" type="text" inputmode="numeric" value="${formatTime(track.end)}" aria-label="End time for track ${index + 1}"></td>
      <td data-label="Artist"><input class="track-artist" type="text" value="${escapeHtml(track.artist)}" aria-label="Artist for track ${index + 1}"></td>
      <td data-label="Title"><input class="track-title" type="text" value="${escapeHtml(track.title)}" aria-label="Title for track ${index + 1}"></td>
    </tr>
  `).join('');

  ui.tracks.querySelectorAll('.track-row').forEach((row) => {
    const index = Number(row.dataset.index);
    const track = draft.tracks[index];
    row.querySelector('.track-include').addEventListener('change', (event) => {
      track.include = event.target.checked;
      row.dataset.included = String(track.include);
      refreshTrackCount();
    });
    row.querySelector('.track-start').addEventListener('click', (event) => {
      seekAndPlay(parseTime(event.currentTarget.value));
    });
    row.querySelector('.track-end').addEventListener('click', (event) => {
      seekAndPlay(parseTime(event.currentTarget.value));
    });
    row.querySelector('.track-start').addEventListener('blur', (event) => {
      const start = parseTime(event.target.value);
      const previous = draft.tracks[index - 1];
      if (!Number.isFinite(start) || start < 0 || start >= track.end || (previous && start <= previous.start)) {
        event.target.setAttribute('aria-invalid', 'true');
        showStatus('That boundary is invalid.', `Track ${index + 1} must start before its end${previous ? ' and after the previous track starts' : ''}.`, 'error');
        event.target.value = formatTime(track.start);
        return;
      }
      event.target.setAttribute('aria-invalid', 'false');
      track.start = start;
      if (previous) previous.end = start;
      renderTracks();
    });
    row.querySelector('.track-end').addEventListener('blur', (event) => {
      const end = parseTime(event.target.value);
      const next = draft.tracks[index + 1];
      if (!Number.isFinite(end) || end <= track.start || end > draft.duration || (next && end >= next.end)) {
        event.target.setAttribute('aria-invalid', 'true');
        showStatus('That boundary is invalid.', `Track ${index + 1} must end after its start and before the next track ends.`, 'error');
        event.target.value = formatTime(track.end);
        return;
      }
      event.target.setAttribute('aria-invalid', 'false');
      track.end = end;
      if (next) next.start = end;
      renderTracks();
    });
    row.querySelector('.track-artist').addEventListener('input', (event) => { track.artist = event.target.value; });
    row.querySelector('.track-title').addEventListener('input', (event) => { track.title = event.target.value; });
  });
  refreshTrackCount();
}

function showWorkspace(job) {
  draft = job.draft;
  ui.workspace.hidden = false;
  ui.actions.hidden = false;
  ui.skeleton.hidden = true;
  ui.sourceTitle.textContent = draft.title;
  ui.segmentation.textContent = `boundaries: ${draft.segmentation}`;
  ui.duration.textContent = formatTime(draft.duration);
  ui.albumTitle.value = draft.title;
  ui.albumArtist.value = draft.album_artist;
  ui.albumYear.value = draft.year || '';
  ui.audio.src = `/api/jobs/${job.id}/audio`;
  if (draft.cover_url) {
    ui.cover.src = draft.cover_url;
    ui.cover.alt = `${draft.title} artwork`;
    ui.cover.hidden = false;
  } else {
    ui.cover.hidden = true;
  }
  renderTracks();
}

function draftPayload() {
  if (!draft) throw new Error('Analyze a compilation first.');
  draft.title = ui.albumTitle.value.trim();
  draft.album_artist = ui.albumArtist.value.trim();
  draft.year = ui.albumYear.value.trim();
  if (!draft.title || !draft.album_artist) throw new Error('Album title and album artist are required.');
  return {
    title: draft.title,
    album_artist: draft.album_artist,
    year: draft.year,
    tracks: draft.tracks.map((track, index) => ({ ...track, index: index + 1 })),
  };
}

async function pollJob() {
  clearTimeout(pollTimer);
  if (!currentJobId) return;
  try {
    const job = await api(`/api/jobs/${currentJobId}`);
    const running = ['queued', 'analyzing', 'exporting', 'importing'].includes(job.state);
    const warnings = job.draft?.warnings?.join(' ') || '';
    showStatus(job.message, job.error || warnings || (job.result?.path ? `Written to ${job.result.path}` : ''), job.state === 'error' ? 'error' : 'default', running);
    if (job.draft && !draft) showWorkspace(job);
    if (job.state === 'ready') {
      showWorkspace(job);
      setButton(ui.analyze, '', 'Analyze comp');
    } else if (job.state === 'complete') {
      setButton(ui.export, 'success', 'Album exported');
      setButton(ui.plexConfirm, 'success', job.result?.scan_requested ? 'Scan requested' : 'Ready');
    } else if (job.state === 'error') {
      setButton(ui.analyze, 'error', 'Try analysis again');
      setButton(ui.export, 'error', 'Try export again');
      setButton(ui.plexConfirm, 'error', 'Try Plex again');
    }
    if (running) pollTimer = setTimeout(pollJob, 1000);
  } catch (error) {
    showStatus('Could not read job status.', error.message, 'error');
  }
}

async function startAnalysis(event) {
  event.preventDefault();
  if (!validateSource()) return;
  draft = null;
  currentJobId = '';
  ui.workspace.hidden = true;
  ui.actions.hidden = true;
  ui.skeleton.hidden = false;
  setButton(ui.analyze, 'loading', 'Analyzing');
  showStatus('Starting analysis.', 'Description timestamps and chapters are checked before audio analysis.', 'default', true);
  try {
    const job = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        source: ui.source.value.trim(),
        download_root: ui.sourceDownloadRoot.value.trim(),
      }),
    });
    currentJobId = job.id;
    await pollJob();
  } catch (error) {
    ui.skeleton.hidden = true;
    setButton(ui.analyze, 'error', 'Try analysis again');
    showStatus('Analysis could not start.', error.message, 'error');
  }
}

async function startExport() {
  try {
    const outputRoot = ui.outputRoot.value.trim();
    if (!outputRoot) throw new Error('Choose an album output folder first.');
    setButton(ui.export, 'loading', 'Exporting');
    const payload = { ...draftPayload(), output_root: outputRoot };
    await api(`/api/jobs/${currentJobId}/export`, { method: 'POST', body: JSON.stringify(payload) });
    await pollJob();
  } catch (error) {
    setButton(ui.export, 'error', 'Try export again');
    showStatus('Export could not start.', error.message, 'error');
  }
}

function splitAtPlayhead() {
  if (!draft) return;
  const at = ui.audio.currentTime;
  const index = draft.tracks.findIndex((track) => at > track.start + 2 && at < track.end - 2);
  if (index < 0) {
    showStatus('The playhead is too close to a boundary.', 'Move at least two seconds inside a track and try again.', 'error');
    return;
  }
  const track = draft.tracks[index];
  const next = {
    index: index + 2,
    start: at,
    end: track.end,
    title: `Track ${String(index + 2).padStart(2, '0')}`,
    artist: 'Unknown Artist',
    source: 'manual',
    confidence: 1,
    include: true,
  };
  track.end = at;
  draft.tracks.splice(index + 1, 0, next);
  renderTracks();
  showStatus('Boundary added.', `Split at ${formatTime(at)}.`, 'default');
}

function plexConfig() {
  return {
    url: ui.plexUrl.value.trim(),
    token: ui.plexToken.value.trim(),
    library: ui.plexLibrary.value.trim(),
    music_root: ui.musicRoot.value.trim(),
    plex_music_root: ui.plexMusicRoot.value.trim(),
  };
}

function openPlex() {
  ui.plexConfirm.disabled = !draft;
  ui.appShell.inert = true;
  ui.actions.inert = true;
  ui.plexDialog.showModal();
  ui.plexUrl.focus();
}

function closePlex() {
  ui.plexDialog.close();
  ui.appShell.inert = false;
  ui.actions.inert = false;
}

async function testPlexConnection() {
  setButton(ui.plexTest, 'loading', 'Testing');
  ui.plexStatus.dataset.tone = '';
  ui.plexStatus.textContent = 'Connecting without saving the token.';
  try {
    const result = await api('/api/plex/test', { method: 'POST', body: JSON.stringify(plexConfig()) });
    setButton(ui.plexTest, 'success', 'Connection works');
    ui.plexStatus.textContent = `${result.server} · ${result.library}`;
  } catch (error) {
    setButton(ui.plexTest, 'error', 'Test again');
    ui.plexStatus.dataset.tone = 'error';
    ui.plexStatus.textContent = error.message;
  }
}

async function startPlexImport(event) {
  event.preventDefault();
  if (!draft) return;
  setButton(ui.plexConfirm, 'loading', 'Writing album');
  try {
    const payload = { ...draftPayload(), plex: plexConfig() };
    await api(`/api/jobs/${currentJobId}/plex`, { method: 'POST', body: JSON.stringify(payload) });
    closePlex();
    await pollJob();
  } catch (error) {
    setButton(ui.plexConfirm, 'error', 'Try Plex again');
    ui.plexStatus.dataset.tone = 'error';
    ui.plexStatus.textContent = error.message;
  }
}

async function loadState() {
  try {
    const state = await api('/api/state');
    ui.version.textContent = `v${state.version}`;
    defaultSourceCache = state.source_cache || '';
    updateSourceDownloadHelp();
  } catch (error) {
    showStatus('Compmaster API is unavailable.', error.message, 'error');
  }
}

ui.sourceForm.addEventListener('submit', startAnalysis);
ui.source.addEventListener('blur', validateSource);
ui.sourceDownloadRoot.addEventListener('change', () => {
  rememberFolderPath(folderKeys.source, ui.sourceDownloadRoot.value.trim());
  updateSourceDownloadHelp();
});
ui.outputRoot.addEventListener('change', () => {
  rememberFolderPath(folderKeys.output, ui.outputRoot.value.trim());
});
ui.export.addEventListener('click', startExport);
ui.splitHere.addEventListener('click', splitAtPlayhead);
ui.audio.addEventListener('timeupdate', () => { ui.playhead.textContent = formatTime(ui.audio.currentTime); });
ui.plexOpen.addEventListener('click', openPlex);
ui.plexImport.addEventListener('click', openPlex);
ui.plexClose.addEventListener('click', closePlex);
ui.plexTest.addEventListener('click', testPlexConnection);
ui.plexForm.addEventListener('submit', startPlexImport);
ui.plexDialog.addEventListener('click', (event) => { if (event.target === ui.plexDialog) closePlex(); });
ui.plexDialog.addEventListener('close', () => {
  ui.appShell.inert = false;
  ui.actions.inert = false;
});

restoreFolderPaths();
loadState();
