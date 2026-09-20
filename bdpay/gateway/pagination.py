"""Cursor pagination — ``?limit=&cursor=`` in, ``{data, next_cursor}`` out."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bdpay.platform.errors import InvalidRequestError

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "build_page", "parse_pagination"]

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def parse_pagination(params: Mapping[str, str]) -> tuple[int, str | None]:
    """Validate ``limit`` (1..100, default 20) and pass the opaque cursor."""
    raw_limit = params.get("limit")
    if raw_limit is None:
        limit = DEFAULT_LIMIT
    else:
        try:
            limit = int(raw_limit, 10)
        except ValueError as exc:
            raise InvalidRequestError(
                "limit must be an integer between 1 and 100", code="invalid_limit"
            ) from exc
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidRequestError(
                "limit must be an integer between 1 and 100", code="invalid_limit"
            )
    cursor = params.get("cursor") or None
    return limit, cursor


def build_page(items: Sequence[Mapping[str, object]], next_cursor: str | None) -> dict:
    """The spec/00 §4 page envelope."""
    return {"data": list(items), "next_cursor": next_cursor}
