"""Storage backends: filesystem locking and the S3 conditional-write design.

The S3 logic is tested against an in-memory client that implements the
If-None-Match semantics. Set SAFEGATE_TEST_S3=s3://bucket/prefix (with the
usual AWS_* variables, e.g. for a local MinIO) to run the same checks against
real object storage.
"""
import io
import json
import multiprocessing as mp
import os
import uuid

import pytest

from safegate.core.cas import EvidenceStore
from safegate.core.storage import ConcurrentAppend, S3Backend


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Just enough of the S3 API, including conditional creates."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
        k = (Bucket, Key)
        if IfNoneMatch == "*" and k in self.objects:
            raise _Err("PreconditionFailed")
        self.objects[k] = bytes(Body)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _Err("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _Err("404")
        return {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for b, k in fake.objects if b == Bucket and k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), 1000):
                    yield {"Contents": [{"Key": k} for k in keys[i:i + 1000]]}

        return P()


def s3_store(client, prefix="p"):
    store = EvidenceStore.__new__(EvidenceStore)
    store.backend = S3Backend(f"s3://bucket/{prefix}", client=client)
    store.root = store.backend.location
    store._key_path = None
    store._key = None
    return store


def test_s3_store_chain_blobs_and_signatures():
    client = FakeS3()
    store = s3_store(client)
    pub = store.generate_key()
    for i in range(5):
        store.append({"campaign_id": "c", "i": i})
    dg = store.put_json({"trace": [1, 2, 3]})
    assert store.put_json({"trace": [1, 2, 3]}) == dg  # idempotent
    assert store.get_json(dg) == {"trace": [1, 2, 3]}
    assert [e.seq for e in store.entries()] == [0, 1, 2, 3, 4]
    assert store.verify_chain(trusted_keys=[pub]) == []
    # entries are separate objects that storage refuses to overwrite
    with pytest.raises(ConcurrentAppend):
        store.backend.append_entry(2, "{}")


def test_s3_stale_writer_retries_on_the_new_head():
    client = FakeS3()
    a, b = s3_store(client), s3_store(client)
    a.append({"campaign_id": "c", "w": "a"})
    b.append({"campaign_id": "c", "w": "b"})       # b caches head at seq 1
    a.append({"campaign_id": "c", "w": "a2"})      # a is stale: seq 1 taken, retries at 2
    b.append({"campaign_id": "c", "w": "b2"})      # b is stale: seq 2 taken, retries at 3
    assert [e.seq for e in a.entries()] == [0, 1, 2, 3]
    assert a.verify_chain() == []


def test_s3_tampering_is_detected():
    client = FakeS3()
    store = s3_store(client)
    for i in range(3):
        store.append({"campaign_id": "c", "i": i})
    key = ("bucket", "p/manifest/000000000001.json")
    d = json.loads(client.objects[key])
    d["payload"]["i"] = 99
    client.objects[key] = json.dumps(d).encode()
    assert store.verify_chain()


def _append_many(location, n, tag):
    store = EvidenceStore(location)
    for i in range(n):
        store.append({"campaign_id": "c", "writer": tag, "i": i})


def test_concurrent_writers_never_share_a_sequence_number(tmp_path):
    procs = [mp.get_context("fork").Process(target=_append_many, args=(str(tmp_path), 60, k))
             for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    store = EvidenceStore(tmp_path)
    seqs = [e.seq for e in store.entries()]
    assert seqs == list(range(240))
    assert store.verify_chain() == []


LIVE = os.environ.get("SAFEGATE_TEST_S3")


@pytest.mark.skipif(not LIVE, reason="set SAFEGATE_TEST_S3=s3://bucket/prefix to run against real storage")
def test_live_object_storage_concurrent_writers():
    location = f"{LIVE.rstrip('/')}/{uuid.uuid4().hex}"
    procs = [mp.get_context("spawn").Process(target=_append_many, args=(location, 25, k))
             for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    store = EvidenceStore(location)
    assert [e.seq for e in store.entries()] == list(range(100))
    assert store.verify_chain() == []
    dg = store.put_bytes(b"evidence")
    assert store.get_bytes(dg) == b"evidence"


def test_cli_keeps_object_storage_uris_intact():
    """Parsing --store as a Path collapsed s3:// into a local directory 's3:'."""
    from safegate.cli import _parser
    for cmd in (["verify"], ["anchor", "-o", "a.json"], ["gate", "p"], ["report", "p"],
                ["run", "p", "--build", "b"]):
        args = _parser().parse_args([*cmd, "--store", "s3://bucket/prefix"])
        assert args.store == "s3://bucket/prefix"


@pytest.mark.skipif(not LIVE, reason="set SAFEGATE_TEST_S3=s3://bucket/prefix to run against real storage")
def test_live_object_storage_cli_pipeline(tmp_path):
    from pathlib import Path

    from safegate.cli import main
    project = str(Path(__file__).resolve().parents[1] / "examples" / "amr_project")
    location = f"{LIVE.rstrip('/')}/{uuid.uuid4().hex}"
    assert main(["keygen", str(tmp_path / "k")]) == 0
    key, pub = str(tmp_path / "k" / "signing.key"), str(tmp_path / "k" / "signing.pub")
    common = ["--store", location]
    assert main(["run", project, *common, "--build", "b", "--only", "TC-MUTE-001",
                 "--boundary", "4", "--sweep", "8", "--falsify", "0",
                 "--signing-key", key]) == 1
    assert main(["report", project, *common, "--trusted-key", pub, "--signing-key", key,
                 "-o", str(tmp_path / "VV.md")]) == 1
    assert main(["anchor", *common, "-o", str(tmp_path / "anchor.json")]) == 0
    assert main(["verify", *common, "--trusted-key", pub, "--anchor", str(tmp_path / "anchor.json"),
                 "--report", str(tmp_path / "VV.md")]) == 0
    assert not Path("s3:").exists()
