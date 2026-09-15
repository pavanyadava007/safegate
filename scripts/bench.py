"""Micro-benchmarks for the two hot paths, labelled with the CPU they ran on.

    python scripts/bench.py --out out/bench.json
"""

from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

import numpy as np

from safegate.sim import SutConfig, build, run_scripts
from safegate.stl import parse_stl
from safegate.stl.robustness import Trace


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def best_of(fn, repeats: int = 5) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    rng = np.random.default_rng(0)
    res: dict = {"cpu": cpu_model(), "python": platform.python_version(), "single_thread": True}

    for n in (1_000, 10_000):
        t = np.arange(n) * 0.01
        tr = Trace(time=t, signals={"x": rng.normal(size=n), "y": rng.normal(size=n)})
        for src in ("always x >= -3", "G[0,1.0] x >= -3", "always (x >= 0 -> F[0,0.5] y >= 0)"):
            f = parse_stl(src)
            res[f"stl[{src}] n={n} ms"] = 1000 * best_of(lambda f=f, tr=tr: f.evaluate(tr))
    n = 1_000
    t = np.arange(n) * 0.01
    tr = Trace(time=t, signals={"x": rng.normal(size=n), "y": rng.normal(size=n)})
    f = parse_stl("x >= 0 U[0,2.0] y >= 1")
    res[f"stl[x >= 0 U[0,2.0] y >= 1] n={n} ms"] = 1000 * best_of(lambda: f.evaluate(tr), 3)

    cfg = SutConfig()
    params = {"truck_speed": 1.0, "entry_gap": 3.0, "warning_field": 0.0}

    def sim() -> int:
        spec = build("occluded_emergence", params, cfg)
        w = run_scripts(spec.world, spec.scripts, spec.horizon_s)
        return w.step_index

    steps = sim()
    dt = best_of(sim, 5)
    res["sim occluded_emergence steps"] = steps
    res["sim occluded_emergence ms per run"] = 1000 * dt
    res["sim steps per s"] = steps / dt
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2))
    for k, v in res.items():
        print(f"{k}: {v:.3f}" if isinstance(v, float) else f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
