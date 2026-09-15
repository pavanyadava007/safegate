"""
safegate.ros.plant
==================

The SafeGate SIL world as a ROS 2 node. Every control period is published as
one `sensor_msgs/JointState` on `/safegate/state` (name[] = signal names,
position[] = values, header.stamp = simulation time), and simulation time on
`/clock`. `safegate/ros/safegate_state_mapping.yaml` maps the topic back to
SafeGate signals.

This exercises the ROS 2 side of the pipeline (publishers, QoS, rosbag2
recording, extraction) with a known plant, so that a site replacing it with
Gazebo or a real truck changes the plant, not the evidence path.

    python -m safegate.ros.plant --sut sut.yaml --scenario scenarios/person_crossing.osc \\
        --params params.json [--rate-factor 20]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import rclpy
from builtin_interfaces.msg import Time
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import JointState

from ..sim.core import SIGNALS, SutConfig, _steps
from ..sim.scenarios import build


def _stamp(t: float) -> Time:
    ns = round(t * 1e9)
    return Time(sec=ns // 1_000_000_000, nanosec=ns % 1_000_000_000)


class PlantNode(Node):
    def __init__(self) -> None:
        super().__init__("safegate_plant")
        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_ALL, depth=10000
        )
        self.state_pub = self.create_publisher(JointState, "/safegate/state", qos)
        self.clock_pub = self.create_publisher(Clock, "/clock", 10)

    def publish(self, world) -> None:
        idx = len(world.recorded["time"]) - 1
        t = world.recorded["time"][idx]
        msg = JointState()
        msg.header.stamp = _stamp(t)
        msg.header.frame_id = "safegate_sim"
        msg.name = list(SIGNALS)
        msg.position = [float(world.recorded[s][idx]) for s in SIGNALS]
        self.state_pub.publish(msg)
        self.clock_pub.publish(Clock(clock=_stamp(t)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="safegate-ros-plant")
    ap.add_argument("--sut", required=True, type=Path)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--params", required=True, type=Path, help="JSON object of scenario parameters")
    ap.add_argument("--rate-factor", type=float, default=20.0,
                    help="simulated seconds per wall second (publishing pace)")
    ap.add_argument("--wait-subscribers", type=float, default=15.0)
    args = ap.parse_args(argv)

    params = {k: float(v) for k, v in json.loads(args.params.read_text()).items()}
    spec = build(args.scenario, params, SutConfig.from_yaml(args.sut))
    world = spec.world

    rclpy.init()
    node = PlantNode()
    deadline = time.monotonic() + args.wait_subscribers
    while node.state_pub.get_subscription_count() == 0 and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
    if node.state_pub.get_subscription_count() == 0:
        node.get_logger().error("no subscriber on /safegate/state; is the recorder running?")
        return 3

    node.publish(world)
    period = world.dt / args.rate_factor
    active = list(spec.scripts)
    for _ in range(_steps(spec.horizon_s, world.dt)):
        world.step()
        still = []
        for s in active:
            try:
                next(s)
                still.append(s)
            except StopIteration:
                pass
        active = still
        node.publish(world)
        time.sleep(period)
    n = len(world.recorded["time"])
    # give the reliable transport time to deliver the tail before the node goes away
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        rclpy.spin_once(node, timeout_sec=0.05)
    node.get_logger().info(f"published {n} states, horizon {spec.horizon_s:.2f} s")
    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
