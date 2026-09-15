# SafeGate — System Architecture

**What it is:** a compiler from hazards to a technical file. Test execution is a
plugin; the conformity evidence chain is the product.

**Who it is for:** manufacturers of driverless industrial trucks and AMRs who must
hold a valid conformity assessment under EN ISO 3691-4 + EN ISO 13849-1, and who
face Regulation (EU) 2023/1230 applying in full on **20 January 2027** with no
grace period.

---

## 1. The problem, stated precisely

A Notified Body assessor asks one question, recursively:

> Show me the evidence that this claim is true, and show me that the evidence
> was produced by the thing you say produced it.

Today that question is answered with a Word document, a spreadsheet of
requirements, a folder of PDF test reports, and an engineer's memory of which
firmware build was on the truck in March. The chain from *hazard* to *evidence*
exists only in people's heads. Consequences:

| Failure | Frequency in practice | Cost |
|---|---|---|
| Report claims a PL the architecture does not achieve | common | recall, CE withdrawal |
| Test results from a superseded firmware build | very common | re-test campaign |
| A failing run quietly dropped before reporting | uncomfortably common | liability, fraud exposure |
| Requirement with no test case, discovered at audit | near-universal | 6–12 week slip |
| "It passed" with 2 mm of margin | invisible | field incident |

None of these are simulation problems. They are **bookkeeping problems with
safety consequences**, and that is precisely why no simulator vendor solves them.

### 1.1 Prior art and the build-vs-buy line

| Layer | Best available | Verdict |
|---|---|---|
| OSC2 parsing, behaviour trees, Gazebo/ROS2 plumbing | Intel Labs `scenario_execution` (arXiv:2409.07080, Apache-2.0) | **Buy.** Good, maintained, free. |
| Automotive scenario tooling | Foretellix, dSPACE, IPG CarMaker, Ansys medini | **Not applicable.** Priced and scoped for ISO 26262/road vehicles, not ISO 3691-4/machinery. |
| Traceability, PL derivation, conformity policy, technical file | **nothing** | **Build. This is the product.** |

Reimplementing scenario execution would burn a year and produce a worse Gazebo
wrapper. The moat is compliance, not simulation.

---

## 2. Architectural principles

Six, in priority order. Every design decision below is traceable to one of them.

**P1 — Everything is content-addressed and immutable.**
Node IDs are a pure function of semantic content (`core/ids.py`), never `uuid4`,
never autoincrement. Two engineers authoring the same hazard in different
branches produce the same ID, so merges preserve traceability instead of
destroying it.

**P2 — The domain model is a typed graph, not a document.**
Traceability is graph reachability. "Is every hazard covered?" is a query, not a
review meeting.

**P3 — Derived values are never stored.**
Performance Level is recomputed from the architecture on every evaluation
(`iso13849/pl.py`). A stored PL is a lie waiting to happen. Same for PFHd,
coverage, and required PLr.

**P4 — Determinism or it is not evidence.**
Five hashes — scenario, SUT build, backend, configuration, seed — are part of a
run's content ID. An adapter that cannot reproduce declares
`deterministic = False`, and the orchestrator runs each point three times;
disagreement yields `FLAKY`, which the gate treats as a **blocker**. A safety
test that does not reproduce has verified nothing.

**P5 — Policy is data, not code.**
Release criteria are negotiated between engineering, the customer's safety
department and the Notified Body, and they change per project. Policy as YAML is
reviewable and diffable. Policy buried in Python is unauditable.

**P6 — The report is a pure function of the evidence graph.**
Nothing in the technical file is hand-written. Hand-editing the report is the
exact failure this system exists to eliminate.

---

## 3. Layered view

