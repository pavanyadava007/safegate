# Verification and Validation Report

| | |
|---|---|
| Manufacturer | Example Intralogistics GmbH (fictional) |
| Product | driverless industrial truck (tow tractor) / T200-warehouse |
| Document | VV-T200 rev A |
| Machine type | driverless industrial truck (tow tractor) |
| Variant | T200-warehouse |
| Issued | 2026-09-15 05:23 UTC |
| Author | P. Yadava |
| Approver | not approved |
| Notified Body | not engaged |
| **Gate status** | **NON-CONFORMING** |

**Evidence commitment**

| | |
|---|---|
| Campaign | `T200-revA` |
| Merkle root | `8f025f271f816bd9a5776ef4e901feeaab1381cef7a6bea33d89a04a8cdcc74d` |
| SUT build | `example-build-revA` |
| Configuration | `a000bd3e78c9d4332e40ec212646b2ce3c36cf2670fefeb7e5d653c7255ed800` |
| Runner / backend | `sil` / `0b2396fe02505741` |
| Project data digest | `33154272c05b7c6f` |
| Campaign seed / budgets | 20260914 / boundary 24, sweep 128, falsify 64 |
| Signer (Ed25519) | `dd2e84a76684eea3805fb9ac39f63e2f34d0280ad3a7fc4eb36fef2531549179` |
| SafeGate | 0.4.0 |

Any party holding this document and the evidence store can
recompute the Merkle root and confirm that no record of this
campaign has been added, removed or altered since issue:

    safegate verify --store <evidence store> --report <this file> \
        --trusted-key <signer key from an independent source>

## 1 Standards applied

- EN ISO 3691-4:2023
- EN ISO 13849-1:2023
- EN ISO 12100:2010
- EN ISO 13855:2010
- Regulation (EU) 2023/1230

## 2 Hazard analysis and required Performance Levels

Required PL derived from the ISO 13849-1 Annex A risk graph.

| Ref | Hazard | Zone | S | F | P | PLr |
|---|---|---|---|---|---|---|
| HAZ-COLL-001 | Collision with a person in the travel path | main_aisle | S2 | F2 | P2 | **e** |
| HAZ-COLL-002 | Collision during reversing at the dock | dock | S2 | F1 | P2 | **d** |
| HAZ-MUTE-001 | Protective device muted beyond its justification | pick_station | S2 | F1 | P2 | **d** |
| HAZ-SPEED-001 | Overspeed in a reduced-speed zone | junction | S2 | F1 | P1 | **c** |

## 3 Safety functions and achieved Performance Levels

| Safety function | Cat | MTTFd | DCavg | CCF | Achieved PL | PFHd band [1/h] |
|---|---|---|---|---|---|---|
| SF-PDS-01 Personnel detection and safe stop | 3 | 33.0 y (high) | 94.3% (medium) | 75 | **d** | 1e-07 to 1e-06 |
| SF-SPD-01 Zone speed supervision | 2 | 32.6 y (high) | 92.7% (medium) | 70 | **d** | 1e-07 to 1e-06 |

### 3.1 Derivations

**SF-PDS-01: Personnel detection and safe stop**

```
Architecture ARCH-PDS-CAT3  Category 3
  channel 1: MTTFd = 33.0 y (capped 33.0 y) -> high
      safety_laser_scanner_front   MTTFd=   100.0 y  DC=99.0%
      safety_controller_ch1        MTTFd=   120.0 y  DC=95.0%
      brake_contactor_ch1          MTTFd=    83.3 y  DC=90.0%
  channel 2: MTTFd = 33.0 y (capped 33.0 y) -> high
      safety_laser_scanner_side    MTTFd=   100.0 y  DC=99.0%
      safety_controller_ch2        MTTFd=   120.0 y  DC=95.0%
      brake_contactor_ch2          MTTFd=    83.3 y  DC=90.0%
  combined MTTFd = 33.0 y (high)
  DCavg = 94.3% (medium)
  CCF score = 75
  => PL d  (PFHd in [1.0e-07, 1.0e-06) 1/h)
  - Determined via the simplified route (ISO 13849-1 Annex K / Figure 7). A full Markov analysis may yield a different PFHd.
```

**SF-SPD-01: Zone speed supervision**

