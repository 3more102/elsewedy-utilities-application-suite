(() => {
  'use strict';

  const content = document.querySelector('#content');
  const pageTitle = document.querySelector('#page-title');
  if (!content) return;

  // Bind only when the visible legacy card and the canonical KPI have the
  // same current-value semantics. Do not infer mappings from similar names.
  // Examples intentionally excluded: Maintenance Cost (legacy lifetime/top-8
  // work cost vs canonical windowed ledger cost), MTBF/MTTR (legacy 365-day
  // reliability basis), live alarms/incidents (point-in-time state), and open
  // work (point-in-time state cannot produce an honest historical trend).
  const METRICS = Object.freeze({
    'Overdue Work': {
      family: 'maintenance',
      metric: 'overdue_work_orders',
      periodDays: 30
    },
    'PM Compliance': {
      family: 'maintenance',
      metric: 'pm_compliance_pct',
      periodDays: 30,
      portfolioOnly: true
    }
  });

  let requestSequence = 0;

  function executiveDashboardVisible() {
    return pageTitle?.textContent?.trim() === 'Executive Dashboard'
      && !!content.querySelector('.dashboard-kpi-grid');
  }

  function cardLabel(card) {
    return card.querySelector('.kpi-label')?.textContent?.trim() || '';
  }

  function queryFor(meta, samples = null) {
    const q = new URLSearchParams({
      family: meta.family,
      metric: meta.metric,
      period_days: String(meta.periodDays || 30)
    });
    if (samples != null) q.set('samples', String(samples));
    if (typeof S !== 'undefined' && S.dashDate) q.set('period_end', S.dashDate);
    if (typeof S !== 'undefined' && S.siteId) q.set('site_id', String(S.siteId));
    return q.toString();
  }

  function formatValue(value, unit) {
    if (value == null) return 'Unavailable';
    if (unit === 'currency') return fmtMoney(value);
    if (unit === '%') return `${fmt(value)}%`;
    if (unit === 'hours') return `${fmt(value)} h`;
    return `${fmt(value)} ${unit || ''}`.trim();
  }

  function formatMagnitude(value, unit) {
    if (value == null) return '—';
    const rendered = typeof value === 'number' ? fmt(value) : esc(value);
    return `${rendered}${unit ? ` ${esc(unit)}` : ''}`;
  }

  function trendChart(data) {
    const samples = data.samples || [];
    const valid = samples
      .map((sample, index) => ({index, value: sample.value}))
      .filter(point => point.value != null && Number.isFinite(Number(point.value)));

    if (!valid.length) {
      return `<div class="kpi-intel-empty">${esc(data.missing_note || 'No computable trend values in the selected windows.')}</div>`;
    }

    const width = 620;
    const height = 180;
    const left = 34;
    const right = 16;
    const top = 18;
    const bottom = 32;
    const min = Math.min(...valid.map(point => Number(point.value)));
    const max = Math.max(...valid.map(point => Number(point.value)));
    const isFlat = Math.abs(max - min) < 1e-12;
    const span = Math.max(max - min, 1e-9);
    const x = index => left + index * (width - left - right) / Math.max(samples.length - 1, 1);
    const y = value => isFlat
      ? top + (height - top - bottom) / 2
      : top + (max - Number(value)) * (height - top - bottom) / span;

    // Never draw a line across a missing bucket: a gap means missing evidence,
    // not continuity. Single valid points remain visible as circles.
    const segments = [];
    let segment = [];
    samples.forEach((sample, index) => {
      if (sample.value != null && Number.isFinite(Number(sample.value))) {
        segment.push({index, value: sample.value});
      } else if (segment.length) {
        segments.push(segment);
        segment = [];
      }
    });
    if (segment.length) segments.push(segment);

    const polylines = segments
      .filter(points => points.length > 1)
      .map(points => `<polyline points="${points.map(point => `${x(point.index).toFixed(2)},${y(point.value).toFixed(2)}`).join(' ')}"></polyline>`)
      .join('');

    const circles = valid.map(point => {
      const sample = samples[point.index];
      const label = `${sample.period_end || 'Window'}: ${formatValue(point.value, data.unit)}`;
      return `<circle cx="${x(point.index).toFixed(2)}" cy="${y(point.value).toFixed(2)}" r="4"><title>${esc(label)}</title></circle>`;
    }).join('');

    const labels = samples.map((sample, index) => {
      const raw = String(sample.period_end || '');
      const label = raw.length >= 10 ? raw.slice(5, 10) : raw;
      return `<text x="${x(index).toFixed(2)}" y="${height - 9}" text-anchor="middle">${esc(label)}</text>`;
    }).join('');

    return `<svg class="kpi-intel-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(data.label)} trend across ${samples.length} windows">
      <line class="kpi-intel-gridline" x1="${left}" y1="${top}" x2="${width - right}" y2="${top}"></line>
      <line class="kpi-intel-gridline" x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}"></line>
      ${polylines}${circles}${labels}
    </svg>`;
  }

  function trendBody(data, meta) {
    const current = (data.samples || []).at(-1)?.value;
    const periodDays = meta.periodDays || 30;
    return `<div class="kpi-intelligence-modal">
      <div class="kpi-intel-summary-grid">
        <div><span>Current</span><strong>${formatValue(current, data.unit)}</strong></div>
        <div><span>Best direction</span><strong>${data.direction === 'higher_is_better' ? 'Higher' : 'Lower'}</strong></div>
        <div><span>Minimum</span><strong>${formatValue(data.min, data.unit)}</strong></div>
        <div><span>Maximum</span><strong>${formatValue(data.max, data.unit)}</strong></div>
      </div>
      <div class="kpi-intel-chart-wrap">${trendChart(data)}</div>
      <p class="kpi-intel-footnote">Six chronological ${periodDays}-day windows, oldest first. Missing buckets remain visually disconnected. Values come from the canonical KPI computation for the selected dashboard scope and as-of date.</p>
    </div>`;
  }

  function explanationBody(data) {
    const movement = data.improved === true
      ? '<span class="kpi-intel-state good">Improved</span>'
      : data.improved === false
        ? '<span class="kpi-intel-state bad">Worsened</span>'
        : '<span class="kpi-intel-state neutral">No directional result</span>';

    const drivers = data.drivers || [];
    const rows = drivers.map(driver => {
      const drill = driver.drill || {};
      const canDrill = drill.module && (drill.id != null || drill.record);
      const drillButton = canDrill
        ? `<button class="btn small kpi-intel-drill" data-module="${esc(drill.module)}" data-id="${esc(drill.id ?? '')}" data-record="${esc(drill.record ?? '')}">Open</button>`
        : '—';
      return `<tr>
        <td><strong>${esc(driver.label || driver.kind || 'Evidence')}</strong><br><small>${esc(driver.kind || '')}</small></td>
        <td>${formatMagnitude(driver.magnitude, driver.unit)}</td>
        <td><span class="kpi-intel-attribution">${esc(driver.attribution || 'evidence')}</span></td>
        <td>${drillButton}</td>
      </tr>`;
    }).join('');

    return `<div class="kpi-intelligence-modal">
      <div class="kpi-intel-summary-grid">
        <div><span>Current</span><strong>${formatValue(data.value, data.unit)}</strong></div>
        <div><span>Previous</span><strong>${formatValue(data.previous_value, data.unit)}</strong></div>
        <div><span>Delta</span><strong>${data.delta == null ? '—' : formatValue(data.delta, data.unit)}</strong></div>
        <div><span>Direction</span><strong>${movement}</strong></div>
      </div>
      <div class="kpi-intel-narrative">${esc(data.summary || 'No comparison summary available.')}</div>
      <h3 class="kpi-intel-heading">Measured contributors</h3>
      ${drivers.length
        ? `<div class="table-wrap"><table class="data-table kpi-intel-table"><thead><tr><th>Evidence</th><th>Magnitude</th><th>Attribution</th><th>Drill</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<div class="kpi-intel-empty">No measured contributors are available for this metric in the current window.</div>'}
      <p class="kpi-intel-footnote">${esc(data.disclaimer || 'Drivers are evidence; correlation is not asserted as cause.')}</p>
    </div>`;
  }

  async function drillTo(button) {
    const module = button.dataset.module;
    const id = Number(button.dataset.id || 0);
    closeModal();

    if (module === 'assets' && id && typeof assetDetail === 'function') {
      await go('assets');
      await assetDetail(id);
      return;
    }
    if (module === 'work' && id && typeof workDetail === 'function') {
      await go('work');
      await workDetail(id);
      return;
    }
    if (module && typeof go === 'function') await go(module);
  }

  function bindDrills() {
    document.querySelectorAll('#modal-body .kpi-intel-drill').forEach(button => {
      button.addEventListener('click', () => drillTo(button).catch(error => toast(error.message)));
    });
  }

  async function openIntelligence(meta, mode) {
    const requestId = `kpi-intel-${++requestSequence}`;
    const title = mode === 'trend' ? `${meta.label} — Trend` : `${meta.label} — WHY`;
    openModal(title, '<div class="kpi-intel-loading">Loading measured KPI intelligence…</div>');
    const body = document.querySelector('#modal-body');
    if (!body) return;
    body.dataset.kpiIntelRequest = requestId;

    try {
      const path = mode === 'trend'
        ? `/api/kpi/trend?${queryFor(meta, 6)}`
        : `/api/kpi/explanation?${queryFor(meta)}`;
      const data = await api(path);
      if (body.dataset.kpiIntelRequest !== requestId) return;
      body.innerHTML = mode === 'trend' ? trendBody(data, meta) : explanationBody(data);
      if (mode === 'why') bindDrills();
    } catch (error) {
      if (body.dataset.kpiIntelRequest !== requestId) return;
      body.innerHTML = `<div class="kpi-intel-empty">${esc(error.message || 'KPI intelligence is unavailable.')}</div>`;
    }
  }

  function actionButton(label, ariaLabel, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'kpi-intel-btn';
    button.textContent = label;
    button.setAttribute('aria-label', ariaLabel);
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      handler();
    });
    return button;
  }

  function decorateIntelligence() {
    if (!executiveDashboardVisible()) return;
    const grid = content.querySelector('.dashboard-kpi-grid');
    if (!grid) return;

    [...grid.children].forEach(card => {
      if (!card.classList.contains('kpi') || card.dataset.kpiIntelligence) return;
      const label = cardLabel(card);
      const spec = METRICS[label];
      if (!spec) return;
      if (spec.portfolioOnly && typeof S !== 'undefined' && S.siteId) return;

      const meta = {...spec, label};
      card.dataset.kpiIntelligence = `${spec.family}/${spec.metric}`;
      card.classList.add('kpi-has-intelligence');

      const actions = document.createElement('div');
      actions.className = 'kpi-intel-actions';
      actions.setAttribute('aria-label', `${label} intelligence actions`);
      actions.append(
        actionButton('Trend', `Show ${label} trend`, () => openIntelligence(meta, 'trend')),
        actionButton('WHY', `Explain ${label} change`, () => openIntelligence(meta, 'why'))
      );
      card.append(actions);
    });
  }

  new MutationObserver(() => queueMicrotask(decorateIntelligence))
    .observe(content, {childList: true, subtree: true});
  decorateIntelligence();
})();