```
┌──────────────────────────────────────────────────────────────────────┐
│ L7  AUDIT & REPORTING          report/technical_file.py              │
│     Technical file generation, NB export, margin trends              │
├──────────────────────────────────────────────────────────────────────┤
│ L6  CONFORMITY POLICY          policy/gate.py                        │
│     Declarative rules, waivers w/ named approver, CI exit codes      │
├──────────────────────────────────────────────────────────────────────┤
│ L5  STANDARDS ANALYSIS         iso13849/pl.py  iso3691/metrics.py    │
│     PL derivation, protective-field budget, ISO 13855 approach       │
├──────────────────────────────────────────────────────────────────────┤
│ L4  ORCHESTRATION              orchestrator/campaign.py              │
│     3-phase campaign, determinism guard, mandatory recording         │
├──────────────────────────────────────────────────────────────────────┤
│ L3  EXECUTION ADAPTERS         execution/adapter.py                  │
│     Null · Replay · scenario_execution(Gazebo/ROS2) · HIL           │
├──────────────────────────────────────────────────────────────────────┤
│ L2  SCENARIO COMPILER          scenario/samplers.py                  │
│     Abstract space → concrete runs; Boundary/Sobol/Falsification     │
├──────────────────────────────────────────────────────────────────────┤
│ L1  SPECIFICATION              stl/robustness.py  stl/parser.py      │
│     STL with quantitative semantics — margin, not booleans           │
├──────────────────────────────────────────────────────────────────────┤
│ L0  EVIDENCE & DOMAIN          core/model.py  core/cas.py  ids.py    │
│     Typed DAG · content-addressed blobs · signed chained manifest    │
└──────────────────────────────────────────────────────────────────────┘
```

The dependency arrow points **downward only**. L5 knows nothing about Gazebo;
L3 knows nothing about Performance Levels. That is what lets a new simulator be
added in a day and a standards revision be absorbed without touching execution.

---

## 4. The traceability DAG (L0)

```
 Hazard ──covered_by──▶ SafetyRequirement ──allocated_to──▶ SafetyFunction
   │                           │                                 │
   │ Annex A risk graph        │                          realised_by
   ▼                           │                                 ▼
 PLr (derived)                 │                        SafetyArchitecture
                               │                         (Category, MTTFd,
                         verified_by                      DCavg, CCF)
                               │                                 │
                               ▼                        evaluate_architecture()
                          TestCase                               │
                     (parameter SPACE                            ▼
                      + STL criterion)                   achieved PL (derived)
                               │
                        concretised_to
                               ▼
                         ConcreteRun ──── Pinning{scenario, sut, backend,
                               │                   config, seed}
                          produces
                               ▼
                          RunResult ──▶ Evidence blobs (CAS)
                               │
                               ▼
                    chained, signed manifest ──▶ Merkle root ──▶ technical file
```

`R-PL-001` is then simply: for every `SafetyFunction`, `max(PLr)` over all
reachable `Hazard`s must be ≤ `achieved PL`. That is a graph traversal, and it
is the check that most hand-maintained safety cases get wrong.

---

## 5. Why STL and not assertions (L1)

A boolean assertion answers "did it pass?". Useless for a safety argument,
because it discards the margin.

STL's quantitative semantics returns a real number ρ:

| ρ | meaning |
|---|---|
| > 0 | satisfied; ρ is the **distance to violation**, in metres or seconds |
| < 0 | violated; \|ρ\| is the **depth** of the violation |
| = 0 | on the boundary |

`always min_distance_to_person >= 0.10` evaluated on a trace returns
`+0.084` — "we cleared it by 84 mm". Three consequences, all commercial:

1. **Falsification becomes optimisation.** Minimising ρ over the parameter space
   is a directed search for the worst case. This finds the 1-in-10⁶ corner
   without running 10⁶ tests.
2. **Margin is reportable.** "Held with a minimum margin of 38 mm across 12,400
   runs" is an argument an assessor can weigh. "1240/1240 passed" is not.
3. **Regression detection gets sensitive.** A commit cutting margin from 380 mm
   to 40 mm passes every boolean test and is a serious regression. `R-MARGIN-001`
   trips on it.

