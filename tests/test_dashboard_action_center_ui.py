from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'static' / 'index.html'
SCRIPT = ROOT / 'static' / 'dashboard-action-center.js'
STYLE = ROOT / 'static' / 'dashboard-action-center.css'
SW = ROOT / 'static' / 'sw.js'


def test_executive_action_center_loads_in_dashboard_shell_and_offline_cache():
    index = INDEX.read_text(encoding='utf-8')
    service_worker = SW.read_text(encoding='utf-8')
    script_path = '/static/dashboard-action-center.js'
    style_path = '/static/dashboard-action-center.css'

    assert script_path in index
    assert style_path in index
    assert index.index('/static/dashboard-attention.js') < index.index(script_path)
    assert script_path in service_worker
    assert style_path in service_worker
    assert "const CACHE='euas-shell-v3.9.0-ui12'" in service_worker
    assert STYLE.read_text(encoding='utf-8').strip()


def test_executive_action_center_renders_only_canonical_server_owned_intelligence():
    script = SCRIPT.read_text(encoding='utf-8')

    for endpoint in (
        '/api/kpi/backlog/risk?',
        '/api/kpi/parts/shortages?',
        '/api/kpi/pm-risk?',
    ):
        assert endpoint in script

    # These are server-computed fields. The browser only renders them and does
    # not create a parallel risk, shortage or capacity formula.
    for field in (
        'risk_weighted_backlog',
        'risk_score',
        'blocked_high_risk',
        'outstanding_short',
        'critical_pm_in_overloaded_weeks',
        'capacity_state',
        'utilization_pct',
    ):
        assert field in script

    assert 'Promise.allSettled' in script
    assert "params.set('site_id', siteId)" in script
    assert "params.set('period_end', periodEnd)" in script
    assert 'innerHTML' not in script
    assert 'insertAdjacentHTML' not in script
    assert '/api/dashboard' not in script
    assert 'eval(' not in script
