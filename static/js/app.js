/**
 * Token Telemetry & Model Observatory
 * Frontend application logic, customizable dashboard system, and scientific LLM observability panels.
 */

// Application State
const state = {
  stats: null,
  sessions: [],
  timeseries: [],
  live: null,
  activeFilter: {
    q: '',
    folder: '',
    provider: '',
    model: '',
    harness: '',
    speed_tier: '',
    window: 'all',
    from: '',
    to: '',
    sort: 'date_desc',
    chip: 'all',
  },
  activeSession: null,
  activeViewId: 'default',
  isEditingLayout: false,
  theme: 'subtle',
  mode: 'dark',
  activePage: 'observatory',
  workspace: null,
  gallery: {
    activeTab: 'all',
    query: '',
  },
  layout: [],
  resizeObservers: new Map(),
};

// router.js reads this; a top-level const is not a window property.
window.state = state;

// Default Dashboard Layout (12-column grid system)
const DEFAULT_LAYOUT = [
  { id: 'kpi-banner', cols: 12, rows: 3 },
  { id: 'tps-trend', cols: 8, rows: 5 },
  { id: 'speculative-burst', cols: 4, rows: 5 },
  { id: 'prefill-vs-decode', cols: 6, rows: 4 },
  { id: 'ttft-latency', cols: 6, rows: 4 },
  { id: 'telemetry-table', cols: 12, rows: 8 },
  { id: 'speed-distribution', cols: 4, rows: 4 },
  { id: 'model-share', cols: 4, rows: 4 },
  { id: 'tool-usage', cols: 4, rows: 4 },
  { id: 'apc-cache', cols: 6, rows: 4 },
  { id: 'decode-acceleration', cols: 6, rows: 4 },
  { id: 'token-volume', cols: 6, rows: 5 },
  { id: 'live-pulse', cols: 6, rows: 4 },
  { id: 'sessions-explorer', cols: 12, rows: 10 },
];

// Built-in dashboards. These live in code and are never copied into storage,
// so a delete cannot reach them - the guard is structural, not a runtime if.
const BUILTIN_DASHBOARDS = {
  'default': {
    name: 'Default Overview',
    icon: '\u26A1',
    layout: DEFAULT_LAYOUT,
    filters: { window: 'all', harness: '', speed_tier: '', pinned: [] },
  },
  'mtp_speculative': {
    name: 'Speculative MTP & Latency',
    icon: '\uD83D\uDE80',
    layout: [
      { id: 'speculative-burst', cols: 6, rows: 5 },
      { id: 'decode-acceleration', cols: 6, rows: 5 },
      { id: 'apc-cache', cols: 6, rows: 4 },
      { id: 'ttft-latency', cols: 6, rows: 4 },
      { id: 'telemetry-table', cols: 12, rows: 8 },
    ],
    filters: { window: 'all', harness: '', speed_tier: 'turbo', pinned: ['speed_tier'] },
  },
  'token_economics': {
    name: 'Token Economics & Tools',
    icon: '\uD83D\uDCCA',
    layout: [
      { id: 'kpi-banner', cols: 12, rows: 3 },
      { id: 'token-volume', cols: 6, rows: 5 },
      { id: 'model-share', cols: 6, rows: 5 },
      { id: 'tool-usage', cols: 6, rows: 4 },
      { id: 'speed-distribution', cols: 6, rows: 4 },
    ],
    filters: { window: 'all', harness: '', speed_tier: '', pinned: [] },
  },
  'realtime_monitor': {
    name: 'Real-Time Monitor',
    icon: '\u23F1',
    layout: [
      { id: 'live-pulse', cols: 12, rows: 4 },
      { id: 'telemetry-table', cols: 12, rows: 8 },
      { id: 'prefill-vs-decode', cols: 6, rows: 4 },
      { id: 'ttft-latency', cols: 6, rows: 4 },
    ],
    filters: { window: '1h', harness: '', speed_tier: '', pinned: ['window'] },
  },
};

// Formatting Utilities
function formatNum(num) {
  if (num === undefined || num === null) return '0';
  if (num >= 1000000) return (num / 1000000).toFixed(2) + 'M';
  if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
  return Number(num).toLocaleString();
}

