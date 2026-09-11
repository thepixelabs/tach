/* ============================================================
   Workspace storage
   ------------------------------------------------------------
   One document, one key. The previous build wrote four independent
   localStorage keys from four code paths with no ordering guarantee, and
   every layout mutation wrote a *global* layout key while Save wrote to a
   *view record* - so switching dashboards silently destroyed unsaved work.
   A single document is either atomically valid or atomically invalid.

   Every function takes an injectable `store` so the migration is testable
   without a DOM. No function here touches document or window.
   ============================================================ */

const WORKSPACE_KEY = 'tach_workspace';
const SCHEMA_VERSION = 1;

// Legacy keys, read once by the migration and left in place for one release
// as a cold backup. Do not write to these.
const V0_KEYS = {
  views: 'token_telemetry_saved_views',
  layout: 'token_telemetry_layout',
  theme: 'token_telemetry_theme',
  mode: 'token_telemetry_mode',
};

function defaultStore() {
  return typeof localStorage !== 'undefined' ? localStorage : null;
}

function newId() {
  return 'db_' + Date.now() + '_' + Math.random().toString(16).slice(2, 6);
}

function nowIso() {
  return new Date().toISOString();
}

function clone(v) {
  return JSON.parse(JSON.stringify(v));
}

function emptyWorkspace() {
  return {
    schemaVersion: SCHEMA_VERSION,
    rev: 1,
    prefs: { theme: 'subtle', mode: 'dark', lastPage: 'observatory', enginesCollapsed: [] },
    defaultDashboardId: 'default',
    activeDashboardId: 'default',
    dashboards: {},   // user dashboards only; built-ins are never copied in
    overrides: {},    // builtin id -> partial dashboard (copy-on-write)
    draft: null,      // single slot: the unsaved working state
  };
}

/* ------------------------------------------------------------
   Resolution
   Built-ins live in code and are never copied into storage, so a
   delete cannot reach them - the guard is structural, not an if.
   ------------------------------------------------------------ */
function resolveDashboard(ws, id, builtins) {
  const base = builtins[id];
  if (base) {
    const ov = (ws.overrides || {})[id] || {};
    return Object.assign({}, base, ov, {
      id,
      source: 'builtin',
      name: base.name,     // name always comes from code, never an override
      icon: base.icon,
    });
  }
  return (ws.dashboards || {})[id] || null;
}

function listDashboards(ws, builtins) {
  const out = [];
  Object.keys(builtins).forEach(id => out.push(resolveDashboard(ws, id, builtins)));
  Object.values(ws.dashboards || {}).forEach(d => out.push(d));
  return out;
}

function isBuiltin(id, builtins) {
  return Object.prototype.hasOwnProperty.call(builtins, id);
}

/* ------------------------------------------------------------
   Normalisation - runs on every load, not only on migration
   ------------------------------------------------------------ */
function normalizeLayout(layout, registry, archetypeFor) {
  if (!Array.isArray(layout)) return [];
  return layout.map(item => {
    if (!item || typeof item.id !== 'string') return null;
    const lim = archetypeFor ? archetypeFor(item.id) : null;

    // Legacy shape carried {col, height?}. Convert a pixel height into rows.
    let cols = Number.isFinite(item.cols) ? item.cols : Number(item.col);
    if (!Number.isFinite(cols)) cols = (registry[item.id] || {}).defaultCol || 6;
    cols = Math.max(1, Math.min(12, Math.round(cols)));

    let rows = Number.isFinite(item.rows) ? item.rows : null;
    if (rows === null && Number.isFinite(Number(item.height))) {
      // rows = round((height + gap) / (rowPx + gap)) with the shipped defaults
      const ROW_PX = 56, GAP_PX = 20;
      rows = Math.round((Number(item.height) + GAP_PX) / (ROW_PX + GAP_PX));
    }
    if (!Number.isFinite(rows)) {
      const def = registry[item.id] || {};
      rows = def.defaultRows || (lim ? lim.rows : 4);
    }

    if (lim) {
      cols = Math.max(lim.minCols, Math.min(lim.maxCols, cols));
      rows = Math.max(lim.minRows, Math.min(lim.maxRows, rows));
    }
    rows = Math.max(2, Math.min(20, Math.round(rows)));

    // An unknown panel id is KEPT, not silently dropped. A panel that
    // disappears in one release and returns in the next must survive the gap.
    return { id: item.id, cols, rows };
  }).filter(Boolean);
}

