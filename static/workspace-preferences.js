(() => {
  'use strict';

  const app = document.querySelector('#app');
  const sidebar = document.querySelector('#sidebar');
  const sideBrand = sidebar?.querySelector('.side-brand');
  const topActions = document.querySelector('.top-actions');
  const mobileQuery = window.matchMedia('(max-width: 820px)');
  const DENSITY_KEY = 'euas_ui_density';
  const SIDEBAR_KEY = 'euas_sidebar_collapsed';

  function readPreference(key, fallback = '') {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  }

  function writePreference(key, value) {
    try { localStorage.setItem(key, value); } catch {}
  }

  function announce(message) {
    const toast = document.querySelector('#toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show', 'toast-neutral');
    window.setTimeout(() => toast.classList.remove('show'), 1800);
  }

  function ensureDensityToggle() {
    if (!topActions) return null;
    let button = document.querySelector('#density-toggle');
    if (button) return button;
    button = document.createElement('button');
    button.id = 'density-toggle';
    button.type = 'button';
    button.className = 'icon-btn workspace-pref-btn';
    button.textContent = '↕';
    button.dataset.preference = 'density';
    const help = document.querySelector('#help-btn');
    if (help?.parentElement === topActions) topActions.insertBefore(button, help);
    else topActions.appendChild(button);
    button.addEventListener('click', () => setDensity(document.body.classList.contains('density-compact') ? 'comfortable' : 'compact', true));
    return button;
  }

  function setDensity(value, userInitiated = false) {
    const compact = value === 'compact';
    document.body.classList.toggle('density-compact', compact);
    document.body.dataset.density = compact ? 'compact' : 'comfortable';
    const button = ensureDensityToggle();
    if (button) {
      const label = compact ? 'Compact' : 'Comfortable';
      button.setAttribute('aria-label', `Display density: ${label}. Activate to switch.`);
      button.title = `Display density: ${label}`;
      button.setAttribute('aria-pressed', String(compact));
    }
    writePreference(DENSITY_KEY, compact ? 'compact' : 'comfortable');
    if (userInitiated) announce(`Display density: ${compact ? 'Compact' : 'Comfortable'}`);
  }

  function ensureSidebarToggle() {
    if (!sideBrand) return null;
    let button = sideBrand.querySelector('#sidebar-collapse');
    if (button) return button;
    button = document.createElement('button');
    button.id = 'sidebar-collapse';
    button.type = 'button';
    button.className = 'icon-btn sidebar-collapse-btn';
    button.dataset.preference = 'sidebar';
    sideBrand.appendChild(button);
    button.addEventListener('click', () => setSidebarCollapsed(!sidebar?.classList.contains('workspace-collapsed'), true));
    return button;
  }

  function setSidebarCollapsed(collapsed, userInitiated = false) {
    if (!sidebar || !app) return;
    const effective = collapsed && !mobileQuery.matches;
    sidebar.classList.toggle('workspace-collapsed', effective);
    app.classList.toggle('workspace-sidebar-collapsed', effective);
    const button = ensureSidebarToggle();
    if (button) {
      button.textContent = effective ? '›' : '‹';
      button.setAttribute('aria-label', effective ? 'Expand navigation sidebar' : 'Collapse navigation sidebar');
      button.title = effective ? 'Expand sidebar' : 'Collapse sidebar';
      button.setAttribute('aria-expanded', String(!effective));
    }
    if (!mobileQuery.matches) writePreference(SIDEBAR_KEY, effective ? 'true' : 'false');
    if (userInitiated) announce(effective ? 'Sidebar collapsed' : 'Sidebar expanded');
  }

  function restorePreferences() {
    const density = readPreference(DENSITY_KEY, 'comfortable');
    setDensity(density === 'compact' ? 'compact' : 'comfortable');
    const collapsed = readPreference(SIDEBAR_KEY, 'false') === 'true';
    setSidebarCollapsed(collapsed);
  }

  function syncResponsiveSidebar() {
    if (mobileQuery.matches) {
      sidebar?.classList.remove('workspace-collapsed');
      app?.classList.remove('workspace-sidebar-collapsed');
      const button = ensureSidebarToggle();
      if (button) button.hidden = true;
    } else {
      const button = ensureSidebarToggle();
      if (button) button.hidden = false;
      setSidebarCollapsed(readPreference(SIDEBAR_KEY, 'false') === 'true');
    }
  }

  document.addEventListener('keydown', event => {
    if (!app || app.classList.contains('hidden')) return;
    if (event.altKey && event.shiftKey && event.key.toLowerCase() === 'd') {
      event.preventDefault();
      ensureDensityToggle()?.click();
    }
  });

  if (mobileQuery.addEventListener) mobileQuery.addEventListener('change', syncResponsiveSidebar);
  else mobileQuery.addListener(syncResponsiveSidebar);

  ensureDensityToggle();
  ensureSidebarToggle();
  restorePreferences();
  syncResponsiveSidebar();
})();
