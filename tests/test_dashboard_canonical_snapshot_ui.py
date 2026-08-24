from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'static' / 'index.html'
SCRIPT = ROOT / 'static' / 'dashboard-canonical-snapshot.js'
SW = ROOT / 'static' / 'sw.js'


def test_canonical_snapshot_adapter_loads_before_dashboard_intelligence_and_is_cached():
    index = INDEX.read_text(encoding='utf-8')
    script_path = '/static/dashboard-canonical-snapshot.js'
    intelligence_path = '/static/dashboard-enhancements.js'

    assert script_path in index
    assert intelligence_path in index
    assert index.index(script_path) < index.index(intelligence_path)

    service_worker = SW.read_text(encoding='utf-8')
    assert "const CACHE='euas-shell-v3.9.0-ui13'" in service_worker
    assert script_path in service_worker


def test_canonical_snapshot_adapter_uses_one_authoritative_surface_without_fake_trends():
    script = SCRIPT.read_text(encoding='utf-8')

    assert '/api/kpi/executive?' in script
    assert '/api/kpi/trend' not in script
    assert 'CANONICAL_PERIOD_DAYS = 30' in script
    assert "['Open Work Orders', {path: 'maintenance.open_wo'" in script
    assert "['PM Compliance', {path: 'maintenance.pm_compliance_pct'" in script
    assert "['MTBF', {path: 'maintenance.mtbf_hours'" in script
    assert "['MTTR', {path: 'maintenance.mttr_hours'" in script
    assert "['Active Alarms', {path: 'condition.active_alarms'" in script
    assert "['Safety Incidents', {path: 'hse.open_incidents'" in script
    assert "['Maintenance Cost', {path: 'costs.maintenance_cost_window'" in script
    assert "card.dataset.canonicalSnapshot = 'true'" in script
    assert "S.cache.canonicalExecutive = snapshot" in script
