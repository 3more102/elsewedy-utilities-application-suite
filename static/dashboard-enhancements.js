(() => {
  'use strict';

  const content = document.querySelector('#content');
  const pageTitle = document.querySelector('#page-title');
  if (!content) return;

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const TREND_SAMPLES = 3;
  const INTELLIGENCE = new Map([
    ['Open Work Orders', {family: 'maintenance', metric: 'open_work_orders', periodDays: 30}],
    ['Overdue Work', {family: 'maintenance', metric: 'overdue_work_orders', periodDays: 30}],
    ['Emergency Work', {family: 'maintenance', metric: 'emergency_work_orders', periodDays: 30}],
    ['PM Compliance', {family: 'maintenance', metric: 'pm_compliance_pct', periodDays: 30}],
    ['MTBF', {family: 'maintenance', metric: 'mtbf_hours', periodDays: 365}],
    ['MTTR', {family: 'maintenance', metric: 'mttr_hours', periodDays: 365}],
    ['Safety Incidents', {family: 'hse', metric: 'open_incidents', periodDays: 30}],
    ['Active Alarms', {family: 'condition', metric: 'active_alarms', periodDays: 30}],
    ['Maintenance Cost', {family: 'cost', metric: 'maintenance_cost_window', periodDays: 30}],
  ]);

  function ensureStylesheet() {
    if (document.querySelector('link[data-euas-dashboard-intelligence]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/dashboard-intelligence.css';
    link.dataset.euasDashboardIntelligence = 'true';
    document.head.append(link);
  }

  function executiveDashboardVisible() {
    return pageTitle?.textContent?.trim() === 'Executive Dashboard' && !!content.querySelector('.kpi-grid');
  }

  function text(node) {
    return node?.textContent?.trim() || '';
  }

  function statusItem(label, value, extraClass = '') {
    const item = document.createElement('div');
    item.className = `dashboard-status-item ${extraClass}`.trim();
    const caption = document.createElement('span');
    caption.textContent = label;
    const strong = document.createElement('strong');
    strong.textContent = value;
    item.append(caption, strong);
    return item;
  }

  function selectedScopeLabel() {
    const selector = document.querySelector('#site-selector');
    const selected = selector?.selectedOptions?.[0];
    if (selected?.value) return text(selected);
    const hero = content.querySelector('.hero');
    return text(hero?.querySelector('p')).replace(/\.$/, '') || 'Portfolio-wide view';
  }

  function scopeParams(config, includeSamples = false) {
    const params = new URLSearchParams({
      family: config.family,
      metric: config.metric,
      period_days: String(config.periodDays),
    });
    const dateValue = content.querySelector('#dash-date')?.value;
    const siteValue = document.querySelector('#site-selector')?.value;
    if (dateValue) params.set('period_end', dateValue);
    if (siteValue) params.set('site_id', siteValue);
    if (includeSamples) params.set('samples', String(TREND_SAMPLES));
    return params;
  }

  async function dashboardApi(path) {
    if (typeof api === 'function') return api(path);
    const token = localStorage.getItem('euas_token') || '';
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: token ? {Authorization: `Bearer ${token}`} : {},
    });
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('json') ? await response.json() : await response.text();
    if (!response.ok) {
      const message = payload?.detail || payload || `Request failed (${response.status})`;
      throw new Error(String(message));
    }
    return payload;
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return new Intl.NumberFormat('en-US', {maximumFractionDigits: 1}).format(number);
  }

  function formatMagnitude(value, unit = '') {
    if (value == null) return '—';
    const rendered = formatNumber(value);
    if (unit === 'currency') {
      return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        maximumFractionDigits: 0,
      }).format(Number(value));
    }
    return `${rendered}${unit === '%' ? '%' : unit ? ` ${unit}` : ''}`;
  }

  function trendState(data, values) {
    if (values.length < 2) return {className: 'neutral', label: 'No comparable baseline'};
    const previous = values.at(-2);
    const current = values.at(-1);
    const delta = current - previous;
    if (delta === 0) return {className: 'neutral', label: `No change · ${data.period_days}d buckets`};

    const improved = data.direction === 'higher_is_better' ? delta > 0 : delta < 0;
    const arrow = delta > 0 ? '▲' : '▼';
    const movement = previous === 0
      ? `${formatNumber(Math.abs(delta))} ${data.unit || ''}`.trim()
      : `${Math.abs(delta / previous * 100).toFixed(1)}%`;
    return {
      className: improved ? 'good' : 'bad',
      label: `${arrow} ${movement} ${improved ? 'better' : 'worse'} · ${data.period_days}d`,
    };
  }

  function sparkline(values, ariaLabel) {
    if (values.length < 2) return null;
    const width = 100;
    const height = 28;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const points = values.map((value, index) => {
      const x = values.length === 1 ? width / 2 : (index / (values.length - 1)) * width;
      const y = height - 3 - ((value - min) / range) * (height - 6);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.classList.add('dashboard-kpi-sparkline');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', ariaLabel);
    const line = document.createElementNS(SVG_NS, 'polyline');
    line.setAttribute('points', points);
    svg.append(line);
    return svg;
  }

  function addIntelligenceShell(card, config) {
    card.dataset.kpiFamily = config.family;
    card.dataset.kpiMetric = config.metric;

    const intelligence = document.createElement('div');
    intelligence.className = 'dashboard-kpi-intelligence';
    intelligence.setAttribute('aria-live', 'polite');

    const trend = document.createElement('div');
    trend.className = 'dashboard-kpi-trend neutral';
    const trendLabel = document.createElement('span');
    trendLabel.className = 'dashboard-kpi-trend-label';
    trendLabel.textContent = 'Canonical trend loading…';
    trend.append(trendLabel);
    intelligence.append(trend);

    const actions = document.createElement('div');
    actions.className = 'dashboard-kpi-actions';
    const why = document.createElement('button');
    why.type = 'button';
    why.className = 'dashboard-kpi-action';
    why.textContent = 'Why?';
    why.setAttribute('aria-label', `Explain ${text(card.querySelector('.kpi-label'))}`);
    why.addEventListener('click', () => showExplanation(card, config));
    actions.append(why);

    card.append(intelligence, actions);
    return {intelligence, trend, trendLabel};
  }

  function decorateKpis(grid) {
    grid.classList.add('dashboard-kpi-grid');
    const kpis = [...grid.querySelectorAll(':scope > .kpi')];
    let attention = 0;
    let critical = 0;
    let explainable = 0;

    kpis.forEach(card => {
      const label = text(card.querySelector('.kpi-label'));
      const value = text(card.querySelector('.kpi-value'));
      const hint = text(card.querySelector('.trend'));
      const trend = card.querySelector('.trend');
      const config = INTELLIGENCE.get(label);

      card.classList.remove('kpi-positive', 'kpi-attention', 'kpi-critical');
      if (trend?.classList.contains('bad')) {
        card.classList.add('kpi-critical');
        critical += 1;
        attention += 1;
      } else if (trend?.classList.contains('warn')) {
        card.classList.add('kpi-attention');
        attention += 1;
      } else if (trend?.classList.contains('good')) {
        card.classList.add('kpi-positive');
      }

      if (config) {
        card.classList.add('kpi-explainable');
        addIntelligenceShell(card, config);
        explainable += 1;
      }

      card.setAttribute('role', 'group');
      card.setAttribute('aria-label', [label && `${label}: ${value}`, hint].filter(Boolean).join('. '));
    });

    return {total: kpis.length, attention, critical, explainable};
  }

  function decorateCharts() {
    [...content.querySelectorAll('.panel')].forEach((panel, index) => {
      const heading = panel.querySelector('.panel-head h3');
      if (!heading) return;
      panel.classList.add('dashboard-chart-panel');
      heading.id ||= `dashboard-panel-${index + 1}`;
      panel.setAttribute('role', 'region');
      panel.setAttribute('aria-labelledby', heading.id);
    });

    content.querySelectorAll('.bar-row').forEach(row => {
      const label = text(row.querySelector('span'));
      const value = text(row.querySelector('b'));
      row.setAttribute('role', 'img');
      row.setAttribute('aria-label', `${label}: ${value}`);
    });

    const donut = content.querySelector('.donut');
    if (donut) {
      const good = text(donut.querySelector('.donut-center strong'));
      donut.setAttribute('role', 'img');
      donut.setAttribute('aria-label', `Asset health distribution: ${good} good condition`);
    }

    const spark = content.querySelector('.spark');
    if (spark) {
      spark.setAttribute('role', 'img');
      spark.setAttribute('aria-label', 'Maintenance cost by asset trend');
    }

    const activity = content.querySelector('.activity');
    if (activity) {
      activity.setAttribute('role', 'list');
      activity.querySelectorAll('.activity-item').forEach(item => item.setAttribute('role', 'listitem'));
    }
  }

  function addDashboardSummary(grid, counts) {
    if (content.querySelector('.dashboard-status-strip')) return;

    const dateInput = content.querySelector('#dash-date');
    const asOf = dateInput?.value || 'Current';
    if (dateInput) dateInput.setAttribute('aria-label', 'Executive dashboard as-of date');
    content.querySelector('#dash-refresh')?.setAttribute('aria-label', 'Refresh executive dashboard');

    const strip = document.createElement('section');
    strip.className = 'dashboard-status-strip';
    strip.setAttribute('aria-label', 'Executive dashboard snapshot');
    strip.append(
      statusItem('Executive snapshot', `${counts.total} portfolio signals`, 'primary'),
      statusItem('Attention signals', String(counts.attention), counts.attention ? 'attention' : ''),
      statusItem('Critical signals', String(counts.critical), counts.critical ? 'critical' : ''),
      statusItem('Explainable KPIs', `${counts.explainable} canonical drill paths`),
      statusItem('As of / scope', `${asOf} · ${selectedScopeLabel()}`)
    );

    const kicker = document.createElement('div');
    kicker.className = 'dashboard-section-kicker';
    kicker.textContent = `Portfolio signals · ${counts.critical} critical · ${counts.explainable} explainable`;

    grid.parentNode.insertBefore(strip, grid);
    grid.parentNode.insertBefore(kicker, grid);
  }

  function ensureExplanationPanel(grid) {
    let panel = content.querySelector('.dashboard-intelligence-panel');
    if (panel) return panel;

    panel = document.createElement('section');
    panel.className = 'dashboard-intelligence-panel';
    panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'dashboard-intelligence-title');
    panel.setAttribute('tabindex', '-1');

    const head = document.createElement('div');
    head.className = 'dashboard-intelligence-head';
    const titleWrap = document.createElement('div');
    const eyebrow = document.createElement('span');
    eyebrow.className = 'dashboard-intelligence-eyebrow';
    eyebrow.textContent = 'Canonical KPI evidence';
    const title = document.createElement('h3');
    title.id = 'dashboard-intelligence-title';
    title.textContent = 'KPI explanation';
    titleWrap.append(eyebrow, title);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'dashboard-intelligence-close';
    close.textContent = 'Close';
    close.setAttribute('aria-label', 'Close KPI explanation');
    close.addEventListener('click', () => {
      panel.hidden = true;
    });
    head.append(titleWrap, close);

    const body = document.createElement('div');
    body.className = 'dashboard-intelligence-body';
    panel.append(head, body);
    grid.insertAdjacentElement('afterend', panel);
    return panel;
  }

  function comparisonItem(label, value) {
    const item = document.createElement('div');
    item.className = 'dashboard-intelligence-stat';
    const caption = document.createElement('span');
    caption.textContent = label;
    const strong = document.createElement('strong');
    strong.textContent = value;
    item.append(caption, strong);
    return item;
  }

  function driverRecordButton(driver) {
    const module = driver?.drill?.module;
    if (!module) return null;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'dashboard-driver-open';
    button.textContent = 'Open record';
    button.addEventListener('click', () => drillTo(driver));
    return button;
  }

  async function drillTo(driver) {
    const module = driver?.drill?.module;
    const rawId = driver?.drill?.id ?? driver?.source_id;
    const id = Number(rawId);
    if (!module || typeof go !== 'function') return;

    const panel = content.querySelector('.dashboard-intelligence-panel');
    if (panel) panel.hidden = true;

    await Promise.resolve(go(module));

    if (!Number.isFinite(id)) return;
    if (driver.source_type === 'work_order' && typeof window.workDetail === 'function') {
      await window.workDetail(id);
    } else if (driver.source_type === 'asset' && typeof window.assetDetail === 'function') {
      await window.assetDetail(id);
    }
  }

  function renderExplanation(panel, data) {
    const title = panel.querySelector('#dashboard-intelligence-title');
    const body = panel.querySelector('.dashboard-intelligence-body');
    title.textContent = `${data.label || 'KPI'} — Why?`;
    body.replaceChildren();

    const summary = document.createElement('p');
    summary.className = 'dashboard-intelligence-summary';
    summary.textContent = data.summary || 'No comparison summary is available for this window.';
    body.append(summary);

    const compare = document.createElement('div');
    compare.className = 'dashboard-intelligence-compare';
    compare.append(
      comparisonItem('Current', formatMagnitude(data.value, data.unit)),
      comparisonItem('Previous', formatMagnitude(data.previous_value, data.unit)),
      comparisonItem(
        'Change',
        data.delta == null
          ? 'Not comparable'
          : `${Number(data.delta) > 0 ? '+' : ''}${formatMagnitude(data.delta, data.unit)}`
      )
    );
    body.append(compare);

    const heading = document.createElement('h4');
    heading.textContent = 'Observed contributors';
    body.append(heading);

    const drivers = Array.isArray(data.drivers) ? data.drivers : [];
    if (!drivers.length) {
      const emptyState = document.createElement('p');
      emptyState.className = 'dashboard-intelligence-empty';
      emptyState.textContent = 'No contributing records were returned by the canonical explanation for this window.';
      body.append(emptyState);
    } else {
      const list = document.createElement('div');
      list.className = 'dashboard-driver-list';
      drivers.forEach(driver => {
        const row = document.createElement('article');
        row.className = 'dashboard-driver';
        const copy = document.createElement('div');
        const name = document.createElement('strong');
        name.textContent = driver.label || driver.kind || 'Observed record';
        const meta = document.createElement('p');
        const evidence = driver.attribution === 'contributor' ? 'Contributor' : 'Correlation';
        meta.textContent = [
          evidence,
          driver.source_type ? String(driver.source_type).replaceAll('_', ' ') : '',
          driver.magnitude == null ? '' : formatMagnitude(driver.magnitude, driver.unit),
        ].filter(Boolean).join(' · ');
        copy.append(name, meta);
        row.append(copy);
        const open = driverRecordButton(driver);
        if (open) row.append(open);
        list.append(row);
      });
      body.append(list);
    }

    const disclaimer = document.createElement('p');
    disclaimer.className = 'dashboard-intelligence-disclaimer';
    disclaimer.textContent = data.disclaimer || 'Evidence is observational and is not presented as proof of causation.';
    body.append(disclaimer);
  }

  async function showExplanation(card, config) {
    const grid = card.closest('.dashboard-kpi-grid');
    if (!grid) return;
    const panel = ensureExplanationPanel(grid);
    const title = panel.querySelector('#dashboard-intelligence-title');
    const body = panel.querySelector('.dashboard-intelligence-body');
    const label = text(card.querySelector('.kpi-label'));

    panel.hidden = false;
    title.textContent = `${label} — Why?`;
    body.replaceChildren();
    const loading = document.createElement('p');
    loading.className = 'dashboard-intelligence-loading';
    loading.textContent = 'Loading scoped canonical evidence…';
    body.append(loading);
    panel.focus();

    try {
      const params = scopeParams(config);
      const data = await dashboardApi(`/api/kpi/explanation?${params}`);
      if (!panel.isConnected || !executiveDashboardVisible()) return;
      renderExplanation(panel, data);
    } catch (error) {
      body.replaceChildren();
      const failure = document.createElement('p');
      failure.className = 'dashboard-intelligence-error';
      failure.textContent = `Explanation unavailable: ${error?.message || String(error)}`;
      body.append(failure);
    }
  }

  async function loadTrend(card, config) {
    const trendNode = card.querySelector('.dashboard-kpi-trend');
    const labelNode = card.querySelector('.dashboard-kpi-trend-label');
    if (!trendNode || !labelNode) return;

    try {
      const params = scopeParams(config, true);
      const data = await dashboardApi(`/api/kpi/trend?${params}`);
      if (!card.isConnected || !executiveDashboardVisible()) return;

      const values = (Array.isArray(data.samples) ? data.samples : [])
        .filter(sample => sample && sample.value != null)
        .map(sample => Number(sample.value))
        .filter(Number.isFinite);
      const state = trendState(data, values);
      trendNode.classList.remove('good', 'bad', 'neutral');
      trendNode.classList.add(state.className);
      labelNode.textContent = state.label;

      const oldSpark = trendNode.querySelector('.dashboard-kpi-sparkline');
      if (oldSpark) oldSpark.remove();
      const graph = sparkline(
        values,
        `${data.label || text(card.querySelector('.kpi-label'))} canonical trend: ${values.join(', ')}`
      );
      if (graph) trendNode.prepend(graph);
    } catch (error) {
      if (!card.isConnected) return;
      trendNode.classList.remove('good', 'bad');
      trendNode.classList.add('neutral');
      labelNode.textContent = 'Canonical trend unavailable';
      trendNode.title = error?.message || String(error);
    }
  }

  async function loadTrends(grid) {
    const jobs = [...grid.querySelectorAll(':scope > .kpi.kpi-explainable')]
      .map(card => {
        const config = INTELLIGENCE.get(text(card.querySelector('.kpi-label')));
        return config ? {card, config} : null;
      })
      .filter(Boolean);

    const queue = [...jobs];
    const workers = Array.from({length: Math.min(3, queue.length)}, async () => {
      while (queue.length) {
        const job = queue.shift();
        if (!job) return;
        await loadTrend(job.card, job.config);
      }
    });
    await Promise.all(workers);
  }

  function decorateDashboard() {
    if (!executiveDashboardVisible()) return;
    const grid = content.querySelector('.kpi-grid');
    if (!grid || grid.dataset.dashboardIntelligence === 'ready') return;

    grid.dataset.dashboardIntelligence = 'ready';
    const counts = decorateKpis(grid);
    addDashboardSummary(grid, counts);
    ensureExplanationPanel(grid);
    decorateCharts();

    const schedule = window.requestIdleCallback || (callback => setTimeout(callback, 0));
    schedule(() => loadTrends(grid));
  }

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const panel = content.querySelector('.dashboard-intelligence-panel');
    if (panel && !panel.hidden) panel.hidden = true;
  });

  ensureStylesheet();
  new MutationObserver(() => queueMicrotask(decorateDashboard)).observe(content, {childList: true});
  decorateDashboard();
})();