function formatSecs(secs) {
  if (!secs) return '0s';
  if (secs < 60) return Math.round(secs) + 's';
  const mins = Math.floor(secs / 60);
  const rem = Math.round(secs % 60);
  return mins + 'm ' + rem + 's';
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// ============================================================
// PANEL ARCHETYPES
// A panel declares size intent rather than a raw pixel height. Rows snap to a
// fixed grid track, so two panels on the same band align by construction -
// the free-form min-height they used before is what produced ragged rows.
// ============================================================
const PANEL_ARCHETYPES = {
  'kpi-strip':   { cols: 12, rows: 3,  minCols: 6, maxCols: 12, minRows: 3, maxRows: 5 },
  'stat':        { cols: 3,  rows: 3,  minCols: 2, maxCols: 6,  minRows: 2, maxRows: 4 },
  'line-chart':  { cols: 8,  rows: 5,  minCols: 4, maxCols: 12, minRows: 4, maxRows: 10 },
  'bar-list':    { cols: 4,  rows: 4,  minCols: 3, maxCols: 8,  minRows: 3, maxRows: 8 },
  'table':       { cols: 12, rows: 8,  minCols: 6, maxCols: 12, minRows: 5, maxRows: 14 },
  'status-grid': { cols: 6,  rows: 4,  minCols: 4, maxCols: 12, minRows: 3, maxRows: 6 },
  'explorer':    { cols: 12, rows: 10, minCols: 8, maxCols: 12, minRows: 6, maxRows: 16 },
};

const PANEL_ARCHETYPE_BY_ID = {
  'kpi-banner': 'kpi-strip',
  'tps-trend': 'line-chart',
  'token-volume': 'line-chart',
  'telemetry-table': 'table',
  'sessions-explorer': 'explorer',
  'speed-distribution': 'bar-list',
  'model-share': 'bar-list',
  'tool-usage': 'bar-list',
  'top-workspaces': 'bar-list',
  'duration-distribution': 'bar-list',
  'decode-acceleration': 'bar-list',
  'speculative-burst': 'bar-list',
  'ttft-latency': 'bar-list',
  'live-pulse': 'status-grid',
  'apc-cache': 'status-grid',
  'prefill-vs-decode': 'status-grid',
};

function archetypeLimits(panelId) {
  const key = PANEL_ARCHETYPE_BY_ID[panelId];
  return PANEL_ARCHETYPES[key] || PANEL_ARCHETYPES['bar-list'];
}

// ============================================================
// PANEL REGISTRY - Scientific Observability Panels
// ============================================================
const PANEL_REGISTRY = {
  'kpi-banner': {
    id: 'kpi-banner',
    title: 'Key Telemetry Metrics',
    category: 'system',
    icon: '⚡',
    description: 'Hero summary of decode speed, peak burst, token throughput, and recorded sessions.',
    defaultCol: 12,
    render: renderKpiBanner,
  },
  'telemetry-table': {
    id: 'telemetry-table',
    title: 'Live Scientific Turn & Request Telemetry',
    category: 'perf',
    icon: '🔬',
    description: 'Detailed inspection table of recent MLX inference turns with TTFT, Prefill, Decode, and Memory.',
    defaultCol: 12,
    render: renderTelemetryTablePanel,
  },
  'speculative-burst': {
    id: 'speculative-burst',
    title: 'Speculative Burst Analyzer',
    category: 'perf',
    icon: '🚀',
    description: 'Multi-Token Prediction (MTP) draft acceptance spikes vs autoregressive decode baseline.',
    defaultCol: 4,
    render: renderSpeculativeBurstPanel,
  },
  'prefill-vs-decode': {
    id: 'prefill-vs-decode',
    title: 'Prefill (Prompt) vs Decode Velocity',
    category: 'perf',
    icon: '⚡',
    description: 'Comparing prompt matrix ingestion rate (20k-50k tok/s) against token decode rate (15-35 tok/s).',
    defaultCol: 6,
    render: renderPrefillVsDecodePanel,
  },
  'ttft-latency': {
    id: 'ttft-latency',
    title: 'Time to First Token (TTFT) Latency',
    category: 'perf',
    icon: '⏱️',
    description: 'Latency before first generated token across prompt context sizes and KV-cache states.',
    defaultCol: 6,
    render: renderTtftLatencyPanel,
  },
  'decode-acceleration': {
    id: 'decode-acceleration',
    title: 'Speculative Warm-up (First 32 vs Last 32)',
    category: 'perf',
    icon: '🏎️',
    description: 'Decode speed acceleration curve showing how MTP drafting warms up during turn generation.',
    defaultCol: 6,
    render: renderDecodeAccelPanel,
  },
  'apc-cache': {
    id: 'apc-cache',
    title: 'Automatic Prefix Cache (APC) Efficiency',
    category: 'system',
    icon: '💾',
    description: 'KV-cache hit rate, matched prompt tokens, and unified memory block reutilization.',
    defaultCol: 6,
    render: renderApcCachePanel,
  },
  'tps-trend': {
    id: 'tps-trend',
    title: 'Decode Speed (tok/s) Timeline',
    category: 'perf',
    icon: '📈',
    description: 'Temporal trend of average generation TPS and peak burst performance with area fill.',
    defaultCol: 8,
    render: renderTpsTrendPanel,
  },
  'speed-distribution': {
    id: 'speed-distribution',
    title: 'TPS Speed Distribution',
    category: 'perf',
    icon: '📊',
    description: 'Histogram breakdown of inference turns across speed tiers (<15 to 60+ tok/s).',
    defaultCol: 4,
    render: renderSpeedDistPanel,
  },
  'model-share': {
    id: 'model-share',
    title: 'Model Output Share',
    category: 'analytics',
    icon: '🍩',
    description: 'Breakdown of token generation across local models and backends (MLX, Ollama, etc.).',
    defaultCol: 4,
    render: renderModelSharePanel,
  },
  'tool-usage': {
    id: 'tool-usage',
    title: 'Agent Tool Call Breakdown',
    category: 'analytics',
    icon: '🔧',
    description: 'Frequency ranking of tool calls (read, bash, edit, mcp) executed during sessions.',
    defaultCol: 4,
    render: renderToolUsagePanel,
  },
  'token-volume': {
    id: 'token-volume',
    title: 'Token Throughput Volume',
    category: 'analytics',
    icon: '🧱',
    description: 'Daily token volume split between prompt context prefill and model generation output.',
    defaultCol: 6,
    render: renderTokenVolumePanel,
  },
  'live-pulse': {
    id: 'live-pulse',
    title: 'Live Engine & Hardware Pulse',
    category: 'perf',
    icon: '🛰️',
    description: 'Real-time health monitor for MLX (:8080) and Ollama (:11434) backends and loaded models.',
    defaultCol: 6,
    render: renderLivePulsePanel,
  },
  'duration-distribution': {
    id: 'duration-distribution',
    title: 'Session Duration Distribution',
    category: 'analytics',
    icon: '⏳',
    description: 'Histogram of conversation session lengths from quick turns to deep coding sprints.',
    defaultCol: 6,
    render: renderDurationDistPanel,
  },
  'top-workspaces': {
    id: 'top-workspaces',
    title: 'Workspace Activity & Output',
    category: 'system',
    icon: '📁',
    description: 'Token output volume and session counts ranked by project repository and folder.',
    defaultCol: 6,
    render: renderTopWorkspacesPanel,
  },
  'sessions-explorer': {
    id: 'sessions-explorer',
    title: 'Sessions & Prompts Explorer',
    category: 'analytics',
    icon: '💬',
    description: 'Filterable session history, topic search, speed tier filter, and turn-level inspection drawer.',
    defaultCol: 12,
    render: renderSessionsExplorerWidget,
  },
};

// ============================================================
// DASHBOARD LIFECYCLE
// A dashboard is a named, saved arrangement of panels. Editing writes to a
// single draft slot attached to a dashboard id; the saved record is only
// touched on Save. Previously every mutation wrote a global layout key while
// Save wrote elsewhere, so switching dashboards destroyed unsaved work.
// ============================================================

function ws() {
  return state.workspace;
}

function persistWorkspace() {
  const res = saveWorkspace(null, state.workspace);
  if (!res.ok && !state.storageWarned) {
    state.storageWarned = true;
    showToast('Settings cannot be saved in this browser session');
  }
  return res.ok;
}

function activeDashboard() {
  return resolveDashboard(ws(), ws().activeDashboardId, BUILTIN_DASHBOARDS);
}

function dashboardIsDirty() {
  return isDirty(ws(), ws().activeDashboardId);
}

// Called by every layout mutation. Writes the draft, never the record.
function commitLayoutEdit() {
  writeDraft(ws(), ws().activeDashboardId, state.layout, state.activeFilter);
  persistWorkspace();
  renderDashboardChrome();
}

function renderDashboardChrome() {
  const d = activeDashboard();
  const nameEl = document.getElementById('dashSwitcherName');
  if (nameEl && d) nameEl.textContent = d.name;

  const badge = document.getElementById('dashSwitcherBadge');
  if (badge) badge.hidden = !(d && d.source === 'builtin');

  const dirty = dashboardIsDirty();
  const dot = document.getElementById('dashDirty');
  if (dot) dot.hidden = !dirty;
  const save = document.getElementById('dashSave');
  if (save) save.hidden = !dirty;

  document.querySelector('.app-shell')?.classList.toggle('is-dirty', dirty);
}

function openDashboard(id, opts = {}) {
  const w = ws();
  if (!resolveDashboard(w, id, BUILTIN_DASHBOARDS)) return;

  // Switching away from unsaved work must be a decision, not a silent loss.
  if (!opts.force && w.draft && w.draft.dashboardId !== id) {
    const keep = confirm(
      'This dashboard has unsaved changes.\n\n' +
      'OK  \u2013 save them and switch\n' +
      'Cancel \u2013 discard them and switch'
    );
    if (keep) commitDraft(w, BUILTIN_DASHBOARDS);
    else discardDraft(w);
  }

  w.activeDashboardId = id;
  const d = resolveDashboard(w, id, BUILTIN_DASHBOARDS);
  const draft = (w.draft && w.draft.dashboardId === id) ? w.draft : null;
  state.layout = JSON.parse(JSON.stringify((draft ? draft.layout : d.layout) || []));

  // Only the filters a dashboard pins are applied, so opening one does not
  // silently rewrite app-wide chrome the user did not ask it to touch.
  const f = d.filters || {};
  const pinned = f.pinned || [];
  if (pinned.includes('window') && f.window) setTimeWindow(f.window, false);
  if (pinned.includes('harness')) selectDataSource(f.harness || '', false);
  if (pinned.includes('speed_tier')) {
    document.querySelectorAll('.explorer-filter-speed').forEach(el => { el.value = f.speed_tier || ''; });
    state.activeFilter.speed_tier = f.speed_tier || '';
  }

  persistWorkspace();
  renderDashboard();
  renderDashboardChrome();
  renderDashboardMenu();

  // All three, so charts cannot disagree with the KPI banner after a switch.
  fetchStats();
  fetchSessions();
  fetchTimeseries();
  announce(`Opened dashboard ${d.name}`);
}

function saveActiveDashboard() {
  const res = commitDraft(ws(), BUILTIN_DASHBOARDS);
  if (!res.ok) return;
  persistWorkspace();
  renderDashboardChrome();
  renderDashboardMenu();
  showToast('Dashboard saved');
}

function discardDashboardChanges() {
  discardDraft(ws());
  persistWorkspace();
  openDashboard(ws().activeDashboardId, { force: true });
  showToast('Changes discarded');
}

function createDashboardPrompt() {
  const name = prompt('Name for the new dashboard:');
  if (!name || !name.trim()) return;
  const id = createDashboard(ws(), BUILTIN_DASHBOARDS, name, state.layout, state.activeFilter);
  persistWorkspace();
  closeDashMenu();
  openDashboard(id, { force: true });
  showToast(`Created "${ws().dashboards[id].name}"`);
}

function duplicateActiveDashboard(id) {
  const src = id || ws().activeDashboardId;
  const draft = (ws().draft && ws().draft.dashboardId === src) ? ws().draft.layout : null;
  const newId = duplicateDashboard(ws(), BUILTIN_DASHBOARDS, src, draft);
  if (!newId) return;
  persistWorkspace();
  closeDashMenu();
  openDashboard(newId, { force: true });
  showToast(`Duplicated as "${ws().dashboards[newId].name}"`);
}

function renameDashboardPrompt(id) {
  const d = resolveDashboard(ws(), id, BUILTIN_DASHBOARDS);
  if (!d) return;
  if (d.source === 'builtin') {
    showToast('Built-in dashboards cannot be renamed \u2013 duplicate it first');
    return;
  }
  const name = prompt('Rename dashboard:', d.name);
  if (!name || !name.trim()) return;
  renameDashboard(ws(), BUILTIN_DASHBOARDS, id, name);
  persistWorkspace();
  renderDashboardChrome();
  renderDashboardMenu();
}

function deleteDashboardConfirm(id) {
  const d = resolveDashboard(ws(), id, BUILTIN_DASHBOARDS);
  if (!d) return;
  if (!confirm(`Delete "${d.name}"? This cannot be undone.`)) return;
  const res = deleteDashboard(ws(), BUILTIN_DASHBOARDS, id);
  if (!res.ok) {
    showToast('Built-in dashboards cannot be deleted');
    return;
  }
  persistWorkspace();
  closeDashMenu();
  openDashboard(ws().activeDashboardId, { force: true });
  showToast(`Deleted "${d.name}"`);
}

function makeDashboardDefault(id) {
  setDefaultDashboard(ws(), BUILTIN_DASHBOARDS, id);
  persistWorkspace();
  renderDashboardMenu();
  const d = resolveDashboard(ws(), id, BUILTIN_DASHBOARDS);
  showToast(`"${d.name}" opens on launch`);
}

function resetDefaultPointer() {
  setDefaultDashboard(ws(), BUILTIN_DASHBOARDS, 'default');
  persistWorkspace();
  renderDashboardMenu();
  showToast('Default reset to Default Overview');
}

// Reset a built-in's CONTENTS to factory. Distinct from resetting which
// dashboard opens on launch, which is resetDefaultPointer above.
function resetDashboardToOriginal(id) {
  const d = resolveDashboard(ws(), id, BUILTIN_DASHBOARDS);
  if (!d || d.source !== 'builtin') {
    showToast('Only built-in dashboards can be reset to original');
    return;
  }
  if (!confirm(`Reset "${d.name}" to its original panels?`)) return;
  resetBuiltin(ws(), BUILTIN_DASHBOARDS, id);
  persistWorkspace();
  closeDashMenu();
  openDashboard(id, { force: true });
  showToast(`"${d.name}" reset to original`);
}

/* ---------- Dashboard menu ---------- */

function renderDashboardMenu() {
  const menu = document.getElementById('dashMenu');
  if (!menu) return;
  const w = ws();
  const activeId = w.activeDashboardId;

  const row = (d) => {
    const isActive = d.id === activeId;
    const isDefault = d.id === w.defaultDashboardId;
    return `
      <div class="dash-menu__row">
        <button class="dash-menu__item" type="button" role="menuitemradio"
                aria-checked="${isActive}" onclick="openDashboard('${d.id}'); closeDashMenu();">
          <span class="dash-menu__check" aria-hidden="true">${isActive ? '\u2713' : ''}</span>
          <span class="dash-menu__icon" aria-hidden="true">${d.icon || ''}</span>
          <span class="dash-menu__label">${escapeHtml(d.name)}</span>
          <span class="dash-menu__meta mono">${(d.layout || []).length}</span>
          ${isDefault ? '<span class="dash-menu__pin" title="Opens on launch">\u2605</span>' : ''}
        </button>
        <button class="dash-menu__more" type="button" aria-haspopup="menu"
                aria-label="Actions for ${escapeHtml(d.name)}"
                onclick="toggleDashSubmenu(event, '${d.id}')">\u22EF</button>
        <div class="dash-submenu" id="dashSub-${d.id}" role="menu" hidden>
          <button role="menuitem" type="button" onclick="duplicateActiveDashboard('${d.id}')">Duplicate</button>
          <button role="menuitem" type="button" onclick="makeDashboardDefault('${d.id}')">Set as default</button>
          ${d.source === 'builtin'
            ? `<button role="menuitem" type="button" onclick="resetDashboardToOriginal('${d.id}')">Reset to original</button>`
            : `<button role="menuitem" type="button" onclick="renameDashboardPrompt('${d.id}')">Rename\u2026</button>
               <button role="menuitem" type="button" class="is-danger" onclick="deleteDashboardConfirm('${d.id}')">Delete</button>`}
        </div>
      </div>
    `;
  };

  const builtins = Object.keys(BUILTIN_DASHBOARDS).map(id => resolveDashboard(w, id, BUILTIN_DASHBOARDS));
  const mine = Object.values(w.dashboards || {});

  menu.innerHTML = `
    <div class="dash-menu__group" role="group" aria-label="Built-in dashboards">
      <p class="dash-menu__title">Built-in</p>
      ${builtins.map(row).join('')}
    </div>
    ${mine.length ? `
      <div class="dash-menu__group" role="group" aria-label="My dashboards">
        <p class="dash-menu__title">My dashboards</p>
        ${mine.map(row).join('')}
      </div>` : ''}
    <hr class="dash-menu__sep" role="separator">
    <button class="dash-menu__item dash-menu__item--action" type="button" role="menuitem"
            onclick="createDashboardPrompt()">
      <span class="dash-menu__check" aria-hidden="true">+</span>
      <span class="dash-menu__label">New dashboard\u2026</span>
    </button>
    <button class="dash-menu__item dash-menu__item--action" type="button" role="menuitem"
            onclick="resetDefaultPointer(); closeDashMenu();">
      <span class="dash-menu__check" aria-hidden="true">\u21BA</span>
      <span class="dash-menu__label">Reset which dashboard opens on launch</span>
    </button>
  `;
}

function toggleDashMenu(ev) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('dashMenu');
  const trigger = document.getElementById('dashSwitcher');
  if (!menu) return;
  const open = menu.hidden;
  if (open) renderDashboardMenu();
  menu.hidden = !open;
  if (trigger) trigger.setAttribute('aria-expanded', String(open));
}

function closeDashMenu() {
  const menu = document.getElementById('dashMenu');
  if (menu) menu.hidden = true;
  const trigger = document.getElementById('dashSwitcher');
  if (trigger) trigger.setAttribute('aria-expanded', 'false');
  document.querySelectorAll('.dash-submenu').forEach(el => { el.hidden = true; });
}

function toggleDashSubmenu(ev, id) {
  ev.stopPropagation();
  const el = document.getElementById('dashSub-' + id);
  const wasHidden = el && el.hidden;
  document.querySelectorAll('.dash-submenu').forEach(s => { s.hidden = true; });
  if (el) el.hidden = !wasHidden;
}

document.addEventListener('click', e => {
  if (!e.target.closest('.topbar-lead')) closeDashMenu();
  if (!e.target.closest('.topbar-center')) {
    const rp = document.getElementById('rangePop');
    if (rp) rp.hidden = true;
  }
});

function toggleRangePopover(ev) {
  if (ev) ev.stopPropagation();
  const rp = document.getElementById('rangePop');
  if (rp) rp.hidden = !rp.hidden;
}

// One live region for the whole app; showToast mirrors into it.
function announce(msg) {
  const el = document.getElementById('a11yStatus');
  if (el) el.textContent = msg;
}

function resetDashboardLayout() {
  const id = ws().activeDashboardId;
  const d = resolveDashboard(ws(), id, BUILTIN_DASHBOARDS);
  if (d && d.source === 'builtin') {
    resetDashboardToOriginal(id);
  } else {
    discardDashboardChanges();
  }
}


function removePanel(panelId) {
  state.layout = state.layout.filter(p => p.id !== panelId);
  commitLayoutEdit();
  renderDashboard();
  renderGallery();
}

function addPanel(panelId) {
  if (!PANEL_REGISTRY[panelId]) return;
  if (state.layout.some(p => p.id === panelId)) return;
  const def = PANEL_REGISTRY[panelId];
  state.layout.push({ id: panelId, col: def.defaultCol || 6 });
  commitLayoutEdit();
  renderDashboard();
  renderGallery();
}

function togglePanelExpand(panelId) {
  const item = state.layout.find(p => p.id === panelId);
  if (!item) return;

  if (item.cols === 12) {
    item.cols = item._prevCols || 6;
  } else {
    item._prevCols = item.cols;
    item.cols = 12;
  }
  commitLayoutEdit();
  renderDashboard();
}

// Fluid Drag Resize Implementation (Dispatch Pattern)
let resizeState = null;

function initPanelResize(e, panelId) {
  e.preventDefault();
  e.stopPropagation();
  const panelEl = document.getElementById('panel-' + panelId);
  if (!panelEl) return;

  const item = state.layout.find(p => p.id === panelId);
  if (!item) return;

  const rect = panelEl.getBoundingClientRect();
  const gridEl = document.getElementById('dashboardGrid');
  const gridRect = gridEl.getBoundingClientRect();
  const colWidth = gridRect.width / 12;

  resizeState = {
    panelId,
    item,
    panelEl,
    startX: e.clientX,
    startY: e.clientY,
    startW: rect.width,
    startH: rect.height,
    colWidth,
  };

  window.addEventListener('mousemove', onPanelResizeMove);
  window.addEventListener('mouseup', onPanelResizeEnd);
  document.body.style.cursor = 'nwse-resize';
  document.body.style.userSelect = 'none';
}

function onPanelResizeMove(e) {
  if (!resizeState) return;
  const dx = e.clientX - resizeState.startX;
  const dy = e.clientY - resizeState.startY;

  // Calculate new column width in exact single-column increments (1 to 12)
  const rawCol = Math.round((resizeState.startW + dx) / resizeState.colWidth);
  const newCol = Math.max(1, Math.min(12, rawCol));

  if (newCol !== resizeState.item.cols) {
    resizeState.item.cols = newCol;
    resizeState.panelEl.style.setProperty('--panel-cols', newCol);
    const badge = document.getElementById('col-badge-' + resizeState.panelId);
    if (badge) badge.textContent = newCol + '/12 Col';
  }

  // Adjust height if dragged vertically (can reduce down to 90px)
  const newH = Math.max(90, resizeState.startH + dy);
  resizeState.panelEl.style.minHeight = Math.round(newH) + 'px';
  resizeState.item.height = Math.round(newH);
}

