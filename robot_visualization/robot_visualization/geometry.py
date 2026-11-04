"""Small frame calculations; never treats odom coordinates as map coordinates."""

import math


def quaternion(q):
    values = (q.x, q.y, q.z, q.w)
    norm = math.hypot(*values)
    if (
        not all(math.isfinite(value) for value in values)
        or not math.isfinite(norm)
        or norm < 1e-8
    ):
        raise ValueError("invalid orientation quaternion")
    return tuple(value / norm for value in values)


def yaw(q):
    x, y, z, w = quaternion(q)
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def rotate(point, rotation):
    x, y, z, w = quaternion(rotation)
    px, py, pz = point
    tx, ty, tz = 2 * (y * pz - z * py), 2 * (z * px - x * pz), 2 * (x * py - y * px)
    return (
        px + w * tx + y * tz - z * ty,
        py + w * ty + z * tx - x * tz,
        pz + w * tz + x * ty - y * tx,
    )


def grid_point(x, y, origin_x, origin_y, origin_yaw, resolution):
    """Inverse map origin pose: metric frame coordinates -> unflipped grid cells."""
    dx, dy = x - origin_x, y - origin_y
    c, s = math.cos(origin_yaw), math.sin(origin_yaw)
    return (c * dx + s * dy) / resolution, (-s * dx + c * dy) / resolution
