(() => {
  'use strict';

  const content = document.querySelector('#content');
  const pageTitle = document.querySelector('#page-title');
  const nav = document.querySelector('#nav');
  if (!content) return;

  const ATTENTION_LIMIT = 6;
  const MODULE_BY_SIGNAL = new Map([
    ['Under Maintenance', 'assets'],
    ['Critical Assets', 'assets'],
    ['Open Work Orders', 'work'],
    ['Overdue Work', 'work'],
    ['Emergency Work', 'work'],
    ['PM Compliance', 'maintenance'],
    ['Low Stock Items', 'inventory'],
    ['Pending POs', 'procurement'],
    ['Active Technicians', 'workforce'],
    ['Safety Incidents', 'hse'],
    ['Open Outages', 'operations'],
    ['Active Dispatches', 'dispatch'],
    ['Active Alarms', 'telemetry'],
    ['Utility Performance', 'operations'],
    ['Asset Health Score', 'assets'],
    ['90d Forecast Load', 'maintenance'],
    ['Parts-Blocked Work', 'operations'],
    ['Maintenance Cost', 'analytics'],
  ]);

  function executiveDashboardVisible() {
    return pageTitle?.textContent?.trim() === 'Executive Dashboard'
      && !!content.querySelector('.dashboard-kpi-grid[data-dashboard-intelligence="ready"]');
  }

  function text(node) {
    return node?.textContent?.replace(/\s+/g, ' ').trim() || '';
  }

  function severity(card) {
    if (card.classList.contains('kpi-critical')) return 'critical';
    if (card.classList.contains('kpi-attention')) return 'attention';
    return null;
  }

  function moduleAvailable(module) {
    if (!module || !nav) return false;
    return [...nav.querySelectorAll('.nav-btn')]
      .some(button => button.dataset.view === module);
  }

  function signalFromCard(card, index) {
    const level = severity(card);
    if (!level) return null;
    const label = text(card.querySelector('.kpi-label'));
    return {
      card,
      index,
      level,
      label,
      value: text(card.querySelector('.kpi-value')) || '—',
      hint: text(card.querySelector(':scope > .trend')),
      module: MODULE_BY_SIGNAL.get(label) || null,
      explainable: !!card.querySelector('.dashboard-kpi-action'),
    };
  }

  function orderedSignals(grid) {
    return [...grid.querySelectorAll(':scope > .kpi')]
      .map(signalFromCard)
      .filter(Boolean)
      .sort((a, b) => {
        const severityDifference = (a.level === 'critical' ? 0 : 1) - (b.level === 'critical' ? 0 : 1);
        return severityDifference || a.index - b.index;
      });
  }

  function signatureFor(signals) {
    return signals.map(signal => [
      signal.level,
      signal.label,
      signal.value,
      signal.hint,
      signal.explainable ? 'explainable' : '',
      moduleAvailable(signal.module) ? signal.module : '',
    ].join('|')).join('||');
  }

  function actionButton(label, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
  }

  function openModule(module) {
    if (!module || typeof go !== 'function' || !moduleAvailable(module)) return;
    Promise.resolve(go(module)).catch(() => {});
  }

  function explainSignal(signal) {
    const why = signal.card.querySelector('.dashboard-kpi-action');
    if (why) why.click();
  }

  function attentionRow(signal) {
    const row = document.createElement('article');
    row.className = `dashboard-attention-row ${signal.level}`;

    const state = document.createElement('span');
    state.className = `dashboard-attention-state ${signal.level}`;
    state.textContent = signal.level === 'critical' ? 'Critical' : 'Attention';

    const copy = document.createElement('div');
    copy.className = 'dashboard-attention-copy';
    const heading = document.createElement('strong');
    heading.textContent = signal.label;
    const detail = document.createElement('p');
    detail.textContent = [signal.value, signal.hint].filter(Boolean).join(' · ');
    copy.append(heading, detail);

    const actions = document.createElement('div');
    actions.className = 'dashboard-attention-actions';
    if (signal.explainable) {
      actions.append(actionButton(
        'Why?',
        'dashboard-attention-action secondary',
        () => explainSignal(signal)
      ));
    }
    if (moduleAvailable(signal.module)) {
      actions.append(actionButton(
        'Open module',
        'dashboard-attention-action',
        () => openModule(signal.module)
      ));
    }

    row.append(state, copy);
    if (actions.childElementCount) row.append(actions);
    return row;
  }

  function buildAttentionCenter(signals, signature) {
    const section = document.createElement('section');
    section.className = 'dashboard-attention-center';
    section.dataset.attentionSignature = signature;
    section.setAttribute('aria-labelledby', 'dashboard-attention-title');

    const head = document.createElement('div');
    head.className = 'dashboard-attention-head';
    const titleWrap = document.createElement('div');
    const eyebrow = document.createElement('span');
    eyebrow.className = 'dashboard-attention-eyebrow';
    eyebrow.textContent = 'Operational priority';
    const title = document.createElement('h3');
    title.id = 'dashboard-attention-title';
    title.textContent = 'Needs Attention';
    const note = document.createElement('p');
    note.textContent = 'Uses the dashboard’s existing critical and warning states only. No client-side risk score is calculated.';
    titleWrap.append(eyebrow, title, note);

    const count = document.createElement('strong');
    count.className = 'dashboard-attention-count';
    count.textContent = String(signals.length);
    count.setAttribute('aria-label', `${signals.length} attention signals`);
    head.append(titleWrap, count);
    section.append(head);

    const body = document.createElement('div');
    body.className = 'dashboard-attention-list';
    if (!signals.length) {
      const empty = document.createElement('p');
      empty.className = 'dashboard-attention-empty';
      empty.textContent = 'No critical or warning signals are present in the current dashboard scope.';
      body.append(empty);
    } else {
      signals.slice(0, ATTENTION_LIMIT).forEach(signal => body.append(attentionRow(signal)));
    }
    section.append(body);

    if (signals.length > ATTENTION_LIMIT) {
      const remainder = document.createElement('p');
      remainder.className = 'dashboard-attention-remainder';
      remainder.textContent = `${signals.length - ATTENTION_LIMIT} additional attention signal(s) remain visible in the KPI grid.`;
      section.append(remainder);
    }
    return section;
  }

  function decorateAttentionCenter() {
    if (!executiveDashboardVisible()) return;
    const grid = content.querySelector('.dashboard-kpi-grid[data-dashboard-intelligence="ready"]');
    const strip = content.querySelector('.dashboard-status-strip');
    if (!grid || !strip) return;

    const signals = orderedSignals(grid);
    const signature = signatureFor(signals);
    const existing = content.querySelector('.dashboard-attention-center');
    if (existing?.dataset.attentionSignature === signature) return;

    existing?.remove();
    strip.insertAdjacentElement('afterend', buildAttentionCenter(signals, signature));
  }

  new MutationObserver(() => queueMicrotask(decorateAttentionCenter))
    .observe(content, {childList: true, subtree: true});
  if (nav) {
    new MutationObserver(() => queueMicrotask(decorateAttentionCenter))
      .observe(nav, {childList: true, subtree: true});
  }
  decorateAttentionCenter();
})();