function onPanelResizeEnd() {
  if (!resizeState) return;
  window.removeEventListener('mousemove', onPanelResizeMove);
  window.removeEventListener('mouseup', onPanelResizeEnd);
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  commitLayoutEdit();
  resizeState = null;
  window.dispatchEvent(new Event('resize'));
}

// Edit Layout Mode Toggle
function toggleEditLayout() {
  state.isEditingLayout = !state.isEditingLayout;
  const shell = document.querySelector('.app-shell');
  if (shell) shell.classList.toggle('is-editing', state.isEditingLayout);

  const btn = document.getElementById('btnCustomizeLayout');
  const icon = document.getElementById('customizeIcon');
  const txt = document.getElementById('customizeText');
  const banner = document.getElementById('editLayoutBanner');

  if (state.isEditingLayout) {
    if (btn) btn.classList.add('editing');
    if (icon) icon.textContent = '✓';
    if (txt) txt.textContent = 'Done Editing';
    if (banner) banner.style.display = 'flex';
  } else {
    if (btn) btn.classList.remove('editing');
    if (icon) icon.textContent = '✏️';
    if (txt) txt.textContent = 'Edit Layout';
    if (banner) banner.style.display = 'none';
  }
}

// Drag to Reorder with 2D Live Placement Ghost Preview
let draggedPanelId = null;
let dragPlaceholder = null;

function onPanelDragStart(e, panelId) {
  if (!state.isEditingLayout) {
    e.preventDefault();
    return;
  }
  draggedPanelId = panelId;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', panelId);

  const panelEl = document.getElementById('panel-' + panelId);
  if (panelEl) {
    panelEl.classList.add('is-dragging');
  }

  // Create ghost preview placeholder matching column span
  const item = state.layout.find(p => p.id === panelId);
  const colSpan = item ? (item.cols || 6) : 6;
  if (!dragPlaceholder) {
    dragPlaceholder = document.createElement('div');
  }
  dragPlaceholder.className = 'drag-placeholder';
  dragPlaceholder.style.setProperty('--panel-cols', colSpan);
  dragPlaceholder.style.minHeight = (panelEl ? panelEl.offsetHeight : 220) + 'px';
}

function initDragReorder() {
  const grid = document.getElementById('dashboardGrid');
  if (!grid || grid._dragInitialized) return;
  grid._dragInitialized = true;

  grid.addEventListener('dragover', e => {
    if (!state.isEditingLayout || !draggedPanelId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    const targetPanel = e.target.closest('.dashboard-panel');
    if (targetPanel && targetPanel.id !== 'panel-' + draggedPanelId) {
      const rect = targetPanel.getBoundingClientRect();
      const midX = rect.left + rect.width / 2;
      const midY = rect.top + rect.height / 2;

      // In a 2D multi-column grid: if cursor is to the right on the same row, or lower down:
      const isAfter = (e.clientY > midY + 18) || (Math.abs(e.clientY - midY) <= rect.height / 2 && e.clientX > midX);
      if (isAfter) {
        targetPanel.after(dragPlaceholder);
      } else {
        targetPanel.before(dragPlaceholder);
      }
    } else if (!targetPanel) {
      // User is dragging into empty space on the right or bottom of the grid
      const panels = Array.from(grid.querySelectorAll('.dashboard-panel:not(.is-dragging)'));
      if (panels.length) {
        let closest = null;
        let minDist = Infinity;
        panels.forEach(p => {
          const r = p.getBoundingClientRect();
          const dist = Math.hypot(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2));
          if (dist < minDist) {
            minDist = dist;
            closest = p;
          }
        });
        if (closest) {
          const r = closest.getBoundingClientRect();
          const isAfter = (e.clientY > r.top + r.height / 2) || (e.clientX > r.left + r.width / 2);
          if (isAfter) {
            closest.after(dragPlaceholder);
          } else {
            closest.before(dragPlaceholder);
          }
        }
      } else {
        grid.appendChild(dragPlaceholder);
      }
    }
  });

  grid.addEventListener('drop', e => {
    if (!state.isEditingLayout || !draggedPanelId) return;
    e.preventDefault();

    const draggedEl = document.getElementById('panel-' + draggedPanelId);
    if (draggedEl && dragPlaceholder && dragPlaceholder.parentNode) {
      dragPlaceholder.parentNode.insertBefore(draggedEl, dragPlaceholder);
    }
    if (dragPlaceholder && dragPlaceholder.parentNode) {
      dragPlaceholder.parentNode.removeChild(dragPlaceholder);
    }

    // Read new layout order directly from DOM
    const currentPanelEls = Array.from(grid.querySelectorAll('.dashboard-panel'));
    const newLayout = [];
    currentPanelEls.forEach(el => {
      const id = el.id.replace('panel-', '');
      const existing = state.layout.find(p => p.id === id);
      if (existing) newLayout.push(existing);
    });

    state.layout = newLayout;
    commitLayoutEdit();

    document.querySelectorAll('.dashboard-panel').forEach(p => {
      p.classList.remove('is-dragging');
      p.classList.remove('drag-over');
    });
    draggedPanelId = null;
    showToast('Dashboard order updated');
  });

  grid.addEventListener('dragend', () => {
    if (dragPlaceholder && dragPlaceholder.parentNode) {
      dragPlaceholder.parentNode.removeChild(dragPlaceholder);
    }
    document.querySelectorAll('.dashboard-panel').forEach(p => {
      p.classList.remove('is-dragging');
      p.classList.remove('drag-over');
    });
    draggedPanelId = null;
  });
}

function cleanupResizeObservers() {
  state.resizeObservers.forEach(obs => obs.disconnect());
  state.resizeObservers.clear();
}

// ============================================================
// PANEL STATE COMPONENTS
// A panel must never render an invented number as if it were measured.
// When a source is unreachable or has produced nothing yet, it says so.
// ============================================================

// A numeric slot with no measurement behind it.
function dash(label) {
  return `<span class="nodata" aria-label="${escapeHtml(label || 'not measured')}">&mdash;</span>`;
}

function stateBlock(kind, opts = {}) {
  const tone = kind === 'error' ? 'bad' : 'neutral';
  const action = opts.action
    ? `<button class="btn btn--ghost btn--sm" type="button" onclick="${opts.action.onclick}">${escapeHtml(opts.action.label)}</button>`
    : '';
  return `
    <div class="panel-state panel-state--${kind}" data-tone="${tone}">
      <p class="panel-state__title">${escapeHtml(opts.title || 'No data')}</p>
      <p class="panel-state__body">${escapeHtml(opts.body || '')}</p>
      ${action}
    </div>
  `;
}

// Live engine panels share one offline treatment so "server is down" never
// looks like "the metric is zero".
function engineOffline(engine, what) {
  return stateBlock('empty', {
    title: `${engine} is offline`,
    body: `${what} is measured live and is only available while ${engine} is running. Nothing is recorded for it while the server is down.`,
  });
}

// ============================================================
// THEME COLOUR ACCESS
// Canvas charts cannot use CSS variables directly, so they read the
// resolved token off the document root at draw time. That keeps every
// chart in step with the active theme instead of hardcoding a palette.
// ============================================================
function themeColor(token, fallback = '#888888') {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
    return v || fallback;
  } catch (e) {
    return fallback;
  }
}

function themeAlpha(rgbToken, alpha, fallback = '136, 136, 136') {
  const triplet = themeColor(rgbToken, fallback);
  return `rgba(${triplet}, ${alpha})`;
}

function renderDashboard() {
  const grid = document.getElementById('dashboardGrid');
  if (!grid) return;

  cleanupResizeObservers();

  if (state.layout.length === 0) {
    grid.innerHTML = `
      <div style="grid-column:span 12;" class="empty-state">
        <p style="font-size:1.15rem;margin-bottom:0.75rem;color:var(--text-main);">Your observatory dashboard has no panels active.</p>
        <button class="btn-primary-neon" onclick="openPanelGallery()">+ Open Panel Gallery to Add Widgets</button>
      </div>
    `;
    return;
  }

  let html = '';
  state.layout.forEach(item => {
    const def = PANEL_REGISTRY[item.id];

    // An unknown panel id is kept and surfaced, never silently dropped, so a
    // panel that disappears in one release and returns in the next survives.
    if (!def) {
      html += `
        <div class="dashboard-panel is-unavailable" id="panel-${item.id}"
             style="--panel-cols:${item.cols || 6};--panel-rows:${item.rows || 3};">
          <div class="panel-state panel-state--empty" data-tone="neutral">
            <p class="panel-state__title">Panel unavailable</p>
            <p class="panel-state__body">"${escapeHtml(item.id)}" is not available in this version.</p>
            <button class="btn btn--ghost btn--sm" type="button" onclick="removePanel('${item.id}')">Remove</button>
          </div>
        </div>`;
      return;
    }

    const styleAttr = `style="--panel-cols:${item.cols || def.defaultCol || 6};--panel-rows:${item.rows || 4};"`;

    html += `
      <div class="dashboard-panel" id="panel-${item.id}" ${styleAttr}>
        <div class="panel-header">
          <div class="panel-title-wrap">
            <span class="panel-drag-handle" title="Drag to reorder panel" draggable="true" ondragstart="onPanelDragStart(event, '${item.id}')">⠿</span>
            <span class="panel-icon">${def.icon}</span>
            <span>${escapeHtml(def.title)}</span>
            <span class="panel-col-badge mono" id="col-badge-${item.id}">${item.cols || 6}\u00d7${item.rows || 4}</span>
          </div>
          <div class="panel-actions">
            <button class="panel-expand-btn" onclick="togglePanelExpand('${item.id}')" title="Toggle Full Width / Restore Width">⤢</button>
            <button class="panel-close-btn" onclick="removePanel('${item.id}')" title="Remove panel from dashboard">✕</button>
          </div>
        </div>
        <div class="panel-body" id="panel-body-${item.id}">
        </div>
        <div class="panel-resize-handle" title="Drag to fluidly adjust width & height" onmousedown="initPanelResize(event, '${item.id}')">⤡</div>
      </div>
    `;
  });

  grid.innerHTML = html;

  state.layout.forEach(item => {
    const def = PANEL_REGISTRY[item.id];
    if (def && typeof def.render === 'function') {
      const container = document.getElementById('panel-body-' + item.id);
      if (container) {
        def.render(container, state, item);
      }
    }
  });
}

// ============================================================
// SCIENTIFIC & CORE PANEL RENDERERS
// ============================================================

// 1. KPI Hero Banner
function renderKpiBanner(container, appState) {
  const s = appState.stats || {};
  const avgTps = (s.avg_decode_tps || 0).toFixed(1);
  const peakTps = (s.peak_decode_tps || 0).toFixed(1);
  const totalTokens = formatNum((s.tokens_output || 0) + (s.tokens_reasoning || 0));
  const totalInput = formatNum(s.tokens_input || 0);
  const totalSessions = s.total_sessions || 0;
  const genTime = formatSecs(s.total_generation_seconds || 0);

  container.innerHTML = `
    <div class="hero-metrics" style="margin-bottom:0;">
      <div class="metric-card accent-acid">
        <div class="metric-label">
          <span>Avg Generation Speed</span>
          <span>⚡</span>
        </div>
        <div class="metric-value mono">
          <span>${avgTps}</span><span class="metric-unit">tok/s</span>
        </div>
        <div class="metric-meta">Across all completed turns</div>
      </div>

      <div class="metric-card accent-pink">
        <div class="metric-label">
          <span>Peak Measured Speed</span>
          <span>🚀</span>
        </div>
        <div class="metric-value mono">
          <span>${peakTps}</span><span class="metric-unit">tok/s</span>
        </div>
        <div class="metric-meta">Maximum decode burst</div>
      </div>

      <div class="metric-card accent-cyan">
        <div class="metric-label">
          <span>Total Output Tokens</span>
          <span>✨</span>
        </div>
        <div class="metric-value mono">
          <span>${totalTokens}</span>
        </div>
        <div class="metric-meta">Generated response content</div>
      </div>

      <div class="metric-card accent-amber">
        <div class="metric-label">
          <span>Total Context Input</span>
          <span>📥</span>
        </div>
        <div class="metric-value mono">
          <span>${totalInput}</span>
        </div>
        <div class="metric-meta">Prefilled prompt tokens</div>
      </div>

      <div class="metric-card">
        <div class="metric-label">
          <span>Recorded Sessions</span>
          <span>📂</span>
        </div>
        <div class="metric-value mono">
          <span>${totalSessions}</span>
        </div>
        <div class="metric-meta">Active time: <strong style="color:var(--text-main);">${genTime}</strong></div>
      </div>
    </div>
  `;
}

