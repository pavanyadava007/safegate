"""
safegate.sim.runner
===================

`SilRunner`: executes a ConcreteRun in the in-process SIL world.

The runner is the SUT boundary. Its configuration hash is the digest of the
SutConfig (field tables, latencies, timeouts), and its backend hash is a
digest of the simulator source, so changing either produces runs with a
different pinning.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.model import ConcreteRun, ExecutionTier
from ..execution.adapter import ExecutionOutcome
from ..stl.robustness import Trace
from .core import SutConfig, run_scripts
from .scenarios import build

_SIM_SOURCES = ("core.py", "scenarios.py", "runner.py")


def simulator_digest(dt: float) -> str:
    h = hashlib.sha256(f"safegate-sil/dt={dt!r}".encode())
    here = Path(__file__).resolve().parent
    for name in _SIM_SOURCES:
        h.update(name.encode())
        h.update((here / name).read_bytes())
    return h.hexdigest()


class SilRunner:
    name = "sil"
    tier = ExecutionTier.SIL
    deterministic = True

    def __init__(self, sut: SutConfig | str | Path, dt: float = 0.01) -> None:
        self.sut = sut if isinstance(sut, SutConfig) else SutConfig.from_yaml(sut)
        self.dt = dt

    @property
    def config_hash(self) -> str:
        return self.sut.digest()

    def backend_hash(self) -> str:
        return simulator_digest(self.dt)

    def execute(self, run: ConcreteRun, workdir: Path) -> ExecutionOutcome:
        if not run.scenario:
            return ExecutionOutcome(None, False, "run has no scenario reference")
        try:
            spec = build(run.scenario, run.assignment, self.sut, self.dt)
        except KeyError as exc:
            return ExecutionOutcome(None, False, str(exc))
        world = run_scripts(spec.world, spec.scripts, spec.horizon_s)
        t, sig = world.arrays()
        sig["__time__"] = t
        return ExecutionOutcome(
            Trace(time=t, signals=sig),
            True,
            metrics={"sim_horizon_s": spec.horizon_s},
        )


__all__ = ["SilRunner", "simulator_digest"]
