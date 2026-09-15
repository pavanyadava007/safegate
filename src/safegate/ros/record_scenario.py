"""
safegate.ros.record_scenario
============================

Run one concrete scenario through ROS 2 and record it with rosbag2:

  1. start `ros2 bag record` (MCAP) on /safegate/state
  2. run the plant node, which waits for the recorder's subscription
  3. stop the recorder with SIGINT so the bag is finalised

    python -m safegate.ros.record_scenario --sut sut.yaml \\
        --scenario scenarios/person_crossing.osc --params params.json --out bag/
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="safegate-ros-record")
    ap.add_argument("--sut", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--rate-factor", default="20")
    args = ap.parse_args(argv)

    # Keep discovery on this host; parallel runs must not record each other.
    os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    recorder = subprocess.Popen(
        ["ros2", "bag", "record", "-s", "mcap", "-o", str(args.out), "/safegate/state"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        plant = subprocess.run(
            [sys.executable, "-m", "safegate.ros.plant", "--sut", args.sut, "--scenario",
             args.scenario, "--params", args.params, "--rate-factor", args.rate_factor],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    finally:
        time.sleep(0.5)
        recorder.send_signal(signal.SIGINT)
        try:
            rec_out, _ = recorder.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            recorder.kill()
            rec_out, _ = recorder.communicate()
    sys.stdout.write(plant.stdout + plant.stderr + (rec_out or "")[-2000:])
    if plant.returncode != 0:
        return plant.returncode
    if not (args.out / "metadata.yaml").exists():
        print("rosbag2 did not finalise the bag", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