```
Architecture ARCH-SPD-CAT2  Category 2
  channel 1: MTTFd = 32.6 y (capped 32.6 y) -> high
      wheel_encoder                MTTFd=    60.0 y  DC=92.0%
      speed_monitor_logic          MTTFd=   100.0 y  DC=95.0%
      drive_cutoff_relay           MTTFd=   250.0 y  DC=90.0%
  combined MTTFd = 32.6 y (high)
  DCavg = 92.7% (medium)
  CCF score = 70
  => PL d  (PFHd in [1.0e-07, 1.0e-06) 1/h)
  - Determined via the simplified route (ISO 13849-1 Annex K / Figure 7). A full Markov analysis may yield a different PFHd.
```

## 4 Protective field sizing

Derived from the latency and braking budget. `L_req` is the minimum protective-field length at each speed.

- Detection + reaction + communication latency: 170 ms
- Guaranteed minimum deceleration: 1.20 m/s²
- Device measurement tolerance: 90 mm
- Human approach term credited: yes, K = 1.6 m/s over latency plus stopping time
- Design margin: 100 mm

```
 v [m/s]   latency   braking  approach     tol   margin  L_req [m]
------------------------------------------------------------------
    0.30     0.051     0.037     0.672   0.090    0.100      0.951
    0.60     0.102     0.150     1.072   0.090    0.100      1.514
    1.00     0.170     0.417     1.605   0.090    0.100      2.382
    1.50     0.255     0.938     2.272   0.090    0.100      3.654
    2.00     0.340     1.667     2.939   0.090    0.100      5.135
```

## 5 Verification results

Runner `sil`. The tier column is the tier of the evidence that was produced, followed by the tier the test case declares.

| Test case | Requirement(s) | Runs | Errors | Tier (evidence / declared) | Verdict | Worst margin | 2-way cov. |
|---|---|---|---|---|---|---|---|
| TC-CROSS-001 | SR-COLL-001 | 151 | 0 | sil / sil | **FAIL** | -0.548 | 100% |
| TC-CROSS-002 | SR-COLL-001 | 151 | 0 | sil / sil | **FAIL** | -0.5423 | 100% |
| TC-FIELD-001 | SR-COLL-002 | 143 | 0 | sil / sil | **FAIL** | -2.635 | 100% |
| TC-MUTE-001 | SR-MUTE-001 | 137 | 0 | sil / sil | **FAIL** | -3.77 | 100% |
| TC-SPEED-001 | SR-SPEED-001 | 152 | 0 | sil / sil | **FAIL** | -0.5925 | 100% |

Total runs executed: **734**.

Worst margin is the minimum STL robustness over all runs, in the unit of the criterion (metres, seconds or m/s). Negative means violated.

### 5.1 Counterexamples, errors and non-reproducible results

**TC-CROSS-001**: fail

- Criterion: `always separation_while_moving >= 0.10`
- Failing runs: 4 of 151; errored runs: 0
- Worst robustness: `-0.548`
- Parameter assignment: `{'entry_gap': 1.0, 'floor_mu': 0.1, 'pedestrian_speed': 1.6, 'pedestrian_start_offset': 0.5, 'truck_speed': 2.0, 'warning_field': 0.0}`
- Full-resolution trace (evidence blob): `b848fa0867f2fcc470b7aabce359e56d682995290107298360bb36a5140b6388`

**TC-CROSS-002**: fail

- Criterion: `always separation_while_moving >= 0.10`
- Failing runs: 6 of 151; errored runs: 0
- Worst robustness: `-0.542318`
- Parameter assignment: `{'entry_gap': 1.0, 'floor_mu': 0.1, 'pedestrian_speed': 1.6, 'rack_clearance': 0.3, 'truck_speed': 2.0, 'warning_field': 0.0}`
- Full-resolution trace (evidence blob): `e4024770842b5c95794806ba62b7b7d340d3e61daf9e4e7b21e2f360afbb2535`

**TC-FIELD-001**: fail

- Criterion: `always protective_field_length >= required_field_length`
- Failing runs: 143 of 143; errored runs: 0
- Worst robustness: `-2.63533`
- Parameter assignment: `{'floor_mu': 0.1, 'goal_distance': 30.0, 'truck_speed': 2.0}`
- Full-resolution trace (evidence blob): `2c5a24aa73b73c2a79202ba98b012b80059b216d21338e4b79aeeb9a1f5597fc`

