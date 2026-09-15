# Demo walkthrough

A ten-minute live tour. Every command runs from the repository root with the
virtual environment active; the numbers you will see match docs/RESULTS.md.

## 1. Derived, not asserted (1 min)

```bash
safegate pl examples/amr_project
```

Point at `SF-PDS-01`: Category 3, MTTFd high, DCavg medium, so PL d. Then at
`ARCH-PDS-ML`: the determination is invalid (CCF 55, one channel), and the note on the
Notified Body route under Regulation (EU) 2023/1230 Annex I Part A.

```bash
safegate fields examples/amr_project
```

The approach column is K x (latency + stopping time). At 1.5 m/s the required field is
3.65 m; the rev A table has 1.70 m.

## 2. The campaign finds the design gaps (2 min)

```bash
safegate keygen /tmp/demo-key
safegate run examples/amr_project --store /tmp/demo-a --build demo-a --workers 16 \
    --signing-key /tmp/demo-key/signing.key
```

Five FAIL verdicts. Talk through two:

- TC-FIELD-001, -2.635 m: the field table was sized with a formula that ignored the
  person walking toward the truck while it brakes.
- TC-CROSS-001: every failing run has `warning_field=0`. The non-safety slowdown was
  hiding the problem; a safety function gets no credit for it.

## 3. The gate and the report (2 min)

```bash
safegate gate examples/amr_project --store /tmp/demo-a \
    --policy examples/amr_project/policy.yaml --trusted-key /tmp/demo-key/signing.pub
safegate report examples/amr_project --store /tmp/demo-a --trusted-key /tmp/demo-key/signing.pub \
    --signing-key /tmp/demo-key/signing.key -o /tmp/demo-a.md
```

Blockers: PL d against PLr e, the reversing requirement with no test case, the five
counterexamples, and R-EXEC-002 (simulation is not HIL). Open `/tmp/demo-a.md`: cover
sheet with the Merkle root, section 5.1 counterexamples, section 7 limitations.

## 4. Tamper evidence (2 min)

```bash
safegate verify --store /tmp/demo-a --trusted-key /tmp/demo-key/signing.pub --report /tmp/demo-a.md
sed -i 's/NON-CONFORMING/CONFORMING/' /tmp/demo-a.md
safegate verify --store /tmp/demo-a --trusted-key /tmp/demo-key/signing.pub --report /tmp/demo-a.md
```

The second call fails: the edited report no longer matches the digest the chain
recorded. For the rewrite-and-re-sign attack and why pinned keys and anchors exist,
show the tamper table in docs/RESULTS.md.

## 5. Rev B, and the honest end state (2 min)

```bash
safegate run examples/amr_project_revb --store /tmp/demo-b --build demo-b --workers 16 \
    --signing-key /tmp/demo-key/signing.key
safegate gate examples/amr_project_revb --store /tmp/demo-b \
    --policy examples/amr_project_revb/policy.yaml --trusted-key /tmp/demo-key/signing.pub
```

All six test cases pass, and the gate still fails on R-EXEC-002 only: the design
argument is complete, physical verification is not, and the tool refuses to let
simulation stand in for it.

Worth telling: the independent code review found that a shared trip offset made rev B
safe-stop above 0.94 m/s, so it had "passed" without cruising at its top test speed.
Fixing the model cut the crossing margin from 0.32 m to 0.056 m. A campaign can only
falsify what the model lets happen.

## 6. Same evidence path, other backends (1 min)

- `--runner scenario_execution`: every run is an OSC2 file executed by Intel Labs
  `scenario_execution`; 1,262 of 1,262 runs bit-identical to the in-process runner.
- `--runner ros2`: the world as a ROS 2 node, recorded with rosbag2 and extracted; the
  first attempt came back FLAKY because parallel containers heard each other over DDS.
- `--store s3://bucket/prefix`: one object per manifest entry, created with a
  conditional write.

## Questions to expect

- *Does passing SIL mean it is safe?* No. Falsification is not proof, the model leaves
  things out (docs/SIMULATOR.md), and R-EXEC-002 blocks release without HIL evidence.
- *Why STL robustness instead of pass/fail?* Margin in physical units, a search
  objective for the falsifier, and regression detection before a boolean test notices.
- *What stops someone deleting failing runs?* Chain linkage detects it; pinned keys and
  published anchors catch a full rewrite; Object Lock on the bucket prevents deletion.
- *What is not done?* A physical HIL rig, Gazebo/Nav2 as the plant, AWS S3 itself (MinIO
  was used), the full Markov PL route.