Implementation notes: interval operators use Lemire's monotonic-wedge sliding
window — **O(n)** per temporal operator, not O(n·w). At 10⁵ traces × 10⁴ samples
in CI, that is the difference between a 4-minute gate and a 4-hour one. `Until`
is O(n²) and documented as such rather than hidden; it is rare in safety
requirements.

Surface syntax is deliberately small enough to teach a TÜV assessor in fifteen
minutes — that is a real acceptance criterion, not a nicety. Unit suffixes
(`150ms`, `40mm`) are parsed and normalised, because engineers write them and
silently dropping a unit is how you ship a 1000× error.

---

## 6. Three-phase campaign (L2 + L4)

Each phase informs the next:

| Phase | Sampler | Purpose | Budget |
|---|---|---|---|
| 1 | `BoundarySampler` | ODD corners + face centres + nominal. Defects concentrate at extremes and corners are cheap. | ~24 |
| 2 | `SobolSampler` | Low-discrepancy sweep. **This is what the coverage claim rests on.** | ~128–256 |
| 3 | `RobustnessGuidedSampler` | Simulated annealing on ρ, seeded from the worst point so far. Multi-restart, because mode switches (speed zones, field switching) make the ρ landscape multi-modal. | ~64–128 |

Phase 3 exits on first counterexample — one is enough to fail the gate, and
compute is better spent on the next requirement.

Coverage is reported as **two-way (pairwise) bin coverage**, the pragmatic middle
ground between 1-way (meaningless) and full factorial (impossible).

### Invariants the orchestrator will not let a caller override

- **I1 — everything executed is recorded**, including failures, errors and
  truncated runs. Selective recording turns a safety tool into a liability.
- **I2 — non-deterministic backends get repeat execution**, and disagreement is
  `FLAKY`, not a pass.

---

## 7. Tamper evidence (L0)

Explicit threat model, because a compliance system that hand-waves this is
theatre:

| # | Threat | Control |
|---|---|---|
| T1 | Careless mutation — a log overwritten, the report silently changes | Blobs immutable, addressed by digest, `chmod 0444`. A changed file is a different address. |
| T2 | **Selective reporting** — failing runs dropped before reporting | Append-only manifest, each entry commits to the previous entry's hash. Removing an entry breaks the chain. Report is generated from the chain, not a directory listing. |
| T3 | Backdating after a field incident | Entries signed (Ed25519) and chained; `anchor()` exposes the root for RFC 3161 timestamping or a signed git tag. |
| T4 | Build/evidence mismatch | `Pinning` is inside the run's content ID, so it is inside the hash the manifest commits to. |

**Demonstrated, not claimed.** In the worked example, deleting the 294 failing-run
entries from a 943-entry manifest produced:

```
EVIDENCE CHAIN COMPROMISED:
  - seq gap at 8: expected 7
  - chain break at seq 8
  ...
```

---

## 8. Policy rules shipped (L6)

| Rule | Severity | Enforces |
|---|---|---|
| `R-PL-001` | blocker | achieved PL ≥ PLr from the Annex A risk graph |
| `R-PL-002` | blocker | the PL determination itself is valid (CCF ≥ 65, channel count, admissible DC band) |
| `R-TRACE-001` | blocker | every hazard has a requirement |
| `R-TRACE-002` | blocker | every requirement has a test case |
| `R-TRACE-003` | major | every requirement has a machine-checkable criterion |
| `R-EXEC-001` | blocker | no FAIL, FLAKY or ERROR |
| `R-EXEC-002` | blocker | **PL d/e functions need HIL-tier evidence or above** — simulation alone does not discharge a physical verification obligation |
| `R-COV-001` | major | two-way coverage above threshold |
| `R-MARGIN-001` | major | worst-case margin above a floor — catches the design that passes with 2 mm to spare |
| `R-EVID-001` | blocker | manifest chain intact and signed |
| `R-ML-001` | blocker | ML in the safety path ⇒ Notified Body route; self-declaration blocked |

