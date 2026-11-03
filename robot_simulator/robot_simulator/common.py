"""Shared QoS, lifecycle and timestamp helpers."""

import rclpy
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy


def latched_qos():
    return QoSProfile(
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )


def set_time(target, nanoseconds):
    target.sec, target.nanosec = divmod(int(nanoseconds), 10**9)


def time_ns(stamp):
    return stamp.sec * 10**9 + stamp.nanosec


def run_node(factory, args=None, *, threaded=False):
    rclpy.init(args=args)
    node = None
    executor = MultiThreadedExecutor(num_threads=4) if threaded else None
    try:
        node = factory()
        rclpy.spin(node, executor=executor)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        stop = getattr(node, "shutdown_event", None)
        if stop is not None:
            stop.set()
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
