"""SAIDI/SAIFI/CAIDI foundation: customer-impact capture on outages,
customers-served configuration, index math and KPI integration."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def _ncs_site_and_asset(client, headers):
    ref = client.get('/api/reference', headers=headers).json()
    site = next(s for s in ref['sites'] if s['site_code'] == 'NCS-01')
    from app.database import db
    with db() as conn:
        return site, conn.execute("SELECT id FROM assets WHERE asset_no='TR-001'").fetchone()['id']


def _create_closed_outage(client, headers, asset_id, *, start, end, customers=None):
    made = client.post('/api/outages', headers=headers, json={
        'asset_id': asset_id, 'outage_type': 'Forced', 'cause_code': 'KPI-REG',
        'impact': 'Regression outage', 'start_at': start,
        **({'customers_interrupted': customers} if customers is not None else {})})
    assert made.status_code == 200, made.text
    closed = client.post(f"/api/outages/{made.json()['id']}/close", headers=headers,
                         json={'end_at': end})
    assert closed.status_code == 200, closed.text
    return made.json()['id']


def test_customers_served_configuration_roundtrip_and_permissions():
    with TestClient(app) as client:
        admin = auth(client)
        tech = auth(client, 'tech1', 'Tech@2026')
        site, _asset = _ncs_site_and_asset(client, admin)

        denied = client.put('/api/reliability/customers', headers=tech,
                            json={'site_id': site['id'], 'customers_served': 1000})
        assert denied.status_code == 403

        put = client.put('/api/reliability/customers', headers=admin,
                         json={'site_id': site['id'], 'customers_served': 1000})
        assert put.status_code == 200, put.text
        assert put.json()['customers_served'] == 1000

        # Update overwrites rather than duplicating.
        put2 = client.put('/api/reliability/customers', headers=admin,
                          json={'site_id': site['id'], 'customers_served': 2000})
        assert put2.status_code == 200
        listing = client.get('/api/reliability/customers', headers=admin).json()['sites']
        matches = [x for x in listing if x['site_id'] == site['id']]
        assert len(matches) == 1 and matches[0]['customers_served'] == 2000
        # Restore a clean figure for the computation tests.
        client.put('/api/reliability/customers', headers=admin,
                   json={'site_id': site['id'], 'customers_served': 1000})


def test_index_math_saidi_saifi_caidi():
    with TestClient(app) as client:
        admin = auth(client)
        site, asset = _ncs_site_and_asset(client, admin)
        day = date.today() - timedelta(days=1)
        # 500 customers off for exactly 4 hours => SAIDI 2h, SAIFI 0.5, CAIDI 4h @ 1000 customers.
        _create_closed_outage(client, admin, asset,
                              start=f'{day.isoformat()}T08:00:00',
                              end=f'{day.isoformat()}T12:00:00', customers=500)

        report = client.get('/api/reliability/indices', headers=admin,
                            params={'site_id': site['id'], 'period_days': 30}).json()
        assert report['data_quality']['customers_served_configured'] is True
        assert report['saidi'] == 2.0
        assert report['saifi'] == 0.5
        assert report['caidi'] == 4.0
        codes = [o['outage_no'] for o in report['sustained_outages']]
        assert len(codes) == 1


def test_momentary_outage_excluded_from_sustained_indices():
    with TestClient(app) as client:
        admin = auth(client)
        site, asset = _ncs_site_and_asset(client, admin)
        before = client.get('/api/reliability/indices', headers=admin,
                            params={'site_id': site['id'], 'period_days': 30}).json()
        # 3-minute blip: below the IEEE 1366 sustained threshold.
        _create_closed_outage(client, admin, asset,
                              start=f'{(date.today()-timedelta(days=1)).isoformat()}T22:00:00',
                              end=f'{(date.today()-timedelta(days=1)).isoformat()}T22:03:00',
                              customers=900)
        after = client.get('/api/reliability/indices', headers=admin,
                           params={'site_id': site['id'], 'period_days': 30}).json()
        assert after['saidi'] == before['saidi']
        assert after['saifi'] == before['saifi']


def test_missing_customer_impact_is_reported_not_fabricated():
    with TestClient(app) as client:
        admin = auth(client)
        site, asset = _ncs_site_and_asset(client, admin)
        day = date.today() - timedelta(days=2)
        _create_closed_outage(client, admin, asset,
                              start=f'{day.isoformat()}T06:00:00',
                              end=f'{day.isoformat()}T09:00:00', customers=None)
        report = client.get('/api/reliability/indices', headers=admin,
                            params={'site_id': site['id'], 'period_days': 30}).json()
        assert report['data_quality']['outages_missing_customer_impact'] >= 1


def test_unconfigured_scope_returns_none_values():
    with TestClient(app) as client:
        admin = auth(client)
        ref = client.get('/api/reference', headers=admin).json()
        unconfigured = next(s for s in ref['sites'] if s['site_code'] == 'ALX-OPS')
        report = client.get('/api/reliability/indices', headers=admin,
                            params={'site_id': unconfigured['id'], 'period_days': 90}).json()
        assert report['saidi'] is None and report['saifi'] is None and report['caidi'] is None
        assert report['data_quality']['customers_served_configured'] is False


def test_saidi_saifi_caidi_kpi_definitions_match_indices_endpoint():
    with TestClient(app) as client:
        admin = auth(client)
        site, asset = _ncs_site_and_asset(client, admin)
        kpis = {k['code']: k for k in client.get('/api/kpis', headers=admin).json()['kpis']}
        assert {'KPI-SAIDI', 'KPI-SAIFI', 'KPI-CAIDI'} <= set(kpis)

        day = date.today() - timedelta(days=3)
        _create_closed_outage(client, admin, asset,
                              start=f'{day.isoformat()}T07:00:00',
                              end=f'{day.isoformat()}T11:30:00', customers=250)

        saidi_snap = client.post(f"/api/kpis/{kpis['KPI-SAIDI']['id']}/recalculate",
                                 headers=admin, json={'as_of': date.today().isoformat()}).json()['snapshot']
        # The seeded definition has a 365-day window; mirror it on the endpoint.
        report = client.get('/api/reliability/indices', headers=admin,
                            params={'site_id': site['id'], 'period_days': 365}).json()
        # KPI is unscoped (whole utility), so compare against the endpoint's own
        # KPI-window computation instead of the site-scoped report: recalculate a
        # site-scoped twin definition for an exact match.
        twin = client.post('/api/kpis', headers=admin, json={
            'code': 'KPI-TEST-SAIDI-NCS', 'name': 'SAIDI NCS scoped',
            'source_key': 'saidi', 'scope': {'site_id': site['id']},
            'time_window_days': 365}).json()
        twin_snap = client.post(f"/api/kpis/{twin['id']}/recalculate",
                                headers=admin, json={'as_of': date.today().isoformat()}).json()['snapshot']
        assert twin_snap['value'] == report['saidi']
        assert twin_snap['value'] is not None
        assert saidi_snap['status'] in ('GREEN', 'AMBER', 'RED', 'UNKNOWN')

        saifi_snap = client.post(f"/api/kpis/{kpis['KPI-SAIFI']['id']}/recalculate",
                                 headers=admin, json={'as_of': date.today().isoformat()}).json()['snapshot']
        assert saifi_snap['numerator'] >= 750  # 500 + 250 interrupted customers recorded