### On waivers

Supported, because pretending they will not happen is naive. But a waiver
requires a **justification and a named approver**, the severity is **not
downgraded**, and every waived finding appears in the technical file. A silent
waiver is not a feature this tool will ever have.

### On `R-ML-001` — the commercially load-bearing rule

Regulation (EU) 2023/1230 Annex I Part A item 5 lists *safety components with
fully or partially self-evolving behaviour using machine-learning approaches
ensuring safety functions* as requiring **third-party conformity assessment by a
Notified Body**. Self-certification is unavailable, and ISO 13849-1 provides no
quantification route for such elements.

Practical consequence, and the one most AMR teams have not internalised: **you
cannot put your fused camera/radar network in the safety path and self-declare.**
The tool encodes this as a blocker rather than a warning.

---

## 9. Deployment topology

```
Developer laptop                 CI runner                  Evidence store
─────────────────                ─────────                  ──────────────
safegate validate    ──┐
safegate pl            │   ┌──▶ safegate validate
safegate fields        │   │    safegate pl
                       │   │    safegate run ───────────┐
git push ──────────────┴───┘    safegate gate  ◀────────┤   S3 / GCS / MinIO
                                safegate report         │   (CAS blobs)
                                safegate verify ◀───────┤
                                    │                   │   manifest.log
                                    ▼                   │   (append-only,
                              merge blocked             │    chained, signed)
                              on blocker                └───────────────────
                                                                  │
                            HIL rig farm ────────────────────────┘
                            (nightly, PL d/e obligations)

                            Notified Body receives:
                              V-and-V.md + evidence store
                              → recomputes Merkle root independently
```

Evidence persists across CI runs so the chain is continuous and margin trends
are comparable commit to commit. SIL runs on every PR; HIL runs nightly or on
release candidates, because `R-EXEC-002` will block release without it.

---

## 10. Worked example — the gate catching real defects

The shipped example project (`examples/amr_project/`) is a Category 3 personnel
detection system on a tow tractor. Running the pipeline produces:

**PL determination** — derived, never asserted:

```
Architecture ARCH-PDS-CAT3  Category 3
  channel 1: MTTFd = 33.0 y (capped 33.0 y) -> high
      safety_laser_scanner_front   MTTFd=   100.0 y  DC=99.0%
      safety_controller_ch1        MTTFd=   120.0 y  DC=95.0%
      brake_contactor_ch1          MTTFd=    83.3 y  DC=90.0%   ← B10d route
  combined MTTFd = 33.0 y (high)
  DCavg = 94.3% (medium)
  CCF score = 75
  => PL d  (PFHd in [1.0e-07, 1.0e-06) 1/h)
```

The ML variant is correctly rejected rather than silently downgraded:

```
Architecture ARCH-PDS-ML  Category 3
  => DETERMINATION INVALID
  ! CCF score 55 < 65 required for Category 3 (Annex F)
  ! Category 3 requires two channels; 1 declared
  - Architecture declares machine learning in the safety path. Under
    Regulation (EU) 2023/1230 Annex I ... requires third-party conformity
    assessment by a Notified Body; self-certification is not available.
```

**Campaign** — 936 runs across four test cases, 100% two-way coverage:

```
  TC-CROSS-001     FAIL    runs=216   worst=-0.1     cov2=100%
  TC-CROSS-002     PASS    runs=312   worst=2.227    cov2=100%
  TC-SPEED-001     FAIL    runs=207   worst=-1.2     cov2=100%
  TC-MUTE-001      FAIL    runs=201   worst=-0.5     cov2=100%
```

**Gate** — nine findings, exit code 1, merge blocked:

