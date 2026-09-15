"""
safegate.core.ids
=================

Deterministic content addressing.

Rule: an ID is a pure function of semantic content. No clocks, no uuid4,
no autoincrement. Two engineers authoring the same hazard in different
branches must produce the same node ID, or the merge destroys traceability.

Canonicalisation is JSON with sorted keys, no whitespace, NFC-normalised
strings, and floats rendered via repr() to avoid platform drift.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

DIGEST_BYTES = 32
ID_PREFIX_LEN = 24  # 96 bits of the digest, hex-encoded


def _canon(obj: Any) -> Any:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, float):
        # repr round-trips exactly in CPython >= 3.1 and is stable across
        # platforms for IEEE-754 doubles.
        return {"__f__": repr(obj)}
    if isinstance(obj, dict):
        return {str(_canon(k)): _canon(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    return obj


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        _canon(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(obj: Any) -> str:
    """Full hex SHA-256 over the canonical encoding."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def content_id(kind: str, fields: dict[str, Any]) -> str:
    """Namespaced, truncated content ID: e.g. 'hazard:3f2a91...'.

    Truncation to 96 bits is safe here: the namespace is a single
    project's design data (10^4-10^6 nodes), so collision probability is
    ~10^-17. Full digests are retained in the evidence store.
    """
    return f"{kind}:{digest({'kind': kind, 'fields': fields})[:ID_PREFIX_LEN]}"


def merkle_root(digests: list[str]) -> str:
    """Binary Merkle root over a list of hex digests.

    Used to reduce a whole campaign to one 32-byte commitment that can be
    signed once and checked by an auditor without re-hashing terabytes of
    rosbags.
    """
    if not digests:
        return hashlib.sha256(b"").hexdigest()
    layer = [bytes.fromhex(d) for d in sorted(digests)]
    while len(layer) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(layer), 2):
            left = layer[i]
            right = layer[i + 1] if i + 1 < len(layer) else left
            nxt.append(hashlib.sha256(b"\x01" + left + right).digest())
        layer = nxt
    return layer[0].hex()


__all__ = ["canonical_bytes", "content_id", "digest", "merkle_root"]