// 2. Speculative Burst & Multi-Token Prediction (MTP) Analyzer
function renderSpeculativeBurstPanel(container, appState) {
  const mlx = appState.live?.mlx || {};
  const reqs = mlx.requests || [];

  if (!mlx.online) {
    container.innerHTML = engineOffline('MLX', 'Speculative burst rate');
    return;
  }
  if (!reqs.length) {
    container.innerHTML = stateBlock('empty', {
      title: 'No requests captured yet',
      body: 'Burst rates are read from completed MLX requests. Send a request through MLX (:8080) and it will appear here.',
    });
    return;
  }

  // Every figure below comes from the request ring buffer. Nothing is invented.
  const decodeRates = reqs.map(r => r.decode_tok_s || 0).filter(Boolean);
  const prefillRates = reqs.map(r => r.prefill_tok_s || 0).filter(Boolean);
  const peakDecode = decodeRates.length ? Math.max(...decodeRates) : 0;
  const peakPrefill = prefillRates.length ? Math.max(...prefillRates) : 0;
  const medDecode = decodeRates.length
    ? decodeRates.slice().sort((a, b) => a - b)[Math.floor(decodeRates.length / 2)]
    : 0;
  const scale = Math.max(peakPrefill, peakDecode, 1);

  const row = (label, value, tone) => `
    <div class="bar-row-item">
      <span class="bar-row-label mono">${label}</span>
      <div class="bar-row-track"><div class="bar-row-fill" style="width:${Math.min((value / scale) * 100, 100)}%;background:var(--tone-${tone});"></div></div>
      <span class="bar-row-val mono" style="color:var(--tone-${tone});">${value ? formatNum(Math.round(value)) : dash()} tok/s</span>
    </div>
  `;

  container.innerHTML = `
    <div class="panel-pad">
      <div class="stat-inline">
        <span class="stat-inline__label">Peak prefill burst</span>
        <span class="stat-inline__value mono">${peakPrefill ? formatNum(Math.round(peakPrefill)) + ' tok/s' : dash()}</span>
      </div>
      <p class="panel-note">
        Prefill evaluates the prompt in parallel, so its token rate is orders of magnitude above sequential decode.
        Both figures are measured over the last ${reqs.length} MLX request${reqs.length === 1 ? '' : 's'}.
      </p>
      ${row('Median decode', medDecode, 'good')}
      ${row('Peak decode', peakDecode, 'primary')}
      ${row('Peak prefill', peakPrefill, 'warn')}
    </div>
  `;
}

// 3. Prefill vs Decode Velocity Benchmark
function renderPrefillVsDecodePanel(container, appState) {
  const mlx = appState.live?.mlx || {};
  const det = mlx.details || {};

  if (!mlx.online) {
    container.innerHTML = engineOffline('MLX', 'Prefill and decode throughput');
    return;
  }

  const prefill = det.prefill_tok_s || 0;
  const decode = det.decode_tok_s || 0;
  if (!prefill && !decode) {
    container.innerHTML = stateBlock('empty', {
      title: 'No throughput measured yet',
      body: 'MLX is running but has not completed a request since it started.',
    });
    return;
  }

  const ratio = (prefill && decode) ? Math.round(prefill / decode) : null;

  container.innerHTML = `
    <div class="panel-pad">
      <p class="panel-note">Prompt matrix parallel evaluation vs sequential autoregressive generation:</p>
      <div class="duo-grid">
        <div class="pulse-box">
          <div class="pulse-box-title">Prefill throughput</div>
          <div class="pulse-box-val mono">${prefill ? formatNum(prefill) + ' <span class="pulse-unit">tok/s</span>' : dash()}</div>
          <div class="pulse-box-sub">Parallel GPU tensor ingestion</div>
        </div>
        <div class="pulse-box">
          <div class="pulse-box-title">Decode throughput</div>
          <div class="pulse-box-val mono">${decode ? decode + ' <span class="pulse-unit">tok/s</span>' : dash()}</div>
          <div class="pulse-box-sub">Sequential autoregressive output</div>
        </div>
      </div>
      ${ratio ? `<p class="panel-note">Prefill is <strong>${ratio}\u00d7 faster</strong> than decode due to full matrix parallelization.</p>` : ''}
    </div>
  `;
}

// 4. Time to First Token (TTFT) Latency Tracker
function renderTtftLatencyPanel(container, appState) {
  const mlx = appState.live?.mlx || {};
  const det = mlx.details || {};
  const reqs = mlx.requests || [];

  if (!mlx.online) {
    container.innerHTML = engineOffline('MLX', 'Time to first token');
    return;
  }
  if (!reqs.length && !det.ttft_s) {
    container.innerHTML = stateBlock('empty', {
      title: 'No TTFT records yet',
      body: 'Time to first token is captured per request. It appears once MLX completes one.',
    });
    return;
  }

  const worst = reqs.length ? Math.max(...reqs.map(r => r.ttft_s || 0), 0.1) : (det.ttft_s || 0.1);
  const rows = reqs.slice(0, 6).map((r, idx) => `
    <div class="bar-row-item">
      <span class="bar-row-label mono">Req #${reqs.length - idx}</span>
      <div class="bar-row-track"><div class="bar-row-fill" style="width:${Math.min(((r.ttft_s || 0) / worst) * 100, 100)}%;background:var(--tone-warn);"></div></div>
      <span class="bar-row-val mono">${r.ttft_s ? r.ttft_s + 's' : dash()}</span>
    </div>
  `).join('');

  container.innerHTML = `
    <div class="panel-pad">
      <div class="stat-inline">
        <span class="stat-inline__label">Latest time to first token</span>
        <span class="stat-inline__value mono">${det.ttft_s ? det.ttft_s + 's' : dash()}</span>
      </div>
      ${rows}
    </div>
  `;
}

// 5. Scientific Turn & Request Telemetry Table
function renderTelemetryTablePanel(container, appState) {
  const mlx = appState.live?.mlx || {};
  const reqs = mlx.requests || [];

  if (!reqs.length) {
    container.innerHTML = `
      <div class="empty-state" style="padding:2rem;">
        No live MLX requests captured in this session yet. As OpenCode queries MLX (:8080), requests appear here with live TTFT, Prefill, Decode, and Memory stats.
      </div>
    `;
    return;
  }

  let tableRows = '';
  reqs.forEach((r, i) => {
    const isTool = r.tool_calls ? '<span class="badge badge-provider mono" style="color:var(--neon-amber);border-color:var(--neon-amber);">tool_calls</span>' : '<span class="badge badge-folder mono">text</span>';
    tableRows += `
      <tr style="border-bottom:1px solid var(--line-1);font-size:0.78rem;">
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono, monospace;color:var(--text-sub);">#${reqs.length - i}</td>
        <td style="padding:0.6rem 0.75rem;font-weight:600;">${escapeHtml(r.model)}</td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;color:var(--neon-cyan);">${formatNum(r.prompt_tokens)}</td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;color:var(--neon-violet);">${formatNum(r.completion_tokens)}</td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;color:var(--neon-cyan);">${formatNum(r.prefill_tok_s)} <span style="font-size:0.68rem;color:var(--text-sub);">tok/s</span></td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;color:var(--neon-acid);font-weight:700;">${r.decode_tok_s} <span style="font-size:0.68rem;color:var(--text-sub);">tok/s</span></td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;color:var(--neon-amber);">${r.ttft_s}s</td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;">${r.sliding_first_32 || '--'} → <strong style="color:var(--neon-pink);">${r.sliding_last_32 || '--'}</strong></td>
        <td style="padding:0.6rem 0.75rem;font-family:JetBrains Mono;">${r.peak_memory_gb} GB</td>
        <td style="padding:0.6rem 0.75rem;">${isTool}</td>
      </tr>
    `;
  });

  container.innerHTML = `
    <div class="telemetry-table-wrap" style="flex:1;min-height:0;overflow:auto;width:100%;border-radius:6px;">
      <table style="width:100%;border-collapse:collapse;text-align:left;">
        <thead style="position:sticky;top:0;background:var(--bg-panel-solid);z-index:2;box-shadow:0 1px 0 var(--border-subtle);">
          <tr style="border-bottom:1px solid var(--border-subtle);font-size:0.72rem;color:var(--text-sub);text-transform:uppercase;letter-spacing:0.06em;">
            <th style="padding:0.5rem 0.75rem;">ID</th>
            <th style="padding:0.5rem 0.75rem;">Model</th>
            <th style="padding:0.5rem 0.75rem;">Input Context</th>
            <th style="padding:0.5rem 0.75rem;">Output Tokens</th>
            <th style="padding:0.5rem 0.75rem;">Prefill Speed</th>
            <th style="padding:0.5rem 0.75rem;">Decode Speed</th>
            <th style="padding:0.5rem 0.75rem;">TTFT</th>
            <th style="padding:0.5rem 0.75rem;">Accel (1st→Last 32)</th>
            <th style="padding:0.5rem 0.75rem;">Peak Memory</th>
            <th style="padding:0.5rem 0.75rem;">Status</th>
          </tr>
        </thead>
        <tbody>
          ${tableRows}
        </tbody>
      </table>
    </div>
  `;
}

// 6. Decode Speed Acceleration Curve
function renderDecodeAccelPanel(container, appState) {
  const mlx = appState.live?.mlx || {};
  const det = mlx.details || {};

  if (!mlx.online) {
    container.innerHTML = engineOffline('MLX', 'Decode warmup curve');
    return;
  }

  const first32 = det.sliding_first_32 || 0;
  const last32 = det.sliding_last_32 || 0;
  if (!first32 && !last32) {
    container.innerHTML = stateBlock('empty', {
      title: 'No warmup samples yet',
      body: 'The acceleration curve compares the first and last 32 decoded tokens of a request. It needs at least one completed generation.',
    });
    return;
  }

  const scale = Math.max(first32, last32, 1);
  const gain = first32 ? Math.round(((last32 - first32) / first32) * 100) : null;

  container.innerHTML = `
    <div class="panel-pad">
      <p class="panel-note">Speculative drafting warmup during output token generation:</p>
      <div class="bar-row-item">
        <span class="bar-row-label mono">First 32 tokens</span>
        <div class="bar-row-track"><div class="bar-row-fill" style="width:${(first32 / scale) * 100}%;background:var(--tone-warn);"></div></div>
        <span class="bar-row-val mono">${first32 ? first32 + ' tok/s' : dash()}</span>
      </div>
      <div class="bar-row-item">
        <span class="bar-row-label mono">Last 32 tokens</span>
        <div class="bar-row-track"><div class="bar-row-fill" style="width:${(last32 / scale) * 100}%;background:var(--tone-good);"></div></div>
        <span class="bar-row-val mono">${last32 ? last32 + ' tok/s' : dash()}</span>
      </div>
      ${gain !== null ? `
        <p class="panel-callout" data-tone="${gain >= 0 ? 'good' : 'warn'}">
          <strong>${gain >= 0 ? '+' : ''}${gain}%</strong> change from first to last window as the KV cache context stabilizes.
        </p>` : ''}
    </div>
  `;
}

// 7. Automatic Prefix Cache (APC) Efficiency
function renderApcCachePanel(container, appState) {
  const mlx = appState.live?.mlx || {};
  const apc = mlx.apc;

  if (!mlx.online) {
    container.innerHTML = engineOffline('MLX', 'Prefix cache efficiency');
    return;
  }
  if (!apc) {
    container.innerHTML = stateBlock('empty', {
      title: 'Prefix cache not reporting',
      body: 'MLX is running but did not report automatic prefix cache statistics.',
    });
    return;
  }
  if (!apc.enabled) {
    container.innerHTML = stateBlock('empty', {
      title: 'Prefix cache disabled',
      body: 'MLX is running with the automatic prefix cache turned off, so no tokens are being reused between requests.',
    });
    return;
  }

  const lookups = (apc.lookups_hit || 0) + (apc.lookups_miss || 0);

  container.innerHTML = `
    <div class="panel-pad">
      <div class="duo-grid">
        <div class="pulse-box">
          <div class="pulse-box-title">Cache hit rate</div>
          <div class="pulse-box-val mono">${apc.hit_rate != null ? apc.hit_rate + '%' : dash()}</div>
          <div class="pulse-box-sub">${lookups ? formatNum(lookups) + ' lookups' : 'No lookups recorded'}</div>
        </div>
        <div class="pulse-box">
          <div class="pulse-box-title">Tokens reused</div>
          <div class="pulse-box-val mono">${apc.matched_tokens != null ? formatNum(apc.matched_tokens) : dash()}</div>
          <div class="pulse-box-sub">Saved from re-computation</div>
        </div>
      </div>
      <p class="panel-note">
        Exact cache hits: <strong>${apc.exact_hits != null ? apc.exact_hits : dash()}</strong>
        turns served directly from unified memory cache blocks without GPU re-encoding.
      </p>
    </div>
  `;
}

