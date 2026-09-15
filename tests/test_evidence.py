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
    d = json.loads(lines[0]); d["payload"]["verdict"] = "pass"
    lines[0] = json.dumps(d, sort_keys=True, separators=(",", ":"))
    s.manifest_path.write_text("\n".join(lines) + "\n")
    assert s.verify_chain(), "editing a verdict must break the chain"


def test_blob_integrity_failure_raises(tmp_path):
    s = EvidenceStore(tmp_path)
    dg = s.put_bytes(b"evidence")
    p = s._blob_path(dg)
    p.chmod(0o644); p.write_bytes(b"tampered")
    with pytest.raises(IOError):
        s.get_bytes(dg)


def test_merkle_root_changes_when_any_entry_changes(tmp_path):
    s = EvidenceStore(tmp_path)
    s.append({"campaign_id": "c", "i": 1})
    r1 = s.campaign_root("c")
    s.append({"campaign_id": "c", "i": 2})
    assert s.campaign_root("c") != r1
