const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');

// parseHash/buildHash are pure; extract them without a DOM.
const src = fs.readFileSync(__dirname + '/../static/js/router.js', 'utf8');
const parseHash = eval('(' + /function parseHash\(hash\) \{[\s\S]*?\n\}/.exec(src)[0].replace(/^function parseHash/, 'function') + ')');
const buildHash = eval('(' + /function buildHash\(route\) \{[\s\S]*?\n\}/.exec(src)[0].replace(/^function buildHash/, 'function') + ')');

test('parseHash extracts page, sub and params', () => {
  const r = parseHash('#/observatory/db_123?window=1h&harness=openclaw');
  assert.strictEqual(r.pageId, 'observatory');
  assert.strictEqual(r.sub, 'db_123');
  assert.deepStrictEqual(r.params, { window: '1h', harness: 'openclaw' });
});

test('buildHash round-trips a parsed route', () => {
  const input = '#/observatory/db_123?window=1h';
  assert.strictEqual(buildHash(parseHash(input)), input);
});

test('parseHash tolerates an empty or bare hash', () => {
  assert.strictEqual(parseHash('').pageId, '');
  assert.strictEqual(parseHash('#').pageId, '');
  assert.strictEqual(parseHash('#/sessions').pageId, 'sessions');
});

test('buildHash omits empty params rather than emitting a trailing ?', () => {
  assert.strictEqual(buildHash({ pageId: 'sessions', params: { q: '' } }), '#/sessions');
});
