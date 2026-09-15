"""Tamper-evidence: the properties that make this a compliance tool."""
import json

import pytest

from safegate.core.cas import EvidenceStore


def test_blobs_are_content_addressed_and_idempotent(tmp_path):
    s = EvidenceStore(tmp_path)
    a = s.put_bytes(b"hello")
    b = s.put_bytes(b"hello")
    assert a == b
    assert s.get_bytes(a) == b"hello"


def test_chain_detects_deletion(tmp_path):
    s = EvidenceStore(tmp_path)
    for i in range(5):
        s.append({"campaign_id": "c", "type": "run", "i": i})
    assert s.verify_chain() == []

    lines = s.manifest_path.read_text().splitlines()
    del lines[2]                                   # drop a failing run
    s.manifest_path.write_text("\n".join(lines) + "\n")
    problems = s.verify_chain()
    assert problems, "deleting an entry must be detectable"


def test_chain_detects_mutation(tmp_path):
    s = EvidenceStore(tmp_path)
    s.append({"campaign_id": "c", "verdict": "fail"})
    s.append({"campaign_id": "c", "verdict": "pass"})
    lines = s.manifest_path.read_text().splitlines()
    d = json.loads(lines[0])
    d["payload"]["verdict"] = "pass"
    lines[0] = json.dumps(d, sort_keys=True, separators=(",", ":"))
    s.manifest_path.write_text("\n".join(lines) + "\n")
    assert s.verify_chain(), "editing a verdict must break the chain"


def test_blob_integrity_failure_raises(tmp_path):
    s = EvidenceStore(tmp_path)
    dg = s.put_bytes(b"evidence")
    p = s._blob_path(dg)
    p.chmod(0o644)
    p.write_bytes(b"tampered")
    with pytest.raises(IOError):
        s.get_bytes(dg)


def test_merkle_root_changes_when_any_entry_changes(tmp_path):
    s = EvidenceStore(tmp_path)
    s.append({"campaign_id": "c", "i": 1})
    r1 = s.campaign_root("c")
    s.append({"campaign_id": "c", "i": 2})
    assert s.campaign_root("c") != r1


def test_editing_the_newest_entry_needs_a_signature_check(tmp_path):
    """Linkage cannot see an edit to the last entry; signatures can."""
    s = EvidenceStore(tmp_path)
    pub = s.generate_key()
    for i in range(3):
        s.append({"campaign_id": "c", "i": i, "verdict": "fail"})
    lines = s.manifest_path.read_text().splitlines()
    d = json.loads(lines[-1])
    d["payload"]["verdict"] = "pass"
    lines[-1] = json.dumps(d, sort_keys=True, separators=(",", ":"))
    s.manifest_path.write_text("\n".join(lines) + "\n")
    # The signature against the entry's own signer catches the edit...
    assert s.verify_chain() == ["entry 2 bad signature"]
    assert s.verify_chain(trusted_keys=[pub]) == ["entry 2 bad signature"]
    # ...but a writer who re-signs the edited entry with their own key is
    # only caught by the pinned key.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from safegate.core.cas import ManifestEntry, public_key_hex
    e = ManifestEntry.from_json(lines[-1])
    rogue = Ed25519PrivateKey.generate()
    resigned = ManifestEntry(seq=e.seq, prev=e.prev, payload=e.payload, recorded_at=e.recorded_at,
                             signature=rogue.sign(bytes.fromhex(e.body_digest())).hex(),
                             signer=public_key_hex(rogue))
    lines[-1] = resigned.to_json()
    s.manifest_path.write_text("\n".join(lines) + "\n")
    assert s.verify_chain() == []
    assert s.verify_chain(trusted_keys=[pub]) == ["entry 2 signed by untrusted key " + public_key_hex(rogue)[:16] + "..."]


def test_external_signing_key_is_not_written_to_the_store(tmp_path):
    from safegate.core.cas import write_keypair
    pub = write_keypair(tmp_path / "ci-key")
    s = EvidenceStore(tmp_path / "store", signing_key=tmp_path / "ci-key" / "signing.key")
    s.append({"campaign_id": "c"})
    assert s.signer_id == pub and not s.key_in_store
    assert not (tmp_path / "store" / "keys").exists()
    assert s.verify_chain(trusted_keys=[pub]) == []


def test_merkle_root_distinguishes_a_duplicated_last_leaf():
    from safegate.core.ids import digest, merkle_root
    a, b, c = (digest(i) for i in range(3))
    assert merkle_root([a, b, c]) != merkle_root([a, b, c, c])
    assert merkle_root([a]) != a


def test_garbage_signature_is_caught_without_trusted_keys(tmp_path):
    s = EvidenceStore(tmp_path)
    s.generate_key()
    s.append({"campaign_id": "c"})
    d = json.loads(s.manifest_path.read_text())
    d["signature"] = "00" * 64
    s.manifest_path.write_text(json.dumps(d, sort_keys=True, separators=(",", ":")) + "\n")
    assert s.verify_chain() == ["entry 0 bad signature"]
