"""
safegate.execution.adapter
==========================

The execution boundary. Everything above this line is standards logic;
everything below is somebody else's simulator.

The `RunnerAdapter` protocol is deliberately tiny (two methods) because
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
                OpenSCENARIO 2 parsing and behaviour-tree execution. Its
                step-based simulation interface drives the SafeGate SIL
                world; a ROS 2 / Gazebo backend is kept for sites that run
                one.
  HilRunner     client for a rig speaking safegate-hil/1, with the contract
                checks a physical rig must pass. `hil_emulator` serves the
                protocol from the SIL world for contract tests; it declares
                itself non-physical and cannot produce HIL-tier evidence.

Build-vs-buy, stated explicitly because it is the most important
architectural decision in the system: scenario *execution* is solved and
open-source. What is not solved by anything on the market is the layer
above it: traceability, PL derivation, conformity policy, technical-file
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
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import yaml

from ..core.model import ConcreteRun, ExecutionTier
from ..stl.robustness import Trace


@dataclass
class ExecutionOutcome:
    trace: Trace | None
    ok: bool
    message: str = ""
    raw_artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)


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
    the truck's path. The truck is a point.

    The truck detects at `detection_range`, reacts after `latency`, then
    decelerates at `brake_decel * floor_friction`. Sensor noise is seeded
    from the run's pinning, so it is reproducible without being constant.

    This is not a physics engine and does not pretend to be. It exists to
    make every layer above it testable in milliseconds. It publishes only
    the signals it actually models: no muting and no speed supervision, so
    a criterion over those signals errors instead of passing vacuously. Use
    `safegate.sim.SilRunner` for the closed-loop SIL world.
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

        for i in range(1, n):
            gap = ped_x - x[i - 1]
            measured = gap + (rng.normal(0.0, noise_sd) if noise_sd > 0 else 0.0)
            occluded = t[i] < occlusion_s
            # Only a person ahead of the truck is in the protective field.
            visible = (
                (not occluded) and 0.0 <= measured <= det_range and abs(ped_y[i]) < 1.5
            )
            if visible and detected_at is math.inf:
                detected_at = t[i]
                brake_from = detected_at + latency
            v[i] = max(0.0, v[i - 1] - a_eff * self.dt) if t[i] >= brake_from else v[i - 1]
            x[i] = x[i - 1] + v[i] * self.dt

        dx = ped_x - x
        # Euclidean distance. Clipping dx at zero (as an earlier version did)
        # pins a truck that has already passed the crossing to the crossing
        # point, and reports "contact" when the person later walks through
        # the empty aisle behind it.
        dist = np.sqrt(dx**2 + ped_y**2)
        min_dist = np.minimum.accumulate(dist)
        in_field = (
            (np.abs(ped_y) < 1.5) & (dx >= 0.0) & (dx <= det_range) & (t >= occlusion_s)
        ).astype(float)

        trace = Trace(
            time=t,
            signals={
                "speed": v,
                "min_distance_to_person": min_dist,
                "distance_to_person": dist,
                "protective_field_length": np.full(n, det_range),
                "person_in_field": in_field,
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

    A recording is either `<bag_root>/<id>.signals.npz` or a rosbag2
    directory `<bag_root>/<id>/`, extracted with the topic-to-signal
    `mapping` (see execution/rosbag.py). Run assignments are numeric, so a
    replay test case selects the recording with `bag_index` (position in the
    sorted list of recording ids; declare it as discrete values) or with a
    numeric `bag_id`.
    """

    name = "replay"
    tier = ExecutionTier.REPLAY
    deterministic = True

    def __init__(
        self, bag_root: str | os.PathLike[str], mapping: str | os.PathLike[str] | None = None
    ) -> None:
        self.bag_root = Path(bag_root)
        self.mapping = Path(mapping) if mapping else None

    def backend_hash(self) -> str:
        h = hashlib.sha256(b"replay/v2")
        if self.mapping is not None:
            h.update(self.mapping.read_bytes())
        for p in sorted(self.bag_root.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(self.bag_root)).encode())
                h.update(str(p.stat().st_size).encode())
        return h.hexdigest()

    def recordings(self) -> list[str]:
        ids = {p.name[: -len(".signals.npz")] for p in self.bag_root.glob("*.signals.npz")}
        ids |= {p.name for p in self.bag_root.iterdir() if p.is_dir()}
        return sorted(ids)

    def _bag_id(self, assignment: dict[str, float]) -> str | None:
        if "bag_index" in assignment:
            ids = self.recordings()
            i = int(assignment["bag_index"])
            return ids[i] if 0 <= i < len(ids) else None
        if "bag_id" in assignment:
            v = float(assignment["bag_id"])
            return str(int(v)) if v.is_integer() else str(v)
        return None

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        bag_id = self._bag_id(run.assignment)
        if bag_id is None:
            return ExecutionOutcome(None, False, "run has no valid bag_index or bag_id")
        cached = self.bag_root / f"{bag_id}.signals.npz"
        if cached.exists():
            return _load_signals(cached, {})
        bag_dir = self.bag_root / bag_id
        if bag_id and bag_dir.is_dir() and self.mapping is not None:
            from .rosbag import BagMapping, extract

            workdir.mkdir(parents=True, exist_ok=True)
            npz = extract(bag_dir, BagMapping.from_yaml(self.mapping), workdir / "signals.npz")
            return _load_signals(npz, {"rosbag": str(bag_dir)})
        return ExecutionOutcome(None, False, f"no extracted signals or mapped rosbag2 for {bag_id!r}")