// 8. TPS Speed Distribution Histogram
function renderSpeedDistPanel(container, appState) {
  const buckets = appState.stats?.tps_buckets;
  if (!buckets || !Object.values(buckets).some(v => v > 0)) {
    container.innerHTML = stateBlock('empty', {
      title: 'No speed samples in this window',
      body: 'Decode speed is measured per assistant turn. Widen the time window or pick a different data source.',
      action: { label: 'Widen to All time', onclick: "setTimeWindow('all')" },
    });
    return;
  }

  const total = Object.values(buckets).reduce((a, b) => a + b, 0) || 1;
  const colors = {
    '< 15': 'var(--neon-coral)',
    '15 - 30': 'var(--neon-amber)',
    '30 - 45': 'var(--neon-cyan)',
    '45 - 60': 'var(--neon-acid)',
    '60+': 'var(--neon-pink)',
  };

  let rows = '';
  Object.entries(buckets).forEach(([bucket, count]) => {
    const pct = Math.round((count / total) * 100);
    const color = colors[bucket] || 'var(--neon-violet)';
    rows += `
      <div class="bar-row-item">
        <span class="bar-row-label mono">${bucket} tok/s</span>
        <div class="bar-row-track">
          <div class="bar-row-fill" style="width:${pct}%;background:${color};"></div>
        </div>
        <span class="bar-row-val mono" style="color:${color};">${count} <span style="font-size:0.65rem;color:var(--text-sub);">(${pct}%)</span></span>
      </div>
    `;
  });

  container.innerHTML = `
    <div style="padding:0.25rem 0;">
      <div style="font-size:0.75rem;color:var(--text-sub);margin-bottom:0.75rem;">Breakdown of all <strong>${total} turns</strong> across decode speed tiers:</div>
      ${rows}
    </div>
  `;
}

// 9. Model Token Output Share
function renderModelSharePanel(container, appState) {
  const models = appState.stats?.model_shares || [];
  if (!models.length) {
    container.innerHTML = `<div class="empty-state">No model share data recorded.</div>`;
    return;
  }

  const palette = ['var(--neon-acid)', 'var(--neon-violet)', 'var(--neon-cyan)', 'var(--neon-pink)', 'var(--neon-amber)'];
  const totalTokens = models.reduce((acc, m) => acc + (m.tokens_output || 0), 0) || 1;

  let rows = '';
  models.slice(0, 5).forEach((m, idx) => {
    const col = palette[idx % palette.length];
    const pct = Math.round(((m.tokens_output || 0) / totalTokens) * 100);
    const shortName = m.name.split('/').pop();
    rows += `
      <div class="bar-row-item">
        <span class="bar-row-label mono" title="${escapeHtml(m.name)}" style="color:${col};">● ${escapeHtml(shortName)}</span>
        <div class="bar-row-track">
          <div class="bar-row-fill" style="width:${pct}%;background:${col};"></div>
        </div>
        <span class="bar-row-val mono">${formatNum(m.tokens_output)} <span style="font-size:0.65rem;color:var(--text-sub);">(${pct}%)</span></span>
      </div>
    `;
  });

  container.innerHTML = `
    <div style="padding:0.25rem 0;">
      <div style="font-size:0.75rem;color:var(--text-sub);margin-bottom:0.75rem;">Model breakdown by total output tokens generated:</div>
      ${rows}
    </div>
  `;
}

// 10. Agent Tool Usage Breakdown
function renderToolUsagePanel(container, appState) {
  const tools = appState.stats?.tool_stats || [];
  if (!tools.length) {
    container.innerHTML = `<div class="empty-state">No agent tool calls recorded yet.</div>`;
    return;
  }

  const maxCount = Math.max(...tools.map(t => t.count), 1);
  let rows = '';
  tools.slice(0, 6).forEach(t => {
    const pct = Math.round((t.count / maxCount) * 100);
    rows += `
      <div class="bar-row-item">
        <span class="bar-row-label mono" style="color:var(--text-main);">🔧 ${escapeHtml(t.name)}</span>
        <div class="bar-row-track">
          <div class="bar-row-fill" style="width:${pct}%;background:linear-gradient(90deg, var(--neon-amber), var(--neon-pink));"></div>
        </div>
        <span class="bar-row-val mono" style="color:var(--neon-amber);">${t.count}</span>
      </div>
    `;
  });

  container.innerHTML = `
    <div style="padding:0.25rem 0;">
      <div style="font-size:0.75rem;color:var(--text-sub);margin-bottom:0.75rem;">Autonomous agent tools executed across sessions:</div>
      ${rows}
    </div>
  `;
}

// 11. Decode Speed (tok/s) Timeline Line Chart
function renderTpsTrendPanel(container, appState) {
  const data = appState.timeseries || [];
  container.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:1.25rem;font-size:0.75rem;margin-bottom:0.75rem;">
      <span style="color:var(--neon-acid);font-weight:600;">— Average TPS</span>
      <span style="color:var(--neon-pink);font-weight:600;">— Peak TPS</span>
    </div>
    <div class="chart-canvas-wrap" id="wrap-tps">
      <canvas id="canvas-tps"></canvas>
    </div>
  `;

  const wrap = document.getElementById('wrap-tps');
  const canvas = document.getElementById('canvas-tps');
  if (!wrap || !canvas) return;

  const draw = () => {
    if (!data.length) return;
    const rect = wrap.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (w <= 0 || h <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const pad = { top: 16, right: 24, bottom: 28, left: 42 };
    const chartW = w - pad.left - pad.right;
    const chartH = h - pad.top - pad.bottom;

    const maxTps = Math.max(...data.map(d => Math.max(d.avg_tps || 0, d.peak_tps || 0)), 35) * 1.15;
    const getX = idx => pad.left + (idx / Math.max(data.length - 1, 1)) * chartW;
    const getY = val => pad.top + chartH - (val / maxTps) * chartH;

    const gridStroke = themeAlpha('--shade-rgb', 0.10);
    const textFill = themeColor('--text-muted');
    const peakColor = themeColor('--accent-pink');
    const avgColor = themeColor('--accent-success');
    const fillStart = themeAlpha('--accent-success-rgb', 0.28);
    const fillEnd = themeAlpha('--accent-success-rgb', 0);
    const dotStroke = themeColor('--bg-panel-solid');

    ctx.strokeStyle = gridStroke;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yVal = (maxTps / 4) * i;
      const y = getY(yVal);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();

      ctx.fillStyle = textFill;
      ctx.font = '10px JetBrains Mono, monospace';
      ctx.textAlign = 'right';
      ctx.fillText(String(Math.round(yVal)), pad.left - 6, y + 3);
    }

    ctx.beginPath();
    data.forEach((d, i) => {
      const x = getX(i);
      const y = getY(d.peak_tps || 0);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = peakColor;
    ctx.lineWidth = 1.8;
    ctx.stroke();

    const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + chartH);
    grad.addColorStop(0, fillStart);
    grad.addColorStop(1, fillEnd);

    ctx.beginPath();
    data.forEach((d, i) => {
      const x = getX(i);
      const y = getY(d.avg_tps || 0);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = avgColor;
    ctx.lineWidth = 2.4;
    ctx.stroke();

    ctx.lineTo(getX(data.length - 1), pad.top + chartH);
    ctx.lineTo(getX(0), pad.top + chartH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    data.forEach((d, i) => {
      const x = getX(i);
      const yAvg = getY(d.avg_tps || 0);

      ctx.beginPath();
      ctx.arc(x, yAvg, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = avgColor;
      ctx.fill();
      ctx.strokeStyle = dotStroke;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      ctx.fillStyle = textFill;
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      const dateLabel = (d.date || '').slice(5);
      ctx.fillText(dateLabel, x, h - 8);
    });
  };

  draw();

  const ro = new ResizeObserver(() => draw());
  ro.observe(wrap);
  state.resizeObservers.set('tps-trend', ro);

  const tooltip = document.getElementById('chartTooltip');
  canvas.onmousemove = e => {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const padLeft = 42;
    const chartW = rect.width - padLeft - 24;
    const idx = Math.round(((mx - padLeft) / chartW) * (data.length - 1));

    if (idx >= 0 && idx < data.length) {
      const d = data[idx];
      tooltip.innerHTML = `
        <div style="font-weight:700;color:var(--text-main);margin-bottom:2px;">📅 ${d.date}</div>
        <div style="color:var(--neon-acid);">Avg Speed: <strong>${(d.avg_tps || 0).toFixed(1)} tok/s</strong></div>
        <div style="color:var(--neon-pink);">Peak Burst: <strong>${(d.peak_tps || 0).toFixed(1)} tok/s</strong></div>
        <div style="color:var(--text-sub);font-size:0.7rem;margin-top:2px;">${d.sessions || 1} session(s)</div>
      `;
      tooltip.style.left = e.clientX + 'px';
      tooltip.style.top = e.clientY + 'px';
      tooltip.classList.add('visible');
    }
  };
  canvas.onmouseleave = () => tooltip.classList.remove('visible');
}

// 12. Token Volume Stacked Bar Chart
function renderTokenVolumePanel(container, appState) {
  const data = appState.timeseries || [];
  container.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:1.25rem;font-size:0.75rem;margin-bottom:0.75rem;">
      <span style="color:var(--neon-violet);font-weight:600;">■ Output Generated</span>
      <span style="color:var(--neon-cyan);font-weight:600;">■ Input Context</span>
    </div>
    <div class="chart-canvas-wrap" id="wrap-tokens">
      <canvas id="canvas-tokens"></canvas>
    </div>
  `;

  const wrap = document.getElementById('wrap-tokens');
  const canvas = document.getElementById('canvas-tokens');
  if (!wrap || !canvas) return;

  const draw = () => {
    if (!data.length) return;
    const rect = wrap.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (w <= 0 || h <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const pad = { top: 16, right: 20, bottom: 28, left: 45 };
    const chartW = w - pad.left - pad.right;
    const chartH = h - pad.top - pad.bottom;

    const maxTokens = Math.max(...data.map(d => d.tokens_total || 0), 1000) * 1.15;
    const barWidth = Math.min((chartW / data.length) * 0.65, 30);

    const gridStroke = themeAlpha('--shade-rgb', 0.10);
    const textFill = themeColor('--text-muted');
    const inColor = themeAlpha('--accent-secondary-rgb', 0.55);
    const outColor = themeColor('--accent-primary');
    const dateFill = themeColor('--text-muted');

    ctx.strokeStyle = gridStroke;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yVal = (maxTokens / 4) * i;
      const y = pad.top + chartH - (yVal / maxTokens) * chartH;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();

      ctx.fillStyle = textFill;
      ctx.font = '9px JetBrains Mono, monospace';
      ctx.textAlign = 'right';
      ctx.fillText(formatNum(yVal), pad.left - 6, y + 3);
    }

    data.forEach((d, i) => {
      const xCenter = pad.left + (i + 0.5) * (chartW / data.length);
      const x = xCenter - barWidth / 2;

      const totalH = ((d.tokens_total || 0) / maxTokens) * chartH;
      const outH = ((d.tokens_output || 0) / maxTokens) * chartH;
      const inH = Math.max(totalH - outH, 0);
      const yBottom = pad.top + chartH;

      ctx.fillStyle = inColor;
      ctx.fillRect(x, yBottom - totalH, barWidth, inH);

      ctx.fillStyle = outColor;
      ctx.fillRect(x, yBottom - outH, barWidth, outH);

      ctx.fillStyle = dateFill;
      ctx.font = '9px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText((d.date || '').slice(5), xCenter, h - 8);
    });
  };

  draw();

  const ro = new ResizeObserver(() => draw());
  ro.observe(wrap);
  state.resizeObservers.set('token-volume', ro);

  const tooltip = document.getElementById('chartTooltip');
  canvas.onmousemove = e => {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const padLeft = 45;
    const chartW = rect.width - padLeft - 20;
    const idx = Math.floor(((mx - padLeft) / chartW) * data.length);

    if (idx >= 0 && idx < data.length) {
      const d = data[idx];
      tooltip.innerHTML = `
        <div style="font-weight:700;color:var(--text-main);margin-bottom:2px;">📅 ${d.date}</div>
        <div style="color:var(--neon-violet);">Output Gen: <strong>${formatNum(d.tokens_output)}</strong></div>
        <div style="color:var(--neon-cyan);">Input Context: <strong>${formatNum(d.tokens_input)}</strong></div>
        <div style="color:var(--text-main);border-top:1px solid var(--line-2);padding-top:2px;margin-top:2px;">Total: <strong>${formatNum(d.tokens_total)}</strong></div>
      `;
      tooltip.style.left = e.clientX + 'px';
      tooltip.style.top = e.clientY + 'px';
      tooltip.classList.add('visible');
    }
  };
  canvas.onmouseleave = () => tooltip.classList.remove('visible');
}