**TC-MUTE-001**: fail

- Criterion: `always muted_elapsed <= 2.0`
- Failing runs: 113 of 137; errored runs: 0
- Worst robustness: `-3.77`
- Parameter assignment: `{'approach_speed': 0.15, 'pick_duration_s': 4.0}`
- Full-resolution trace (evidence blob): `ace8247a73f240d7e5fc6eb94305da8a09aecf9fee718b28918a5580bd8023ab`

**TC-SPEED-001**: fail

- Criterion: `always speed <= zone_speed_limit + 0.05`
- Failing runs: 50 of 152; errored runs: 0
- Worst robustness: `-0.592499`
- Parameter assignment: `{'floor_mu': 0.1, 'nav_position_error': -0.5, 'truck_speed': 2.0, 'zone_speed_limit': 0.3}`
- Full-resolution trace (evidence blob): `db78089ec31754ab5034e4f33e2bd0e57302561e5b101a700f04a71da4e6e29b`

## 6 Findings

| Severity | Rule | Subject | Disposition | Detail |
|---|---|---|---|---|
| blocker | R-EXEC-001 | TC-CROSS-001 | open | falsified: worst robustness -0.548 at {'entry_gap': 1.0, 'floor_mu': 0.1, 'pedestrian_speed': 1.6, 'pedestrian_start_offset': 0.5, 'truck_speed': 2.0, 'warning_field': 0.0} |
| blocker | R-EXEC-001 | TC-CROSS-002 | open | falsified: worst robustness -0.542318 at {'entry_gap': 1.0, 'floor_mu': 0.1, 'pedestrian_speed': 1.6, 'rack_clearance': 0.3, 'truck_speed': 2.0, 'warning_field': 0.0} |
| blocker | R-EXEC-001 | TC-FIELD-001 | open | falsified: worst robustness -2.63533 at {'floor_mu': 0.1, 'goal_distance': 30.0, 'truck_speed': 2.0} |
| blocker | R-EXEC-001 | TC-SPEED-001 | open | falsified: worst robustness -0.592499 at {'floor_mu': 0.1, 'nav_position_error': -0.5, 'truck_speed': 2.0, 'zone_speed_limit': 0.3} |
| blocker | R-EXEC-001 | TC-MUTE-001 | open | falsified: worst robustness -3.77 at {'approach_speed': 0.15, 'pick_duration_s': 4.0} |
| blocker | R-EXEC-002 | TC-CROSS-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-CROSS-002 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-FIELD-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-SPEED-001 | open | verifies a PL d obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-MUTE-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-PL-001 | SF-PDS-01 | open | achieved PL d < required PLr e (Cat 3, MTTFd high, DCavg medium, CCF 75) |
| blocker | R-TRACE-002 | SR-COLL-003 | open | requirement has no test case |

## 7 Limitations of the method

Stated explicitly so that the scope of the claims in this
document is not overread.

1. **Falsification, not proof.** The verification campaign
   searches the scenario parameter space for counterexamples
   using boundary sampling, low-discrepancy sweeps and
   robustness-guided optimisation. Absence of a counterexample
   is evidence, not proof, that none exists. No claim of
   exhaustive coverage of a continuous parameter space is made
   or implied.

2. **Simulation is not physical verification.** Results
   obtained at MIL, SIL or replay tier support the design
   argument. They do not on their own discharge the
   verification obligations of EN ISO 3691-4, which are
   ultimately physical. The evidence tier of every result is
   recorded in section 5 and enforced by policy rule
   R-EXEC-002.

3. **Model validity bounds the result.** A simulated result is
   only as good as the vehicle, sensor and environment models
   behind it. Model validation evidence is a separate
   obligation and is not contained in this document.

4. **The simplified route for Performance Level.** Performance
   Levels in section 3 are determined via the simplified route
   of ISO 13849-1 Annex K. A full Markov analysis may yield a
   different PFHd. Where a certified subsystem PFHd is
   available from the component manufacturer, it takes
   precedence.

5. **Parameter coverage is a proxy.** Two-way coverage
   measures how much of the declared parameter space was
   exercised. It says nothing about whether the declared space
   correctly bounds the intended operating conditions. That
   judgement is an input to this process, not an output.
