(() => {
  'use strict';

  const content = document.querySelector('#content');
  const pageTitle = document.querySelector('#page-title');
  const siteSelector = document.querySelector('#site-selector');
  if (!content || !pageTitle) return;

  const CANONICAL_PERIOD_DAYS = 30;
  const KPI_ROLES = new Set([
    'admin', 'maintenance_manager', 'executive', 'asset_manager', 'planner', 'supervisor'
  ]);

  const CARD_BINDINGS = new Map([
    ['Open Work Orders', {path: 'maintenance.open_wo', format: 'number'}],
    ['Overdue Work', {path: 'maintenance.overdue_wo', format: 'number'}],
    ['Emergency Work', {path: 'maintenance.emergency_wo', format: 'number'}],
    ['PM Compliance', {path: 'maintenance.pm_compliance_pct', format: 'percent'}],
    ['MTBF', {path: 'maintenance.mtbf_hours', format: 'hours'}],
    ['MTTR', {path: 'maintenance.mttr_hours', format: 'hours'}],
    ['Active Technicians', {path: 'workforce.technicians_total', format: 'number'}],
    ['Safety Incidents', {path: 'hse.open_incidents', format: 'number'}],
    ['Active Alarms', {path: 'condition.active_alarms', format: 'number'}],
    ['Maintenance Cost', {path: 'costs.maintenance_cost_window', format: 'currency'}],
  ]);

  let generation = 0;
  let lastRequestKey = '';
  let scheduled = false;

  function dashboardVisible() {
    return pageTitle.textContent.trim() === 'Executive Dashboard' && !!content.querySelector('.kpi-grid');
  }

  function roleAllowed() {
    return typeof S !== 'undefined' && KPI_ROLES.has(S.user?.role);
  }

  function readPath(payload, path) {
    return path.split('.').reduce((value, key) => (
      value && typeof value === 'object' ? value[key] : undefined
    ), payload);
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return new Intl.NumberFormat('en-US', {maximumFractionDigits: 1}).format(number);
  }

  function formatValue(value, format) {
    if (value == null) return '—';
    if (format === 'percent') return `${formatNumber(value)}%`;
    if (format === 'hours') return `${formatNumber(value)} h`;
    if (format === 'currency') {
      const number = Number(value);
      if (!Number.isFinite(number)) return '—';
      return new Intl.NumberFormat('en-US', {
        style: 'currency', currency: 'USD', maximumFractionDigits: 0,
      }).format(number);
    }
    return formatNumber(value);
  }

  function paramsForDashboard() {
    const params = new URLSearchParams({period_days: String(CANONICAL_PERIOD_DAYS)});
    const periodEnd = content.querySelector('#dash-date')?.value;
    const siteId = siteSelector?.value;
    if (periodEnd) params.set('period_end', periodEnd);
    if (siteId) params.set('site_id', siteId);
    return params;
  }

  async function canonicalApi(path) {
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

  function hintFor(label, snapshot) {
    const maintenance = snapshot.maintenance || {};
    const condition = snapshot.condition || {};
    if (label === 'Open Work Orders') {
      return `${formatNumber(maintenance.overdue_wo || 0)} overdue · canonical ${CANONICAL_PERIOD_DAYS}d scope`;
    }
    if (label === 'Overdue Work') return `Past target finish · canonical ${CANONICAL_PERIOD_DAYS}d scope`;
    if (label === 'Emergency Work') return `Immediate-response backlog · canonical ${CANONICAL_PERIOD_DAYS}d scope`;
    if (label === 'PM Compliance') return `Target ≥ 95% · canonical ${CANONICAL_PERIOD_DAYS}d window`;
    if (label === 'MTBF') return `Canonical ${CANONICAL_PERIOD_DAYS}d reliability window`;
    if (label === 'MTTR') return `Canonical ${CANONICAL_PERIOD_DAYS}d repair window`;
    if (label === 'Active Technicians') return 'Active technician accounts · canonical state';
    if (label === 'Safety Incidents') return 'Open HSE records · canonical state';
    if (label === 'Active Alarms') {
      return `${formatNumber(condition.critical_active_alarms || 0)} critical · canonical state`;
    }
    if (label === 'Maintenance Cost') return `Posted maintenance ledger · canonical ${CANONICAL_PERIOD_DAYS}d window`;
    return `Canonical ${CANONICAL_PERIOD_DAYS}d snapshot`;
  }

  function applySnapshot(snapshot) {
    if (!dashboardVisible()) return;
    const cards = [...content.querySelectorAll('.kpi')];
    for (const card of cards) {
      const label = card.querySelector('.kpi-label')?.textContent?.trim();
      const binding = CARD_BINDINGS.get(label);
      if (!binding) continue;
      const value = readPath(snapshot, binding.path);
      if (value === undefined) continue;
      const valueNode = card.querySelector('.kpi-value');
      if (!valueNode) continue;
      valueNode.textContent = formatValue(value, binding.format);
      const hint = card.querySelector('.trend');
      if (hint) hint.textContent = hintFor(label, snapshot);
      card.dataset.canonicalSnapshot = 'true';
      card.dataset.canonicalPath = binding.path;
      card.dataset.canonicalPeriodDays = String(CANONICAL_PERIOD_DAYS);
    }
    if (typeof S !== 'undefined' && S.cache) S.cache.canonicalExecutive = snapshot;
  }

  async function syncCanonicalSnapshot() {
    scheduled = false;
    if (!dashboardVisible() || !roleAllowed()) return;

    const params = paramsForDashboard();
    const requestKey = params.toString();
    if (requestKey === lastRequestKey && content.querySelector('[data-canonical-snapshot="true"]')) return;
    lastRequestKey = requestKey;
    const run = ++generation;

    try {
      const snapshot = await canonicalApi(`/api/kpi/executive?${params}`);
      if (run !== generation || !dashboardVisible()) return;
      applySnapshot(snapshot);
    } catch (error) {
      if (run !== generation || !dashboardVisible()) return;
      // Keep the already-rendered legacy values if canonical data is unavailable.
      // The canonical layer is an accuracy upgrade, never a reason to blank the dashboard.
      console.warn('EUAS canonical dashboard snapshot unavailable', error);
    }
  }

  function scheduleSync() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(syncCanonicalSnapshot);
  }

  new MutationObserver(scheduleSync).observe(content, {childList: true});
  scheduleSync();
})();