```
BLOCKER  R-EXEC-001   TC-CROSS-001
         falsified: worst robustness -0.1 at {'brake_decel': 0.8,
         'detection_range': 2.5, 'floor_friction': 0.55, 'latency': 0.1,
         'truck_speed': 2.0, ...}
BLOCKER  R-PL-001     SF-PDS-01
         achieved PL d < required PLr e (Cat 3, MTTFd high, DCavg medium, CCF 75)
BLOCKER  R-EXEC-002   TC-CROSS-001
         verifies a PL d function but the highest evidence tier is below hil
BLOCKER  R-TRACE-002  SR-COLL-002
         requirement has no test case
```

Note what each finding is:

- `R-EXEC-001` — the falsifier found the physically correct worst corner: maximum
  speed, minimum braking, worst floor friction, shortest detection range. A human
  writing test scripts would not have picked that combination.
- `R-PL-001` — a genuine design shortfall. `HAZ-COLL-001` is S2/F2/P2 → **PLr e**,
  but the Cat 3 architecture with medium DCavg achieves only **PL d**. Under the
  simplified route, PL e requires Category 4 with high DCavg. This is the single
  most common real-world error in AMR safety cases, and it is invisible to a
  spreadsheet.
- `R-TRACE-002` — `SR-COLL-002` (protective field adequacy) has no test case.
  A gap that surfaces at audit, six weeks before shipping, without this check.

---

## 11. Scaling and known limits

| Dimension | Current | Path |
|---|---|---|
| Runs per campaign | ~10³ single-process | Campaign is embarrassingly parallel per `ConcreteRun`; shard by test case across a run farm, merge manifests by seq |
| Trace storage | Decimated ×10, full resolution on failure | Correct default: the failing trace is the one an engineer opens |
| STL `Until` | O(n²) | Rare in safety requirements; documented, not hidden |
| Evidence backend | Filesystem | Swap `_blob_path` for S3/GCS; manifest → an append-only log service |
| Multi-variant projects | One project per variant | Variant axis in `Project`; PL results diffed across variants |

### What this tool does **not** do, stated up front

1. **It does not prove absence of violations.** Falsification is not verification.
   The technical file says so in §7, and that honesty is why an assessor trusts
   the rest of the document.
2. **It does not replace physical testing.** `R-EXEC-002` exists precisely to
   stop the tool being misused that way.
3. **It does not validate your models.** A simulated result is only as good as
   the vehicle, sensor and environment models behind it. Model validation is a
   separate obligation.
4. **It does not validate your ODD.** Two-way coverage measures how much of the
   *declared* parameter space was exercised. Whether that space correctly bounds
   the intended operating conditions is an input to this process, not an output.

A safety tool that oversells its coverage is more dangerous than no tool.

---

## 12. Commercial read

**Why this is sellable in 2026, and why it is a solo-founder-shaped product:**

- **Deadline-driven demand.** EU Machinery Regulation 2023/1230 applies in full
  20 Jan 2027. Every AMR maker selling into the EU must redo conformity
  assessment. Annex I item 5 forces anyone with ML in the safety path to a
  Notified Body — external scrutiny of evidence most teams currently generate
  by hand.
- **Asset-light.** Software. No BOM, no factory, no €25k-per-unit hardware, no
  integrator bandwidth constraint.
- **The skills are scarce in this market.** ISO 26262 / SOTIF / MISRA-grade V&V
  discipline is standard in automotive and rare in intralogistics. Mapping it to
  ISO 3691-4 and ISO 13849 is a translation job, not a research project.
- **Perception ML skills are the wrong moat here.** In structured warehouses,
  SLAM and obstacle detection are commodities (Nav2, off-the-shelf), and under
  ISO 13849/3691-4 they legally cannot carry the safety function. The scarce
  thing is the evidence chain.

**Falsification test before building further:** interview 8–10 AMR OEMs and
integrators (Agilox, MiR, Idealworks, ek robotics, Safelog) plus a Notified Body,
on 2027 readiness and willingness to pay for validation tooling. **Kill if fewer
than three name a budget.**