// 13. Live Engine & Hardware Pulse
function renderLivePulsePanel(container, appState) {
  const live = appState.live || {};
  const mlx = live.mlx || {};
  const ollama = live.ollama || {};
  const claw = live.openclaw || {};

  const mlxDet = mlx.details || {};
  const ollamaDet = ollama.details || {};
  const clawDet = claw.details || {};

  const box = (opts) => `
    <div class="pulse-box ${opts.online ? 'is-online' : 'is-offline'}" style="--pulse-accent:${opts.accent};">
      <div class="pulse-box-title">
        <span>${escapeHtml(opts.name)}</span>
        <span class="pulse-state mono">${opts.online ? '● ONLINE' : '○ OFFLINE'}</span>
      </div>
      <div class="pulse-box-val mono">${opts.value}</div>
      <div class="pulse-box-sub">${opts.sub}</div>
    </div>
  `;

  container.innerHTML = `
    <div class="live-pulse-grid">
      ${box({
        name: 'MLX Engine (:8080)',
        online: mlx.online,
        accent: 'var(--accent-success)',
        value: mlx.online ? `${mlxDet.decode_tok_s || 0} <span class="pulse-unit">tok/s</span>` : 'Idle',
        sub: mlx.online
          ? escapeHtml(mlxDet.model?.split('/').pop() || 'Loaded')
          : 'Start with mlx_vlm.server',
      })}
      ${box({
        name: 'Ollama Engine (:11434)',
        online: ollama.online,
        accent: 'var(--accent-secondary)',
        value: ollama.online
          ? (ollamaDet.active_model ? escapeHtml(ollamaDet.active_model) : 'Standby')
          : 'Offline',
        sub: ollama.online
          ? (ollamaDet.size_vram_gb ? `${ollamaDet.size_vram_gb} GB VRAM` : '0 models active')
          : 'Local daemon',
      })}
      ${box({
        name: `OpenClaw Gateway (:${clawDet.port || 18789})`,
        online: claw.online,
        accent: 'var(--accent-primary)',
        value: claw.online
          ? (clawDet.version ? escapeHtml(String(clawDet.version)) : 'Running')
          : 'Offline',
        sub: `${clawDet.sessions || 0} transcript${clawDet.sessions === 1 ? '' : 's'} on disk`,
      })}
    </div>
  `;
}

// 14. Session Duration Distribution
function renderDurationDistPanel(container, appState) {
  const buckets = appState.stats?.duration_buckets || {};
  const total = Object.values(buckets).reduce((a, b) => a + b, 0) || 1;

  let rows = '';
  Object.entries(buckets).forEach(([dur, count]) => {
    const pct = Math.round((count / total) * 100);
    rows += `
      <div class="bar-row-item">
        <span class="bar-row-label mono">${dur}</span>
        <div class="bar-row-track">
          <div class="bar-row-fill" style="width:${pct}%;background:linear-gradient(90deg, var(--neon-cyan), var(--neon-violet));"></div>
        </div>
        <span class="bar-row-val mono" style="color:var(--neon-cyan);">${count} <span style="font-size:0.65rem;color:var(--text-sub);">(${pct}%)</span></span>
      </div>
    `;
  });

  container.innerHTML = `
    <div style="padding:0.25rem 0;">
      <div style="font-size:0.75rem;color:var(--text-sub);margin-bottom:0.75rem;">Distribution of session conversation lengths:</div>
      ${rows}
    </div>
  `;
}

// 15. Top Workspaces Panel
function renderTopWorkspacesPanel(container, appState) {
  const dirs = appState.stats?.directories || [];
  if (!dirs.length) {
    container.innerHTML = `<div class="empty-state">No workspaces tracked yet.</div>`;
    return;
  }

  const maxTokens = Math.max(...dirs.map(d => d.tokens_output || 0), 1);
  let rows = '';
  dirs.slice(0, 5).forEach(d => {
    const pct = Math.round(((d.tokens_output || 0) / maxTokens) * 100);
    rows += `
      <div class="bar-row-item">
        <span class="bar-row-label mono" style="color:var(--text-main);" title="${escapeHtml(d.path)}">📁 ${escapeHtml(d.folder)}</span>
        <div class="bar-row-track">
          <div class="bar-row-fill" style="width:${pct}%;background:var(--neon-violet);"></div>
        </div>
        <span class="bar-row-val mono" style="color:var(--neon-violet);">${d.count} <span style="font-size:0.65rem;color:var(--text-sub);">sess</span></span>
      </div>
    `;
  });

  container.innerHTML = `
    <div style="padding:0.25rem 0;">
      <div style="font-size:0.75rem;color:var(--text-sub);margin-bottom:0.75rem;">Most active workspaces by session turns:</div>
      ${rows}
    </div>
  `;
}

// ============================================================
// PANEL GALLERY MODAL
// ============================================================
function openPanelGallery() {
  const modal = document.getElementById('galleryBackdrop');
  if (modal) {
    modal.classList.add('open');
    renderGallery();
    const searchInput = document.getElementById('gallerySearchInput');
    if (searchInput) {
      setTimeout(() => searchInput.focus(), 60);
    }
  }
}

function closePanelGallery() {
  const modal = document.getElementById('galleryBackdrop');
  if (modal) {
    modal.classList.remove('open');
  }
}

function setGalleryTab(tab) {
  state.gallery.activeTab = tab;
  document.querySelectorAll('.gallery-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.cat === tab);
  });
  renderGallery();
}

function filterGalleryCards() {
  const input = document.getElementById('gallerySearchInput');
  state.gallery.query = (input ? input.value : '').trim().toLowerCase();
  renderGallery();
}

function renderGallery() {
  const totalEl = document.getElementById('galleryTotalCount');
  if (totalEl) totalEl.textContent = Object.keys(PANEL_REGISTRY).length + ' Available';

  const container = document.getElementById('galleryCardsGrid');
  if (!container) return;

  const activeIds = new Set(state.layout.map(p => p.id));
  const allPanels = Object.values(PANEL_REGISTRY);

  const countAll = allPanels.length;
  const countPerf = allPanels.filter(p => p.category === 'perf').length;
  const countAnalytics = allPanels.filter(p => p.category === 'analytics').length;
  const countSystem = allPanels.filter(p => p.category === 'system').length;

  const elAll = document.getElementById('countCatAll');
  if (elAll) elAll.textContent = countAll;
  const elPerf = document.getElementById('countCatPerf');
  if (elPerf) elPerf.textContent = countPerf;
  const elAnalytics = document.getElementById('countCatAnalytics');
  if (elAnalytics) elAnalytics.textContent = countAnalytics;
  const elSystem = document.getElementById('countCatSystem');
  if (elSystem) elSystem.textContent = countSystem;

  const q = state.gallery.query;
  const tab = state.gallery.activeTab;

  const filtered = allPanels.filter(p => {
    if (tab !== 'all' && p.category !== tab) return false;
    if (!q) return true;
    return p.title.toLowerCase().includes(q) || p.description.toLowerCase().includes(q);
  });

  if (!filtered.length) {
    container.innerHTML = `
      <div style="grid-column:1/-1;text-align:center;padding:3rem 1rem;color:var(--text-sub);">
        No widgets found matching "${escapeHtml(q)}".
      </div>
    `;
    return;
  }

  container.innerHTML = filtered
    .map(p => {
      const isAdded = activeIds.has(p.id);
      const catClass = p.category === 'perf' ? 'perf' : (p.category === 'analytics' ? 'analytics' : 'system');
      const catLabel = p.category === 'perf' ? 'Performance' : (p.category === 'analytics' ? 'Analytics' : 'System');
      const sizeLabel = p.defaultCol === 12 ? 'Full Width' : (p.defaultCol === 8 ? '2/3 Width' : (p.defaultCol === 6 ? '1/2 Width' : '1/3 Width'));

      return `
        <div class="gallery-card ${isAdded ? 'added' : ''}">
          <div class="gallery-card-top">
            <div class="gallery-card-icon">${p.icon}</div>
            <div class="gallery-card-meta">
              <div class="gallery-card-title">${escapeHtml(p.title)}</div>
              <div class="gallery-card-desc">${escapeHtml(p.description)}</div>
            </div>
          </div>
          <div class="gallery-card-footer">
            <div style="display:flex;align-items:center;gap:0.4rem;">
              <span class="category-tag ${catClass}">${catLabel}</span>
              <span style="font-size:0.68rem;color:var(--text-sub);">${sizeLabel}</span>
            </div>
            ${
              isAdded
                ? `<button class="btn-ghost" onclick="removePanel('${p.id}')" style="color:var(--accent-success);border-color:rgba(var(--accent-success-rgb),0.35);">✓ Active</button>`
                : `<button class="btn-primary-neon" onclick="addPanel('${p.id}')" style="padding:0.3rem 0.75rem;font-size:0.75rem;">+ Add</button>`
            }
          </div>
        </div>
      `;
    })
    .join('');
}

// ============================================================
// DATA FETCHING & API INTEGRATIONS
// ============================================================
async function fetchStats() {
  try {
    const params = new URLSearchParams();
    if (state.activeFilter.window) params.set('window', state.activeFilter.window);
    if (state.activeFilter.from) params.set('from', state.activeFilter.from);
    if (state.activeFilter.to) params.set('to', state.activeFilter.to);
    if (state.activeFilter.harness) params.set('harness', state.activeFilter.harness);

    const queryStr = params.toString() ? '?' + params.toString() : '';
    const res = await fetch('/api/stats' + queryStr);
    state.stats = await res.json();
    if (state.stats.live) {
      state.live = state.stats.live;
    }
    renderDashboard();
    renderFilterDropdowns();
    updateDataSourcePillCounts();
    renderSystemInfo();
  } catch (err) {
    console.error('Failed to load stats', err);
  }
}

async function fetchLiveStatus() {
  try {
    const res = await fetch('/api/live');
    state.live = await res.json();

    // Re-render live panels if present on board
    const livePanel = document.getElementById('panel-body-live-pulse');
    if (livePanel) renderLivePulsePanel(livePanel, state);

    const burstPanel = document.getElementById('panel-body-speculative-burst');
    if (burstPanel) renderSpeculativeBurstPanel(burstPanel, state);

    const prefillPanel = document.getElementById('panel-body-prefill-vs-decode');
    if (prefillPanel) renderPrefillVsDecodePanel(prefillPanel, state);

    const ttftPanel = document.getElementById('panel-body-ttft-latency');
    if (ttftPanel) renderTtftLatencyPanel(ttftPanel, state);

    const tablePanel = document.getElementById('panel-body-telemetry-table');
    if (tablePanel) renderTelemetryTablePanel(tablePanel, state);
  } catch (err) {
    console.error('Failed to load live status', err);
  }
}

async function fetchTimeseries() {
  try {
    const params = new URLSearchParams();
    if (state.activeFilter.window) params.set('window', state.activeFilter.window);
    if (state.activeFilter.from) params.set('from', state.activeFilter.from);
    if (state.activeFilter.to) params.set('to', state.activeFilter.to);
    if (state.activeFilter.harness) params.set('harness', state.activeFilter.harness);

    const qStr = params.toString() ? '?' + params.toString() : '';
    const res = await fetch('/api/timeseries' + qStr);
    state.timeseries = await res.json();
    const tpsWrap = document.getElementById('panel-body-tps-trend');
    if (tpsWrap) renderTpsTrendPanel(tpsWrap, state);
    const volWrap = document.getElementById('panel-body-token-volume');
    if (volWrap) renderTokenVolumePanel(volWrap, state);
  } catch (err) {
    console.error('Failed to load timeseries', err);
  }
}

async function fetchSessions() {
  try {
    const params = new URLSearchParams();
    if (state.activeFilter.q) params.set('q', state.activeFilter.q);
    if (state.activeFilter.harness) params.set('harness', state.activeFilter.harness);
    if (state.activeFilter.folder) params.set('folder', state.activeFilter.folder);
    if (state.activeFilter.provider) params.set('provider', state.activeFilter.provider);
    if (state.activeFilter.model) params.set('model', state.activeFilter.model);
    if (state.activeFilter.speed_tier) params.set('speed_tier', state.activeFilter.speed_tier);
    if (state.activeFilter.window) params.set('window', state.activeFilter.window);
    if (state.activeFilter.from) params.set('from', state.activeFilter.from);
    if (state.activeFilter.to) params.set('to', state.activeFilter.to);
    if (state.activeFilter.sort) params.set('sort', state.activeFilter.sort);

    const res = await fetch('/api/sessions?' + params.toString());
    state.sessions = await res.json();
    renderSessionsList();
  } catch (err) {
    console.error('Failed to load sessions', err);
  }
}

function exportFilteredData() {
  const params = new URLSearchParams();
  if (state.activeFilter.q) params.set('q', state.activeFilter.q);
  if (state.activeFilter.harness) params.set('harness', state.activeFilter.harness);
  if (state.activeFilter.folder) params.set('folder', state.activeFilter.folder);
  if (state.activeFilter.provider) params.set('provider', state.activeFilter.provider);
  if (state.activeFilter.model) params.set('model', state.activeFilter.model);
  if (state.activeFilter.speed_tier) params.set('speed_tier', state.activeFilter.speed_tier);
  if (state.activeFilter.window) params.set('window', state.activeFilter.window);
  if (state.activeFilter.from) params.set('from', state.activeFilter.from);
  if (state.activeFilter.to) params.set('to', state.activeFilter.to);
  if (state.activeFilter.sort) params.set('sort', state.activeFilter.sort);

  window.open('/api/export?' + params.toString(), '_blank');
}

