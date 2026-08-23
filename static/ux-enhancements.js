(() => {
  'use strict';

  const $ = selector => document.querySelector(selector);
  const focusableSelector = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  const sidebar = $('.sidebar');
  const mobileMenu = $('#mobile-menu');
  const modalLayer = $('#modal-layer');
  const drawer = $('#drawer');
  const searchResults = $('#search-results');
  const globalSearch = $('#global-search');
  const content = $('#content');

  const scrim = document.createElement('div');
  scrim.className = 'mobile-scrim';
  scrim.setAttribute('aria-hidden', 'true');
  document.body.appendChild(scrim);

  function syncMobileNav() {
    if (!sidebar) return;
    const open = sidebar.classList.contains('open');
    scrim.classList.toggle('show', open);
    document.body.classList.toggle('nav-open', open);
    if (mobileMenu) mobileMenu.setAttribute('aria-expanded', String(open));
  }

  function closeMobileNav() {
    if (!sidebar) return;
    sidebar.classList.remove('open');
    syncMobileNav();
  }

  if (mobileMenu) {
    mobileMenu.setAttribute('aria-expanded', 'false');
    mobileMenu.addEventListener('click', () => queueMicrotask(syncMobileNav));
  }
  scrim.addEventListener('click', closeMobileNav);
  if (sidebar) {
    new MutationObserver(syncMobileNav).observe(sidebar, {attributes: true, attributeFilter: ['class']});
    sidebar.addEventListener('click', event => {
      if (event.target.closest('.nav-btn')) queueMicrotask(syncMobileNav);
    });
  }

  let modalReturnFocus = null;
  let drawerReturnFocus = null;

  function firstFocusable(root) {
    return root ? root.querySelector(focusableSelector) : null;
  }

  function syncModal() {
    if (!modalLayer) return;
    const open = !modalLayer.classList.contains('hidden');
    document.body.classList.toggle('modal-open', open);
    const modal = modalLayer.querySelector('.modal');
    if (modal) modal.tabIndex = -1;
    if (open) {
      if (!modalReturnFocus) modalReturnFocus = document.activeElement;
      requestAnimationFrame(() => (firstFocusable(modal) || modal)?.focus());
    } else if (modalReturnFocus) {
      const target = modalReturnFocus;
      modalReturnFocus = null;
      if (document.contains(target)) requestAnimationFrame(() => target.focus());
    }
  }

  function syncDrawer() {
    if (!drawer) return;
    const open = !drawer.classList.contains('hidden');
    document.body.classList.toggle('drawer-open', open);
    drawer.tabIndex = -1;
    if (open) {
      if (!drawerReturnFocus) drawerReturnFocus = document.activeElement;
      requestAnimationFrame(() => (firstFocusable(drawer) || drawer).focus());
    } else if (drawerReturnFocus) {
      const target = drawerReturnFocus;
      drawerReturnFocus = null;
      if (document.contains(target)) requestAnimationFrame(() => target.focus());
    }
  }

  if (modalLayer) new MutationObserver(syncModal).observe(modalLayer, {attributes: true, attributeFilter: ['class']});
  if (drawer) new MutationObserver(syncDrawer).observe(drawer, {attributes: true, attributeFilter: ['class']});

  function trapTab(event, root) {
    const items = [...root.querySelectorAll(focusableSelector)].filter(el => el.offsetParent !== null);
    if (!items.length) {
      event.preventDefault();
      root.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  let activeSearchIndex = -1;

  function searchItems() {
    return searchResults ? [...searchResults.querySelectorAll('.search-item')] : [];
  }

  function decorateSearchItems() {
    activeSearchIndex = -1;
    if (!searchResults) return;
    const items = searchItems();
    items.forEach((item, index) => {
      item.setAttribute('role', 'option');
      item.id ||= `euas-search-option-${index}`;
      item.classList.remove('keyboard-active');
    });
    if (globalSearch) {
      globalSearch.setAttribute('aria-expanded', String(!searchResults.classList.contains('hidden')));
      globalSearch.removeAttribute('aria-activedescendant');
    }
  }

  function setActiveSearchItem(nextIndex) {
    const items = searchItems();
    if (!items.length) return;
    activeSearchIndex = (nextIndex + items.length) % items.length;
    items.forEach((item, index) => item.classList.toggle('keyboard-active', index === activeSearchIndex));
    const active = items[activeSearchIndex];
    active.scrollIntoView({block: 'nearest'});
    globalSearch?.setAttribute('aria-activedescendant', active.id);
  }

  if (searchResults) {
    new MutationObserver(decorateSearchItems).observe(searchResults, {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
  }
  if (globalSearch) {
    globalSearch.setAttribute('aria-autocomplete', 'list');
    globalSearch.setAttribute('aria-controls', 'search-results');
    globalSearch.addEventListener('input', () => {
      activeSearchIndex = -1;
      globalSearch.removeAttribute('aria-activedescendant');
    });
    globalSearch.addEventListener('keydown', event => {
      if (!searchResults || searchResults.classList.contains('hidden')) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setActiveSearchItem(activeSearchIndex + 1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        setActiveSearchItem(activeSearchIndex - 1);
      } else if (event.key === 'Enter' && activeSearchIndex >= 0) {
        event.preventDefault();
        searchItems()[activeSearchIndex]?.click();
      }
    });
  }

  function decorateLoadingState() {
    if (!content) return;
    const child = content.firstElementChild;
    if (child?.classList.contains('empty') && /^Loading(?:…|\.\.\.)?$/.test(child.textContent.trim())) {
      child.classList.add('loading-state');
      child.setAttribute('role', 'status');
      child.setAttribute('aria-live', 'polite');
      child.setAttribute('aria-label', 'Loading module');
    }
  }
  if (content) new MutationObserver(decorateLoadingState).observe(content, {childList: true});

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      if (modalLayer && !modalLayer.classList.contains('hidden')) {
        event.preventDefault();
        $('#modal-close')?.click();
        return;
      }
      if (drawer && !drawer.classList.contains('hidden')) {
        event.preventDefault();
        $('#drawer-close')?.click();
        return;
      }
      if (searchResults && !searchResults.classList.contains('hidden')) {
        event.preventDefault();
        searchResults.classList.add('hidden');
        globalSearch?.setAttribute('aria-expanded', 'false');
        return;
      }
      if (sidebar?.classList.contains('open')) {
        event.preventDefault();
        closeMobileNav();
      }
    }

    if (event.key === 'Tab') {
      if (modalLayer && !modalLayer.classList.contains('hidden')) {
        const modal = modalLayer.querySelector('.modal');
        if (modal) trapTab(event, modal);
      } else if (drawer && !drawer.classList.contains('hidden')) {
        trapTab(event, drawer);
      }
    }
  });

  syncMobileNav();
  decorateSearchItems();
  decorateLoadingState();
})();
