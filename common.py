"""共享基础工具：错误类型、时间处理与空域几何，供判定、存储与接口层共同使用。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized
