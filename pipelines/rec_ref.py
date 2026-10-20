"""The recommendation token's mint, in one place.

The token (`rec_clicks.rec_ref`) is minted by whoever SERVES a
recommendation, and it has to be the SAME token on both sides: the
serving side writes the click row, and the merchant's order upload
carries the token back. A deterministic mint (uuid v5 over the
recommendation's identity) is what makes a repeat serve land on the same
row instead of raising - "the staff sent this twice" is normal.

So the mint lives in one module, in data's ground, and the serving side
mirrors this file byte-for-byte (the same rule as every other mirrored
module: copy it with the files it reads at import, never fork it). A
second implementation in the serving repo is a second answer to "what is
this token", and the two answers would meet on the same order line.

The namespace is derived, not typed: uuid v5 over the RFC 4122 DNS
namespace and the name `crm-cdp/rec_ref`. Change it and every token
already in flight stops resolving - which is the same failure as
deleting the click rows, so it is not a thing to do quietly.
"""

from __future__ import annotations

import uuid

#: The RFC 4122 DNS namespace. The VALUE is in flight (tokens minted with
#: it are in the dev cluster), so it does not move: only the name was
#: wrong - it said URL while carrying the DNS namespace, and a reader who
#: "corrected" the constant to match the name would re-mint every token.
NAMESPACE_DNS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

#: The token namespace: derived once, so both sides derive the same one.
NAMESPACE_REC_REF = uuid.uuid5(NAMESPACE_DNS, "crm-cdp/rec_ref")


def mint_rec_ref(batch_id: str, customer_id: str, product_id: str,
                 rank: int) -> str:
    """The token for one served recommendation.

    The identity of a recommendation is (batch, customer, product, rank):
    the same customer can be recommended the same product twice in one
    batch at two ranks, and those are two different recommendations. The
    rank is the LAST part of the identity, so a token never leaks it -
    the token is a uuid, not an integer.
    """
    name = f"{batch_id}:{customer_id}:{product_id}:{int(rank)}"
    return str(uuid.uuid5(NAMESPACE_REC_REF, name))
