"""Shared offset-pagination helper (Part A).

Cursor params stay the source of truth on every list endpoint; when the
caller passes ``page`` the repo switches to OFFSET/LIMIT mode and returns
``{data, page, page_size, total, total_pages}``. ``page`` is 1-based,
``page_size`` is clamped to 1..100 upstream in deps (defaults 1 / 20).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional


def clamp_page_size(raw: Any, default: int = 20) -> int:
    try:
        size = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(100, size))


def coerce_page(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        page = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("page must be an integer >= 1")
    if page < 1:
        raise ValueError("page must be an integer >= 1")
    return page


PageBody = dict[str, Any]


def offset_payload(
    rows: list[dict[str, Any]],
    total: int,
    page: int,
    page_size: int,
    map_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> PageBody:
    data = [map_row(r) for r in rows] if map_row else rows
    return {
        "data": data,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": math.ceil(total / page_size) if total else 0,
    }
