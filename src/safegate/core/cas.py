"""
safegate.core.cas
=================

Evidence store: content-addressed blobs + an append-only, signed manifest
log. This is the layer that turns "we ran some tests" into "here is a
tamper-evident record an assessor can verify without trusting us".

Threat model — deliberately explicit, because this is a compliance system:

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

  T3. Backdating. Evidence is fabricated after a field incident.
      -> Entries are signed and chained; anchoring the head hash to an
         external timestamp authority (RFC 3161) or a git tag bounds
         when the chain existed. Hook provided via `anchor()`.

  T4. Build/evidence mismatch. The report claims firmware X but the runs
      used firmware Y.
      -> Pinning is part of the run's content ID (see model.ConcreteRun),
         so it is inside the hash that the manifest commits to.

Signing uses Ed25519 from `cryptography` when available and degrades to
an unsigned (but still chained) log otherwise, so the package remains
importable in minimal environments. The policy engine treats an unsigned
chain as a `major` finding rather than silently accepting it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .ids import canonical_bytes, digest, merkle_root

try:  # pragma: no cover - environment dependent
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _CRYPTO = True
except Exception:  # pragma: no cover
    _CRYPTO = False


GENESIS = "0" * 64


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
    def from_json(line: str) -> "ManifestEntry":
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
    """Filesystem-backed CAS. Swap for S3/GCS by replacing `_blob_path`.

    Layout::

        <root>/blobs/ab/cdef...        immutable content
        <root>/manifest.log            append-only chained JSONL
        <root>/keys/signing.key        optional Ed25519 private key
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.log"
        self.manifest_path.touch(exist_ok=True)
        self._key = self._load_key()

    # ---- keys -----------------------------------------------------------

    def _load_key(self) -> Any:
        key_path = self.root / "keys" / "signing.key"
        if not (_CRYPTO and key_path.exists()):
            return None
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    def generate_key(self) -> str:
        """Create a signing key. Returns the public key in hex."""
        if not _CRYPTO:
            raise RuntimeError("cryptography not installed; cannot sign")
        key_dir = self.root / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        (key_dir / "signing.key").write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        (key_dir / "signing.pub").write_text(pub.hex())
        self._key = key
        return pub.hex()

    @property
    def signer_id(self) -> str | None:
        pub_path = self.root / "keys" / "signing.pub"
        return pub_path.read_text().strip() if pub_path.exists() else None

    # ---- blobs ----------------------------------------------------------

    def _blob_path(self, dg: str) -> Path:
        return self.root / "blobs" / dg[:2] / dg[2:]

    def put_bytes(self, data: bytes) -> str:
        dg = hashlib.sha256(data).hexdigest()
        p = self._blob_path(dg)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)  # atomic
            p.chmod(0o444)  # read-only: mutation must be conscious
        return dg

    def put_file(self, path: str | os.PathLike[str]) -> str:
        src = Path(path)
        h = hashlib.sha256()
        with src.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        dg = h.hexdigest()
        dst = self._blob_path(dg)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
            dst.chmod(0o444)
        return dg

    def put_json(self, obj: Any) -> str:
        return self.put_bytes(canonical_bytes(obj))

    def get_bytes(self, dg: str) -> bytes:
        p = self._blob_path(dg)
        if not p.exists():
            raise KeyError(f"blob {dg} not found")
        data = p.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != dg:
            # Bit-rot or tampering. Never return silently-wrong evidence.
            raise IOError(f"blob integrity failure: expected {dg}, got {actual}")
        return data

    def get_json(self, dg: str) -> Any:
        return json.loads(self.get_bytes(dg))

    # ---- manifest chain -------------------------------------------------

    def entries(self) -> Iterator[ManifestEntry]:
        with self.manifest_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield ManifestEntry.from_json(line)

    def head(self) -> tuple[int, str]:
        seq, prev = -1, GENESIS
        for e in self.entries():
            seq, prev = e.seq, e.body_digest()
        return seq, prev

    def append(self, payload: dict[str, Any]) -> ManifestEntry:
        seq, prev = self.head()
        entry = ManifestEntry(
            seq=seq + 1,
            prev=prev,
            payload=payload,
            recorded_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        )
        sig = None
        if self._key is not None:
            sig = self._key.sign(bytes.fromhex(entry.body_digest())).hex()
        entry = ManifestEntry(
            seq=entry.seq,
            prev=entry.prev,
            payload=entry.payload,
            recorded_at=entry.recorded_at,
            signature=sig,
            signer=self.signer_id,
        )
        with self.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    # ---- verification ---------------------------------------------------

    def verify_chain(self, public_key_hex: str | None = None) -> list[str]:
        """Return a list of integrity problems. Empty list == intact."""
        problems: list[str] = []
        pub: Any = None
        if public_key_hex and _CRYPTO:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))

        expected_seq, expected_prev = 0, GENESIS
        any_entry = False
        for e in self.entries():
            any_entry = True
            if e.seq != expected_seq:
                problems.append(f"seq gap at {e.seq}: expected {expected_seq}")
            if e.prev != expected_prev:
                problems.append(f"chain break at seq {e.seq}")
            if pub is not None:
                if not e.signature:
                    problems.append(f"entry {e.seq} unsigned")
                else:
                    try:
                        pub.verify(
                            bytes.fromhex(e.signature),
                            bytes.fromhex(e.body_digest()),
                        )
                    except Exception:
                        problems.append(f"entry {e.seq} bad signature")
            expected_seq = e.seq + 1
            expected_prev = e.body_digest()
        if not any_entry:
            problems.append("empty manifest")
        return problems

    def campaign_root(self, campaign_id: str) -> str:
        """Merkle root over every entry belonging to one campaign.

        This single hex string is what goes on the signed cover sheet of
        the technical file.
        """
        ds = [
            e.body_digest()
            for e in self.entries()
            if e.payload.get("campaign_id") == campaign_id
        ]
        return merkle_root(ds)

    def anchor(self, campaign_id: str) -> dict[str, str]:
        """Produce an anchorable commitment.

        In production, submit `root` to an RFC 3161 TSA or commit it in a
        signed git tag. Included here so the integration point is explicit
        rather than an afterthought.
        """
        return {
            "campaign_id": campaign_id,
            "merkle_root": self.campaign_root(campaign_id),
            "head": self.head()[1],
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }


__all__ = ["EvidenceStore", "ManifestEntry", "GENESIS"]
