(() => {
  'use strict';

  const content = document.querySelector('#content');
  const pageTitle = document.querySelector('#page-title');
  const siteSelector = document.querySelector('#site-selector');
  const nav = document.querySelector('#nav');
  if (!content || !pageTitle) return;

  const KPI_ROLES = new Set([
    'admin', 'maintenance_manager', 'executive', 'asset_manager', 'planner', 'supervisor'
  ]);
  const BACKLOG_LIMIT = 5;
  const SHORTAGE_LIMIT = 5;
  const PM_HORIZON_DAYS = 84;
  let generation = 0;
  let lastScope = '';
  let scheduled = false;

  function dashboardVisible() {
    return pageTitle.textContent.trim() === 'Executive Dashboard'
      && !!content.querySelector('.dashboard-kpi-grid[data-dashboard-intelligence="ready"]');
  }

  function roleAllowed() {
    return typeof S !== 'undefined' && KPI_ROLES.has(S.user?.role);
  }

  function text(node) {
    return node?.textContent?.replace(/\s+/g, ' ').trim() || '';
  }

  function formatNumber(value, digits = 1) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return new Intl.NumberFormat('en-US', {maximumFractionDigits: digits}).format(number);
  }

  function currentScope() {
    const siteId = siteSelector?.value || '';
    const periodEnd = content.querySelector('#dash-date')?.value || '';
    return {siteId, periodEnd};
  }

  function scopeLabel() {
    const selected = siteSelector?.selectedOptions?.[0];
    return selected?.value ? text(selected) : 'All sites';
  }

  function scopedParams(extra = {}, includeDate = false) {
    const {siteId, periodEnd} = currentScope();
    const params = new URLSearchParams();
    Object.entries(extra).forEach(([key, value]) => params.set(key, String(value)));
    if (siteId) params.set('site_id', siteId);
    if (includeDate && periodEnd) params.set('period_end', periodEnd);
    return params;
  }

  async function actionApi(path) {
    if (typeof api === 'function') return api(path);
    const token = localStorage.getItem('euas_token') || '';
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: token ? {Authorization: `Bearer ${token}`} : {},
    });
    const type = response.headers.get('content-type') || '';
    const payload = type.includes('json') ? await response.json() : await response.text();
    if (!response.ok) throw new Error(payload?.detail || payload || `Request failed (${response.status})`);
    return payload;
  }

  function navButtonFor(module) {
    if (!module || !nav) return null;
    return [...nav.querySelectorAll('.nav-btn')]
      .find(button => button.dataset.view === module) || null;
  }

  async function openWorkOrder(id) {
    const button = navButtonFor('work');
    if (!button) return;
    if (typeof go === 'function') await Promise.resolve(go('work'));
    else button.click();
    const numericId = Number(id);
    if (Number.isFinite(numericId) && typeof window.workDetail === 'function') {
      await window.workDetail(numericId);
    }
  }

  function openModule(module) {
    navButtonFor(module)?.click();
  }

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = String(value);
    return element;
  }

  function metric(label, value, tone = '') {
    const item = node('div', `dashboard-action-metric ${tone}`.trim());
    item.append(node('span', '', label), node('strong', '', value));
    return item;
  }

  function actionButton(label, handler) {
    const button = node('button', 'dashboard-action-button', label);
    button.type = 'button';
    button.addEventListener('click', handler);
    return button;
  }

  function sectionShell(eyebrow, title) {
    const section = node('section', 'dashboard-action-section');
    const head = node('div', 'dashboard-action-section-head');
    const copy = node('div');
    copy.append(node('span', 'dashboard-action-eyebrow', eyebrow), node('h4', '', title));
    head.append(copy);
    const body = node('div', 'dashboard-action-section-body');
    section.append(head, body);
    return {section, head, body};
  }

  function emptyState(body, message) {
    body.replaceChildren(node('p', 'dashboard-action-empty', message));
  }

  function backlogSection(result) {
    const {section, head, body} = sectionShell('Execution risk', 'Risk-ranked backlog');
    if (result.status !== 'fulfilled') {
      emptyState(body, 'Backlog risk is unavailable for this scope.');
      return section;
    }
    const data = result.value || {};
    const summary = data.summary || {};
    const stats = node('div', 'dashboard-action-metrics');
    stats.append(
      metric('Risk-weighted backlog', formatNumber(summary.risk_weighted_backlog || 0)),
      metric('Critical', formatNumber(summary.critical_count || 0, 0), Number(summary.critical_count) ? 'critical' : ''),
      metric('Parts-blocked high risk', formatNumber(summary.blocked_high_risk || 0, 0), Number(summary.blocked_high_risk) ? 'attention' : '')
    );
    body.append(stats);

    const rows = Array.isArray(data.rows) ? data.rows.slice(0, BACKLOG_LIMIT) : [];
    if (!rows.length) {
      body.append(node('p', 'dashboard-action-empty', 'No open work is present in the canonical risk backlog.'));
      return section;
    }
    const list = node('div', 'dashboard-action-list');
    rows.forEach(row => {
      const item = node('article', 'dashboard-action-row');
      const copy = node('div', 'dashboard-action-row-copy');
      copy.append(
        node('strong', '', row.wo_no || 'Work order'),
        node('p', '', [row.title, row.priority, row.asset_no].filter(Boolean).join(' · '))
      );
      const score = node('span', 'dashboard-action-score', `Risk ${formatNumber(row.risk_score || 0)}`);
      item.append(copy, score);
      if (navButtonFor('work')) item.append(actionButton('Open', () => openWorkOrder(row.id)));
      list.append(item);
    });
    body.append(list);
    return section;
  }

  function shortageSection(result) {
    const {section, body} = sectionShell('Material readiness', 'Parts blocking work');
    if (result.status !== 'fulfilled') {
      emptyState(body, 'Parts-shortage detail is unavailable for this scope.');
      return section;
    }
    const data = result.value || {};
    const summary = data.summary || {};
    const stats = node('div', 'dashboard-action-metrics');
    stats.append(
      metric('Blocked work orders', formatNumber(summary.blocked_work_orders || 0, 0)),
      metric('Short lines', formatNumber(summary.short_lines || 0, 0)),
      metric('High-risk lines', formatNumber(summary.high_risk_lines || 0, 0), Number(summary.high_risk_lines) ? 'attention' : '')
    );
    body.append(stats);

    const lines = Array.isArray(data.lines) ? data.lines.slice(0, SHORTAGE_LIMIT) : [];
    if (!lines.length) {
      body.append(node('p', 'dashboard-action-empty', 'No canonical parts shortages are blocking open work.'));
      return section;
    }
    const list = node('div', 'dashboard-action-list');
    lines.forEach(line => {
      const item = node('article', 'dashboard-action-row');
      const copy = node('div', 'dashboard-action-row-copy');
      const amount = `${formatNumber(line.outstanding_short)} ${line.unit || ''}`.trim();
      copy.append(
        node('strong', '', line.item_no || line.item_name || 'Inventory line'),
        node('p', '', [line.wo_no, line.priority, line.asset_no].filter(Boolean).join(' · '))
      );
      item.append(copy, node('span', 'dashboard-action-score attention', `${amount} short`));
      if (navButtonFor('work')) item.append(actionButton('Work', () => openWorkOrder(line.wo_id)));
      list.append(item);
    });
    body.append(list);
    return section;
  }

  function pmSection(result) {
    const {section, head, body} = sectionShell('Planning foresight', 'Critical PM capacity risk');
    if (navButtonFor('maintenance')) head.append(actionButton('Maintenance', () => openModule('maintenance')));
    if (result.status !== 'fulfilled') {
      emptyState(body, 'PM capacity risk is unavailable for this scope.');
      return section;
    }
    const data = result.value || {};
    const stats = node('div', 'dashboard-action-metrics');
    stats.append(
      metric('Critical PMs', formatNumber(data.critical_pm_total || 0, 0)),
      metric('In overloaded weeks', formatNumber(data.critical_pm_in_overloaded_weeks || 0, 0), Number(data.critical_pm_in_overloaded_weeks) ? 'critical' : ''),
      metric('Capacity source', data.capacity_source || 'Unavailable')
    );
    body.append(stats);

    const weeks = Array.isArray(data.overloaded_weeks) ? data.overloaded_weeks.slice(0, 4) : [];
    if (!weeks.length) {
      const message = data.unavailable_note
        || 'No critical PMs fall inside overloaded forecast weeks.';
      body.append(node('p', 'dashboard-action-empty', message));
      return section;
    }
    const list = node('div', 'dashboard-action-list');
    weeks.forEach(week => {
      const item = node('article', 'dashboard-action-row');
      const copy = node('div', 'dashboard-action-row-copy');
      const pmCount = Array.isArray(week.critical_pms) ? week.critical_pms.length : 0;
      copy.append(
        node('strong', '', `Week of ${week.week_start || '—'}`),
        node('p', '', `${pmCount} critical PM${pmCount === 1 ? '' : 's'} · ${week.capacity_state || 'Capacity state unavailable'}`)
      );
      item.append(copy, node('span', 'dashboard-action-score critical', `${formatNumber(week.utilization_pct)}%`));
      list.append(item);
    });
    body.append(list);
    if (data.unavailable_note) body.append(node('p', 'dashboard-action-note', data.unavailable_note));
    return section;
  }

  function buildCenter(results) {
    const center = node('section', 'dashboard-action-center');
    center.dataset.executiveActionCenter = 'true';
    center.setAttribute('aria-labelledby', 'dashboard-action-center-title');

    const head = node('div', 'dashboard-action-head');
    const copy = node('div');
    copy.append(
      node('span', 'dashboard-action-eyebrow', 'Canonical operations intelligence'),
      node('h3', '', 'Executive Action Center'),
      node('p', '', `Decision-ready queues · ${scopeLabel()} · ${PM_HORIZON_DAYS}d PM horizon`)
    );
    copy.querySelector('h3').id = 'dashboard-action-center-title';
    const refresh = actionButton('Refresh actions', () => loadActionCenter(true));
    refresh.classList.add('dashboard-action-refresh');
    head.append(copy, refresh);

    const grid = node('div', 'dashboard-action-grid');
    grid.append(backlogSection(results[0]), shortageSection(results[1]), pmSection(results[2]));
    center.append(head, grid);
    return center;
  }

  function placeCenter(center) {
    const existing = content.querySelector('.dashboard-action-center');
    if (existing) {
      existing.replaceWith(center);
      return;
    }
    const attention = content.querySelector('.dashboard-attention-center');
    const strip = content.querySelector('.dashboard-status-strip');
    const anchor = attention || strip;
    if (anchor) anchor.insertAdjacentElement('afterend', center);
  }

  async function loadActionCenter(force = false) {
    scheduled = false;
    if (!dashboardVisible() || !roleAllowed()) return;
    const scope = currentScope();
    const signature = `${scope.siteId}|${scope.periodEnd}`;
    if (!force && signature === lastScope && content.querySelector('.dashboard-action-center')) return;
    lastScope = signature;
    const run = ++generation;

    const backlog = scopedParams({limit: BACKLOG_LIMIT, period_days: 30}, true);
    const shortages = scopedParams({limit: SHORTAGE_LIMIT});
    const pmRisk = scopedParams({horizon_days: PM_HORIZON_DAYS});
    const results = await Promise.allSettled([
      actionApi(`/api/kpi/backlog/risk?${backlog}`),
      actionApi(`/api/kpi/parts/shortages?${shortages}`),
      actionApi(`/api/kpi/pm-risk?${pmRisk}`),
    ]);
    if (run !== generation || !dashboardVisible()) return;
    placeCenter(buildCenter(results));
  }

  function scheduleLoad() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => loadActionCenter(false));
  }

  siteSelector?.addEventListener('change', () => {
    lastScope = '';
    scheduleLoad();
  });
  content.addEventListener('euas:canonical-dashboard-snapshot', scheduleLoad);
  new MutationObserver(scheduleLoad).observe(content, {childList: true});
  scheduleLoad();
})();
