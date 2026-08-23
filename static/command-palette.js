(() => {
  'use strict';

  const app = document.querySelector('#app');
  const nav = document.querySelector('#nav');
  const topActions = document.querySelector('.top-actions');
  const globalSearch = document.querySelector('#global-search');
  const RECENTS_KEY = 'euas_recent_modules';
  const MAX_RECENTS = 6;

  let lastFocused = null;
  let activeIndex = 0;
  let currentItems = [];

  function readRecents() {
    try {
      const parsed = JSON.parse(localStorage.getItem(RECENTS_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.filter(Boolean).slice(0, MAX_RECENTS) : [];
    } catch {
      return [];
    }
  }

  function writeRecents(items) {
    try { localStorage.setItem(RECENTS_KEY, JSON.stringify(items.slice(0, MAX_RECENTS))); } catch {}
  }

  function rememberModule(view) {
    if (!view) return;
    writeRecents([view, ...readRecents().filter(item => item !== view)]);
  }

  function moduleLabel(button) {
    const label = button?.querySelector('span:last-child')?.textContent || button?.textContent || '';
    return label.replace(/\s+/g, ' ').trim();
  }

  function moduleItems() {
    if (!nav) return [];
    return [...nav.querySelectorAll('.nav-btn')].map(button => ({
      id: `module:${button.dataset.view}`,
      type: 'module',
      view: button.dataset.view || '',
      label: moduleLabel(button),
      subtitle: 'Open module',
      keywords: `${button.dataset.view || ''} ${moduleLabel(button)}`.toLowerCase(),
      run: () => button.click()
    }));
  }

  function actionItems() {
    const density = document.querySelector('#density-toggle');
    const sidebarToggle = document.querySelector('#sidebar-collapse');
    const sidebar = document.querySelector('#sidebar');
    return [
      {
        id: 'action:search', type: 'action', label: 'Global search',
        subtitle: 'Search assets, work orders, documents and inspections',
        keywords: 'search find asset work order document inspection',
        run: () => { globalSearch?.focus(); globalSearch?.select(); }
      },
      {
        id: 'action:notifications', type: 'action', label: 'Notifications',
        subtitle: 'Open notification centre', keywords: 'notifications alerts messages inbox',
        run: () => document.querySelector('#notification-btn')?.click()
      },
      {
        id: 'action:help', type: 'action', label: 'Help',
        subtitle: 'Open EUAS help', keywords: 'help support documentation shortcuts',
        run: () => document.querySelector('#help-btn')?.click()
      },
      {
        id: 'action:profile', type: 'action', label: 'Profile',
        subtitle: 'Open your EUAS profile', keywords: 'profile account sessions user',
        run: () => document.querySelector('#profile-btn')?.click()
      },
      ...(density ? [{
        id: 'action:density', type: 'action',
        label: document.body.classList.contains('density-compact') ? 'Use comfortable density' : 'Use compact density',
        subtitle: 'Change table and workspace spacing', keywords: 'density compact comfortable spacing tables',
        run: () => density.click()
      }] : []),
      ...(sidebarToggle && !sidebarToggle.hidden ? [{
        id: 'action:sidebar', type: 'action',
        label: sidebar?.classList.contains('workspace-collapsed') ? 'Expand sidebar' : 'Collapse sidebar',
        subtitle: 'Change desktop navigation width', keywords: 'sidebar navigation expand collapse',
        run: () => sidebarToggle.click()
      }] : [])
    ];
  }

  function recentItems(modules) {
    const byView = new Map(modules.map(item => [item.view, item]));
    return readRecents().map(view => byView.get(view)).filter(Boolean)
      .map(item => ({...item, type: 'recent', subtitle: 'Recently opened module'}));
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  }

  function ensurePalette() {
    let layer = document.querySelector('#command-palette-layer');
    if (layer) return layer;

    layer = document.createElement('div');
    layer.id = 'command-palette-layer';
    layer.className = 'command-palette-layer hidden';
    layer.innerHTML = `
      <section class="command-palette" role="dialog" aria-modal="true" aria-labelledby="command-palette-title">
        <header class="command-palette-head">
          <div><div class="eyebrow dark">EUAS QUICK SWITCHER</div><h2 id="command-palette-title">Command Palette</h2></div>
          <button type="button" class="icon-btn" id="command-palette-close" aria-label="Close command palette">×</button>
        </header>
        <label class="command-palette-search-wrap">
          <span class="sr-only">Search commands and modules</span>
          <input id="command-palette-search" type="search" autocomplete="off" placeholder="Type a module or action…" aria-controls="command-palette-results" aria-autocomplete="list">
          <span class="command-palette-shortcut" aria-hidden="true">Ctrl Shift P</span>
        </label>
        <div id="command-palette-results" class="command-palette-results" role="listbox" aria-label="Commands and modules"></div>
        <footer class="command-palette-foot" aria-hidden="true"><span>↑↓ Navigate</span><span>Enter Open</span><span>Esc Close</span></footer>
      </section>`;
    document.body.appendChild(layer);

    layer.addEventListener('mousedown', event => { if (event.target === layer) closePalette(); });
    layer.querySelector('#command-palette-close')?.addEventListener('click', closePalette);
    layer.querySelector('#command-palette-search')?.addEventListener('input', () => { activeIndex = 0; renderPalette(); });
    return layer;
  }

  function ensureToggle() {
    if (!topActions) return null;
    let button = document.querySelector('#command-palette-toggle');
    if (button) return button;
    button = document.createElement('button');
    button.id = 'command-palette-toggle';
    button.type = 'button';
    button.className = 'icon-btn command-palette-toggle';
    button.textContent = '⌘';
    button.setAttribute('aria-label', 'Open command palette');
    button.setAttribute('aria-keyshortcuts', 'Control+Shift+P Meta+Shift+P');
    button.title = 'Command palette (Ctrl/Cmd+Shift+P)';
    const density = document.querySelector('#density-toggle');
    if (density?.parentElement === topActions) topActions.insertBefore(button, density);
    else {
      const help = document.querySelector('#help-btn');
      if (help?.parentElement === topActions) topActions.insertBefore(button, help);
      else topActions.appendChild(button);
    }
    button.addEventListener('click', openPalette);
    return button;
  }

  function score(item, query) {
    if (!query) return 1;
    const label = item.label.toLowerCase();
    const haystack = `${label} ${item.keywords || ''}`;
    if (label === query) return 100;
    if (label.startsWith(query)) return 80;
    if (label.includes(query)) return 60;
    if (haystack.includes(query)) return 35;
    const tokens = query.split(/\s+/).filter(Boolean);
    return tokens.every(token => haystack.includes(token)) ? 20 : 0;
  }

  function renderPalette() {
    const layer = ensurePalette();
    const input = layer.querySelector('#command-palette-search');
    const results = layer.querySelector('#command-palette-results');
    const query = (input?.value || '').trim().toLowerCase();
    const modules = moduleItems();
    const actions = actionItems();
    const recents = recentItems(modules);
    const recentViews = new Set(recents.map(item => item.view));
    const nonRecentModules = modules.filter(item => !recentViews.has(item.view));
    const ranked = [...actions, ...modules]
      .map(item => ({item, score: score(item, query)}))
      .filter(entry => entry.score > 0)
      .sort((a, b) => b.score - a.score || a.item.label.localeCompare(b.item.label))
      .map(entry => entry.item);

    currentItems = query ? ranked.slice(0, 18) : [...recents, ...actions, ...nonRecentModules].slice(0, 18);
    activeIndex = Math.min(activeIndex, Math.max(0, currentItems.length - 1));

    if (!currentItems.length) {
      results.innerHTML = '<div class="command-palette-empty" role="status">No matching modules or actions</div>';
      input?.removeAttribute('aria-activedescendant');
      return;
    }

    results.innerHTML = currentItems.map((item, index) => `
      <button type="button" class="command-palette-item ${index === activeIndex ? 'is-active' : ''}" role="option" aria-selected="${index === activeIndex}" id="command-palette-option-${index}" data-index="${index}">
        <span class="command-palette-item-icon" aria-hidden="true">${item.type === 'action' ? '⌁' : item.type === 'recent' ? '↺' : '→'}</span>
        <span class="command-palette-item-copy"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.subtitle || '')}</small></span>
        <span class="command-palette-kind">${item.type === 'action' ? 'Action' : item.type === 'recent' ? 'Recent' : 'Module'}</span>
      </button>`).join('');

    [...results.querySelectorAll('.command-palette-item')].forEach(button => {
      button.addEventListener('mouseenter', () => setActive(Number(button.dataset.index)));
      button.addEventListener('click', () => runItem(Number(button.dataset.index)));
    });
    syncActiveDescendant();
  }

  function setActive(index) {
    if (!currentItems.length) return;
    activeIndex = Math.max(0, Math.min(index, currentItems.length - 1));
    const layer = ensurePalette();
    [...layer.querySelectorAll('.command-palette-item')].forEach((button, buttonIndex) => {
      const active = buttonIndex === activeIndex;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
      if (active) button.scrollIntoView({block: 'nearest'});
    });
    syncActiveDescendant();
  }

  function syncActiveDescendant() {
    const input = ensurePalette().querySelector('#command-palette-search');
    if (!input) return;
    if (!currentItems.length) input.removeAttribute('aria-activedescendant');
    else input.setAttribute('aria-activedescendant', `command-palette-option-${activeIndex}`);
  }

  function runItem(index) {
    const item = currentItems[index];
    if (!item) return;
    closePalette(false);
    window.requestAnimationFrame(() => item.run());
  }

  function trapFocus(event) {
    const palette = ensurePalette().querySelector('.command-palette');
    const focusable = [...palette.querySelectorAll('button:not([disabled]), input:not([disabled])')];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  }

  function handlePaletteKeys(event) {
    if (event.key === 'ArrowDown') {
      event.preventDefault(); setActive((activeIndex + 1) % Math.max(1, currentItems.length));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); setActive((activeIndex - 1 + Math.max(1, currentItems.length)) % Math.max(1, currentItems.length));
    } else if (event.key === 'Home') {
      event.preventDefault(); setActive(0);
    } else if (event.key === 'End') {
      event.preventDefault(); setActive(currentItems.length - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault(); runItem(activeIndex);
    } else if (event.key === 'Escape') {
      event.preventDefault(); closePalette();
    } else if (event.key === 'Tab') {
      trapFocus(event);
    }
  }

  function openPalette() {
    if (!app || app.classList.contains('hidden')) return;
    const layer = ensurePalette();
    lastFocused = document.activeElement;
    activeIndex = 0;
    layer.classList.remove('hidden');
    document.body.classList.add('command-palette-open');
    const input = layer.querySelector('#command-palette-search');
    if (input) input.value = '';
    renderPalette();
    window.requestAnimationFrame(() => input?.focus());
  }

  function closePalette(restoreFocus = true) {
    const layer = ensurePalette();
    layer.classList.add('hidden');
    document.body.classList.remove('command-palette-open');
    if (restoreFocus && lastFocused instanceof HTMLElement) lastFocused.focus({preventScroll: true});
    lastFocused = null;
  }

  document.addEventListener('keydown', event => {
    const layer = ensurePalette();
    const shortcut = (event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'p';
    if (shortcut) {
      event.preventDefault();
      if (layer.classList.contains('hidden')) openPalette(); else closePalette();
      return;
    }
    if (!layer.classList.contains('hidden')) handlePaletteKeys(event);
  });

  nav?.addEventListener('click', event => {
    const button = event.target.closest('.nav-btn');
    if (button?.dataset.view) rememberModule(button.dataset.view);
  });

  if (nav) new MutationObserver(() => ensureToggle()).observe(nav, {childList: true});
  ensurePalette();
  ensureToggle();
})();
