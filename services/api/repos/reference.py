"""Reference-data lookups over the `reference_values` table.

Single choke point for vocabulary validation: routers call require_active()
so unknown codes fail with a 422 whose detail names the domain. The campaign
status machine (repos/campaigns.py) derives legal transitions from the
sort_rank order of the campaign_status rows.
"""

from __future__ import annotations


class UnknownCodeError(Exception):
    """A code is not an active member of its reference domain."""

    def __init__(self, domain: str, value: str):
        self.domain = domain
        self.value = value
        super().__init__(f"unknown {domain}: {value!r}")


async def list_domains(conn) -> list[str]:  # type: ignore[no-untyped-def]
    rows = await conn.fetch(
        "SELECT DISTINCT domain FROM reference_values ORDER BY domain"
    )
    return [r["domain"] for r in rows]


async def list_values(conn, domain: str) -> list[dict]:  # type: ignore[no-untyped-def]
    """Active codes for one domain, ranked (sort_rank, code)."""
    rows = await conn.fetch(
        "SELECT domain, code, label_en, label_zh, sort_rank"
        " FROM reference_values WHERE domain = $1 AND is_active"
        " ORDER BY sort_rank, code",
        domain,
    )
    return [dict(r) for r in rows]


async def domain_exists(conn, domain: str) -> bool:  # type: ignore[no-untyped-def]
    return (
        await conn.fetchval(
            "SELECT 1 FROM reference_values WHERE domain = $1 LIMIT 1", domain
        )
        is not None
    )


async def is_active(conn, domain: str, code: str) -> bool:  # type: ignore[no-untyped-def]
    return (
        await conn.fetchval(
            "SELECT 1 FROM reference_values"
            " WHERE domain = $1 AND code = $2 AND is_active",
            domain,
            code,
        )
        is not None
    )


async def require_active(conn, domain: str, code: str) -> None:  # type: ignore[no-untyped-def]
    """Raise UnknownCodeError unless `code` is active in `domain`."""
    if not await is_active(conn, domain, code):
        raise UnknownCodeError(domain, code)


async def ordered_codes(conn, domain: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Active codes in machine order (sort_rank, code). Used by the campaign
    status machine: legal moves are same-rank (no-op) or next-rank-up."""
    rows = await conn.fetch(
        "SELECT code FROM reference_values"
        " WHERE domain = $1 AND is_active ORDER BY sort_rank, code",
        domain,
    )
    return [r["code"] for r in rows]
