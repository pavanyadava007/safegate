# SafeGate

Safety verification and conformity evidence for driverless industrial trucks.
**EN ISO 3691-4 · EN ISO 13849-1 · Regulation (EU) 2023/1230.**

A compiler from hazards to a technical file. Test execution is a plugin; the
conformity evidence chain is the product.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design rationale.

## Install

```bash
pip install -e ".[sign,qmc,dev]"
```

## Use

```bash
safegate validate examples/amr_project        # referential integrity
safegate pl       examples/amr_project        # derive Performance Levels
safegate fields   examples/amr_project        # protective field sizing table

safegate run examples/amr_project \
    --store .evidence --build "$(git rev-parse HEAD)" \
    --sweep 256 --falsify 128

safegate gate   examples/amr_project --store .evidence --policy examples/amr_project/policy.yaml
safegate report examples/amr_project --store .evidence -o V-and-V.md
safegate verify --store .evidence             # chain integrity
```

Exit codes: `0` pass · `1` gate failure · `2` load error. Wire `gate` into CI as
a required check.

## What it enforces

| | |
|---|---|
| **Performance Level** | derived from the architecture on every run, never asserted. CCF < 65 on Cat 3 invalidates the determination rather than silently downgrading it. |
| **Traceability** | every hazard → requirement → test case, checked as graph reachability |
| **Falsification** | STL robustness as an optimisation objective; finds the worst corner instead of the corner you thought of |
| **Margin** | a design that passes with 2 mm of headroom is a finding, not a pass |
| **Determinism** | five-hash pinning; a non-reproducible safety test is `FLAKY`, which is a blocker |
| **Tamper evidence** | append-only chained manifest + content-addressed blobs; deleting a failing run breaks the chain |
| **Evidence tier** | PL d/e functions cannot be discharged by simulation alone |
| **ML in the safety path** | blocked from self-declaration — Reg. (EU) 2023/1230 Annex I requires a Notified Body |

## Layout

```
src/safegate/
  core/        model.py ids.py cas.py loader.py   domain DAG, CAS, chained manifest
  stl/         robustness.py parser.py            STL with quantitative semantics
  iso13849/    pl.py                              MTTFd · DCavg · CCF · Category → PL
  iso3691/     metrics.py                         protective field & stopping budget
  scenario/    samplers.py                        boundary · Sobol · falsification
  execution/   adapter.py                         Null · Replay · scenario_execution · HIL
  orchestrator/campaign.py                        3-phase campaign, determinism guard
  policy/      gate.py                            declarative rules, waivers
  report/      technical_file.py                  the deliverable
examples/amr_project/                             worked Cat 3 tow-tractor example
tests/                                            38 tests
```

## Status

Working end to end on the bundled example: 936 runs, PL derivation, gate,
signed technical file, demonstrated tamper detection.

Not production-ready. `HilRunner` is a documented stub; `ScenarioExecutionRunner`
needs a site-specific signal extractor; the evidence backend is filesystem-only.

## Licence notes

Delegates scenario execution to Intel Labs `scenario_execution`
(arXiv:2409.07080, Apache-2.0). Standards texts (ISO/EN) are not reproduced —
only engineering relationships are implemented; clause references are project
data, not hard-coded.
