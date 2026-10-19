"""Coded row rejections: a quarantine row's `reason` is for a human, `code` for a machine.

The code set is deliberately small and closed in practice: a validator that
rejects a row names *why* in one stable token, so a run summary can count rows
per reason (`{"ordered_at_unparseable": 12}`) instead of asking a reader to
grep free text. Anything that does not name a code lands as `other` -- visible
as an unclassified bucket rather than silently merged into a plausible one.

Subclasses keep the old exception types (`ValueError` / `LookupError`) so every
existing `except` clause and test continues to behave identically; only the
`.code` attribute is new.
"""
from __future__ import annotations


class RowRejected(ValueError):
    """A staged row cannot be normalised or applied; `code` says why."""

    code = "other"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class RowMissing(LookupError):
    """A row references something that is not there; `code` says which."""

    code = "missing"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


def code_of(exc: BaseException) -> str:
    """The reason code for a rejection; `other` when the site has none yet."""
    return getattr(exc, "code", None) or "other"
