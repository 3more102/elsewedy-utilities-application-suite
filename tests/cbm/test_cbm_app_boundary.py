from pathlib import Path


def test_cbm_app_does_not_import_monolithic_main():
    root = Path(__file__).resolve().parents[2]
    source = (root / 'apps' / 'cbm' / 'service.py').read_text(encoding='utf-8')
    assert 'app.main' not in source
    assert 'create_condition_work_order' in source
    assert 'condition_matches' in source