# --------------------------------------------------------------------------


class ScenarioExecutionRunner:
    """Delegate to Intel Labs `scenario_execution` (arXiv:2409.07080).

    Two backends:

      simulation (default)  scenario_execution's step-based
                            SimulationInterface driving the SafeGate SIL
                            world (`safegate.sim.osc_bridge`). The OSC2 file
                            is parsed and executed by scenario_execution
                            itself; time is the simulation clock, so the run
                            is deterministic and the tests check it is
                            bit-identical to the in-process SilRunner.
      ros                   `ros2 launch scenario_execution_ros` against
                            Gazebo/Nav2, recording a rosbag2 that is extracted
                            with a topic-to-signal mapping (execution/rosbag.py).
                            Needs a site's ROS 2 / Gazebo installation and is not
                            exercised by this repository's tests (the rosbag2
                            recording and extraction path is, via Ros2Runner).
                            Gazebo physics and ROS 2 scheduling are not
                            bit-exact, so the runner declares itself
                            non-deterministic and the campaign repeats every
                            point.

    For every run the concrete assignment is written to a parameter file
    (`--scenario-parameter-file`), so the scenario template on disk is never
    modified and the exact file that ran is covered by the scenario hash.
    """

    name = "scenario_execution"
    tier = ExecutionTier.SIL

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        sut_config: str | os.PathLike[str] | None = None,
        backend: str = "simulation",
        simulation: str = "safegate.sim.osc_bridge:SafeGateSimulation",
        ros_setup: str = "/opt/ros/jazzy/setup.bash",
        launch_pkg: str = "scenario_execution_ros",
        timeout_s: float = 300.0,
        bag_mapping: str | os.PathLike[str] | None = None,
    ) -> None:
        if backend not in ("simulation", "ros"):
            raise ValueError("backend must be 'simulation' or 'ros'")
        self.project_root = Path(project_root)
        self.sut_config = Path(sut_config) if sut_config else None
        self.backend = backend
        self.simulation = simulation
        self.ros_setup = ros_setup
        self.launch_pkg = launch_pkg
        self.timeout_s = timeout_s
        self.bag_mapping = Path(bag_mapping) if bag_mapping else None
        self.deterministic = backend == "simulation"
        if backend == "simulation" and self.sut_config is None:
            raise ValueError("the simulation backend needs a SUT config")

    @property
    def config_hash(self) -> str:
        if self.sut_config is None:
            return "unknown"
        from ..sim.core import SutConfig

        return SutConfig.from_yaml(self.sut_config).digest()

    def backend_hash(self) -> str:
        if self.backend == "simulation":
            from importlib.metadata import version

            from ..sim.runner import simulator_digest

            lib = Path(__file__).resolve().parents[1] / "sim" / "lib_osc" / "safegate.osc"
            h = hashlib.sha256(b"scenario_execution-simulation/v1")
            h.update(version("scenario_execution").encode())
            h.update(simulator_digest(float(os.environ.get("SAFEGATE_DT", "0.01"))).encode())
            h.update(lib.read_bytes())
            return h.hexdigest()
        try:
            out = subprocess.run(
                ["bash", "-lc", f"source {self.ros_setup} && ros2 pkg xml {self.launch_pkg}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout
        except Exception:  # noqa: BLE001 - no ROS installation is a hash, not a crash
            out = "unavailable"
        return hashlib.sha256(out.encode()).hexdigest()

    @staticmethod
    def scenario_name(osc: Path) -> str:
        for line in osc.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*scenario\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if m:
                return m.group(1)
        raise ValueError(f"{osc}: no scenario declaration")

    def _command(self, osc: Path, params: Path, out: Path) -> list[str]:
        exe = Path(sys.executable).with_name("scenario_execution")
        prog = str(exe) if exe.exists() else (shutil.which("scenario_execution") or "")
        if not prog:
            raise FileNotFoundError("scenario_execution executable not found")
        return [
            prog,
            "--simulation",
            self.simulation,
            "--scenario-parameter-file",
            str(params),
            "-o",
            str(out),
            str(osc),
        ]

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        osc = self.project_root / run.scenario
        if not run.scenario or not osc.exists():
            return ExecutionOutcome(None, False, f"scenario file {osc} not found")
        try:
            name = self.scenario_name(osc)
        except ValueError as exc:
            return ExecutionOutcome(None, False, str(exc))
        params = workdir / "params.yaml"
        params.write_text(
            yaml.safe_dump({name: {k: float(v) for k, v in run.assignment.items()}}),
            encoding="utf-8",
        )
        out = workdir / "out"
        if self.backend == "ros":
            return self._execute_ros(osc, params, out)

        env = dict(os.environ)
        env["SAFEGATE_SUT_CONFIG"] = str(self.sut_config)
        try:
            proc = subprocess.run(
                self._command(osc, params, out),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ExecutionOutcome(None, False, "scenario execution timed out")
        except FileNotFoundError as exc:
            return ExecutionOutcome(None, False, str(exc))
        failure = _junit_failure(out / "test.xml")
        if proc.returncode != 0 or failure:
            return ExecutionOutcome(
                None, False, (failure or "") + " " + (proc.stderr or proc.stdout)[-1500:]
            )
        return _load_signals(out / "signals.npz", {"stdout": proc.stdout[-4000:]})

    def _execute_ros(self, osc: Path, params: Path, out: Path) -> ExecutionOutcome:
        if shutil.which("ros2") is None:
            return ExecutionOutcome(None, False, "ros2 not on PATH")
        bag = out / "bag"
        cmd = (
            f"source {self.ros_setup} && "
            f"ros2 launch {self.launch_pkg} scenario_launch.py "
            f"scenario:={osc} scenario_parameter_file:={params} "
            f"output_dir:={bag} scenario_status:=true"
        )
        try:
            proc = subprocess.run(
                ["bash", "-lc", cmd],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ExecutionOutcome(None, False, "scenario execution timed out")
        if proc.returncode != 0:
            return ExecutionOutcome(None, False, proc.stderr[-2000:])
        npz = out / "signals.npz"
        if self.bag_mapping is not None and bag.is_dir():
            from .rosbag import BagMapping, extract

            try:
                extract(bag, BagMapping.from_yaml(self.bag_mapping), npz)
            except (KeyError, ValueError) as exc:
                return ExecutionOutcome(None, False, f"bag extraction failed: {exc}")
        return _load_signals(npz, {"rosbag": str(bag), "stdout": proc.stdout[-4000:]})


def _junit_failure(path: Path) -> str | None:
    """Failure text from scenario_execution's JUnit output, if any."""
    if not path.exists():
        return "no test.xml written"
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        if int(suite.get("failures", "0")) or int(suite.get("errors", "0")):
            node = suite.find(".//failure")
            if node is None:
                node = suite.find(".//error")
            text = (node.get("message", "") if node is not None else "") or "failed"
            return f"scenario_execution reported failure: {text}"
    return None


def _load_signals(npz: Path, raw: dict[str, str]) -> ExecutionOutcome:
    if not npz.exists():
        return ExecutionOutcome(None, False, "signal extraction produced no output")
    d = np.load(npz)
    sigs = {k: d[k] for k in d.files if k != "time"}
    sigs["__time__"] = d["time"]
    return ExecutionOutcome(Trace(time=d["time"], signals=sigs), True, raw_artifacts=raw)


# --------------------------------------------------------------------------


DEFAULT_BAG_MAPPING = Path(__file__).resolve().parents[1] / "ros" / "safegate_state_mapping.yaml"


class Ros2Runner:
    """Closed loop through ROS 2: plant node, rosbag2 recording, extraction.

    Each concrete run starts `python -m safegate.ros.record_scenario` in a
    ROS 2 environment (by default the `safegate-ros:jazzy` container from
    docker/ros2/Dockerfile). The SafeGate plant node publishes the world on
    /safegate/state while `ros2 bag record` writes MCAP; the bag is then
    extracted on the host with the topic-to-signal mapping.

    A site with Gazebo or a real truck replaces the plant and the mapping,
    not the evidence path. ROS 2 transport can drop or reorder messages, so
    the runner declares itself non-deterministic: the campaign repeats every
    point and records disagreement as FLAKY.

    Isolation matters. Parallel runs on Docker's default bridge network
    discover each other over DDS multicast, and a recorder then captures
    another run's plant. The first campaign through this runner came back
    FLAKY for exactly that reason. Each container therefore runs with
    `--network none` and ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST. Native
    (non-container) parallel runs need a distinct ROS_DOMAIN_ID each.

    `command` overrides the container invocation. It is a list of argument
    templates with the placeholders {workdir} {sut} {scenario} {params} {bag}.
    """

    name = "ros2"
    tier = ExecutionTier.SIL
    deterministic = False

    def __init__(
        self,
        sut_config: str | os.PathLike[str],
        mapping: str | os.PathLike[str] | None = None,
        image: str = "safegate-ros:jazzy",
        command: list[str] | None = None,
        timeout_s: float = 600.0,
        rate_factor: float = 20.0,
    ) -> None:
        self.sut_config = Path(sut_config).resolve()
        self.mapping = Path(mapping) if mapping else DEFAULT_BAG_MAPPING
        self.image = image
        self.command = command
        self.timeout_s = timeout_s
        self.rate_factor = rate_factor

    @property
    def config_hash(self) -> str:
        from ..sim.core import SutConfig

        return SutConfig.from_yaml(self.sut_config).digest()

    def backend_hash(self) -> str:
        h = hashlib.sha256(b"ros2-runner/v2-isolated")
        h.update(self.mapping.read_bytes())
        if self.command is None:
            try:
                image_id = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", self.image],
                    capture_output=True, text=True, timeout=30, check=False,
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                image_id = "unavailable"
            h.update(image_id.encode())
        else:
            h.update(json.dumps(self.command).encode())
        return h.hexdigest()

    def _argv(self, workdir: Path, scenario: str) -> list[str]:
        if self.command is not None:
            fields = {
                "workdir": str(workdir), "sut": str(self.sut_config), "scenario": scenario,
                "params": str(workdir / "params.json"), "bag": str(workdir / "bag"),
            }
            return [part.format(**fields) for part in self.command]
        return [
            "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/work", "-e", "ROS_LOG_DIR=/work/log",
            "-e", "ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST",
            "-v", f"{workdir}:/work", "-v", f"{self.sut_config}:/sut.yaml:ro",
            self.image, "python3", "-m", "safegate.ros.record_scenario",
            "--sut", "/sut.yaml", "--scenario", scenario, "--params", "/work/params.json",
            "--out", "/work/bag", "--rate-factor", str(self.rate_factor),
        ]

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        from .rosbag import BagMapping, extract

        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "params.json").write_text(json.dumps(run.assignment), encoding="utf-8")
        try:
            proc = subprocess.run(
                self._argv(workdir, run.scenario), capture_output=True, text=True,
                timeout=self.timeout_s, check=False,
            )
        except subprocess.TimeoutExpired:
            return ExecutionOutcome(None, False, "ROS 2 run timed out")
        except OSError as exc:
            return ExecutionOutcome(None, False, f"cannot start ROS 2 run: {exc}")
        if proc.returncode != 0 or not (workdir / "bag" / "metadata.yaml").exists():
            return ExecutionOutcome(
                None, False, f"ROS 2 run failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-1500:]}"
            )
        try:
            npz = extract(workdir / "bag", BagMapping.from_yaml(self.mapping), workdir / "signals.npz")
        except (KeyError, ValueError) as exc:
            return ExecutionOutcome(None, False, f"bag extraction failed: {exc}")
        return _load_signals(npz, {"rosbag": "bag"})


# --------------------------------------------------------------------------


class HilRunner:
    """Client for a hardware-in-the-loop rig speaking `safegate-hil/1`.

    The rig implementation is site-specific (dSPACE SCALEXIO, Vector VT
    System, or a bespoke bench with the real safety controller and scanner);
    this class is the contract every rig must meet. The protocol is plain
    JSON over HTTP and is specified in docs/HIL_PROTOCOL.md:

      GET  /identity   rig_id, physical, firmware_digest, rig_image_digest,
                       scanner_config_checksum, protocol
      POST /runs       scenario, assignment, pinning, expected checksum
                       -> ok, message, the digests again, time, signals

    Contract points enforced here, not trusted to the rig:

      1. The rig reports its own firmware, image and scanner-configuration
         digests, and they become the backend and config hashes of every
         run. A run whose response reports different digests than the rig
         reported at campaign start is an ERROR: the rig changed underneath
         the campaign.
      2. If the project declares the scanner configuration checksum, a rig
         reporting another one is refused before any run. A HIL result whose
         scanner configuration is unknown is not evidence; it is an anecdote
         with a timestamp.
      3. Only a rig that declares `physical: true` produces HIL-tier
         evidence. An emulated rig is refused unless `allow_emulated=True`,
         and then its runs are recorded at SIL tier under the name
         `hil-emulated`, so policy R-EXEC-002 cannot be satisfied by it.
    """

    deterministic = False
    PROTOCOL = "safegate-hil/1"

    def __init__(
        self,
        endpoint: str,
        expected_scanner_config_checksum: str | None = None,
        allow_emulated: bool = False,
        timeout_s: float = 300.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.expected_checksum = expected_scanner_config_checksum
        self.timeout_s = timeout_s
        self.identity = self._get("/identity")
        if self.identity.get("protocol") != self.PROTOCOL:
            raise RuntimeError(
                f"rig speaks {self.identity.get('protocol')!r}, expected {self.PROTOCOL!r}"
            )
        for key in ("rig_id", "firmware_digest", "rig_image_digest", "scanner_config_checksum"):
            if not self.identity.get(key):
                raise RuntimeError(f"rig identity is missing {key!r}; pinning would be incomplete")
        if (
            self.expected_checksum
            and self.identity["scanner_config_checksum"] != self.expected_checksum
        ):
            raise RuntimeError(
                "rig scanner configuration checksum "
                f"{self.identity['scanner_config_checksum']} differs from the project's "
                f"{self.expected_checksum}; refusing to produce evidence"
            )
        physical = self.identity.get("physical") is True
        if not physical and not allow_emulated:
            raise RuntimeError(
                f"rig {self.identity['rig_id']} reports physical: false; an emulated rig "
                "cannot produce HIL-tier evidence (pass allow_emulated for contract tests)"
            )
        self.tier = ExecutionTier.HIL if physical else ExecutionTier.SIL
        self.name = "hil" if physical else "hil-emulated"

    # ---- transport -------------------------------------------------------

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.endpoint + path, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.endpoint + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- adapter ---------------------------------------------------------

    @property
    def config_hash(self) -> str:
        return str(self.identity["scanner_config_checksum"])

    @property
    def sut_build_hash(self) -> str:
        """The firmware actually on the rig. The campaign's build hash must be
        this, so evidence cannot be filed under a build the rig was not running."""
        return str(self.identity["firmware_digest"])

    def backend_hash(self) -> str:
        ident = self.identity
        return hashlib.sha256(
            "|".join(
                (self.PROTOCOL, ident["rig_id"], ident["firmware_digest"], ident["rig_image_digest"])
            ).encode()
        ).hexdigest()

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        del workdir
        body = {
            "scenario": run.scenario,
            "assignment": run.assignment,
            "pinning": run.pinning.model_dump(),
            "expected_scanner_config_checksum": self.identity["scanner_config_checksum"],
        }
        try:
            resp = self._post("/runs", body)
        except urllib.error.HTTPError as exc:
            return ExecutionOutcome(None, False, f"rig refused the run: HTTP {exc.code} {exc.read()[:500]!r}")
        except (urllib.error.URLError, TimeoutError) as exc:
            return ExecutionOutcome(None, False, f"rig unreachable: {exc}")
        for key in ("firmware_digest", "rig_image_digest", "scanner_config_checksum"):
            if resp.get(key) != self.identity[key]:
                return ExecutionOutcome(
                    None,
                    False,
                    f"rig {key} changed during the campaign "
                    f"({self.identity[key]} -> {resp.get(key)}); run discarded",
                )
        if not resp.get("ok"):
            return ExecutionOutcome(None, False, str(resp.get("message", "rig reported failure")))
        t = np.asarray(resp["time"], dtype=float)
        sigs = {k: np.asarray(v, dtype=float) for k, v in resp["signals"].items()}
        sigs["__time__"] = t
        return ExecutionOutcome(Trace(time=t, signals=sigs), True, message=str(resp.get("message", "")))


__all__ = [
    "DEFAULT_BAG_MAPPING",
    "ExecutionOutcome",
    "HilRunner",
    "NullRunner",
    "ReplayRunner",
    "Ros2Runner",
    "RunnerAdapter",
    "ScenarioExecutionRunner",
]
