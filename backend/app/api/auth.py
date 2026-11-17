"""The demo token (phase 3, "Public exposure"): uploads and decisions need it, reads do not.

- Sent as `Authorization: Bearer <token>` (docs/decisions.md names no header).
- The server holds only sha256 hex digests (DEMO_TOKEN_HASHES), never a token: the
  presented token is hashed and compared with every configured hash in constant time.
- What gets recorded is the hash (as `demo_token_hash`, for the per-token LLM sub-cap) or
  its short fingerprint `demo:<12 hex>` (the reviewer id in approvals and the audit chain),
  never the token itself.
- Fails closed: no configured hash means no valid token, so every gated request is 401.
"""

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header

from app.api.common import get_app_settings
from app.config import Settings

# Longer than any token we hand out; anything beyond it is refused before hashing.
TOKEN_MAX = 256
FINGERPRINT_LEN = 12


class DemoTokenRejected(Exception):
    """Missing, malformed or unknown token: answered with 401 by the app's handler."""


@dataclass(frozen=True)
class DemoToken:
    hash: str
    reviewer: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def reviewer_id(digest: str) -> str:
    return f"demo:{digest[:FINGERPRINT_LEN]}"


def bearer_token(authorization: str | None) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, else None."""
    if authorization is None:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > TOKEN_MAX:
        return None
    return parts[1]


def match_token(token: str, hashes: Iterable[str]) -> str | None:
    """The configured hash `token` hashes to, or None. Every hash is compared, with
    hmac.compare_digest, so the time taken says nothing about which one (if any) matched."""
    presented = token_hash(token)
    matched: str | None = None
    for digest in hashes:
        if hmac.compare_digest(presented, digest):
            matched = digest
    return matched


def require_demo_token(
    settings: Annotated[Settings, Depends(get_app_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> DemoToken:
    """FastAPI dependency for the gated endpoints; raises DemoTokenRejected."""
    token = bearer_token(authorization)
    digest = None if token is None else match_token(token, settings.demo_token_hash_list)
    if digest is None:
        raise DemoTokenRejected()
    return DemoToken(hash=digest, reviewer=reviewer_id(digest))
