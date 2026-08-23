from pathlib import Path

from app.report_html import render_snapshot_report_html, render_work_order_report_html


ROOT = Path(__file__).resolve().parents[1]


def test_protected_report_renderers_escape_database_markup():
    attack = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    escaped = '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&lt;script&gt;alert(2)&lt;/script&gt;'

    snapshot_html = render_snapshot_report_html(
        {
            'report_no': attack,
            'title': attack,
            'generated_at': attack,
            'content_hash': attack,
        },
        {
            'asset': {
                'asset_no': attack,
                'name': attack,
                'site_name': attack,
                'location_name': attack,
                'condition': attack,
                'status': attack,
                'criticality': attack,
            },
            'work_orders': [
                {
                    'wo_no': attack,
                    'title': attack,
                    'status': attack,
                    'actual_hours': attack,
                    'actual_cost': attack,
                }
            ],
            'costs': [{'amount': 5}],
        },
    )
    work_html = render_work_order_report_html(
        {
            'wo_no': attack,
            'title': attack,
            'asset_no': attack,
            'asset_name': attack,
            'status': attack,
            'priority': attack,
            'work_type': attack,
            'assigned_to_name': attack,
            'supervisor_name': attack,
            'description': attack,
            'instructions': attack,
            'safety_requirements': attack,
            'actual_cost': 12.5,
        },
        [{'full_name': attack, 'hours': attack, 'notes': attack}],
        [{'item_no': attack, 'name': attack, 'quantity': attack, 'unit_cost': attack}],
    )

    for document in (snapshot_html, work_html):
        assert attack not in document
        assert '<script>' not in document.casefold()
        assert 'onerror="alert(1)"' not in document
        assert escaped in document
        assert '<link rel="stylesheet" href="/static/report.css">' in document

    application_source = (ROOT / 'app' / 'application.py').read_text(encoding='utf-8')
    assert 'render_snapshot_report_html(r,d)' in application_source
    assert 'render_work_order_report_html(w,labor,mats)' in application_source
    assert "html=f'''<html><head><title>{r['report_no']}" not in application_source
    assert "html=f'''<html><head><title>{w['wo_no']}" not in application_source
