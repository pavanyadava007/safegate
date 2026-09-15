"""
safegate.core.cas
=================

Evidence store: content-addressed blobs + an append-only, signed manifest
log. This is the layer that turns "we ran some tests" into "here is a
tamper-evident record an assessor can verify without trusting us".

Threat model: deliberately explicit, because this is a compliance system:

  T1. Careless mutation. An engineer re-runs a test, overwrites a log,
      and the report silently changes.
      -> Blobs are immutable and addressed by digest. Overwriting is a
         no-op; a changed file is a different address.

  T2. Selective reporting. Failing runs are quietly dropped before the
      report is generated.
      -> The manifest log is append-only and chained (each entry commits
         to the previous entry's hash). Removing an entry breaks the
         chain. The report is generated from the chain, not from a
         directory listing.

  T3. Backdating or wholesale rewrite. Evidence is fabricated after a
      field incident, or the whole manifest is regenerated without the
      failing runs and re-signed.
      -> Chaining alone cannot catch a complete rewrite: a rewritten chain
         is internally consistent. Two controls do. (a) Signatures are
         checked against public keys pinned outside the store (policy
         `trusted_signers`), so a writer without the CI signing key cannot
         produce a valid chain. (b) `anchor()` produces a commitment to the
         head that is published outside the store (signed git tag, RFC 3161
         timestamp); `verify_anchor()` detects any rewrite of the anchored
         prefix, even one signed with a trusted key.

  T4. Build/evidence mismatch. The report claims firmware X but the runs
      used firmware Y.
      -> Pinning is part of the run's content ID (see model.ConcreteRun),
         so it is inside the hash that the manifest commits to, and the
         gate and report read exactly one campaign from the chain.

Signing uses Ed25519 from `cryptography` when available and degrades to
an unsigned (but still chained) log otherwise, so the package remains
importable in minimal environments. The policy engine treats an unsigned
or self-attested chain as a `major` finding rather than silently
accepting it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ids import canonical_bytes, digest, merkle_root
from .storage import FilesystemBackend, S3Backend, open_backend

try:  # pragma: no cover - environment dependent
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _CRYPTO = True
except ImportError:  # pragma: no cover
    _CRYPTO = False


GENESIS = "0" * 64

# Entry types that attest to a finished campaign rather than record it.
ATTESTATION_TYPES = frozenset({"technical_file", "anchor"})


@dataclass(frozen=True)
class ManifestEntry:
    """One link in the append-only chain."""

    seq: int
    prev: str
    payload: dict[str, Any]
    recorded_at: str
    signature: str | None = None
    signer: str | None = None

    def body_digest(self) -> str:
        return digest(
            {
                "seq": self.seq,
                "prev": self.prev,
                "payload": self.payload,
                "recorded_at": self.recorded_at,
            }
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "prev": self.prev,
                "payload": self.payload,
                "recorded_at": self.recorded_at,
                "signature": self.signature,
                "signer": self.signer,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(line: str) -> ManifestEntry:
        d = json.loads(line)
        return ManifestEntry(
            seq=d["seq"],
            prev=d["prev"],
            payload=d["payload"],
            recorded_at=d["recorded_at"],
            signature=d.get("signature"),
            signer=d.get("signer"),
        )


class EvidenceStore:
    """Content-addressed blobs plus a chained, signed manifest.

    The integrity logic lives here; bytes live in a storage backend
    (`core/storage.py`): a directory, or `s3://bucket/prefix` for S3 and
    S3-compatible object stores.

    Filesystem layout::

        <root>/blobs/ab/cdef...        immutable content
        <root>/manifest.log            append-only chained JSONL
        <root>/keys/signing.key        optional in-store Ed25519 key (local use only)
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        signing_key: str | os.PathLike[str] | None = None,
    ) -> None:
        self.backend: FilesystemBackend | S3Backend = open_backend(root)
        self.root: Path | str = (
            self.backend.root if isinstance(self.backend, FilesystemBackend) else str(root)
        )
        self._key_path = Path(signing_key) if signing_key else None
        self._key = self._load_key()

    @property
    def location(self) -> str:
        return self.backend.location

    @property
    def manifest_path(self) -> Path:
        """Path of the manifest file (filesystem stores only)."""
        if not isinstance(self.backend, FilesystemBackend):
            raise AttributeError("manifest_path exists only for filesystem stores")
        return self.backend.manifest_path

    # ---- keys -----------------------------------------------------------
    #
    # Where the private key lives matters more than which algorithm signs.
    # A key stored inside the evidence store protects nothing against
    # anyone who can write to the store: they can rewrite the manifest and
    # re-sign it. Production use keeps the key outside the store (a CI
    # secret or an HSM) and pins the public key in the project policy
    # (`trusted_signers`). The in-store key is kept only as a convenience
    # for local runs, and the gate reports it as self-attested.

    def _load_key(self) -> Any:
        if not _CRYPTO:
            return None
        if self._key_path is not None:
            if not self._key_path.exists():
                raise FileNotFoundError(f"signing key {self._key_path} not found")
            return serialization.load_pem_private_key(
                self._key_path.read_bytes(), password=None
            )
        data = self.backend.read_file("keys/signing.key")
        if data is None:
            return None
        return serialization.load_pem_private_key(data, password=None)

    @property
    def key_in_store(self) -> bool:
        """True when entries are signed with a key kept inside the store."""
        return self._key is not None and self._key_path is None

    def generate_key(self) -> str:
        """Create an in-store signing key. Returns the public key in hex."""
        if not _CRYPTO:
            raise RuntimeError("cryptography not installed; cannot sign")
        key = Ed25519PrivateKey.generate()
        self.backend.write_file("keys/signing.key", _private_pem(key))
        self.backend.write_file("keys/signing.pub", (public_key_hex(key) + "\n").encode())
        if isinstance(self.backend, FilesystemBackend):
            (self.backend.root / "keys" / "signing.key").chmod(0o600)
        self._key = self._load_key()
        return public_key_hex(key)

    @property
    def signer_id(self) -> str | None:
        """Hex public key matching the private key this store signs with."""
        if self._key is None:
            return None
        return public_key_hex(self._key)

    # ---- blobs ----------------------------------------------------------

    def _blob_path(self, dg: str) -> Path:
        if not isinstance(self.backend, FilesystemBackend):
            raise AttributeError("blob paths exist only for filesystem stores")
        return self.backend.blob_path(dg)

    def put_bytes(self, data: bytes) -> str:
        dg = hashlib.sha256(data).hexdigest()
        self.backend.put_blob(dg, data)
        return dg

    def put_file(self, path: str | os.PathLike[str]) -> str:
        return self.put_bytes(Path(path).read_bytes())

    def put_json(self, obj: Any) -> str:
        return self.put_bytes(canonical_bytes(obj))

    def get_bytes(self, dg: str) -> bytes:
        data = self.backend.get_blob(dg)
        actual = hashlib.sha256(data).hexdigest()
        if actual != dg:
            # Bit-rot or tampering. Never return silently-wrong evidence.
            raise OSError(f"blob integrity failure: expected {dg}, got {actual}")
        return data

    def get_json(self, dg: str) -> Any:
        return json.loads(self.get_bytes(dg))

    # ---- manifest chain -------------------------------------------------

    def entries(self) -> Iterator[ManifestEntry]:
        for line in self.backend.entries():
            yield ManifestEntry.from_json(line)

    def _read_head(self) -> tuple[int, str, str | None]:
        line = self.backend.last_entry()
        if line is None:
            return -1, GENESIS, None
        e = ManifestEntry.from_json(line)
        return e.seq, e.body_digest(), line

    def head(self) -> tuple[int, str]:
        seq, prev, _ = self._read_head()
        return seq, prev

    def append(self, payload: dict[str, Any]) -> ManifestEntry:
        """Append one entry. Safe with several writers on the same store: the
        filesystem backend serialises appends under a lock, and the S3 backend
        uses conditional creates that refuse a stale sequence number."""
        recorded_at = _dt.datetime.now(_dt.UTC).isoformat()
        built: dict[str, ManifestEntry] = {}

        def build(last_line: str | None) -> tuple[int, str]:
            if last_line is None:
                seq, prev = 0, GENESIS
            else:
                last = ManifestEntry.from_json(last_line)
                seq, prev = last.seq + 1, last.body_digest()
            entry = ManifestEntry(seq=seq, prev=prev, payload=payload, recorded_at=recorded_at)
            if self._key is not None:
                entry = ManifestEntry(
                    seq=seq,
                    prev=prev,
                    payload=payload,
                    recorded_at=recorded_at,
                    signature=self._key.sign(bytes.fromhex(entry.body_digest())).hex(),
                    signer=self.signer_id,
                )
            built["entry"] = entry
            return seq, entry.to_json()

        self.backend.append(build)
        return built["entry"]

    # ---- verification ---------------------------------------------------

    def verify_chain(
        self,
        public_key_hex: str | None = None,
        trusted_keys: Iterable[str] | None = None,
    ) -> list[str]:
        """Return a list of integrity problems. Empty list == intact.

        `trusted_keys` is the set of signer public keys the verifier trusts,
        obtained out of band (project policy, not the store). When given,
        every entry must be signed by one of them. `public_key_hex` is the
        older single-key form and is treated as a one-element trust set.

        Without trusted keys, every signature that is present is still checked
        against the key the entry names. That catches corruption and edits by
        anyone who does not re-sign, but not a writer who re-signs with a key
        of their own; only pinned keys and anchors catch that.
        """
        problems: list[str] = []
        trust = {k.lower() for k in (trusted_keys or [])}
        if public_key_hex:
            trust.add(public_key_hex.lower())
        verifiers: dict[str, Any] = {}
        if trust and not _CRYPTO:
            problems.append("cryptography not installed; signatures not checked")
            trust = set()
        for k in trust:
            try:
                verifiers[k] = Ed25519PublicKey.from_public_bytes(bytes.fromhex(k))
            except Exception:  # noqa: BLE001 - a malformed key is a finding
                problems.append(f"trusted key {k[:16]}... is not a valid Ed25519 key")

        self_keys: dict[str, Any] = {}
        expected_seq, expected_prev = 0, GENESIS
        any_entry = False
        for e in self.entries():
            any_entry = True
            if e.seq != expected_seq:
                problems.append(f"seq gap at {e.seq}: expected {expected_seq}")
            if e.prev != expected_prev:
                problems.append(f"chain break at seq {e.seq}")
            signer = (e.signer or "").lower()
            if verifiers:
                if not e.signature:
                    problems.append(f"entry {e.seq} unsigned")
                elif signer not in verifiers:
                    problems.append(
                        f"entry {e.seq} signed by untrusted key {signer[:16] or 'unknown'}..."
                    )
                elif not _signature_ok(verifiers[signer], e):
                    problems.append(f"entry {e.seq} bad signature")
            elif e.signature and _CRYPTO:
                key = self_keys.get(signer)
                if key is None:
                    try:
                        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer))
                    except Exception:  # noqa: BLE001 - a malformed signer id is a finding
                        key = False
                    self_keys[signer] = key
                if key is False or not _signature_ok(key, e):
                    problems.append(f"entry {e.seq} bad signature")
            expected_seq = e.seq + 1
            expected_prev = e.body_digest()
        if not any_entry:
            problems.append("empty manifest")
        return problems

    # ---- campaigns ------------------------------------------------------

    def campaign_ids(self) -> list[str]:
        """Campaign ids in the order their campaign_start entries appear."""
        out: list[str] = []
        for e in self.entries():
            if e.payload.get("type") == "campaign_start":
                cid = e.payload.get("campaign_id")
                if cid and cid not in out:
                    out.append(cid)
        return out

    def campaign_entries(self, campaign_id: str) -> list[ManifestEntry]:
        """Evidence entries of one campaign, excluding later attestations.

        Attestations (a technical file, an anchor) are appended after the
        campaign ends and commit to its root. If they were part of the root
        themselves, writing the report would change the number printed in
        the report, and nobody could ever recompute it.
        """
        return [
            e
            for e in self.entries()
            if e.payload.get("campaign_id") == campaign_id
            and e.payload.get("type") not in ATTESTATION_TYPES
        ]

    def campaign_root(self, campaign_id: str) -> str:
        """Merkle root over every evidence entry belonging to one campaign.

        This single hex string is what goes on the cover sheet of the
        technical file.
        """
        return merkle_root([e.body_digest() for e in self.campaign_entries(campaign_id)])

    def anchor(self, campaign_id: str) -> dict[str, Any]:
        """Produce an anchorable commitment.

        Publish the result somewhere the store's writers cannot rewrite: a
        signed git tag, an RFC 3161 timestamp, or the release notes. A later
        `verify_anchor` then detects a rewritten chain even when every
        entry in the rewrite is correctly linked and signed.
        """
        seq, head = self.head()
        return {
            "campaign_id": campaign_id,
            "merkle_root": self.campaign_root(campaign_id),
            "head_seq": seq,
            "head": head,
            "at": _dt.datetime.now(_dt.UTC).isoformat(),
        }

    def verify_anchor(self, anchor: dict[str, Any]) -> list[str]:
        """Check that the chain still contains the anchored state."""
        problems: list[str] = []
        want_seq = int(anchor["head_seq"])
        found = None
        for e in self.entries():
            if e.seq == want_seq:
                found = e
                break
        if found is None:
            problems.append(f"anchored entry seq {want_seq} is missing")
        elif found.body_digest() != anchor["head"]:
            problems.append(
                f"entry at anchored seq {want_seq} differs from the anchor; "
                "the chain was rewritten after it was anchored"
            )
        cid = anchor.get("campaign_id")
        if cid and "merkle_root" in anchor:
            root = self.campaign_root(cid)
            if root != anchor["merkle_root"]:
                problems.append(
                    f"campaign {cid} root {root[:16]}... differs from anchored "
                    f"{str(anchor['merkle_root'])[:16]}..."
                )
        return problems


# --------------------------------------------------------------------------
# Key helpers
# --------------------------------------------------------------------------


def _signature_ok(public_key: Any, entry: ManifestEntry) -> bool:
    try:
        public_key.verify(bytes.fromhex(entry.signature or ""), bytes.fromhex(entry.body_digest()))
    except Exception:  # noqa: BLE001 - any failure is a bad signature
        return False
    return True


def _private_pem(key: Any) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def write_keypair(directory: str | os.PathLike[str]) -> str:
    """Write `signing.key` (PKCS8 PEM) and `signing.pub` (hex). Returns hex."""
    if not _CRYPTO:
        raise RuntimeError("cryptography not installed; cannot sign")
    key_dir = Path(directory)
    key_dir.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    priv = key_dir / "signing.key"
    priv.write_bytes(_private_pem(key))
    priv.chmod(0o600)
    pub = public_key_hex(key)
    (key_dir / "signing.pub").write_text(pub + "\n")
    return pub


def public_key_hex(private_key: Any) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


__all__ = [
    "ATTESTATION_TYPES",
    "GENESIS",
    "EvidenceStore",
    "ManifestEntry",
    "public_key_hex",
    "write_keypair",
]
