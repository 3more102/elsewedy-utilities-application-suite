"""Backward-compatible database import surface.

New code should import shared database infrastructure from ``core.database``.
"""
from core.database import *
from core.database import _pg_insert_or_ignore, _pg_sql, _postgresize_schema