function normalizeWorkspace(ws, builtins, registry, archetypeFor) {
  const out = Object.assign(emptyWorkspace(), ws || {});
  out.prefs = Object.assign({ theme: 'subtle', mode: 'dark', lastPage: 'observatory', enginesCollapsed: [] }, out.prefs || {});
  if (!Array.isArray(out.prefs.enginesCollapsed)) out.prefs.enginesCollapsed = [];
  out.dashboards = out.dashboards || {};
  out.overrides = out.overrides || {};

  Object.keys(out.dashboards).forEach(id => {
    const d = out.dashboards[id];
    if (!d || typeof d !== 'object') { delete out.dashboards[id]; return; }
    d.id = id;
    d.source = 'user';
    d.name = String(d.name || 'Untitled dashboard');
    d.icon = d.icon || 'DB';
    d.layout = normalizeLayout(d.layout, registry, archetypeFor);
    d.filters = d.filters || {};
  });

  Object.keys(out.overrides).forEach(id => {
    if (!isBuiltin(id, builtins)) { delete out.overrides[id]; return; }
    const ov = out.overrides[id];
    if (ov && ov.layout) ov.layout = normalizeLayout(ov.layout, registry, archetypeFor);
  });

  // A default pointing at a deleted dashboard would break every boot.
  if (!resolveDashboard(out, out.defaultDashboardId, builtins)) {
    out.defaultDashboardId = 'default';
  }
  if (!resolveDashboard(out, out.activeDashboardId, builtins)) {
    out.activeDashboardId = out.defaultDashboardId;
  }

  // Drop a draft whose dashboard no longer resolves.
  if (out.draft) {
    if (!resolveDashboard(out, out.draft.dashboardId, builtins)) {
      out.draft = null;
    } else {
      out.draft.layout = normalizeLayout(out.draft.layout, registry, archetypeFor);
    }
  }

  out.schemaVersion = SCHEMA_VERSION;
  return out;
}

/* ------------------------------------------------------------
   Migration - idempotent, lossless
   ------------------------------------------------------------ */
