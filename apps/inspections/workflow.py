from __future__ import annotations


def inspection_result(responses: list[dict]) -> str:
    return 'Fail' if any((row.get('response') or 'N/A') == 'Fail' for row in responses) else 'Pass'


def corrective_required(result: str, create_corrective_on_fail: bool) -> bool:
    return result == 'Fail' and bool(create_corrective_on_fail)
