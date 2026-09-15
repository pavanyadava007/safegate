# Verification and Validation Report

| | |
|---|---|
| Manufacturer | Example Intralogistics GmbH |
| Product | driverless industrial truck (tow tractor) / T200-warehouse |
| Document | VV-T200 rev A |
| Machine type | driverless industrial truck (tow tractor) |
| Variant | T200-warehouse |
| Issued | 2026-09-14 22:19 UTC |
| Author | P. Yadava |
| Approver | — not approved — |
| Notified Body | not engaged |
| **Gate status** | **NON-CONFORMING** |

**Evidence commitment**

| | |
|---|---|
| Campaign | `amr-tug-t200-edeaaff3f177` |
| Merkle root | `848813aed04d92aa5e11947f4a189e8816f6892fa01ad379689dc824d5c1ac8a` |
| Manifest head | `adbaa92d96b9dc0248cd2af3015411cfa0b7b086c7eadafeafd87b435e0a89b0` |
| SUT build | `edeaaff3f1774ad2888673770c6d64097e391bc3` |
| Configuration | `cfg-a1b2` |

Any party holding this document and the evidence store can
recompute the Merkle root and confirm that no test record has
been added, removed or altered since issue.

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
| HAZ-COLL-002 | Collision during reversing without direct line of sight | dock | S2 | F1 | P2 | **d** |
| HAZ-MUTE-001 | Protective device muted beyond its justification | pick_station | S2 | F1 | P2 | **d** |
| HAZ-SPEED-001 | Overspeed in a reduced-speed zone | junction | S2 | F1 | P1 | **c** |

## 3 Safety functions and achieved Performance Levels

| Safety function | Cat | MTTFd | DCavg | CCF | Achieved PL | PFHd band [1/h] |
|---|---|---|---|---|---|---|
| SF-PDS-01 Personnel detection and safe stop | 3 | 33.0 y (high) | 94.3% (medium) | 75 | **d** | 1e-07 – 1e-06 |
| SF-SPD-01 Zone speed supervision | 2 | 32.6 y (high) | 92.7% (medium) | 70 | **d** | 1e-07 – 1e-06 |

### 3.1 Derivations

**SF-PDS-01 — Personnel detection and safe stop**

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

**SF-SPD-01 — Zone speed supervision**

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
- Human approach term credited: yes (K = 1.6 m/s)
- Design margin: 100 mm

```
 v [m/s]   latency   braking     tol   margin  L_req [m]
--------------------------------------------------------
    0.30     0.051     0.037   0.090    0.100      0.550
    0.60     0.102     0.150   0.090    0.100      0.714
    1.00     0.170     0.417   0.090    0.100      1.049
    1.50     0.255     0.938   0.090    0.100      1.655
    2.00     0.340     1.667   0.090    0.100      2.469
```

## 5 Verification results

| Test case | Requirement(s) | Runs | Tier | Verdict | Worst margin | 2-way cov. |
|---|---|---|---|---|---|---|
| TC-CROSS-001 | SR-COLL-001 | 216 | sil | **FAIL** | -0.1 | 100% |
| TC-CROSS-002 | SR-COLL-001 | 312 | sil | **PASS** | 2.227 | 100% |
| TC-MUTE-001 | SR-MUTE-001 | 201 | sil | **FAIL** | -0.5 | 100% |
| TC-SPEED-001 | SR-SPEED-001 | 207 | sil | **FAIL** | -1.2 | 100% |

Total runs executed: **936**.

### 5.1 Counterexamples and non-reproducible results

**TC-CROSS-001** — fail

- Worst robustness: `-0.1`
- Parameter assignment: `{'brake_decel': 0.8, 'detection_range': 2.5, 'floor_friction': 0.55, 'latency': 0.1, 'pedestrian_lateral_offset': 6.0, 'pedestrian_speed': 0.5, 'truck_speed': 2.0}`
- Full-resolution trace: `a26abb3c8be2ca4e9ae9c206364c022717879fc78dfcad1bcb91ae62c1f87475`

**TC-MUTE-001** — fail

- Worst robustness: `-0.5`
- Parameter assignment: `{'occlusion_duration': 1.5, 'truck_speed': 0.65}`
- Full-resolution trace: `49b315c8ff6a267de8ef3e075cd92a9660244248cb78737090989e00a4eb1e70`

**TC-SPEED-001** — fail

- Worst robustness: `-1.2`
- Parameter assignment: `{'brake_decel': 1.0, 'truck_speed': 1.8, 'zone_speed_limit': 0.6}`
- Full-resolution trace: `8c56a0a69b1428662f6728e2297a2997e4a9ec27cdd8d24d6f44dea3be8d3559`

## 6 Findings

| Severity | Rule | Subject | Disposition | Detail |
|---|---|---|---|---|
| blocker | R-EXEC-001 | TC-CROSS-001 | open | falsified: worst robustness -0.1 at {'brake_decel': 0.8, 'detection_range': 2.5, 'floor_friction': 0.55, 'latency': 0.1, 'pedestrian_lateral_offset': 6.0, 'pedestrian_speed': 0.5, 'truck_speed': 2.0} |
| blocker | R-EXEC-001 | TC-SPEED-001 | open | falsified: worst robustness -1.2 at {'brake_decel': 1.0, 'truck_speed': 1.8, 'zone_speed_limit': 0.6} |
| blocker | R-EXEC-001 | TC-MUTE-001 | open | falsified: worst robustness -0.5 at {'occlusion_duration': 1.5, 'truck_speed': 0.65} |
| blocker | R-EXEC-002 | TC-CROSS-001 | open | verifies a PL d function but the highest evidence tier is below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-CROSS-002 | open | verifies a PL d function but the highest evidence tier is below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-SPEED-001 | open | verifies a PL d function but the highest evidence tier is below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-MUTE-001 | open | verifies a PL d function but the highest evidence tier is below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-PL-001 | SF-PDS-01 | open | achieved PL d < required PLr e (Cat 3, MTTFd high, DCavg medium, CCF 75) |
| blocker | R-TRACE-002 | SR-COLL-002 | open | requirement has no test case |

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