function migrateWorkspace(store, builtins, registry, archetypeFor) {
  store = store || defaultStore();
  if (!store) return emptyWorkspace();

  // 1. Short-circuit. Running this N times is a no-op.
  let raw = null;
  try { raw = store.getItem(WORKSPACE_KEY); } catch (e) { raw = null; }
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && Number.isInteger(parsed.schemaVersion) && parsed.schemaVersion >= 1) {
        return normalizeWorkspace(parsed, builtins, registry, archetypeFor);
      }
    } catch (e) {
      // 2. Quarantine before touching anything.
      try { store.setItem(WORKSPACE_KEY + '_corrupt_' + Date.now(), raw); } catch (e2) {}
    }
  }

  const ws = emptyWorkspace();

  // 3. Read v0 keys defensively; one bad key must not block the others.
  const readJson = (k, fallback) => {
    try {
      const v = store.getItem(k);
      return v ? JSON.parse(v) : fallback;
    } catch (e) { return fallback; }
  };
  const readStr = (k, fallback) => {
    try { return store.getItem(k) || fallback; } catch (e) { return fallback; }
  };

  // 4. Classify saved views: a built-in id becomes an override, everything
  //    else becomes a user dashboard. This matches what the old code already
  //    did at read time, so nothing changes meaning.
  const oldViews = readJson(V0_KEYS.views, {}) || {};
  Object.keys(oldViews).forEach(id => {
    const v = oldViews[id];
    if (!v || typeof v !== 'object') return;
    const filters = mapLegacyFilter(v.filter);
    if (isBuiltin(id, builtins)) {
      ws.overrides[id] = {
        layout: normalizeLayout(v.layout, registry, archetypeFor),
        filters,
        updatedAt: v.updated_at || nowIso(),
      };
    } else {
      ws.dashboards[id] = {
        id,                                   // preserved verbatim; a stored id is an identity
        name: String(v.name || 'Untitled dashboard'),
        icon: 'DB',
        layout: normalizeLayout(v.layout, registry, archetypeFor),
        filters,
        source: 'user',
        baseId: null,
        createdAt: v.created_at || nowIso(),
        updatedAt: v.updated_at || nowIso(),
      };
    }
  });

  // 5. Rescue the orphan working layout. In v0 it had no owner, so if it
  //    differs from the default dashboard it becomes an unsaved draft rather
  //    than being discarded. This is the no-data-loss guarantee.
  const oldLayout = readJson(V0_KEYS.layout, null);
  if (Array.isArray(oldLayout) && oldLayout.length) {
    const mine = normalizeLayout(oldLayout, registry, archetypeFor);
    const base = resolveDashboard(ws, 'default', builtins);
    const theirs = normalizeLayout((base && base.layout) || [], registry, archetypeFor);
    if (JSON.stringify(mine) !== JSON.stringify(theirs)) {
      ws.draft = {
        dashboardId: 'default',
        layout: mine,
        filters: {},
        touchedAt: nowIso(),
      };
    }
  }

  // 6. Preferences.
  ws.prefs.theme = readStr(V0_KEYS.theme, 'subtle');
  ws.prefs.mode = readStr(V0_KEYS.mode, 'dark');

  // 7. Write. On failure run in memory rather than deleting anything.
  saveWorkspace(store, ws);

  // 8. v0 keys are deliberately NOT deleted. They are the rollback path.
  return normalizeWorkspace(ws, builtins, registry, archetypeFor);
}

function mapLegacyFilter(f) {
  f = f || {};
  const pinned = [];
  ['window', 'harness', 'speed_tier'].forEach(k => {
    if (f[k] !== undefined && f[k] !== '') pinned.push(k);
  });
  return {
    window: f.window || 'all',
    from: '', to: '',
    harness: f.harness || '',
    speed_tier: f.speed_tier || '',
    pinned,
  };
}

function loadWorkspace(store, builtins, registry, archetypeFor) {
  return migrateWorkspace(store || defaultStore(), builtins, registry, archetypeFor);
}

function saveWorkspace(store, ws) {
  store = store || defaultStore();
  if (!store) return { ok: false, reason: 'no-store' };
  try {
    ws.rev = (ws.rev || 0) + 1;
    store.setItem(WORKSPACE_KEY, JSON.stringify(ws));
    return { ok: true };
  } catch (e) {
    // Quota or private mode. Never delete the v0 backup on this path.
    return { ok: false, reason: 'quota', error: String(e) };
  }
}

/* ------------------------------------------------------------
   Dashboard CRUD - pure over (ws, args) so the state machine is
   testable without a browser.
   ------------------------------------------------------------ */
function uniqueName(ws, builtins, wanted) {
  const taken = new Set(listDashboards(ws, builtins).map(d => d.name));
  if (!taken.has(wanted)) return wanted;
  let n = 2;
  while (taken.has(`${wanted} ${n}`)) n++;
  return `${wanted} ${n}`;
}

function createDashboard(ws, builtins, name, layout, filters) {
  const id = newId();
  ws.dashboards[id] = {
    id,
    name: uniqueName(ws, builtins, (name || 'New dashboard').trim()),
    icon: 'DB',
    layout: clone(layout || []),
    filters: filters ? clone(filters) : { window: 'all', from: '', to: '', harness: '', speed_tier: '', pinned: [] },
    source: 'user',
    baseId: null,
    createdAt: nowIso(),
    updatedAt: nowIso(),
  };
  return id;
}