function setTimeWindow(win, triggerFetch = true) {
  state.activeFilter.window = win;
  state.activeFilter.from = '';
  state.activeFilter.to = '';

  document.querySelectorAll('.timewindow-pill').forEach(pill => {
    pill.classList.toggle('active', pill.getAttribute('data-window') === win);
  });

  const fromInput = document.getElementById('filterFromDT');
  const toInput = document.getElementById('filterToDT');
  if (fromInput) fromInput.value = '';
  if (toInput) toInput.value = '';

  if (triggerFetch) {
    fetchStats();
    fetchTimeseries();
    fetchSessions();
  }
}

function onCustomDateTimeChange() {
  const fromVal = document.getElementById('filterFromDT')?.value;
  const toVal = document.getElementById('filterToDT')?.value;
  if (fromVal || toVal) {
    state.activeFilter.window = '';
    state.activeFilter.from = fromVal || '';
    state.activeFilter.to = toVal || '';
    document.querySelectorAll('.timewindow-pill').forEach(pill => pill.classList.remove('active'));
    fetchStats();
    fetchTimeseries();
    fetchSessions();
  }
}

function clearCustomDateTime() {
  const fromInput = document.getElementById('filterFromDT');
  const toInput = document.getElementById('filterToDT');
  if (fromInput) fromInput.value = '';
  if (toInput) toInput.value = '';
  setTimeWindow('all');
}

function selectDataSource(harness, triggerFetch = true) {
  state.activeFilter.harness = harness;
  document.querySelectorAll('.sidebar-source-item').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-harness') === harness);
  });
  document.querySelectorAll('.datasource-pill').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-harness') === harness);
  });

  document.querySelectorAll('.explorer-filter-harness').forEach(sel => {
    sel.value = harness;
  });

  if (triggerFetch) {
    fetchStats();
    fetchTimeseries();
    fetchSessions();
  }
}

// Sidebar counts come from the unfiltered harness inventory, so selecting one
// source does not zero out the others.
function updateDataSourcePillCounts() {
  if (!state.stats) return;

  const targets = {
    all: ['sideCountAll', 'dsCountAll'],
    opencode: ['sideCountOpenCode', 'dsCountOpenCode'],
    openclaw: ['sideCountOpenClaw', 'dsCountOpenClaw'],
    aider: ['sideCountAider', 'dsCountAider'],
    continue: ['sideCountContinue', 'dsCountContinue'],
  };

  (state.stats.harnesses || []).forEach(h => {
    const ids = targets[h.id];
    if (!ids) return;
    const el = document.getElementById(ids[0]) || document.getElementById(ids[1]);
    if (el) el.textContent = h.count || 0;

    const btn = document.querySelector(`.sidebar-source-item[data-harness="${h.id === 'all' ? '' : h.id}"]`);
    if (btn) btn.classList.toggle('is-empty', !h.count);
  });

  updateServerStatuses();
}

// Local inference servers (MLX, Ollama, OpenClaw gateway) are engines rather than
// session archives, so their reachability is tracked separately from harness counts.
function updateServerStatuses() {
  if (!state.live) return;

  const setChip = (id, online) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = online ? 'Online' : 'Offline';
    el.className = online ? 'status-chip mono online' : 'status-chip mono offline';
  };
  const setDot = (id, online, title) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = online ? 'backend-dot-status online' : 'backend-dot-status offline';
    const dot = el.querySelector('.dot');
    if (dot) dot.className = online ? 'dot' : 'dot red';
    if (title) el.title = title;
  };
  const setItem = (selector, online) => {
    const el = document.querySelector(selector);
    if (el) el.classList.toggle('is-online', online);
  };

  const mlxOnline = !!state.live.mlx?.online;
  const ollamaOnline = !!state.live.ollama?.online;
  const clawOnline = !!state.live.openclaw?.online;
  const clawPort = state.live.openclaw?.details?.port || 18789;

  setChip('sideStatusMlx', mlxOnline);
  setChip('sideStatusOllama', ollamaOnline);
  setChip('sideStatusOpenClawGw', clawOnline);

  setItem('.sidebar-server-item[data-server="mlx"]', mlxOnline);
  setItem('.sidebar-server-item[data-server="ollama"]', ollamaOnline);
  setItem('.sidebar-server-item[data-server="openclaw"]', clawOnline);

  const portEl = document.getElementById('sidePortOpenClawGw');
  if (portEl) portEl.textContent = `:${clawPort}`;

  setDot('dotMlx', mlxOnline, `MLX Server (:8080) ${mlxOnline ? 'Online' : 'Offline'}`);
  setDot('dotOllama', ollamaOnline, `Ollama (:11434) ${ollamaOnline ? 'Online' : 'Offline'}`);
  setDot('dotOpenClawGw', clawOnline, `OpenClaw gateway (:${clawPort}) ${clawOnline ? 'Online' : 'Offline'}`);
}

// Clicking a local server jumps to the live pulse dashboard rather than filtering
// sessions - a server is not a session source.
function showServerPanel(serverId) {
  document.querySelectorAll('.sidebar-server-item').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-server') === serverId);
  });

  const live = state.live || {};
  const det = live[serverId]?.details || {};
  const online = !!live[serverId]?.online;
  const labels = { mlx: 'MLX LM (:8080)', ollama: 'Ollama (:11434)', openclaw: `OpenClaw gateway (:${det.port || 18789})` };

  navigateTo('observatory');
  const panel = document.getElementById('panel-live-pulse');
  if (panel) {
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    panel.classList.add('panel-flash');
    setTimeout(() => panel.classList.remove('panel-flash'), 1200);
  }

  showToast(`${labels[serverId] || serverId}: ${online ? 'online' : 'offline'}`);
}

function showToast(msg) {
  announce(msg);
  let toast = document.getElementById('toastNotification');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toastNotification';
    toast.style.cssText = 'position:fixed;bottom:24px;right:24px;background:var(--bg-panel-solid);border:1px solid var(--accent-primary);color:var(--text-main);padding:0.6rem 1.1rem;border-radius:8px;box-shadow:var(--glow-violet);font-family:var(--font-display);font-size:0.82rem;font-weight:600;z-index:9999;transition:opacity 0.25s ease;pointer-events:none;';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.opacity = '1';
  clearTimeout(toast._timeout);
  toast._timeout = setTimeout(() => { toast.style.opacity = '0'; }, 2200);
}

async function openSessionDetail(sessionId) {
  try {
    const res = await fetch('/api/session/' + sessionId);
    const detail = await res.json();
    state.activeSession = detail;
    renderSessionDrawer(detail);
    document.getElementById('drawerBackdrop').classList.add('open');
  } catch (err) {
    console.error('Failed to load session detail', err);
  }
}

function closeSessionDetail() {
  document.getElementById('drawerBackdrop').classList.remove('open');
}

function renderFilterDropdowns() {
  if (!state.stats) return;

  document.querySelectorAll('.explorer-filter-harness').forEach(harnessSelect => {
    const currentVal = state.activeFilter.harness || harnessSelect.value || '';
    if (state.stats.harnesses) {
      let opts = '<option value="">All Harnesses (' + (state.stats.total_sessions || 0) + ')</option>';
      state.stats.harnesses.forEach(h => {
        if (h.id === 'all') return;
        const label = h.detected ? `${h.name} (${h.count || 0})` : `${h.name} (Inactive)`;
        opts += `<option value="${escapeHtml(h.id)}">${escapeHtml(label)}</option>`;
      });
      harnessSelect.innerHTML = opts;
      harnessSelect.value = currentVal;
    }
  });

  document.querySelectorAll('.explorer-filter-folder').forEach(folderSelect => {
    const currentVal = state.activeFilter.folder || folderSelect.value || '';
    let opts = '<option value="">All Folders</option>';
    (state.stats.directories || []).forEach(d => {
      opts += `<option value="${escapeHtml(d.folder)}">${escapeHtml(d.folder)} (${d.count})</option>`;
    });
    folderSelect.innerHTML = opts;
    folderSelect.value = currentVal;
  });

  document.querySelectorAll('.explorer-filter-model').forEach(modelSelect => {
    const currentVal = state.activeFilter.model || modelSelect.value || '';
    let opts = '<option value="">All Models</option>';
    Object.entries(state.stats.models || {}).forEach(([fullId, count]) => {
      const parts = fullId.split('/');
      const shortModel = parts.slice(1).join('/') || fullId;
      opts += `<option value="${escapeHtml(shortModel)}">${escapeHtml(fullId)} (${count})</option>`;
    });
    modelSelect.innerHTML = opts;
    modelSelect.value = currentVal;
  });

  document.querySelectorAll('.explorer-filter-speed').forEach(speedSelect => {
    speedSelect.value = state.activeFilter.speed_tier || '';
  });

  document.querySelectorAll('.explorer-filter-sort').forEach(sortSelect => {
    sortSelect.value = state.activeFilter.sort || 'latest';
  });
}

function renderSessionsList() {
  const containers = document.querySelectorAll('.sessions-list');
  if (!containers.length) return;

  let innerContent = '';
  if (!state.sessions || !state.sessions.length) {
    innerContent = `
      <div class="empty-state">
        <p style="font-size:1.05rem;margin-bottom:0.4rem;color:var(--text-main);">No conversation sessions match your filter criteria.</p>
        <span style="font-size:0.8rem;color:var(--text-sub);">Try clearing search or expanding the time window.</span>
      </div>
    `;
  } else {
    innerContent = state.sessions
      .map(s => {
        const providerClass = s.provider === 'mlx' ? 'badge-tps' : 'badge-provider';
        const tpsDisplay = s.tps > 0 ? (s.tps + ' tok/s') : 'n/a';
        return `
          <div class="session-card" onclick="openSessionDetail('${s.id}')">
            <div class="session-main">
              <div class="session-title">${escapeHtml(s.title)}</div>
              <div class="session-meta-row">
                <span class="badge badge-folder mono">${escapeHtml(s.folder)}</span>
                <span class="badge badge-provider mono">${escapeHtml(s.harness || 'opencode')}</span>
                <span class="badge ${providerClass} mono">${escapeHtml(s.provider)} / ${escapeHtml(s.model.split('/').pop())}</span>
                <span>📅 ${s.date_str}</span>
                <span>⏱ ${formatSecs(s.duration_s)}</span>
                <span>💬 ${s.message_count} turns</span>
              </div>
            </div>
            <div class="session-stats">
              <div class="stat-item">
                <span class="stat-num mono" style="color:var(--accent-success);">${tpsDisplay}</span>
                <span class="stat-lbl">Session TPS</span>
              </div>
              <div class="stat-item">
                <span class="stat-num mono">${formatNum(s.tokens_output + s.tokens_reasoning)}</span>
                <span class="stat-lbl">Gen Tokens</span>
              </div>
              <div class="stat-item">
                <span class="stat-num mono" style="color:var(--accent-secondary);">${formatNum(s.tokens_input)}</span>
                <span class="stat-lbl">Input Tokens</span>
              </div>
            </div>
          </div>
        `;
      })
      .join('');
  }

  containers.forEach(c => {
    c.innerHTML = innerContent;
  });
}

function renderSessionDrawer(detail) {
  document.getElementById('drawerTitle').textContent = detail.title || 'Untitled Session';
  document.getElementById('drawerFolder').textContent = detail.folder || 'root';
  document.getElementById('drawerModel').textContent = detail.provider + ' / ' + detail.model;
  document.getElementById('drawerDate').textContent = detail.date_str;
  document.getElementById('drawerDuration').textContent = formatSecs(detail.duration_s);
  document.getElementById('drawerAvgTps').textContent = (detail.avg_tps || 0) + ' tok/s';
  document.getElementById('drawerTotalTokens').textContent = formatNum(detail.tokens_total);

  const container = document.getElementById('drawerMessages');
  if (!container) return;

  if (!detail.messages || !detail.messages.length) {
    container.innerHTML = `<div class="empty-state">No messages recorded for this session.</div>`;
    return;
  }

  container.innerHTML = detail.messages
    .map(msg => {
      const isUser = msg.role === 'user';
      const bubbleClass = isUser ? 'user' : 'assistant';
      const roleLabel = isUser ? 'User' : 'Assistant';

      let partsHtml = '';

      (msg.parts || []).forEach(p => {
        if (p.type === 'reasoning' && (p.content || p.text)) {
          const reasoningContent = p.content || p.text || '';
          partsHtml += `
            <details class="thinking-box" open>
              <summary class="thinking-title">🧠 Reasoning Chain (${reasoningContent.length} chars)</summary>
              <div class="msg-text md thinking-content" style="font-size:0.8rem;margin-top:0.4rem;line-height:1.5;">${renderMarkdown(reasoningContent)}</div>
            </details>
          `;
        } else if (p.type === 'tool') {
          const argKeys = Object.keys(p.input || {}).join(', ');
          const outPreview = typeof p.output === 'string' ? p.output : JSON.stringify(p.output, null, 2);
          partsHtml += `
            <details class="tool-box">
              <summary class="tool-header">
                <span>🔧 Tool Call: <strong>${escapeHtml(p.tool || 'tool')}</strong> [${escapeHtml(argKeys)}]</span>
                <span style="font-size:0.7rem;color:var(--text-sub);">${p.status || 'done'}</span>
              </summary>
              <div class="tool-body" style="margin-top:0.5rem;"><strong>Input:</strong>
${escapeHtml(JSON.stringify(p.input, null, 2))}

<strong>Output:</strong>
${escapeHtml(outPreview)}</div>
            </details>
          `;
        } else if (p.type === 'text' && (p.content || p.text)) {
          partsHtml += `<div class="msg-text md">${renderMarkdown(p.content || p.text)}</div>`;
        }
      });

      if (!partsHtml && (msg.content || msg.text)) {
        partsHtml += `<div class="msg-text md">${renderMarkdown(msg.content || msg.text)}</div>`;
      }

      const tpsBadge = !isUser && msg.tps > 0 
        ? `<span class="msg-speed-pill mono">⚡ ${msg.tps} tok/s</span>` 
        : '';

      const tokensBadge = !isUser && msg.tokens_output > 0
        ? `<span class="mono" style="font-size:0.72rem;color:var(--text-sub);">out: ${msg.tokens_output} (${formatSecs(msg.duration_s)})</span>`
        : '';

      return `
        <div class="message-bubble ${bubbleClass}">
          <div class="msg-header">
            <span class="msg-role ${bubbleClass}">${roleLabel}</span>
            <div style="display:flex;align-items:center;gap:0.75rem;">
              ${tokensBadge}
              ${tokensBadge ? '' : ''}
              ${tpsBadge}
              <span>${msg.date_str}</span>
            </div>
          </div>
          <div class="msg-body">
            ${partsHtml || '<div class="msg-text" style="color:var(--text-sub);font-style:italic;">(Tool execution / state step)</div>'}
          </div>
        </div>
      `;
    })
    .join('');
}

