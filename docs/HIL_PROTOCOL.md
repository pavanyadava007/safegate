# safegate-hil/1

The contract between `HilRunner` and a hardware-in-the-loop rig: a bench with the
real safety controller and safety scanner, driven by a plant model or a physical
target, such as a dSPACE SCALEXIO, a Vector VT System or a bespoke rig. The rig
implements two HTTP endpoints with JSON bodies.

## `GET /identity`

```json
{
  "protocol": "safegate-hil/1",
  "rig_id": "hil-bench-02",
  "physical": true,
  "firmware_digest": "sha256 of the safety controller firmware image",
  "rig_image_digest": "sha256 of the rig software and plant model",
  "scanner_config_checksum": "the checksum the scanner reports for its field configuration"
}
```

`protocol`, `rig_id`, `firmware_digest`, `rig_image_digest` and
`scanner_config_checksum` are required. `physical` is `true` only when the safety
controller and scanner in the loop are real hardware; if it is absent it counts as
`false`.

## `POST /runs`

Request:

```json
{
  "scenario": "scenarios/person_crossing.osc",
  "assignment": {"truck_speed": 1.0, "entry_gap": 3.0},
  "pinning": {"scenario_hash": "...", "sut_build_hash": "...", "backend_hash": "...",
              "config_hash": "...", "seed": 123},
  "expected_scanner_config_checksum": "..."
}
```

Response:

```json
{
  "ok": true,
  "message": "",
  "firmware_digest": "...",
  "rig_image_digest": "...",
  "scanner_config_checksum": "...",
  "time": [0.0, 0.01],
  "signals": {"speed": [1.0, 1.0], "min_distance_to_person": [4.2, 4.19]}
}
```

`signals` uses the names in docs/SIMULATOR.md, so the same STL criteria and derived
signals apply. A rig whose scanner configuration differs from
`expected_scanner_config_checksum` must refuse the run (HTTP 409).

## What the runner enforces

1. **Complete pinning.** The identity digests become the backend and configuration
   hashes of every run, and the rig's firmware digest becomes the campaign's build
   hash: `--build` may be omitted, and a `--build` that differs is refused. A missing
   required identity field aborts the campaign before any run; a missing `physical`
   is treated as `false` (see 4).
2. **Known scanner configuration.** With `--scanner-checksum`, a rig reporting a
   different checksum is refused at connection.
3. **No change mid-campaign.** Every response repeats the digests; a run whose
   digests differ from the identity is recorded as an ERROR ("rig changed during
   the campaign") and is not used as evidence.
4. **Only physical rigs make HIL evidence.** `physical: false` is refused unless
   `--allow-emulated-rig` is given, and then runs are recorded at SIL tier under
   the runner name `hil-emulated`.
5. **Non-deterministic by default.** Physical rigs do not reproduce bit-exactly, so
   the campaign runs every point three times and records disagreement as FLAKY.

## Emulator

`python -m safegate.execution.hil_emulator --sut sut_config.yaml --port 8765`
serves the protocol from the SIL world and always reports `physical: false`. It
exists for the contract tests in `tests/test_runners.py` and for trying the
client:

```bash
python -m safegate.execution.hil_emulator --sut examples/amr_project_revb/sut_config.yaml &
safegate run examples/amr_project_revb --runner hil --rig-url http://127.0.0.1:8765 \
    --allow-emulated-rig --only TC-MUTE-001 --store /tmp/hil-demo
```

Nothing it produces satisfies R-EXEC-002.
