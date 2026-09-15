"""
safegate.execution.adapter
==========================

The execution boundary. Everything above this line is standards logic;
everything below is somebody else's simulator.

The `RunnerAdapter` protocol is deliberately tiny — two methods — because
the integration surface is where tools like this rot. A fat adapter
interface means every new backend is a six-week project and the product
stalls at two backends.

Implementations shipped:

  NullRunner    closed-form kinematics. Exactly reproducible. Exists so
                the tool itself can be tested, and so CI has a smoke path
                with no simulator installed.
  ReplayRunner  rosbag2 / MCAP playback, open loop.
  ScenarioExecutionRunner
                shells out to Intel Labs' `scenario_execution`
                (arXiv:2409.07080, Apache-2.0), which already solves
                OpenSCENARIO 2 parsing, behaviour-tree execution, Gazebo
                and ROS2 plumbing, and rosbag capture.
  HilRunner     documented stub; the contract a physical rig must meet.

Build-vs-buy, stated explicitly because it is the most important
architectural decision in the system: scenario *execution* is solved and
open-source. What is not solved by anything on the market is the layer
above it — traceability, PL derivation, conformity policy, technical-file
generation. Reimplementing OSC2 parsing would burn a year and add nothing
defensible. The moat is compliance, not simulation.

Determinism contract: given identical `Pinning`, `execute` MUST return an
identical trace. Adapters that cannot honour that set
`deterministic = False`, and the orchestrator then runs each point three
times and marks disagreement as FLAKY rather than silently reporting
whichever result came first. A non-deterministic safety test is itself a
finding.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from ..core.model import ConcreteRun, ExecutionTier
from ..stl.robustness import Trace


@dataclass
class ExecutionOutcome:
    trace: Trace | None
    ok: bool
    message: str = ""
    raw_artifacts: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class RunnerAdapter(Protocol):
    name: str
    tier: ExecutionTier
    deterministic: bool

    def backend_hash(self) -> str:
        """Digest identifying the backend build. Becomes part of Pinning."""
        ...

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome: ...


# --------------------------------------------------------------------------


class NullRunner:
    """Closed-form kinematic model of one scenario family: a person crosses
    the truck's path.

    The truck detects at `detection_range`, reacts after `latency`, then
    decelerates at `brake_decel * floor_friction`. Sensor noise is seeded
    from the run's pinning, so it is reproducible without being constant.

    This is not a physics engine and does not pretend to be. It exists to
    make every layer above it testable in milliseconds, which is what
    keeps the conformity logic honest.
    """

    name = "null"
    tier = ExecutionTier.SIL
    deterministic = True

    def __init__(self, dt: float = 0.01, horizon_s: float = 12.0) -> None:
        self.dt = dt
        self.horizon = horizon_s

    def backend_hash(self) -> str:
        return hashlib.sha256(
            f"null-runner/v3/dt={self.dt}/T={self.horizon}".encode()
        ).hexdigest()

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        p = run.assignment
        v0 = float(p.get("truck_speed", 1.2))
        a_brake = float(p.get("brake_decel", 1.5))
        latency = float(p.get("latency", 0.25))
        det_range = float(p.get("detection_range", 4.0))
        ped_speed = float(p.get("pedestrian_speed", 1.2))
        lateral0 = float(p.get("pedestrian_lateral_offset", 3.0))
        friction = float(p.get("floor_friction", 1.0))
        noise_sd = float(p.get("sensor_noise_sd", 0.0))
        occlusion_s = float(p.get("occlusion_duration", 0.0))
        zone_limit_v = float(p.get("zone_speed_limit", 2.0))

        a_eff = max(0.05, a_brake * friction)
        rng = np.random.default_rng(run.pinning.seed)

        n = int(self.horizon / self.dt) + 1
        t = np.arange(n) * self.dt
        x = np.zeros(n)
        v = np.zeros(n)
        v[0] = v0
        x[0] = -(det_range + v0 * 2.0)
        ped_x = 0.0
        ped_y = lateral0 - ped_speed * t

        detected_at = math.inf
        brake_from = math.inf
        muted = np.zeros(n)

        for i in range(1, n):
            gap = ped_x - x[i - 1]
            measured = gap + (rng.normal(0.0, noise_sd) if noise_sd > 0 else 0.0)
            occluded = t[i] < occlusion_s
            muted[i] = 1.0 if occluded else 0.0
            visible = (not occluded) and measured <= det_range and abs(ped_y[i]) < 1.5
            if visible and detected_at is math.inf:
                detected_at = t[i]
                brake_from = detected_at + latency
            v[i] = max(0.0, v[i - 1] - a_eff * self.dt) if t[i] >= brake_from else v[i - 1]
            x[i] = x[i - 1] + v[i] * self.dt

        dx = ped_x - x
        dist = np.sqrt(np.maximum(dx, 0.0) ** 2 + ped_y**2)
        min_dist = np.minimum.accumulate(dist)
        in_field = ((np.abs(ped_y) < 1.5) & (dx <= det_range) & (t >= occlusion_s)).astype(float)

        trace = Trace(
            time=t,
            signals={
                "speed": v,
                "min_distance_to_person": min_dist,
                "distance_to_person": dist,
                "protective_field_length": np.full(n, det_range),
                "person_in_field": in_field,
                "zone_speed_limit": np.full(n, zone_limit_v),
                "protective_device_muted": muted,
                "truck_x": x,
                "pedestrian_y": ped_y,
                "__time__": t,
            },
        )
        return ExecutionOutcome(trace=trace, ok=True)


# --------------------------------------------------------------------------


class ReplayRunner:
    """Open-loop replay of recorded field data.

    Evidence weight is lower than closed-loop, and the policy engine knows
    it: replay shows the perception stack would have produced the right
    output, not that the vehicle would have stopped. Treating the two as
    equivalent is a common and serious error in AMR safety cases.
    """

    name = "replay"
    tier = ExecutionTier.REPLAY
    deterministic = True

    def __init__(self, bag_root: str | os.PathLike[str]) -> None:
        self.bag_root = Path(bag_root)

    def backend_hash(self) -> str:
        h = hashlib.sha256(b"replay/v1")
        for p in sorted(self.bag_root.rglob("*")):
            if p.is_file():
                h.update(p.name.encode())
                h.update(str(p.stat().st_size).encode())
        return h.hexdigest()

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        bag_id = str(run.assignment.get("bag_id", "")).strip()
        cached = self.bag_root / f"{bag_id}.signals.npz"
        if not cached.exists():
            return ExecutionOutcome(None, False, f"no extracted signals for {bag_id}")
        d = np.load(cached)
        sigs = {k: d[k] for k in d.files if k != "time"}
        sigs["__time__"] = d["time"]
        return ExecutionOutcome(Trace(time=d["time"], signals=sigs), True)


# --------------------------------------------------------------------------


class ScenarioExecutionRunner:
    """Delegate to Intel Labs `scenario_execution` for OSC2 + Gazebo + ROS2."""

    name = "scenario_execution"
    tier = ExecutionTier.SIL
    deterministic = False  # Gazebo physics + ROS2 scheduling are not bit-exact

    def __init__(
        self,
        ros_setup: str = "/opt/ros/jazzy/setup.bash",
        launch_pkg: str = "scenario_execution_ros",
        timeout_s: float = 300.0,
        signal_extractor: str | None = None,
    ) -> None:
        self.ros_setup = ros_setup
        self.launch_pkg = launch_pkg
        self.timeout_s = timeout_s
        self.signal_extractor = signal_extractor

    def backend_hash(self) -> str:
        try:
            out = subprocess.run(
                ["bash", "-lc", f"source {self.ros_setup} && ros2 pkg xml {self.launch_pkg}"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except Exception:
            out = "unavailable"
        return hashlib.sha256(out.encode()).hexdigest()

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        if shutil.which("ros2") is None:
            return ExecutionOutcome(None, False, "ros2 not on PATH")
        osc = workdir / "scenario.osc"
        bag = workdir / "bag"
        cmd = (
            f"source {self.ros_setup} && "
            f"ros2 launch {self.launch_pkg} scenario_launch.py "
            f"scenario:={osc} output_dir:={bag} scenario_status:=true"
        )
        try:
            proc = subprocess.run(
                ["bash", "-lc", cmd], capture_output=True, text=True, timeout=self.timeout_s
            )
        except subprocess.TimeoutExpired:
            return ExecutionOutcome(None, False, "scenario execution timed out")
        if proc.returncode != 0:
            return ExecutionOutcome(None, False, proc.stderr[-2000:])
        npz = workdir / "signals.npz"
        if self.signal_extractor:
            subprocess.run(
                ["bash", "-lc", f"{self.signal_extractor} {bag} {npz}"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        if not npz.exists():
            return ExecutionOutcome(None, False, "signal extraction produced no output")
        d = np.load(npz)
        sigs = {k: d[k] for k in d.files if k != "time"}
        sigs["__time__"] = d["time"]
        return ExecutionOutcome(
            Trace(time=d["time"], signals=sigs),
            True,
            raw_artifacts={"rosbag": str(bag), "stdout": proc.stdout[-4000:]},
        )


# --------------------------------------------------------------------------


class HilRunner:
    """Contract for a physical hardware-in-the-loop rig.

    Left as a documented stub because the implementation is rig-specific
    (dSPACE SCALEXIO, Vector VT System, or a bespoke bench). Two contract
    points that are not negotiable:

      1. The rig reports its own firmware and configuration digests, so
         `Pinning` is complete.
      2. The rig refuses to run if the safety scanner's configuration
         checksum differs from the one the project declares. A HIL result
         whose scanner configuration is unknown is not evidence; it is an
         anecdote with a timestamp.
    """

    name = "hil"
    tier = ExecutionTier.HIL
    deterministic = False

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def backend_hash(self) -> str:
        raise NotImplementedError("query the rig for firmware + config digests")

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        raise NotImplementedError("site-specific")


__all__ = [
    "ExecutionOutcome",
    "HilRunner",
    "NullRunner",
    "ReplayRunner",
    "RunnerAdapter",
    "ScenarioExecutionRunner",
]
