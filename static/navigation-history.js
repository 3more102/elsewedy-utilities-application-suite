(() => {
  'use strict';

  const app = document.querySelector('#app');
  const nav = document.querySelector('#nav');
  const ROUTE_KEY = 'module';

  let applyingRoute = false;
  let initialized = false;

  function appVisible() {
    return app && !app.classList.contains('hidden');
  }

  function navButtons() {
    return nav ? [...nav.querySelectorAll('.nav-btn[data-view]')] : [];
  }

  function buttonFor(view) {
    if (!view) return null;
    return navButtons().find(button => button.dataset.view === view) || null;
  }

  function currentView() {
    return nav?.querySelector('.nav-btn.active[data-view]')?.dataset.view || '';
  }

  function routeFromLocation() {
    const raw = window.location.hash.replace(/^#/, '');
    if (!raw) return '';
    const params = new URLSearchParams(raw.includes('=') ? raw : `${ROUTE_KEY}=${raw}`);
    return params.get(ROUTE_KEY) || '';
  }

  function routeUrl(view) {
    const url = new URL(window.location.href);
    const params = new URLSearchParams(url.hash.replace(/^#/, ''));
    params.set(ROUTE_KEY, view);
    url.hash = params.toString();
    return `${url.pathname}${url.search}${url.hash}`;
  }

  function historyState(view) {
    const previous = history.state && typeof history.state === 'object' ? history.state : {};
    return {...previous, euasView: view};
  }

  function replaceRoute(view) {
    if (!view) return;
    history.replaceState(historyState(view), '', routeUrl(view));
  }

  function pushRoute(view) {
    if (!view || routeFromLocation() === view) return;
    history.pushState(historyState(view), '', routeUrl(view));
  }

  function activateView(view) {
    const button = buttonFor(view);
    if (!button) return false;
    if (button.classList.contains('active')) return true;

    applyingRoute = true;
    button.click();
    window.requestAnimationFrame(() => { applyingRoute = false; });
    return true;
  }

  function syncInitialRoute() {
    if (initialized || !appVisible() || !navButtons().length) return;

    const requested = routeFromLocation();
    if (requested && activateView(requested)) {
      replaceRoute(requested);
      initialized = true;
      return;
    }

    const active = currentView();
    if (active) {
      replaceRoute(active);
      initialized = true;
    }
  }

  function applyLocationRoute() {
    if (!appVisible() || !navButtons().length) return;
    if (!initialized) {
      syncInitialRoute();
      return;
    }

    const requested = routeFromLocation();
    if (requested && activateView(requested)) return;

    const active = currentView();
    if (active && requested !== active) replaceRoute(active);
  }

  nav?.addEventListener('click', event => {
    const button = event.target.closest('.nav-btn[data-view]');
    if (!button) return;

    const view = button.dataset.view || '';
    if (!view) return;

    if (!initialized) {
      window.requestAnimationFrame(syncInitialRoute);
      return;
    }

    if (!applyingRoute) pushRoute(view);
  });

  window.addEventListener('popstate', applyLocationRoute);
  window.addEventListener('hashchange', applyLocationRoute);

  if (nav) {
    new MutationObserver(syncInitialRoute).observe(nav, {childList: true, subtree: false});
  }

  if (app) {
    new MutationObserver(syncInitialRoute).observe(app, {attributes: true, attributeFilter: ['class']});
  }

  syncInitialRoute();
})();
