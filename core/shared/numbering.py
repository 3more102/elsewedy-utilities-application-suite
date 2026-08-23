from __future__ import annotations


def next_no(conn, table: str, field: str, prefix: str, start: int = 1) -> str:
    """Generate the next repository-style prefixed sequence value."""
    values = [
        row[0]
        for row in conn.execute(
            f"SELECT {field} FROM {table} WHERE {field} LIKE ?", (prefix + '%',)
        ).fetchall()
    ]
    numbers = []
    for value in values:
        try:
            numbers.append(int(str(value).replace(prefix, '')))
        except (TypeError, ValueError):
            pass
    number = max(numbers, default=start - 1) + 1
    return f'{prefix}{number}'
