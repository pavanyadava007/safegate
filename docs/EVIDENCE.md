# Evidence integrity in practice

What the evidence store guarantees depends on where the signing key lives and
whether the chain head is published. This page is the setup that makes the
guarantees real, and what each piece protects against.

## The store

```
<store>/blobs/ab/cdef...     content-addressed traces and reports, read-only
<store>/manifest.log         append-only JSONL; each entry commits to the previous one
```

Every executed run, including failures, errors and repeats, is an entry. The
campaign Merkle root covers the campaign's evidence entries (start, runs, test-case
summaries, end). A later technical-file entry commits to that root and is not part of
it, so issuing a report does not change the number printed in it. An anchor is not an
entry at all: it is a file you publish outside the store.

On object storage (`--store s3://bucket/prefix`, credentials and endpoint from the
standard `AWS_*` environment) the layout is `prefix/blobs/ab/cdef...` and one object
per entry at `prefix/manifest/<seq>.json`. Every write is a conditional create
(`If-None-Match: *`), so the storage refuses to overwrite an entry and two writers can
never take the same sequence number; the loser retries on the new head. The
S3-compatible store must honour conditional writes (MinIO does; the tests run against
it). For deletion resistance, not only detection, enable Object Lock in compliance
mode on the bucket.

## Production setup

1. **Generate the CI signing key once, outside any store.**

   ```bash
   safegate keygen ./ci-key        # prints the public key
   ```

   Store `ci-key/signing.key` as a CI secret. Do not commit it.

2. **Pin the public key in the project policy.**

   ```yaml
   # policy.yaml
   trusted_signers:
     - 3f9c...e1   # CI signing key, rotated 2026-09
   ```

   The gate verifies every entry against this list; entries signed by any other key
   are blockers. With `require_signed_evidence: true` (the default), unsigned entries
   and entries from more than one signer are blockers too, and a signed store with no
   pinned key is a major finding ("self-attested").

3. **Sign in CI.**

   ```bash
   export SAFEGATE_SIGNING_KEY=$RUNNER_TEMP/signing.key
   safegate run safety/ --store .evidence --build "$(git rev-parse HEAD)" --workers 8
   safegate report safety/ --store .evidence --policy safety/policy.yaml -o V-and-V.md
   ```

4. **Anchor the head where the store's writers cannot rewrite it.**

   ```bash
   safegate anchor --store .evidence -o anchor.json
   git tag -s "evidence-$(git rev-parse --short HEAD)" -F anchor.json
   ```

   An RFC 3161 timestamp of `anchor.json` works as well.

5. **Verify independently.** An assessor holding the report, the store, the pinned
   key and the anchor runs:

   ```bash
   safegate verify --store .evidence --trusted-key trusted_signers.txt \
       --anchor anchor.json --report V-and-V.md
   ```

## What each control catches

The tamper demonstration in docs/RESULTS.md exercises the first four rows and the anchor
check on the rev A store; `tests/test_evidence.py`, `tests/test_end_to_end.py` and
`tests/test_review_fixes.py` cover the rest, including `--expected-build`.

| Manipulation | Caught by |
|---|---|
| Delete or reorder entries | chain linkage (always) |
| Edit an entry | chain linkage; for the newest entry, which nothing links to yet, the signature check (against the entry's own signer, or a pinned key if the editor re-signed) or an anchor |
| Edit an issued report | report digest recorded in the chain |
| Replace the store with a rewritten chain signed by a new key | pinned `trusted_signers` |
| Rewrite the chain with access to the CI key | published anchor |
| Evidence from another firmware build or design data | R-EVID-002 (project digest, `--expected-build`) |

What none of this catches: a runner that lies, a plant model that is wrong, or
tests that were never written. Those are covered by other rules (R-EXEC-002,
R-TRACE-*), by model validation, and by review, not by cryptography.

## Keys inside the store

`safegate run` without `--signing-key` generates a key inside the store so local
runs are still signed. That signature only shows the chain is self-consistent;
anyone who can write the store can regenerate both. The gate reports it as
self-attested, and the tamper demonstration in RESULTS.md shows such a forgery
passing plain `verify` and failing once a trusted key is given.
