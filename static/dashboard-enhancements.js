(() => {
  'use strict';

  const content = document.querySelector('#content');
  const pageTitle = document.querySelector('#page-title');
  if (!content) return;

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

  function decorateKpis(grid) {
    grid.classList.add('dashboard-kpi-grid');
    const kpis = [...grid.querySelectorAll('.kpi')];
    let attention = 0;
    let critical = 0;

    kpis.forEach(card => {
      const label = text(card.querySelector('.kpi-label'));
      const value = text(card.querySelector('.kpi-value'));
      const hint = text(card.querySelector('.trend'));
      const trend = card.querySelector('.trend');

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

      card.setAttribute('role', 'group');
      card.setAttribute('aria-label', [label && `${label}: ${value}`, hint].filter(Boolean).join('. '));
    });

    return {total: kpis.length, attention, critical};
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

    const hero = content.querySelector('.hero');
    const scope = text(hero?.querySelector('p')).replace(/\.$/, '') || 'Portfolio-wide view';
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
      statusItem('As of', asOf),
      statusItem('Scope', scope)
    );

    const kicker = document.createElement('div');
    kicker.className = 'dashboard-section-kicker';
    kicker.textContent = counts.critical
      ? `Portfolio signals · ${counts.critical} critical`
      : 'Portfolio signals';

    grid.parentNode.insertBefore(strip, grid);
    grid.parentNode.insertBefore(kicker, grid);
  }

  function decorateDashboard() {
    if (!executiveDashboardVisible()) return;
    const grid = content.querySelector('.kpi-grid');
    if (!grid || grid.classList.contains('dashboard-kpi-grid')) return;

    const counts = decorateKpis(grid);
    addDashboardSummary(grid, counts);
    decorateCharts();
  }

  new MutationObserver(() => queueMicrotask(decorateDashboard)).observe(content, {childList: true});
  decorateDashboard();
})();
