const test = require('node:test');
const assert = require('node:assert');
const S = require('../static/js/storage.js');

// Minimal stand-ins for the app's real registries.
const BUILTINS = {
  default: { name: 'Default Overview', icon: 'A', layout: [{ id: 'kpi-banner', cols: 12, rows: 3 }], filters: {} },
  mtp_speculative: { name: 'Speculative MTP', icon: 'B', layout: [{ id: 'ttft-latency', cols: 6, rows: 4 }], filters: {} },
};
const REGISTRY = { 'kpi-banner': { defaultCol: 12 }, 'ttft-latency': { defaultCol: 6 }, 'tps-trend': { defaultCol: 8 } };

function fakeStore(seed = {}) {
  const m = new Map(Object.entries(seed));
  return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k),
    _map: m,
  };
}
const migrate = store => S.migrateWorkspace(store, BUILTINS, REGISTRY, null);

test('migrateWorkspace is idempotent: two runs yield an identical document', () => {
  const store = fakeStore({
    [S.V0_KEYS.theme]: 'neon',
    [S.V0_KEYS.mode]: 'light',
  });
  const first = migrate(store);
  const rev1 = first.rev;
  const second = migrate(store);
  assert.deepStrictEqual({ ...second, rev: rev1 }, { ...first, rev: rev1 });
  assert.strictEqual(second.prefs.theme, 'neon');
});

test('a customized legacy layout survives as an unsaved draft (no data loss)', () => {
  const custom = [{ id: 'tps-trend', col: 8 }, { id: 'kpi-banner', col: 12 }];
  const store = fakeStore({ [S.V0_KEYS.layout]: JSON.stringify(custom) });
  const ws = migrate(store);
  assert.ok(ws.draft, 'expected a draft to be created');
  assert.strictEqual(ws.draft.dashboardId, 'default');
  assert.deepStrictEqual(ws.draft.layout.map(i => i.id), ['tps-trend', 'kpi-banner']);
});

test('a legacy layout identical to the default produces no spurious draft', () => {
  const store = fakeStore({
    [S.V0_KEYS.layout]: JSON.stringify([{ id: 'kpi-banner', col: 12, height: 200 }]),
  });
  const ws = migrate(store);
  assert.strictEqual(ws.draft, null);
});

test('a saved view keyed by a builtin id becomes an override, not a user dashboard', () => {
  const store = fakeStore({
    [S.V0_KEYS.views]: JSON.stringify({
      mtp_speculative: { name: 'x', layout: [{ id: 'tps-trend', col: 4 }], filter: { window: '1h' } },
      view_123: { name: 'Mine', layout: [{ id: 'kpi-banner', col: 12 }], filter: {} },
    }),
  });
  const ws = migrate(store);
  assert.ok(ws.overrides.mtp_speculative, 'builtin override missing');
  assert.strictEqual(ws.dashboards.mtp_speculative, undefined);
  assert.ok(ws.dashboards.view_123, 'user dashboard missing');
  assert.strictEqual(ws.dashboards.view_123.id, 'view_123', 'stored id must be preserved');
});

test('corrupt workspace JSON is quarantined and defaults boot', () => {
  const store = fakeStore({ [S.WORKSPACE_KEY]: '{not json' });
  const ws = migrate(store);
  assert.strictEqual(ws.schemaVersion, S.SCHEMA_VERSION);
  const quarantined = [...store._map.keys()].filter(k => k.includes('_corrupt_'));
  assert.strictEqual(quarantined.length, 1, 'raw value must be preserved');
});

test('an unknown panel id is kept, not silently dropped', () => {
  const rows = S.normalizeLayout([{ id: 'panel-from-the-future', col: 6 }], REGISTRY, null);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].id, 'panel-from-the-future');
});

test('legacy pixel height converts to a row span', () => {
  const rows = S.normalizeLayout([{ id: 'tps-trend', col: 8, height: 380 }], REGISTRY, null);
  assert.strictEqual(rows[0].cols, 8);
  assert.ok(rows[0].rows >= 4 && rows[0].rows <= 6, `got ${rows[0].rows}`);
});

