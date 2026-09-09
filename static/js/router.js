/* ============================================================
   Page registry + hash router
   ------------------------------------------------------------
   The previous build hardcoded two page ids in four places inside
   switchNavTab, so a third page meant editing the router, the markup and
   the toolbar. Pages now register once and the router derives everything.

   Hash routing rather than the History API: server.py matches "/" and
   "/api/*" and falls through to a static file lookup, so "/sessions" would
   404 on refresh or on a pasted link. A hash cannot 404 and needs no
   server change.
   ============================================================ */

const PAGE_REGISTRY = {};

function registerPage(def) {
  PAGE_REGISTRY[def.id] = def;
}

const _mountedPages = new Set();
const _pageControllers = new Map();   // pageId -> AbortController
const _pageScroll = new Map();        // pageId -> scrollTop

function pageHost() {
  return document.getElementById('pageHost');
}

function pageContainer(pageId) {
  return document.getElementById('page-' + pageId);
}

/* ------------------------------------------------------------
   Hash parsing. Pure string functions, testable without a DOM.
   ------------------------------------------------------------ */
function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const segs = pathPart.split('/').filter(Boolean);
  const params = {};
  if (queryPart) {
    queryPart.split('&').forEach(pair => {
      const [k, v] = pair.split('=');
      if (k) params[decodeURIComponent(k)] = decodeURIComponent(v || '');
    });
  }
  return {
    pageId: segs[0] || '',
    sub: segs[1] ? decodeURIComponent(segs[1]) : '',
    params,
  };
}

function buildHash(route) {
  let out = '#/' + encodeURIComponent(route.pageId || '');
  if (route.sub) out += '/' + encodeURIComponent(route.sub);
  const keys = Object.keys(route.params || {}).filter(k => route.params[k] !== '' && route.params[k] != null);
  if (keys.length) {
    out += '?' + keys.map(k => `${encodeURIComponent(k)}=${encodeURIComponent(route.params[k])}`).join('&');
  }
  return out;
}

/* ------------------------------------------------------------
   One-way data flow.
   navigateTo only writes the hash; applyRoute only reads it and is the
   single mutator of state.activePage. No function does both, which is what
   prevents the render-loop class of bug.
   ------------------------------------------------------------ */
function navigateTo(pageId, params, sub) {
  const next = buildHash({ pageId, sub: sub || '', params: params || {} });
  if (location.hash === next) {
    applyRoute();          // same hash fires no event; apply directly
    return;
  }
  location.hash = next;
}

function fallbackPageId() {
  return PAGE_REGISTRY.observatory ? 'observatory' : Object.keys(PAGE_REGISTRY)[0];
}

function applyRoute() {
  const route = parseHash(location.hash);
  let pageId = route.pageId;

  if (!PAGE_REGISTRY[pageId]) {
    pageId = (window.state && state.workspace && state.workspace.prefs.lastPage) || fallbackPageId();
    if (!PAGE_REGISTRY[pageId]) pageId = fallbackPageId();
    // Rewrite the hash so Back does not re-trigger the bad route.
    const fixed = buildHash({ pageId, sub: '', params: {} });
    if (location.hash !== fixed) {
      location.replace(fixed);
      return;                       // the replace fires hashchange; let it drive
    }
  }

  const prev = window.state ? state.activePage : null;
  if (prev && prev !== pageId) {
    const prevEl = pageContainer(prev);
    if (prevEl) _pageScroll.set(prev, prevEl.scrollTop);
    const def = PAGE_REGISTRY[prev];
    const ctrl = _pageControllers.get(prev);
    if (ctrl) { ctrl.abort(); _pageControllers.delete(prev); }
    if (def && typeof def.unmount === 'function' && prevEl) def.unmount(prevEl);
  }

  if (window.state) {
    state.activePage = pageId;
    state.routeSub = route.sub;
    state.routeParams = route.params;
    if (state.workspace) {
      state.workspace.prefs.lastPage = pageId;
    }
  }

  const def = PAGE_REGISTRY[pageId];
  const host = pageHost();
  if (!def || !host) return;

  let el = pageContainer(pageId);
  if (!el) {
    el = document.createElement('section');
    el.id = 'page-' + pageId;
    el.className = 'page';
    host.appendChild(el);
  }

  Array.from(host.children).forEach(child => {
    child.classList.toggle('is-active', child.id === 'page-' + pageId);
  });

  if (!_mountedPages.has(pageId)) {
    const ctrl = new AbortController();
    _pageControllers.set(pageId, ctrl);
    _mountedPages.add(pageId);
    if (typeof def.mount === 'function') def.mount(el, window.state, ctrl.signal);
  } else if (!_pageControllers.has(pageId)) {
    _pageControllers.set(pageId, new AbortController());
  }

  if (typeof def.refresh === 'function') def.refresh(el, window.state);

  const savedScroll = _pageScroll.get(pageId);
  if (savedScroll) el.scrollTop = savedScroll;

  renderNav();
  renderToolbar(pageId);
  document.title = def.title ? `${def.title} · Token Telemetry` : 'Token Telemetry';
}

// Re-run the active page's refresh. Theme and mode changes route through here
// so a non-dashboard page cannot keep stale colours until a reload.
function refreshActivePage() {
  if (!window.state || !state.activePage) return;
  const def = PAGE_REGISTRY[state.activePage];
  const el = pageContainer(state.activePage);
  if (def && el && typeof def.refresh === 'function') def.refresh(el, window.state);
}

/* ------------------------------------------------------------
   Nav + per-page toolbar, both derived from the registry
   ------------------------------------------------------------ */
function renderNav() {
  const groups = {};
  Object.values(PAGE_REGISTRY).forEach(p => {
    if (p.hidden) return;
    const g = p.navSection || 'main';
    (groups[g] = groups[g] || []).push(p);
  });

  const build = (list, host) => {
    if (!host) return;
    host.innerHTML = list.map(p => `
      <li>
        <button class="navitem" type="button" data-page="${p.id}"
                aria-label="${escapeHtml(p.title)}"
                ${state.activePage === p.id ? 'aria-current="page"' : ''}
                onclick="navigateTo('${p.id}')">
          <span class="navitem__icon" aria-hidden="true">${p.icon || ''}</span>
          <span class="navitem__label">${escapeHtml(p.title)}</span>
          ${p.count != null ? `<span class="navitem__count mono">${p.count}</span>` : ''}
        </button>
      </li>
    `).join('');
  };

  build(groups.main || [], document.getElementById('navListMain'));
  build(groups.analysis || [], document.getElementById('navListAnalysis'));

  const analysisGroup = document.getElementById('navGroupAnalysis');
  if (analysisGroup) analysisGroup.hidden = !(groups.analysis || []).length;

  // Settings is pinned to the sidebar foot rather than mixed into the page list.
  const foot = document.getElementById('navListFoot');
  if (foot) build(groups.foot || [], foot);
}

function renderToolbar(pageId) {
  const def = PAGE_REGISTRY[pageId] || {};
  const allowed = new Set(def.toolbar || []);
  document.querySelectorAll('[data-toolbar]').forEach(el => {
    el.hidden = !allowed.has(el.dataset.toolbar);
  });
}

window.addEventListener('hashchange', applyRoute);