// 16. Moveable Sessions & Prompts Explorer Widget
function renderSessionsExplorerWidget(container, appState, item) {
  const panelH = item?.height || 520;
  container.innerHTML = `
    <div class="sessions-widget-wrap" style="display:flex;flex-direction:column;gap:0.85rem;height:100%;">
      <div class="sessions-filter-bar">
        <div style="display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap;">
          <div class="search-box" style="flex:1;min-width:220px;">
            <span class="search-icon">🔍</span>
            <input type="text" class="search-input explorer-search-input" placeholder="Search sessions, topics, prompts..." value="${escapeHtml(state.activeFilter.q || '')}">
          </div>
          <div class="filter-selects">
            <select class="custom-select explorer-filter-harness" title="Filter by Harness">
              <option value="">All Harnesses</option>
            </select>
            <select class="custom-select explorer-filter-folder" title="Filter by Workspace Folder">
              <option value="">All Folders</option>
            </select>
            <select class="custom-select explorer-filter-model" title="Filter by Model">
              <option value="">All Models</option>
            </select>
            <select class="custom-select explorer-filter-speed" title="Filter by Speed Tier">
              <option value="">All Speeds</option>
              <option value="turbo">⚡ Turbo (45+ TPS)</option>
              <option value="fast">🚀 Fast (25-45 TPS)</option>
              <option value="standard">🏎 Standard (15-25 TPS)</option>
              <option value="deep">🐢 Deep Reasoner (<15 TPS)</option>
            </select>
            <select class="custom-select explorer-filter-sort" title="Sort Order">
              <option value="latest">Latest First</option>
              <option value="oldest">Oldest First</option>
              <option value="duration">Longest First</option>
              <option value="tokens">Most Tokens</option>
              <option value="speed">Fastest TPS</option>
            </select>
          </div>
        </div>
      </div>
      <div class="sessions-container sessions-list" style="flex:1;min-height:0;overflow-y:auto;padding-right:4px;">
        <!-- Populated by renderSessionsList() -->
      </div>
    </div>
  `;

  bindExplorerWidgetEvents(container);
  renderFilterDropdowns();
  renderSessionsList();
}

function bindExplorerWidgetEvents(container) {
  let searchTimeout;
  const searchInput = container.querySelector('.explorer-search-input');
  if (searchInput) {
    searchInput.addEventListener('input', e => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        state.activeFilter.q = e.target.value;
        document.querySelectorAll('.explorer-search-input').forEach(inp => {
          if (inp !== e.target) inp.value = e.target.value;
        });
        fetchSessions();
      }, 250);
    });
  }

  const harnessSelect = container.querySelector('.explorer-filter-harness');
  if (harnessSelect) {
    harnessSelect.addEventListener('change', e => {
      selectDataSource(e.target.value);
    });
  }

  const folderSelect = container.querySelector('.explorer-filter-folder');
  if (folderSelect) {
    folderSelect.addEventListener('change', e => {
      state.activeFilter.folder = e.target.value;
      fetchSessions();
    });
  }

  const modelSelect = container.querySelector('.explorer-filter-model');
  if (modelSelect) {
    modelSelect.addEventListener('change', e => {
      state.activeFilter.model = e.target.value;
      fetchSessions();
    });
  }

  const speedSelect = container.querySelector('.explorer-filter-speed');
  if (speedSelect) {
    speedSelect.addEventListener('change', e => {
      state.activeFilter.speed_tier = e.target.value;
      fetchSessions();
    });
  }

  const sortSelect = container.querySelector('.explorer-filter-sort');
  if (sortSelect) {
    sortSelect.addEventListener('change', e => {
      state.activeFilter.sort = e.target.value;
      fetchSessions();
    });
  }
}

// ============================================================
// PAGES
// Adding a page is one registry entry: no router edit, no markup edit, no
// toolbar edit. A page declares which top-bar controls apply to it, so
// dashboard-only chrome cannot leak onto an unrelated page.
// ============================================================

registerPage({
  id: 'observatory',
  title: 'Dashboards',
  icon: '\u26A1',
  navSection: 'main',
  toolbar: ['dashboardPicker', 'timeWindow', 'editLayout', 'addPanel', 'export'],
  mount(el) {
    el.innerHTML = `
      <div class="edit-layout-banner" id="editLayoutBanner" style="display:none;">
        <div class="banner-text">
          <span aria-hidden="true">\u270F\uFE0F</span>
          <span><strong>Editing this dashboard:</strong> drag a panel by its header to reorder, or drag the bottom-right corner to resize.</span>
        </div>
        <div class="banner-actions">
          <button class="btn btn--ghost btn--sm" type="button" onclick="resetDashboardLayout()">\u21BA Reset</button>
          <button class="btn btn--accent btn--sm" type="button" onclick="toggleEditLayout()">\u2713 Done</button>
        </div>
      </div>
      <div class="dashboard-grid" id="dashboardGrid"></div>
    `;
    initDragReorder();
  },
  refresh() {
    renderDashboard();
    renderDashboardChrome();
  },
});

registerPage({
  id: 'sessions',
  title: 'Sessions',
  icon: '\uD83D\uDCAC',
  navSection: 'main',
  toolbar: ['timeWindow', 'export'],
  mount(el) {
    el.innerHTML = '<div class="sessions-full-view" id="sessionsFullViewContainer"></div>';
    const c = document.getElementById('sessionsFullViewContainer');
    if (c) renderSessionsExplorerWidget(c, state, { cols: 12, rows: 12 });
  },
  refresh() {
    const c = document.getElementById('sessionsFullViewContainer');
    if (c) renderSessionsExplorerWidget(c, state, { cols: 12, rows: 12 });
    fetchSessions();
  },
});

registerPage({
  id: 'settings',
  title: 'Settings & Theme',
  icon: '\u2699\uFE0F',
  navSection: 'foot',
  toolbar: [],
  mount(el) {
    const tpl = document.getElementById('tplSettings');
    if (tpl) el.appendChild(tpl.content.cloneNode(true));
  },
  refresh() {
    // Re-sync from state rather than relying on a click handler having run.
    document.querySelectorAll('.theme-card').forEach(card => {
      card.classList.toggle('active', card.getAttribute('data-theme') === state.theme);
    });
    document.querySelectorAll('.mode-btn').forEach(btn => {
      btn.classList.toggle('active', btn.id === 'btnMode' + (state.mode || 'dark').charAt(0).toUpperCase() + (state.mode || 'dark').slice(1));
    });
    renderSystemInfo();
  },
});

// Kept so the 47 inline handlers in index.html keep working during the
// transition; both now route to the settings page.
function toggleMode() {
  applyMode((state.mode === 'light') ? 'dark' : 'light');
}

function updateModeToggle() {
  const btn = document.getElementById('modeToggle');
  const label = document.getElementById('modeToggleLabel');
  if (!btn) return;
  const isLight = document.documentElement.getAttribute('data-mode') === 'light';
  btn.setAttribute('aria-pressed', String(isLight));
  btn.setAttribute('aria-label', isLight ? 'Switch to dark mode' : 'Switch to light mode');
  if (label) label.textContent = isLight ? 'Light mode' : 'Dark mode';
  const icon = btn.querySelector('.mode-toggle__icon');
  if (icon) icon.textContent = isLight ? '\u2600' : '\u263D';
}

function openSettingsModal() { navigateTo('settings'); }
function closeSettingsModal() {}

// ============================================================
// THEME & APPEARANCE SYSTEM (SUBTLE SLATE DEFAULT)
// ============================================================
const THEME_NAMES = {
  subtle: 'Subtle Slate & Indigo',
  neon: 'Cyber Neon',
  light: 'Creamy Studio',
  solar: 'Solar Monochrome'
};

function applyTheme(themeName) {
  state.theme = themeName;
  document.documentElement.setAttribute('data-theme', themeName);
  if (state.workspace) {
    state.workspace.prefs.theme = themeName;
    saveWorkspace(null, state.workspace);
  }

  document.querySelectorAll('.theme-card').forEach(card => {
    card.classList.toggle('active', card.getAttribute('data-theme') === themeName);
  });

  const themeLabel = document.getElementById('currentThemeName');
  if (themeLabel) {
    themeLabel.textContent = 'Theme: ' + (THEME_NAMES[themeName] || themeName);
  }

  refreshActivePage();
}

function applyMode(mode) {
  state.mode = mode;
  if (state.workspace) {
    state.workspace.prefs.mode = mode;
    saveWorkspace(null, state.workspace);
  }

  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.id === ('btnMode' + mode.charAt(0).toUpperCase() + mode.slice(1)));
  });

  let effectiveMode = mode;
  if (mode === 'system') {
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    effectiveMode = isDark ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-mode', effectiveMode);

  updateModeToggle();
  refreshActivePage();
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (state.mode === 'system') {
    applyMode('system');
  }
});

function openSettingsModal() {
  const modal = document.getElementById('settingsModal');
  if (modal) modal.classList.add('open');
  renderSystemInfo();
}

// The paths panel reports what the server actually resolved on this machine,
// rather than the defaults that used to be hardcoded in the markup.
function renderSystemInfo() {
  const info = state.stats?.system_info || {};
  const live = state.live || {};

  const set = (id, value, title) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.title = title || value;
  };

  set('sysOpenCodeDb', info.opencode_db || 'not detected');
  set('sysOpenClawDb', info.openclaw_db || 'not detected');

  const homes = info.openclaw_homes || [];
  set(
    'sysOpenClawHomes',
    homes.length ? homes.join('  ·  ') : 'not detected',
    homes.length ? homes.join('\n') : 'No OpenClaw home directory found'
  );

  const clawPort = live.openclaw?.details?.port || 18789;
  const clawState = live.openclaw?.online ? 'online' : 'offline';
  set('sysOpenClawGw', `http://127.0.0.1:${clawPort} (${clawState})`);
  set('sysMlxUrl', `http://127.0.0.1:8080 (${live.mlx?.online ? 'online' : 'offline'})`);
  set('sysOllamaUrl', `http://127.0.0.1:11434 (${live.ollama?.online ? 'online' : 'offline'})`);
}

function closeSettingsModal() {
  const modal = document.getElementById('settingsModal');
  if (modal) modal.classList.remove('open');
}

window.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeDashMenu();
    closePanelGallery();
    closeSessionDetail();
  }
});

// ============================================================
// INITIALIZATION ON DOM READY
// ============================================================
window.addEventListener('DOMContentLoaded', () => {
  // 1. Workspace first: it carries prefs, dashboards and any rescued draft.
  state.workspace = loadWorkspace(null, BUILTIN_DASHBOARDS, PANEL_REGISTRY, archetypeLimits);

  applyTheme(state.workspace.prefs.theme || 'subtle');
  applyMode(state.workspace.prefs.mode || 'dark');

  // 2. Open the dashboard that should be showing, without prompting on boot.
  const bootId = state.workspace.activeDashboardId || state.workspace.defaultDashboardId || 'default';
  state.workspace.activeDashboardId = bootId;
  const d = resolveDashboard(state.workspace, bootId, BUILTIN_DASHBOARDS);
  const draft = (state.workspace.draft && state.workspace.draft.dashboardId === bootId)
    ? state.workspace.draft : null;
  state.layout = JSON.parse(JSON.stringify((draft ? draft.layout : (d && d.layout)) || []));

  // 3. Route. An empty hash falls back to the last page, then to Observatory.
  applyRoute();
  renderDashboardChrome();
  renderDashboardMenu();

  // 4. Data.
  fetchStats();
  fetchTimeseries();
  fetchSessions();
  fetchLiveStatus();
  setInterval(fetchLiveStatus, 3500);
});

