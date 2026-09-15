"""
safegate.execution.rosbag
=========================

rosbag2 (MCAP or SQLite3) to SafeGate signals.

Every site records different topics, so extraction is driven by a mapping
file instead of code:

    dt: 0.01                       # output grid, seconds
    signals:
      speed:                       # SafeGate signal name
        topic: /odom
        field: twist.twist.linear.x
        stamp: header.stamp        # message time; omit to use the bag receive time
      min_distance_to_person:
        topic: /safegate/state     # sensor_msgs/JointState used as named values
        name: min_distance_to_person
        stamp: header.stamp

`field` is a dotted path into the message. `name` selects an entry of a
JointState-style message by its `name[]` label (value from `position[]`,
or from `field` if given, e.g. `velocity`).

Signals recorded on different topics and at different rates are aligned by
zero-order hold onto one grid that starts when every signal has its first
sample. The output is the `signals.npz` format ReplayRunner and the
scenario runners read.

Reading uses the pure-Python `rosbags` library (extra: `ros`), so extraction
runs on a CI machine without a ROS installation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml


@dataclass(frozen=True)
class SignalMap:
    signal: str
    topic: str
    field: str | None = None
    name: str | None = None
    stamp: str | None = None


@dataclass(frozen=True)
class BagMapping:
    signals: tuple[SignalMap, ...]
    dt: float = 0.01

    @staticmethod
    def from_yaml(path: str | Path) -> BagMapping:
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        sigs = []
        for name, spec in (d.get("signals") or {}).items():
            if "topic" not in spec:
                raise ValueError(f"signal {name!r}: topic is required")
            if not spec.get("field") and not spec.get("name"):
                raise ValueError(f"signal {name!r}: give a field, a name, or both")
            sigs.append(
                SignalMap(name, spec["topic"], spec.get("field"), spec.get("name"), spec.get("stamp"))
            )
        if not sigs:
            raise ValueError(f"{path}: no signals mapped")
        return BagMapping(tuple(sigs), float(d.get("dt", 0.01)))


def _get(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def _stamp_seconds(stamp: Any) -> float:
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def read_samples(bag: str | Path, mapping: BagMapping) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Raw (time, value) samples per mapped signal."""
    from rosbags.highlevel import AnyReader  # optional dependency: pip install safegate[ros]

    by_topic: dict[str, list[SignalMap]] = {}
    for m in mapping.signals:
        by_topic.setdefault(m.topic, []).append(m)
    raw: dict[str, tuple[list[float], list[float]]] = {m.signal: ([], []) for m in mapping.signals}
    with AnyReader([Path(bag)]) as reader:
        conns = [c for c in reader.connections if c.topic in by_topic]
        missing = set(by_topic) - {c.topic for c in conns}
        if missing:
            raise KeyError(f"topics not in bag {bag}: {sorted(missing)}")
        for conn, t_ns, data in reader.messages(connections=conns):
            msg = reader.deserialize(data, conn.msgtype)
            for m in by_topic[conn.topic]:
                if m.name is not None:
                    names = list(msg.name)
                    if m.name not in names:
                        continue
                    arr = _get(msg, m.field or "position")
                    value = float(arr[names.index(m.name)])
                else:
                    value = float(_get(msg, m.field or ""))
                t = _stamp_seconds(_get(msg, m.stamp)) if m.stamp else t_ns * 1e-9
                raw[m.signal][0].append(t)
                raw[m.signal][1].append(value)
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sig, (ts, vs) in raw.items():
        if not ts:
            raise ValueError(f"signal {sig!r}: no samples found on its topic")
        order = np.argsort(np.asarray(ts), kind="stable")
        out[sig] = (np.asarray(ts)[order], np.asarray(vs, dtype=float)[order])
    return out


def resample(
    samples: dict[str, tuple[np.ndarray, np.ndarray]], dt: float
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Zero-order hold onto a common grid starting when every signal exists."""
    t0 = max(ts[0] for ts, _ in samples.values())
    t1 = min(ts[-1] for ts, _ in samples.values())
    if t1 < t0:
        raise ValueError("mapped signals do not overlap in time")
    n = math.floor((t1 - t0) / dt + 1e-6) + 1
    grid = t0 + np.arange(n) * dt
    signals = {}
    for sig, (ts, vs) in samples.items():
        idx = np.searchsorted(ts, grid + 1e-9, side="right") - 1
        signals[sig] = vs[np.clip(idx, 0, len(vs) - 1)]
    return grid - t0, signals


def extract(bag: str | Path, mapping: BagMapping, out: str | Path) -> Path:
    time, signals = resample(read_samples(bag, mapping), mapping.dt)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out, time=time, **signals)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="safegate-extract-bag")
    ap.add_argument("bag", type=Path, help="rosbag2 directory (MCAP or SQLite3)")
    ap.add_argument("out", type=Path, help="signals.npz to write")
    ap.add_argument("--mapping", type=Path, required=True)
    args = ap.parse_args(argv)
    path = extract(args.bag, BagMapping.from_yaml(args.mapping), args.out)
    d = np.load(path)
    print(f"wrote {path}: {d['time'].size} samples, signals {sorted(k for k in d.files if k != 'time')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BagMapping", "SignalMap", "extract", "read_samples", "resample"]
