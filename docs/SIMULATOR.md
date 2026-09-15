# The SIL world

`safegate.sim` is the system under test for the example campaigns: a deterministic,
closed-loop 2D model of a driverless tow tractor in a warehouse aisle, with the
safety-controller behaviour that EN ISO 3691-4 personnel detection and speed
supervision depend on. It replaces the closed-form `NullRunner` as the default
runner; `NullRunner` is kept as a millisecond smoke path.

It is a software-in-the-loop backend. Its evidence is recorded at SIL tier, and
policy rule R-EXEC-002 keeps SIL evidence from discharging a PL d or PL e
verification obligation.

## What is modelled

| Part | Model | Parameters (`sut_config.yaml`) |
|---|---|---|
| Truck | rectangle 2.0 x 1.0 m on the aisle axis, forward or reverse, 100 Hz | `truck_length`, `truck_width` |
| Braking | safe stop decelerates at `min(safety_brake_decel, mu * g)`; navigation accelerates and brakes at its own limits, also capped by `mu * g` | `safety_brake_decel`, `nav_accel`, `nav_decel`; `floor_mu` is a scenario parameter |
| Safety scanners | one per end; protective field is a rectangle ahead of the leading edge, `field_side_margin` wider than the truck, shortened by the measurement tolerance; detection needs line of sight (ray cast against racks); output delayed by the response time | `front_field_sets`, `rear_field_sets`, `scanner_response_s`, `scanner_tolerance_m` |
| Field switching | the field set is chosen from the measured speed (delayed by the encoder latency) plus a selection margin; above the largest set the controller trips | `field_selection_speed_margin`, `encoder_latency_s`, `field_overspeed_trip_offset` |
| Safe stop | any stop cause reaches the brakes after controller reaction plus bus latency; the stop latches until the cause has been clear for `restart_delay_s` at standstill | `controller_reaction_s`, `comm_latency_s`, `restart_delay_s` |
| Timing | every latency is a delay line of whole control periods, rounded up (never shorter than configured); timeouts count integer steps | `dt` = 0.01 s; a runner argument, not a `sut_config.yaml` key (`SAFEGATE_DT` for the OSC2 path) |
| Zone speed supervision | trips when the measured speed exceeds the zone limit plus the trip offset; optional ramp monitoring trips ahead of a slower zone when the speed is above the braking curve to it (latency-compensated, guaranteed deceleration) | `zone_ramp_monitoring`, `zone_ramp_decel`, `zone_trip_offset`, `speed_zones` |
| Muting | navigation requests muting; the controller suppresses detection while the request holds, withdraws it after `mute_timeout_s`, and re-arms only when the request drops | `mute_timeout_s` |
| Navigation (not safety-rated) | cruise, stop at goal, brake for zones using its own position estimate (which can be wrong), slow to `warning_speed` when a wide warning field sees a person; a reversal of direction waits for standstill | `warning_field_*`, `warning_speed`; `nav_position_error` is a scenario parameter |
| Pedestrians | circles of radius 0.25 m walking to a target at constant speed; they do not walk into the truck body, but the truck can drive into them | scenario parameters |

Recorded signals (one sample per control period): `speed`, `speed_commanded`,
`truck_x`, `leading_edge_x`, `min_distance_to_person` (signed: negative is
overlap depth), `person_in_field`, `detection`, `safe_stop`,
`protective_field_length`, `protective_device_muted`, `mute_requested`,
`zone_speed_limit`, `warning_active`, `floor_decel_limit`.

Derived signals (`safegate.iso3691.derived`, added before the criterion is
evaluated, for every runner): `required_field_length` from the project's
stopping budget, `muted_elapsed`, `separation_while_moving`.

## What is not modelled

Tyre slip beyond the friction cap, load transfer, steering and curved paths,
scanner noise, reflectivity and blind zones, pedestrians reacting to the truck,
multiple trucks, and any fault in the safety controller itself. A result is only
as good as these assumptions; model validation is a separate obligation and the
technical file says so.

## Scenario families

| Family | Situation | Parameters |
|---|---|---|
| `person_crossing` | a person walks into an open aisle in front of a cruising truck | `truck_speed`, `pedestrian_speed`, `entry_gap`, `pedestrian_start_offset`, `approach_side`, `floor_mu`, `warning_field` |
| `occluded_emergence` | a person walks out of a cross aisle between two rack blocks | `truck_speed`, `pedestrian_speed`, `entry_gap`, `rack_clearance`, `approach_side`, `floor_mu`, `warning_field` |
| `field_switching` | accelerate from rest, cruise, stop at a goal, empty aisle | `truck_speed`, `goal_distance`, `floor_mu` |
| `speed_zone_entry` | a cruising truck enters a slower zone | `truck_speed`, `zone_speed_limit`, `nav_position_error`, `floor_mu` |
| `muting_pick_station` | the truck docks and navigation requests muting for the pick | `approach_speed`, `pick_duration_s`, `mute_start_gap` |
| `reversing_at_dock` | the truck starts reversing while a person walks toward it | `reverse_speed`, `person_gap`, `pedestrian_speed`, `lateral_offset`, `floor_mu`, `warning_field` |

`entry_gap` is the distance from the truck's leading edge to the crossing line at
the moment the person reaches the truck's corridor, had the truck kept cruising.
It is the ODD assumption that matters most: nothing protects a person who steps
in closer than the stopping distance.

`warning_field: [0, 1]` sweeps the non-safety slowdown off as well as on. The rev A
campaign shows why: with the warning field on, navigation slows early enough that
most crossings pass, and the counterexamples appear only with it off. A safety
function must hold without credit for non-safety layers.

## Two ways to run the same scenario

The scenario file named by a test case (for example `scenarios/person_crossing.osc`)
is an OpenSCENARIO 2 file.

- `--runner sil` builds the world in-process from the file's stem and advances
  the behaviour as Python coroutines. Fast (see the micro-benchmarks in RESULTS.md)
  and used for CI.
- `--runner scenario_execution` hands the file to Intel Labs `scenario_execution`
  1.5.0 with `--simulation safegate.sim.osc_bridge:SafeGateSimulation` and a
  per-run `--scenario-parameter-file`. `scenario_execution` parses the OSC2, builds
  the py_trees behaviour tree and drives the loop on its simulation clock; the
  SafeGate actions (`import osc.safegate`) call the same `Actions` methods the
  Python scripts call.

Both loops do one physics step, then one behaviour tick, so an action takes
effect on the same control period. `tests/test_runners.py` checks every family is
bit-identical across the two runners, and `scripts/reproduce.py --osc` compares a
full campaign run by run.

## Through ROS 2

`--runner ros2` runs the same world as a ROS 2 Jazzy node (`safegate.ros.plant`) in an
isolated container built from `docker/ros2/Dockerfile`. Every control period is one
`sensor_msgs/JointState` on `/safegate/state` (names are the signals above, positions
their values, the header stamp is simulation time). `ros2 bag record` writes MCAP, and
`safegate.execution.rosbag` extracts the bag on the host with
`safegate/ros/safegate_state_mapping.yaml`. A site replaces the plant (Gazebo, a real
truck) and the mapping; the extraction and evidence path stay the same. RESULTS.md
compares a campaign through ROS 2 with the in-process runner.

Containers run with `--network none` and localhost-only ROS 2 discovery. Without that,
parallel runs on Docker's default bridge network discovered each other over DDS
multicast, recorders captured other runs, and the campaign came back FLAKY.

The `scenario_execution_ros` / Gazebo path (`ScenarioExecutionRunner(backend="ros")`)
is kept for sites that have it and uses the same bag extraction. It is not exercised
here.
