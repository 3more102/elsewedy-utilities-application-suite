(() => {
  'use strict';

  const app = document.querySelector('#app');
  const topActions = document.querySelector('.top-actions');
  const helpButton = document.querySelector('#help-btn');

  let lastFocused = null;

  const SHORTCUTS = [
    ['Global search', 'Ctrl/⌘ + K', 'Focus the global asset, work order and document search.'],
    ['Find module', 'Alt + M', 'Focus the module finder in the primary sidebar.'],
    ['Command Palette', 'Ctrl/⌘ + Shift + P', 'Search and run module or workspace actions.'],
    ['Keyboard shortcuts', '?', 'Open this shortcut reference.'],
    ['Navigate results', '↑ / ↓', 'Move through global-search or command-palette results.'],
    ['Open selected result', 'Enter', 'Activate the highlighted search or palette result.'],
    ['Close transient UI', 'Esc', 'Close dialogs, drawers, menus and the command palette.'],
    ['Module history', 'Browser Back / Forward', 'Move through shareable module deep links.']
  ];

  function appVisible() {
    return app && !app.classList.contains('hidden');
  }

  function isEditable(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]'));
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[char]));
  }

  function shortcutRows() {
    return SHORTCUTS.map(([label, keys, description]) => `
      <li class="shortcut-center-item">
        <span class="shortcut-center-copy"><strong>${escapeHtml(label)}</strong><small>${escapeHtml(description)}</small></span>
        <kbd>${escapeHtml(keys)}</kbd>
      </li>`).join('');
  }

  function ensureCenter() {
    let layer = document.querySelector('#shortcut-center-layer');
    if (layer) return layer;

    layer = document.createElement('div');
    layer.id = 'shortcut-center-layer';
    layer.className = 'shortcut-center-layer hidden';
    layer.innerHTML = `
      <section class="shortcut-center" role="dialog" aria-modal="true" aria-labelledby="shortcut-center-title" aria-describedby="shortcut-center-description">
        <header class="shortcut-center-head">
          <div>
            <div class="eyebrow dark">EUAS PRODUCTIVITY</div>
            <h2 id="shortcut-center-title">Keyboard Shortcuts</h2>
            <p id="shortcut-center-description">Use these shortcuts to move through EUAS without leaving the keyboard.</p>
          </div>
          <button type="button" class="icon-btn" id="shortcut-center-close" aria-label="Close keyboard shortcuts">×</button>
        </header>
        <ul class="shortcut-center-list" aria-label="Available keyboard shortcuts">${shortcutRows()}</ul>
        <footer class="shortcut-center-foot">
          <span>Tip: module URLs now preserve <code>#module=…</code> for sharing and browser history.</span>
        </footer>
      </section>`;
    document.body.appendChild(layer);

    layer.addEventListener('mousedown', event => {
      if (event.target === layer) closeCenter();
    });
    layer.querySelector('#shortcut-center-close')?.addEventListener('click', closeCenter);
    return layer;
  }

  function ensureToggle() {
    if (!topActions) return null;
    let button = document.querySelector('#shortcut-center-toggle');
    if (button) return button;

    button = document.createElement('button');
    button.id = 'shortcut-center-toggle';
    button.type = 'button';
    button.className = 'icon-btn shortcut-center-toggle';
    button.textContent = '⌨';
    button.title = 'Keyboard shortcuts (?)';
    button.setAttribute('aria-label', 'Open keyboard shortcuts');
    button.setAttribute('aria-keyshortcuts', '?');

    if (helpButton?.parentElement === topActions) topActions.insertBefore(button, helpButton);
    else topActions.appendChild(button);
    button.addEventListener('click', openCenter);
    return button;
  }

  function focusableElements() {
    const dialog = ensureCenter().querySelector('.shortcut-center');
    return [...dialog.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')];
  }

  function trapFocus(event) {
    const focusable = focusableElements();
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openCenter() {
    if (!appVisible()) return;
    const layer = ensureCenter();
    if (!layer.classList.contains('hidden')) return;
    lastFocused = document.activeElement;
    layer.classList.remove('hidden');
    document.body.classList.add('shortcut-center-open');
    ensureToggle()?.setAttribute('aria-expanded', 'true');
    window.requestAnimationFrame(() => layer.querySelector('#shortcut-center-close')?.focus());
  }

  function closeCenter(restoreFocus = true) {
    const layer = ensureCenter();
    if (layer.classList.contains('hidden')) return;
    layer.classList.add('hidden');
    document.body.classList.remove('shortcut-center-open');
    ensureToggle()?.setAttribute('aria-expanded', 'false');
    if (restoreFocus && lastFocused instanceof HTMLElement) lastFocused.focus({preventScroll: true});
    lastFocused = null;
  }

  document.addEventListener('keydown', event => {
    const layer = ensureCenter();
    const open = !layer.classList.contains('hidden');

    if (open) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeCenter();
      } else if (event.key === 'Tab') {
        trapFocus(event);
      }
      return;
    }

    if (event.key === '?' && !event.ctrlKey && !event.metaKey && !event.altKey && !isEditable(event.target)) {
      event.preventDefault();
      openCenter();
    }
  });

  helpButton?.setAttribute('aria-describedby', 'shortcut-center-hint');
  if (helpButton && !document.querySelector('#shortcut-center-hint')) {
    const hint = document.createElement('span');
    hint.id = 'shortcut-center-hint';
    hint.className = 'sr-only';
    hint.textContent = 'Press question mark for keyboard shortcuts.';
    helpButton.insertAdjacentElement('afterend', hint);
  }

  ensureCenter();
  const toggle = ensureToggle();
  toggle?.setAttribute('aria-expanded', 'false');
  toggle?.setAttribute('aria-controls', 'shortcut-center-layer');
})();