function duplicateDashboard(ws, builtins, srcId, layoutOverride) {
  const src = resolveDashboard(ws, srcId, builtins);
  if (!src) return null;
  const id = newId();
  ws.dashboards[id] = {
    id,
    name: uniqueName(ws, builtins, src.name + ' copy'),
    icon: src.icon || 'DB',
    layout: clone(layoutOverride || src.layout || []),
    filters: clone(src.filters || {}),
    source: 'user',
    baseId: srcId,
    createdAt: nowIso(),
    updatedAt: nowIso(),
  };
  return id;
}

function renameDashboard(ws, builtins, id, name) {
  // Renaming a built-in would desync the code-side name from the override.
  if (isBuiltin(id, builtins)) return { ok: false, reason: 'builtin' };
  const d = ws.dashboards[id];
  if (!d) return { ok: false, reason: 'missing' };
  d.name = uniqueName(ws, builtins, String(name || '').trim() || d.name);
  d.updatedAt = nowIso();
  return { ok: true };
}

function deleteDashboard(ws, builtins, id) {
  // Built-ins are not in ws.dashboards, so this cannot reach them.
  if (isBuiltin(id, builtins)) return { ok: false, reason: 'builtin' };
  if (!ws.dashboards[id]) return { ok: false, reason: 'missing' };
  delete ws.dashboards[id];
  if (ws.defaultDashboardId === id) ws.defaultDashboardId = 'default';
  if (ws.activeDashboardId === id) ws.activeDashboardId = ws.defaultDashboardId;
  if (ws.draft && ws.draft.dashboardId === id) ws.draft = null;
  return { ok: true };
}

function setDefaultDashboard(ws, builtins, id) {
  if (!resolveDashboard(ws, id, builtins)) return { ok: false, reason: 'missing' };
  ws.defaultDashboardId = id;
  return { ok: true };
}

// "Reset to original" for a built-in: drop the override and the frozen factory
// definition returns, including anything shipped since.
function resetBuiltin(ws, builtins, id) {
  if (!isBuiltin(id, builtins)) return { ok: false, reason: 'not-builtin' };
  delete ws.overrides[id];
  if (ws.draft && ws.draft.dashboardId === id) ws.draft = null;
  return { ok: true };
}

function writeDraft(ws, dashboardId, layout, filters) {
  ws.draft = {
    dashboardId,
    layout: clone(layout),
    filters: filters ? clone(filters) : {},
    touchedAt: nowIso(),
  };
}

function discardDraft(ws) {
  ws.draft = null;
}

function commitDraft(ws, builtins) {
  const d = ws.draft;
  if (!d) return { ok: false, reason: 'no-draft' };
  const id = d.dashboardId;
  if (isBuiltin(id, builtins)) {
    ws.overrides[id] = Object.assign({}, ws.overrides[id], {
      layout: clone(d.layout),
      updatedAt: nowIso(),
    });
  } else if (ws.dashboards[id]) {
    ws.dashboards[id].layout = clone(d.layout);
    ws.dashboards[id].updatedAt = nowIso();
  } else {
    return { ok: false, reason: 'missing' };
  }
  ws.draft = null;
  return { ok: true, id };
}

function isDirty(ws, dashboardId) {
  return !!(ws.draft && ws.draft.dashboardId === dashboardId);
}

// Requireable from node --test; inert in a browser.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    WORKSPACE_KEY, SCHEMA_VERSION, V0_KEYS,
    emptyWorkspace, normalizeWorkspace, normalizeLayout, migrateWorkspace,
    loadWorkspace, saveWorkspace, resolveDashboard, listDashboards, isBuiltin,
    createDashboard, duplicateDashboard, renameDashboard, deleteDashboard,
    setDefaultDashboard, resetBuiltin,
    writeDraft, discardDraft, commitDraft, isDirty, uniqueName,
  };
}
