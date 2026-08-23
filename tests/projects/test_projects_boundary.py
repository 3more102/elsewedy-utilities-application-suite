import pytest
from apps.projects import InvalidProjectTask, normalize_task_changes


def test_project_task_policy_normalizes_completion_and_rejects_unknown_status():
    assert normalize_task_changes({'status':'Completed'}) == {'status':'Completed','progress':100}
    assert normalize_task_changes({'status':'In Progress','progress':35}) == {'status':'In Progress','progress':35}
    with pytest.raises(InvalidProjectTask):
        normalize_task_changes({'status':'Unknown'})