test('deleteDashboard refuses a builtin and leaves the document unchanged', () => {
  const ws = S.emptyWorkspace();
  const before = JSON.stringify(ws);
  const res = S.deleteDashboard(ws, BUILTINS, 'default');
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'builtin');
  assert.strictEqual(JSON.stringify(ws), before);
  assert.ok(S.resolveDashboard(ws, 'default', BUILTINS), 'builtin must still resolve');
});

test('renameDashboard refuses a builtin but allows a user dashboard', () => {
  const ws = S.emptyWorkspace();
  assert.strictEqual(S.renameDashboard(ws, BUILTINS, 'default', 'Nope').ok, false);
  const id = S.createDashboard(ws, BUILTINS, 'Mine', []);
  assert.strictEqual(S.renameDashboard(ws, BUILTINS, id, 'Renamed').ok, true);
  assert.strictEqual(ws.dashboards[id].name, 'Renamed');
});

test('resetBuiltin drops the override and restores the factory layout', () => {
  const ws = S.emptyWorkspace();
  ws.overrides.default = { layout: [{ id: 'tps-trend', cols: 4, rows: 4 }] };
  assert.strictEqual(S.resolveDashboard(ws, 'default', BUILTINS).layout[0].id, 'tps-trend');
  S.resetBuiltin(ws, BUILTINS, 'default');
  assert.strictEqual(S.resolveDashboard(ws, 'default', BUILTINS).layout[0].id, 'kpi-banner');
});

test('editing writes a draft and does not mutate the saved record until committed', () => {
  const ws = S.emptyWorkspace();
  S.writeDraft(ws, 'default', [{ id: 'tps-trend', cols: 4, rows: 4 }]);
  assert.strictEqual(S.resolveDashboard(ws, 'default', BUILTINS).layout[0].id, 'kpi-banner',
    'record must be untouched while a draft is pending');
  assert.strictEqual(S.isDirty(ws, 'default'), true);
  S.commitDraft(ws, BUILTINS);
  assert.strictEqual(S.resolveDashboard(ws, 'default', BUILTINS).layout[0].id, 'tps-trend');
  assert.strictEqual(ws.draft, null);
});

test('duplicating a builtin yields a deletable user dashboard with a unique name', () => {
  const ws = S.emptyWorkspace();
  const a = S.duplicateDashboard(ws, BUILTINS, 'default');
  const b = S.duplicateDashboard(ws, BUILTINS, 'default');
  assert.notStrictEqual(ws.dashboards[a].name, ws.dashboards[b].name);
  assert.strictEqual(ws.dashboards[a].source, 'user');
  assert.strictEqual(ws.dashboards[a].baseId, 'default');
  assert.strictEqual(S.deleteDashboard(ws, BUILTINS, a).ok, true);
});

test('deleting the default dashboard repairs the default pointer', () => {
  const ws = S.emptyWorkspace();
  const id = S.createDashboard(ws, BUILTINS, 'Mine', []);
  S.setDefaultDashboard(ws, BUILTINS, id);
  S.deleteDashboard(ws, BUILTINS, id);
  assert.strictEqual(ws.defaultDashboardId, 'default');
});

test('a default pointing at a missing dashboard self-heals on normalize', () => {
  const ws = S.emptyWorkspace();
  ws.defaultDashboardId = 'ghost';
  const out = S.normalizeWorkspace(ws, BUILTINS, REGISTRY, null);
  assert.strictEqual(out.defaultDashboardId, 'default');
});

test('saveWorkspace reports failure instead of throwing when the store rejects', () => {
  const bad = { getItem: () => null, setItem: () => { throw new Error('QuotaExceeded'); }, removeItem: () => {} };
  const res = S.saveWorkspace(bad, S.emptyWorkspace());
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'quota');
});

test('migration does not delete the v0 keys (rollback path)', () => {
  const store = fakeStore({
    [S.V0_KEYS.views]: JSON.stringify({ view_1: { name: 'A', layout: [], filter: {} } }),
    [S.V0_KEYS.theme]: 'solar',
  });
  migrate(store);
  assert.ok(store.getItem(S.V0_KEYS.views), 'v0 views key must remain as backup');
  assert.strictEqual(store.getItem(S.V0_KEYS.theme), 'solar');
});
