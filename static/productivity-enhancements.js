(() => {
  'use strict';

  const content = document.querySelector('#content');
  const globalSearch = document.querySelector('#global-search');
  const nav = document.querySelector('#nav');
  const sidebar = document.querySelector('#sidebar');
  const tableSelector = '.table-wrap .data-table';
  const formSelector = '.modal form, #content form';
  const moduleSections = [
    ['Overview', ['home', 'dashboard']],
    ['Assets & Maintenance', ['assets', 'work', 'maintenance', 'inspections']],
    ['Resources', ['workforce', 'inventory', 'procurement', 'approvals', 'contracts', 'vendors']],
    ['Operations', ['operations', 'telemetry', 'map', 'field', 'dispatch', 'hse', 'projects']],
    ['Governance', ['documents', 'analytics', 'automation', 'administration']]
  ];

  function visibleText(node) {
    return (node?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function decorateTable(table, index = 0) {
    if (!table || table.dataset.productivityReady === 'true') return;
    table.dataset.productivityReady = 'true';

    const wrap = table.closest('.table-wrap');
    const headers = [...table.querySelectorAll('thead th')].map(visibleText);
    const rows = [...table.querySelectorAll('tbody tr')];

    if (wrap) {
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', table.getAttribute('aria-label') || `Data table ${index + 1}`);
      wrap.classList.toggle('is-empty-table', rows.length === 1 && !!rows[0].querySelector('.empty'));
    }

    table.setAttribute('aria-rowcount', String(rows.length + 1));
    [...table.querySelectorAll('thead th')].forEach(th => th.setAttribute('scope', 'col'));

    rows.forEach(row => {
      [...row.children].forEach((cell, cellIndex) => {
        if (cell.tagName !== 'TD') return;
        const label = headers[cellIndex] || `Column ${cellIndex + 1}`;
        cell.dataset.label = label;
      });
    });

    if (wrap) updateOverflowState(wrap);
  }

  function updateOverflowState(wrap) {
    if (!wrap) return;
    const hasHorizontalOverflow = wrap.scrollWidth - wrap.clientWidth > 4;
    wrap.classList.toggle('has-horizontal-overflow', hasHorizontalOverflow);
    wrap.classList.toggle('at-scroll-end', !hasHorizontalOverflow || wrap.scrollLeft + wrap.clientWidth >= wrap.scrollWidth - 4);
  }

  function decorateTables(root = document) {
    [...root.querySelectorAll(tableSelector)].forEach(decorateTable);
  }

  function decorateField(control) {
    if (!control || control.dataset.productivityReady === 'true') return;
    control.dataset.productivityReady = 'true';

    if (control.required) {
      control.setAttribute('aria-required', 'true');
      const label = control.closest('label');
      if (label) label.classList.add('required-field');
    }

    const clearInvalid = () => {
      control.classList.remove('field-invalid');
      control.removeAttribute('aria-invalid');
      control.closest('.field')?.classList.remove('field-has-error');
    };
    control.addEventListener('input', clearInvalid);
    control.addEventListener('change', clearInvalid);
    control.addEventListener('invalid', () => {
      control.classList.add('field-invalid');
      control.setAttribute('aria-invalid', 'true');
      control.closest('.field')?.classList.add('field-has-error');
    });
  }

  function decorateForm(form) {
    if (!form || form.dataset.productivityReady === 'true') return;
    form.dataset.productivityReady = 'true';
    [...form.querySelectorAll('input, select, textarea')].forEach(decorateField);

    form.addEventListener('submit', () => {
      requestAnimationFrame(() => {
        const invalid = form.querySelector(':invalid');
        if (invalid) {
          invalid.classList.add('field-invalid');
          invalid.setAttribute('aria-invalid', 'true');
          invalid.focus({preventScroll: true});
          invalid.scrollIntoView({block: 'center', behavior: 'smooth'});
        }
      });
    });
  }

  function decorateForms(root = document) {
    [...root.querySelectorAll(formSelector)].forEach(decorateForm);
  }

  function decorateToolbars(root = document) {
    [...root.querySelectorAll('.toolbar')].forEach(toolbar => {
      if (toolbar.dataset.productivityReady === 'true') return;
      toolbar.dataset.productivityReady = 'true';
      toolbar.setAttribute('role', 'search');
      toolbar.setAttribute('aria-label', 'Table filters and actions');
      [...toolbar.querySelectorAll('input, select')].forEach(control => {
        if (!control.getAttribute('aria-label')) {
          control.setAttribute('aria-label', control.placeholder || control.title || 'Filter');
        }
      });
    });
  }

  function ensureModuleFinder() {
    if (!sidebar || !nav) return null;
    let finder = sidebar.querySelector('.module-finder');
    if (finder) return finder;

    finder = document.createElement('div');
    finder.className = 'module-finder';
    finder.innerHTML = '<label class="sr-only" for="module-filter">Find module</label><input id="module-filter" type="search" autocomplete="off" placeholder="Find module…" aria-label="Find module" aria-controls="nav"><span class="module-filter-shortcut" aria-hidden="true">Alt M</span><div class="module-filter-empty hidden" role="status" aria-live="polite"></div>';
    nav.before(finder);

    const input = finder.querySelector('#module-filter');
    input.addEventListener('input', () => applyModuleFilter(input.value));
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        input.value = '';
        applyModuleFilter('');
        input.blur();
      } else if (event.key === 'Enter') {
        const first = [...nav.querySelectorAll('.nav-btn:not([hidden])')][0];
        if (first) {
          event.preventDefault();
          first.click();
        }
      }
    });
    return finder;
  }

  function sectionFor(view) {
    return moduleSections.find(([, views]) => views.includes(view))?.[0] || 'Other';
  }

  function decorateNavigation() {
    if (!nav) return;
    const buttons = [...nav.querySelectorAll('.nav-btn')];
    if (!buttons.length) return;
    const signature = buttons.map(button => button.dataset.view || '').join('|');
    if (nav.dataset.productivityNavSignature === signature && nav.querySelector('.nav-section-label')) return;
    nav.dataset.productivityNavSignature = signature;

    nav.querySelectorAll('.nav-section-label').forEach(label => label.remove());
    let previousSection = '';
    buttons.forEach(button => {
      const section = sectionFor(button.dataset.view);
      if (section !== previousSection) {
        const label = document.createElement('div');
        label.className = 'nav-section-label';
        label.dataset.section = section;
        label.textContent = section;
        button.before(label);
        previousSection = section;
      }
      button.title ||= visibleText(button);
    });
    ensureModuleFinder();
    const query = sidebar?.querySelector('#module-filter')?.value || '';
    applyModuleFilter(query);
  }

  function applyModuleFilter(value) {
    if (!nav) return;
    const query = value.trim().toLowerCase();
    const buttons = [...nav.querySelectorAll('.nav-btn')];
    let matches = 0;
    buttons.forEach(button => {
      const haystack = `${visibleText(button)} ${button.dataset.view || ''}`.toLowerCase();
      const visible = !query || haystack.includes(query);
      button.hidden = !visible;
      if (visible) matches += 1;
    });

    nav.querySelectorAll('.nav-section-label').forEach(label => {
      const section = label.dataset.section;
      const hasVisible = buttons.some(button => sectionFor(button.dataset.view) === section && !button.hidden);
      label.hidden = !hasVisible;
    });

    const empty = sidebar?.querySelector('.module-filter-empty');
    if (empty) {
      empty.textContent = query && !matches ? 'No matching modules' : '';
      empty.classList.toggle('hidden', !query || matches > 0);
    }
  }

  function clearModuleFilter() {
    const input = sidebar?.querySelector('#module-filter');
    if (!input || !input.value) return;
    input.value = '';
    applyModuleFilter('');
  }

  function decorateContent() {
    decorateTables(content || document);
    decorateForms(content || document);
    decorateToolbars(content || document);
  }

  document.addEventListener('scroll', event => {
    const wrap = event.target.closest?.('.table-wrap');
    if (wrap) updateOverflowState(wrap);
  }, true);

  window.addEventListener('resize', () => {
    document.querySelectorAll('.table-wrap').forEach(updateOverflowState);
  });

  document.addEventListener('keydown', event => {
    const searchShortcut = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k';
    if (searchShortcut && globalSearch && !document.querySelector('#app')?.classList.contains('hidden')) {
      event.preventDefault();
      globalSearch.focus();
      globalSearch.select();
      return;
    }

    if (event.altKey && event.key.toLowerCase() === 'm' && !document.querySelector('#app')?.classList.contains('hidden')) {
      const input = ensureModuleFinder()?.querySelector('#module-filter');
      if (input) {
        event.preventDefault();
        input.focus();
        input.select();
      }
    }
  });

  nav?.addEventListener('click', event => {
    if (event.target.closest('.nav-btn')) clearModuleFilter();
  });

  const observer = new MutationObserver(() => decorateContent());
  if (content) observer.observe(content, {childList: true, subtree: true});
  const modalBody = document.querySelector('#modal-body');
  if (modalBody) observer.observe(modalBody, {childList: true, subtree: true});

  if (nav) new MutationObserver(() => requestAnimationFrame(decorateNavigation)).observe(nav, {childList: true});

  decorateContent();
  decorateNavigation();
})();
