# Verification and Validation Report

| | |
|---|---|
| Manufacturer | Example Intralogistics GmbH (fictional) |
| Product | driverless industrial truck (tow tractor) / T200-warehouse rev B |
| Document | VV-T200 rev B |
| Machine type | driverless industrial truck (tow tractor) |
| Variant | T200-warehouse rev B |
| Issued | 2026-09-15 05:23 UTC |
| Author | P. Yadava |
| Approver | not approved |
| Notified Body | not engaged |
| **Gate status** | **NON-CONFORMING** |

**Evidence commitment**

| | |
|---|---|
| Campaign | `T200-revB` |
| Merkle root | `223d5e70babd17387686824ffab7738a743c21624ae908eb7c7d60b3f55ce83a` |
| SUT build | `example-build-revB` |
| Configuration | `1750acdb7399100a7cc40c5885c544e68b158aa5f54b0b2d982de7405209e637` |
| Runner / backend | `sil` / `0b2396fe02505741` |
| Project data digest | `70f0921e9c2762a4` |
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
| SF-PDS-01 Personnel detection and safe stop | 4 | 33.0 y (high) | 99.0% (high) | 80 | **e** | 1e-08 to 1e-07 |
| SF-SPD-01 Zone speed supervision | 2 | 32.6 y (high) | 92.7% (medium) | 70 | **d** | 1e-07 to 1e-06 |

### 3.1 Derivations

**SF-PDS-01: Personnel detection and safe stop**

```
Architecture ARCH-PDS-CAT4  Category 4
  channel 1: MTTFd = 33.0 y (capped 33.0 y) -> high
      safety_laser_scanner_front   MTTFd=   100.0 y  DC=99.0%
      safety_controller_ch1        MTTFd=   120.0 y  DC=99.0%
      brake_contactor_ch1          MTTFd=    83.3 y  DC=99.0%
  channel 2: MTTFd = 33.0 y (capped 33.0 y) -> high
      safety_laser_scanner_rear    MTTFd=   100.0 y  DC=99.0%
      safety_controller_ch2        MTTFd=   120.0 y  DC=99.0%
      brake_contactor_ch2          MTTFd=    83.3 y  DC=99.0%
  combined MTTFd = 33.0 y (high)
  DCavg = 99.0% (high)
  CCF score = 80
  => PL e  (PFHd in [1.0e-08, 1.0e-07) 1/h)
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
- Guaranteed minimum deceleration: 0.95 m/s²
- Device measurement tolerance: 90 mm
- Human approach term credited: yes, K = 1.6 m/s over latency plus stopping time
- Design margin: 100 mm

```
 v [m/s]   latency   braking  approach     tol   margin  L_req [m]
------------------------------------------------------------------
    0.30     0.051     0.047     0.777   0.090    0.100      1.066
    0.60     0.102     0.189     1.283   0.090    0.100      1.764
    1.00     0.170     0.526     1.956   0.090    0.100      2.843
    1.50     0.255     1.184     2.798   0.090    0.100      4.428
    2.00     0.340     2.105     3.640   0.090    0.100      6.276
```

## 5 Verification results

Runner `sil`. The tier column is the tier of the evidence that was produced, followed by the tier the test case declares.

| Test case | Requirement(s) | Runs | Errors | Tier (evidence / declared) | Verdict | Worst margin | 2-way cov. |
|---|---|---|---|---|---|---|---|
| TC-CROSS-001 | SR-COLL-001 | 214 | 0 | sil / sil | **PASS** | 0.05621 | 100% |
| TC-CROSS-002 | SR-COLL-001 | 214 | 0 | sil / sil | **PASS** | 0.05875 | 100% |
| TC-FIELD-001 | SR-COLL-002 | 206 | 0 | sil / sil | **PASS** | 0.2975 | 100% |
| TC-MUTE-001 | SR-MUTE-001 | 200 | 0 | sil / sil | **PASS** | 0.2 | 100% |
| TC-REV-001 | SR-COLL-003 | 214 | 0 | sil / sil | **PASS** | 0.0977 | 100% |
| TC-SPEED-001 | SR-SPEED-001 | 214 | 0 | sil / sil | **PASS** | 0.055 | 100% |

Total runs executed: **1262**.

Worst margin is the minimum STL robustness over all runs, in the unit of the criterion (metres, seconds or m/s). Negative means violated.

## 6 Findings

| Severity | Rule | Subject | Disposition | Detail |
|---|---|---|---|---|
| blocker | R-EXEC-002 | TC-CROSS-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-CROSS-002 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-FIELD-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-SPEED-001 | open | verifies a PL d obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-MUTE-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |
| blocker | R-EXEC-002 | TC-REV-001 | open | verifies a PL e obligation but the best evidence tier is sil, below hil. Simulation alone does not discharge a verification obligation at this PL. |

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
