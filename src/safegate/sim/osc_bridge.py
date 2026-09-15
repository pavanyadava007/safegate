"""
safegate.sim.osc_bridge
=======================

Integration with Intel Labs `scenario_execution` (arXiv:2409.07080,
Apache-2.0), using its step-based `SimulationInterface`.

`scenario_execution` parses the OpenSCENARIO 2 file, applies the parameter
overrides, builds the py_trees behaviour tree and drives the loop:
`simulation.step()` then one tree tick, with `SimulationClock` so that
simulated time, not wall time, sets the pace. This module supplies the
simulation (the SafeGate SIL world) and the actions the scenario files use.

Configuration comes through environment variables, because the framework
instantiates the simulation class itself:

  SAFEGATE_SUT_CONFIG   path to the SUT configuration YAML (required)
  SAFEGATE_DT           control period in seconds (default 0.01)

At shutdown the recorded signals are written to `<output_dir>/signals.npz`,
the same format ScenarioExecutionRunner and ReplayRunner read.

Importing this module requires `scenario_execution` (extra: `osc`).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from py_trees.common import Status
from scenario_execution.actions.base_action import BaseAction
from scenario_execution.simulation import SimulationInterface

from .core import SutConfig, _steps
from .scenarios import Actions, ScenarioSpec, build


def get_osc_library() -> tuple[str, str]:
    return "safegate.sim", "safegate.osc"


class SafeGateSimulation(SimulationInterface):
    def __init__(self) -> None:
        self._dt = float(os.environ.get("SAFEGATE_DT", "0.01"))
        self.spec: ScenarioSpec | None = None
        self.output_dir: str | None = None
        self.cfg: SutConfig | None = None

    @property
    def dt(self) -> float:
        return self._dt

    def setup(self, **kwargs) -> None:
        path = os.environ.get("SAFEGATE_SUT_CONFIG")
        if not path:
            raise RuntimeError("SAFEGATE_SUT_CONFIG is not set")
        self.cfg = SutConfig.from_yaml(path)
        self.output_dir = kwargs.get("output_dir")

    # The framework passes scenario parameters by name and ignores **kwargs,
    # so every parameter a family can read is listed. test_osc_bridge checks
    # this list against safegate.sim.scenarios.PARAMETERS.
    def reset(
        self,
        family: str,
        approach_side: float | None = None,
        approach_speed: float | None = None,
        entry_gap: float | None = None,
        floor_mu: float | None = None,
        goal_distance: float | None = None,
        lateral_offset: float | None = None,
        mute_start_gap: float | None = None,
        nav_position_error: float | None = None,
        pedestrian_speed: float | None = None,
        pedestrian_start_offset: float | None = None,
        person_gap: float | None = None,
        pick_duration_s: float | None = None,
        rack_clearance: float | None = None,
        reverse_speed: float | None = None,
        truck_speed: float | None = None,
        warning_field: float | None = None,
        zone_speed_limit: float | None = None,
    ) -> None:
        assert self.cfg is not None
        params = {
            k: float(v)
            for k, v in locals().items()
            if k not in ("self", "family") and v is not None
        }
        self.spec = build(family, params, self.cfg, self._dt)
        self.horizon_steps = _steps(self.spec.horizon_s, self._dt)

    def step(self) -> None:
        assert self.spec is not None
        self.spec.world.step()

    def shutdown(self) -> None:
        if self.spec is None or not self.output_dir:
            return
        t, sig = self.spec.world.arrays()
        out = Path(self.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        np.savez(out / "signals.npz", time=t, **sig)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


class _SimAction(BaseAction):
    def setup(self, **kwargs) -> None:
        sim = kwargs.get("simulation")
        if not isinstance(sim, SafeGateSimulation):
            raise RuntimeError("safegate actions need --simulation SafeGateSimulation")
        self.sim = sim

    @property
    def act(self) -> Actions:
        assert self.sim.spec is not None
        return Actions(self.sim.spec.world)

    @property
    def geom(self) -> dict[str, float]:
        assert self.sim.spec is not None
        return self.sim.spec.geometry


class RunToHorizon(_SimAction):
    def execute(self, pad: float = 0.0) -> None:
        del pad

    def update(self) -> Status:
        world = self.sim.spec.world
        return Status.SUCCESS if world.step_index >= self.sim.horizon_steps else Status.RUNNING


class WaitForTrigger(_SimAction):
    def execute(self, pad: float = 0.0) -> None:
        del pad

    def update(self) -> Status:
        g = self.geom
        return (
            Status.SUCCESS
            if self.act.gap_to_line(g["x_line"]) <= g["trigger"]
            else Status.RUNNING
        )


class WaitGap(_SimAction):
    def execute(self, gap: float) -> None:
        self.gap = gap

    def update(self) -> Status:
        return (
            Status.SUCCESS
            if self.act.gap_to_line(self.geom["x_line"]) <= self.gap
            else Status.RUNNING
        )


class WaitStopped(_SimAction):
    def execute(self, pad: float = 0.0) -> None:
        del pad

    def update(self) -> Status:
        return Status.SUCCESS if self.act.truck_stopped() else Status.RUNNING


class WaitSeconds(_SimAction):
    def execute(self, seconds: float) -> None:
        self.seconds = seconds
        self.start: float | None = None

    def update(self) -> Status:
        world = self.sim.spec.world
        if self.start is None:
            self.start = world.t
        return (
            Status.SUCCESS
            if world.t - self.start >= self.seconds - 1e-9
            else Status.RUNNING
        )


class PedestrianWalk(_SimAction):
    def execute(self, pad: float = 0.0) -> None:
        del pad

    def update(self) -> Status:
        g = self.geom
        self.act.walk("p1", g["walk_x"], g["walk_y"], g["walk_speed"])
        return Status.SUCCESS


class RequestMuting(_SimAction):
    def execute(self, enabled: bool) -> None:
        self.on = bool(enabled)

    def update(self) -> Status:
        self.act.mute(self.on)
        return Status.SUCCESS


class Idle(_SimAction):
    def execute(self, pad: float = 0.0) -> None:
        del pad

    def update(self) -> Status:
        return Status.RUNNING


__all__ = [
    "Idle",
    "PedestrianWalk",
    "RequestMuting",
    "RunToHorizon",
    "SafeGateSimulation",
    "WaitForTrigger",
    "WaitGap",
    "WaitSeconds",
    "WaitStopped",
    "get_osc_library",
]
