(() => {
  'use strict';

  const content = document.querySelector('#content');
  const app = document.querySelector('#app');
  const toast = document.querySelector('#toast');
  const loginError = document.querySelector('#login-error');
  const modalLayer = document.querySelector('#modal-layer');
  const helpButton = document.querySelector('#help-btn');

  const errorPattern = /(error|failed|unable|expired|denied|forbidden|not authorized|network|request failed|timed out|unavailable)/i;
  const successPattern = /(success|saved|created|updated|completed|approved|submitted|assigned|marked|uploaded|deleted|closed|started|paused|received|generated)/i;

  let activeRequests = 0;
  let networkIdleTimer = null;
  let onlineHideTimer = null;

  const activity = document.createElement('div');
  activity.className = 'network-activity';
  activity.setAttribute('aria-hidden', 'true');
  document.body.appendChild(activity);

  const connectionStatus = document.createElement('span');
  connectionStatus.className = 'connection-status hidden';
  connectionStatus.setAttribute('role', 'status');
  connectionStatus.setAttribute('aria-live', 'polite');
  if (helpButton?.parentElement) helpButton.before(connectionStatus);

  function isAppVisible() {
    return app && !app.classList.contains('hidden');
  }

  function syncConnection(initial = false) {
    if (!connectionStatus) return;
    clearTimeout(onlineHideTimer);
    const online = navigator.onLine;
    document.body.classList.toggle('is-offline', !online);
    connectionStatus.classList.toggle('is-offline', !online);
    connectionStatus.classList.toggle('is-recovered', online);

    if (!online) {
      connectionStatus.textContent = 'Offline';
      connectionStatus.title = 'Network connection is unavailable. Some actions may fail until connectivity returns.';
      connectionStatus.classList.remove('hidden');
      return;
    }

    connectionStatus.textContent = 'Back online';
    connectionStatus.title = 'Network connection restored.';
    if (initial || !isAppVisible()) {
      connectionStatus.classList.add('hidden');
      return;
    }

    connectionStatus.classList.remove('hidden');
    onlineHideTimer = setTimeout(() => connectionStatus.classList.add('hidden'), 2400);
  }

  function clearFormBusy(form) {
    if (!form || form.dataset.euasSubmitting !== 'true') return;
    form.dataset.euasSubmitting = 'false';
    form.removeAttribute('aria-busy');
    form.querySelectorAll('[data-euas-submit-busy="true"]').forEach(button => {
      button.removeAttribute('aria-busy');
      button.dataset.euasSubmitBusy = 'false';
      if (button.dataset.euasWasDisabled !== 'true') button.disabled = false;
      delete button.dataset.euasWasDisabled;
    });
  }

  function clearSubmittingForms() {
    document.querySelectorAll('form[data-euas-submitting="true"]').forEach(clearFormBusy);
  }

  function scheduleNetworkIdle() {
    clearTimeout(networkIdleTimer);
    networkIdleTimer = setTimeout(() => {
      if (activeRequests === 0) clearSubmittingForms();
    }, 220);
  }

  function requestStarted() {
    activeRequests += 1;
    clearTimeout(networkIdleTimer);
    activity.classList.add('is-active');
    document.body.classList.add('network-busy');
  }

  function requestFinished() {
    activeRequests = Math.max(0, activeRequests - 1);
    if (activeRequests !== 0) return;
    activity.classList.remove('is-active');
    document.body.classList.remove('network-busy');
    scheduleNetworkIdle();
  }

  function installFetchTracking() {
    const originalFetch = window.fetch;
    if (!originalFetch || originalFetch.__euasOperationalWrapped) return;

    const trackedFetch = async (...args) => {
      requestStarted();
      try {
        return await originalFetch.apply(window, args);
      } finally {
        requestFinished();
      }
    };
    Object.defineProperty(trackedFetch, '__euasOperationalWrapped', {value: true});
    window.fetch = trackedFetch;
  }

  function markFormBusy(form) {
    if (!form || form.dataset.euasSubmitting === 'true') return false;
    form.dataset.euasSubmitting = 'true';
    form.setAttribute('aria-busy', 'true');
    form.querySelectorAll('button[type="submit"], button:not([type])').forEach(button => {
      button.dataset.euasWasDisabled = String(button.disabled);
      button.dataset.euasSubmitBusy = 'true';
      button.setAttribute('aria-busy', 'true');
      button.disabled = true;
    });
    setTimeout(() => clearFormBusy(form), 15000);
    return true;
  }

  function rootStateMessage(node) {
    return (node?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function decorateContentState() {
    if (!content || content.children.length !== 1) return;
    const state = content.firstElementChild;
    if (!state?.classList.contains('empty') || state.dataset.operationalState === 'true') return;

    const message = rootStateMessage(state);
    if (!message || /^Loading(?:…|\.\.\.)?$/i.test(message)) return;

    const isError = errorPattern.test(message);
    state.dataset.operationalState = 'true';
    state.classList.add(isError ? 'operational-error-state' : 'operational-empty-state');
    state.setAttribute('role', isError ? 'alert' : 'status');
    state.setAttribute('aria-live', isError ? 'assertive' : 'polite');

    const icon = document.createElement('span');
    icon.className = 'operational-state-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = isError ? '!' : '—';

    const body = document.createElement('span');
    body.className = 'operational-state-copy';
    const title = document.createElement('strong');
    title.textContent = message;
    const detail = document.createElement('small');
    detail.textContent = isError
      ? 'The view could not be loaded. Check connectivity and try again.'
      : 'No items are available in the current view.';
    body.append(title, detail);

    state.replaceChildren(icon, body);

    if (isError) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'btn small operational-retry';
      retry.textContent = 'Try again';
      retry.addEventListener('click', () => document.querySelector('.nav-btn.active')?.click());
      state.appendChild(retry);
    }
  }

  function classifyToast() {
    if (!toast) return;
    const message = rootStateMessage(toast);
    if (!message) return;
    const isError = errorPattern.test(message);
    const isSuccess = !isError && successPattern.test(message);
    toast.classList.toggle('toast-error', isError);
    toast.classList.toggle('toast-success', isSuccess);
    toast.classList.toggle('toast-neutral', !isError && !isSuccess);
    toast.setAttribute('role', isError ? 'alert' : 'status');
    toast.setAttribute('aria-live', isError ? 'assertive' : 'polite');
    if (isError) clearSubmittingForms();
  }

  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    if (form.dataset.euasSubmitting === 'true') {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }

    if (!form.checkValidity()) return;
    markFormBusy(form);
  }, true);

  window.addEventListener('offline', () => syncConnection(false));
  window.addEventListener('online', () => syncConnection(false));

  if (content) {
    new MutationObserver(() => requestAnimationFrame(decorateContentState))
      .observe(content, {childList: true, subtree: false});
  }

  if (toast) {
    new MutationObserver(classifyToast)
      .observe(toast, {childList: true, characterData: true, subtree: true});
  }

  if (loginError) {
    new MutationObserver(() => {
      if (rootStateMessage(loginError)) clearFormBusy(document.querySelector('#login-form'));
    }).observe(loginError, {childList: true, characterData: true, subtree: true});
  }

  if (modalLayer) {
    new MutationObserver(() => {
      if (modalLayer.classList.contains('hidden')) {
        modalLayer.querySelectorAll('form[data-euas-submitting="true"]').forEach(clearFormBusy);
      }
    }).observe(modalLayer, {attributes: true, attributeFilter: ['class']});
  }

  installFetchTracking();
  syncConnection(true);
  decorateContentState();
  classifyToast();
})();
