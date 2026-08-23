from apps.inspections import corrective_required, inspection_result

def test_inspection_workflow_policy():
    assert inspection_result([{'response':'Pass'},{'response':'N/A'}]) == 'Pass'
    assert inspection_result([{'response':'Pass'},{'response':'Fail'}]) == 'Fail'
    assert corrective_required('Fail', True)
    assert not corrective_required('Fail', False)
