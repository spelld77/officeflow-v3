from __future__ import annotations


def fts_prefix_query(value: str) -> str:
    """Build a safe FTS5 AND query with prefix matching for each user term."""
    terms = value.strip().split()
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
