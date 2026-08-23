(() => {
  'use strict';

  const content = document.querySelector('#content');
  const globalSearch = document.querySelector('#global-search');
  const tableSelector = '.table-wrap .data-table';
  const formSelector = '.modal form, #content form';

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
    const shortcut = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k';
    if (!shortcut || !globalSearch || document.querySelector('#app')?.classList.contains('hidden')) return;
    event.preventDefault();
    globalSearch.focus();
    globalSearch.select();
  });

  const observer = new MutationObserver(() => decorateContent());
  if (content) observer.observe(content, {childList: true, subtree: true});
  const modalBody = document.querySelector('#modal-body');
  if (modalBody) observer.observe(modalBody, {childList: true, subtree: true});

  decorateContent();
})();
