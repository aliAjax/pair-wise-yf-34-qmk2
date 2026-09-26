#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}


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


class NegotiationPolicy:
    """冲突协商单的判定规则：条目状态、是否可放行、是否应自动退出。纯逻辑，不碰存储和接口。"""

    OPEN = "open"
    REJECTED = "rejected"      # 审核员拒绝
    WITHDRAWN = "withdrawn"    # 主计划运营方撤回
    CANCELED = "canceled"      # 成员计划取消等自动退出
    EXPIRED = "expired"        # 单子过期自动退出
    RELEASED = "released"      # 全部确认且无冲突，审核放行
    CLOSED_STATUSES = {REJECTED, WITHDRAWN, CANCELED, EXPIRED, RELEASED}

    @staticmethod
    def item_state(item: sqlite3.Row | dict[str, Any]) -> str:
        """confirmed = 已确认且版本仍一致；stale = 计划改动后确认失效，需重做；need_resubmit = 计划被拒绝需重新提交；pending = 未确认。"""
        confirmed = item["confirmed_revision"]
        if confirmed is None:
            return "need_resubmit" if item["plan_status"] == "rejected" else "pending"
        if confirmed != item["current_revision"]: return "stale"
        if item["plan_status"] == "rejected": return "need_resubmit"
        if item["plan_status"] not in ACTIVE_STATUSES: return "stale"
        return "confirmed"

    @classmethod
    def evaluate(cls, items: list[sqlite3.Row | dict[str, Any]], reports: dict[int, dict[str, Any]]) -> dict[str, Any]:
        views: list[dict[str, Any]] = []
        pending_operators: list[str] = []
        ready = True
        for item in items:
            state = cls.item_state(item)
            report = reports.get(item["plan_id"], {})
            remaining = [c for c in report.get("blocking_conflicts", []) if c.get("code") == "adjacent_traffic"]
            view = {"plan_id": item["plan_id"], "operator_id": item["operator_id"], "callsign": item["callsign"], "is_main": bool(item["is_main"]),
                    "base_revision": item["base_revision"], "current_revision": item["current_revision"], "confirmed_revision": item["confirmed_revision"],
                    "plan_status": item["plan_status"], "state": state,
                    "remaining_adjacent_conflicts": [p["plan_id"] for c in remaining for p in c.get("plans", [])]}
            views.append(view)
            if state != "confirmed":
                ready = False
                pending_operators.append(item["operator_id"])
            elif remaining:
                ready = False
        return {"ready": ready, "items": views, "pending_operators": sorted(set(pending_operators))}

    @staticmethod
    def is_expired(main_row: sqlite3.Row | dict[str, Any], now_dt: datetime) -> bool:
        """主计划起飞时间已过且尚未放行的单子自动过期退出。"""
        return parse_time(main_row["plan_starts_at"]) <= now_dt


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row; self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS negotiations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            main_plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            status TEXT NOT NULL DEFAULT 'open',
            reason TEXT, close_reason TEXT,
            created_by TEXT NOT NULL, created_role TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            decided_by TEXT, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS negotiation_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            negotiation_id INTEGER NOT NULL REFERENCES negotiations(id),
            plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            operator_id TEXT NOT NULL, is_main INTEGER NOT NULL DEFAULT 0,
            base_revision INTEGER NOT NULL,
            confirmed_revision INTEGER, confirmed_by TEXT, confirmed_at TEXT,
            UNIQUE(negotiation_id,plan_id)
        );
        CREATE TABLE IF NOT EXISTS negotiation_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            negotiation_id INTEGER NOT NULL REFERENCES negotiations(id),
            actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            reason TEXT, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))

    # ---- 协商单存储（与判定、接口入口分开维护）----
    @staticmethod
    def create_negotiation(conn: sqlite3.Connection, main_plan_id: int, actor: str, role: str, reason: str, now: str) -> int:
        cur = conn.execute("""INSERT INTO negotiations(main_plan_id,status,reason,created_by,created_role,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?)""", (main_plan_id, "open", reason or None, actor, role, now, now))
        return cur.lastrowid

    @staticmethod
    def add_negotiation_item(conn: sqlite3.Connection, negotiation_id: int, plan_id: int, operator_id: str, is_main: bool, revision: int) -> None:
        conn.execute("""INSERT INTO negotiation_items(negotiation_id,plan_id,operator_id,is_main,base_revision) VALUES(?,?,?,?,?)""",
                     (negotiation_id, plan_id, operator_id, 1 if is_main else 0, revision))

    @staticmethod
    def negotiation_row(conn: sqlite3.Connection, negotiation_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM negotiations WHERE id=?", (negotiation_id,)).fetchone()
        if not row: raise ApiError(404, "negotiation_not_found", "冲突协商单不存在")
        return row

    @staticmethod
    def negotiation_items(conn: sqlite3.Connection, negotiation_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("""SELECT i.*, p.callsign, p.status AS plan_status, p.revision AS current_revision,
                                           p.operator_id AS plan_operator, p.starts_at AS plan_starts_at, p.ends_at AS plan_ends_at
                                    FROM negotiation_items i JOIN flight_plans p ON p.id=i.plan_id
                                    WHERE i.negotiation_id=? ORDER BY i.is_main DESC, i.id""", (negotiation_id,)))

    @staticmethod
    def negotiation_events(conn: sqlite3.Connection, negotiation_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM negotiation_events WHERE negotiation_id=? ORDER BY id", (negotiation_id,)))

    @staticmethod
    def open_negotiation_for_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM negotiations WHERE main_plan_id=? AND status='open' ORDER BY id DESC", (plan_id,)).fetchone()

    @staticmethod
    def open_negotiations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM negotiations WHERE status='open' ORDER BY id"))

    @staticmethod
    def list_negotiations(conn: sqlite3.Connection, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            return list(conn.execute(f"SELECT * FROM negotiations WHERE status IN ({placeholders}) ORDER BY id DESC", statuses))
        return list(conn.execute("SELECT * FROM negotiations ORDER BY id DESC"))

    @staticmethod
    def item_row(conn: sqlite3.Connection, negotiation_id: int, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM negotiation_items WHERE negotiation_id=? AND plan_id=?", (negotiation_id, plan_id)).fetchone()
        if not row: raise ApiError(404, "negotiation_item_not_found", "该计划不在此协商单中")
        return row

    @staticmethod
    def items_for_plan(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM negotiation_items WHERE plan_id=? AND negotiation_id IN (SELECT id FROM negotiations WHERE status='open')", (plan_id,)))

    @staticmethod
    def confirm_item(conn: sqlite3.Connection, negotiation_id: int, plan_id: int, revision: int, actor: str, now: str) -> None:
        conn.execute("""UPDATE negotiation_items SET confirmed_revision=?,confirmed_by=?,confirmed_at=?
                        WHERE negotiation_id=? AND plan_id=?""", (revision, actor, now, negotiation_id, plan_id))
        conn.execute("UPDATE negotiations SET updated_at=? WHERE id=?", (now, negotiation_id))

    @staticmethod
    def close_negotiation(conn: sqlite3.Connection, negotiation_id: int, status: str, close_reason: str, actor: str, now: str) -> None:
        conn.execute("UPDATE negotiations SET status=?,close_reason=?,decided_by=?,decided_at=?,updated_at=? WHERE id=?",
                     (status, close_reason, actor, now, now, negotiation_id))

    @staticmethod
    def add_negotiation_event(conn: sqlite3.Connection, negotiation_id: int, actor: str, role: str, action: str, reason: str | None, detail: dict[str, Any], now: str) -> None:
        conn.execute("""INSERT INTO negotiation_events(negotiation_id,actor,role,action,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?)""",
                     (negotiation_id, actor, role, action, reason, json.dumps(detail, ensure_ascii=False, sort_keys=True), now))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            return dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                  VALUES(?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, iso()))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid, "override_kind": override_kind}

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"]})
            for item in Repository.items_for_plan(conn, plan_id):
                conn.execute("UPDATE negotiations SET updated_at=? WHERE id=?", (iso(), item["negotiation_id"]))
                Repository.add_negotiation_event(conn, item["negotiation_id"], actor, role, "confirmation_invalidated", None,
                                                 {"plan_id": plan_id, "from_revision": expected, "to_revision": revision, "operator_id": operator}, iso())
                peers = Repository.negotiation_items(conn, item["negotiation_id"])
                self._notify_negotiation(conn, peers, "negotiation_confirmation_invalidated",
                                         f"协商单 #{item['negotiation_id']}：{operator} 的计划 {plan['callsign']} 已修改为版本 {revision}，其确认失效，需重新修改并确认")
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            self._auto_close_negotiations(conn)
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    # ---- 冲突协商单：判定见 NegotiationPolicy，存储见 Repository，入口见 Handler ----
    @staticmethod
    def _notify_negotiation(conn: sqlite3.Connection, items: list[sqlite3.Row], kind: str, message: str) -> None:
        for item in items: Repository.notify(conn, item["plan_id"], kind, message)

    def _auto_close_negotiations(self, conn: sqlite3.Connection, actor: str = "system", role: str = "airspace_reviewer") -> list[int]:
        """取消、过期的协商单自动退出；在所有协商单读写路径上惰性执行。"""
        now, nows, closed = utcnow(), iso(), []
        for neg in Repository.open_negotiations(conn):
            items = Repository.negotiation_items(conn, neg["id"])
            main = next(item for item in items if item["is_main"])
            if parse_time(main["plan_starts_at"]) <= now:
                status, label, action, close_reason = NegotiationPolicy.EXPIRED, "过期", "negotiation_expired", "主计划起飞时间已过，协商单自动过期退出"
            else:
                gone = next((item for item in items if item["plan_status"] in {"canceled", "expired"}), None)
                if not gone: continue
                status, label, action = NegotiationPolicy.CANCELED, "取消", "negotiation_canceled"
                close_reason = f"成员计划 {gone['callsign']} 已{'取消' if gone['plan_status']=='canceled' else '过期'}，协商单自动退出"
            Repository.close_negotiation(conn, neg["id"], status, close_reason, actor, nows)
            Repository.add_negotiation_event(conn, neg["id"], actor, role, action, close_reason, {"auto": True}, nows)
            Repository.audit(conn, main["plan_id"], actor, role, action, {"negotiation_id": neg["id"], "auto": True})
            self._notify_negotiation(conn, items, "negotiation_closed", f"冲突协商单 #{neg['id']} 已{label}：{close_reason}")
            closed.append(neg["id"])
        return closed

    def _negotiation_view(self, conn: sqlite3.Connection, neg: sqlite3.Row, with_reports: bool = True) -> dict[str, Any]:
        items = Repository.negotiation_items(conn, neg["id"])
        reports: dict[int, dict[str, Any]] = {}
        if with_reports and neg["status"] == NegotiationPolicy.OPEN:
            for item in items:
                plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (item["plan_id"],)).fetchone()
                reports[item["plan_id"]] = self._conflict_report(conn, plan)
        verdict = NegotiationPolicy.evaluate(items, reports)
        return {"id": neg["id"], "status": neg["status"], "main_plan_id": neg["main_plan_id"], "reason": neg["reason"],
                "close_reason": neg["close_reason"], "created_by": neg["created_by"], "created_role": neg["created_role"],
                "created_at": neg["created_at"], "updated_at": neg["updated_at"], "decided_by": neg["decided_by"], "decided_at": neg["decided_at"],
                "ready": verdict["ready"] if neg["status"] == NegotiationPolicy.OPEN else False,
                "pending_operators": verdict["pending_operators"] if neg["status"] == NegotiationPolicy.OPEN else [],
                "items": verdict["items"],
                "events": [{"id": e["id"], "actor": e["actor"], "role": e["role"], "action": e["action"], "reason": e["reason"],
                            "detail": json.loads(e["detail_json"]), "created_at": e["created_at"]}
                           for e in Repository.negotiation_events(conn, neg["id"])]}

    @staticmethod
    def _negotiation_visible(role: str, operator: str, items: list[sqlite3.Row]) -> bool:
        if role in {"airspace_reviewer", "commander", "auditor", "viewer"}: return True
        return any(item["operator_id"] == operator for item in items)

    def create_negotiation(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator" and role not in {"airspace_reviewer", "commander"}:
            raise ApiError(403, "negotiation_forbidden", "当前角色不能发起冲突协商")
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能对其他运营方计划发起协商")
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以发起冲突协商")
            if Repository.open_negotiation_for_plan(conn, plan_id): raise ApiError(409, "negotiation_already_open", "该计划已有进行中的冲突协商单")
            report = self._conflict_report(conn, plan)
            adjacent = [c for c in report["blocking_conflicts"] if c.get("code") == "adjacent_traffic"]
            if not adjacent: raise ApiError(409, "no_adjacent_traffic", "当前未检查到相邻计划，无需开协商单", report)
            related_ids: list[int] = []
            for conflict in adjacent:
                for other in conflict.get("plans", []):
                    if other["plan_id"] != plan_id and other["plan_id"] not in related_ids: related_ids.append(other["plan_id"])
            nows = iso(); neg_id = Repository.create_negotiation(conn, plan_id, actor, role, str(body.get("reason", "")).strip(), nows)
            Repository.add_negotiation_item(conn, neg_id, plan_id, plan["operator_id"], True, plan["revision"])
            for other_id in related_ids:
                other = self._plan_row(conn, other_id)
                Repository.add_negotiation_item(conn, neg_id, other_id, other["operator_id"], False, other["revision"])
            items = Repository.negotiation_items(conn, neg_id)
            Repository.add_negotiation_event(conn, neg_id, actor, role, "negotiation_opened", None, {"plan_ids": [plan_id, *related_ids]}, nows)
            Repository.audit(conn, plan_id, actor, role, "negotiation_opened", {"negotiation_id": neg_id, "plan_ids": [plan_id, *related_ids]})
            self._notify_negotiation(conn, items, "negotiation_opened", f"冲突协商单 #{neg_id} 已建立：主计划 {plan['callsign']} 与相邻计划需各方确认避让结果")
            return self._negotiation_view(conn, Repository.negotiation_row(conn, neg_id))

    def get_negotiation(self, negotiation_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            neg = Repository.negotiation_row(conn, negotiation_id)
            items = Repository.negotiation_items(conn, negotiation_id)
            if not self._negotiation_visible(role, operator, items): raise ApiError(403, "negotiation_forbidden", "不能查看其他运营方的协商单")
            view = self._negotiation_view(conn, neg)
            if role == "viewer":
                if neg["status"] != NegotiationPolicy.RELEASED: raise ApiError(403, "negotiation_forbidden", "进行中的协商单不对外公开")
                view = {key: view[key] for key in ("id", "status", "main_plan_id", "ready", "items")}
            return view

    def confirm_negotiation(self, negotiation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "negotiation_forbidden", "只有运营方可以确认自己的计划")
        plan_id, revision = body.get("plan_id"), body.get("confirmed_revision")
        if not isinstance(plan_id, int) or not isinstance(revision, int): raise ApiError(400, "confirm_details_required", "plan_id 和 confirmed_revision 必填")
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            neg = Repository.negotiation_row(conn, negotiation_id)
            if neg["status"] != NegotiationPolicy.OPEN: raise ApiError(409, "negotiation_closed", "协商单已结束，不能确认")
            item = Repository.item_row(conn, negotiation_id, plan_id)
            if item["operator_id"] != operator: raise ApiError(403, "negotiation_forbidden", "只能确认本运营方的计划")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] not in ACTIVE_STATUSES: raise ApiError(409, "plan_not_active", "计划需处于已提交或已批准状态才能确认")
            if plan["revision"] != revision: raise ApiError(409, "revision_conflict", "确认版本与计划当前版本不一致，请按当前版本确认")
            nows = iso()
            Repository.confirm_item(conn, negotiation_id, plan_id, revision, actor, nows)
            Repository.add_negotiation_event(conn, negotiation_id, actor, role, "plan_confirmed", None, {"plan_id": plan_id, "revision": revision}, nows)
            Repository.audit(conn, plan_id, actor, role, "negotiation_plan_confirmed", {"negotiation_id": negotiation_id, "revision": revision})
            items = Repository.negotiation_items(conn, negotiation_id)
            self._notify_negotiation(conn, items, "negotiation_confirmed", f"协商单 #{negotiation_id}：{operator} 的计划 {plan['callsign']} 已确认避让结果（版本 {revision}）")
            return self._negotiation_view(conn, Repository.negotiation_row(conn, negotiation_id))

    def reject_negotiation(self, negotiation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以拒绝协商单")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "拒绝协商单必须填写原因")
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            neg = Repository.negotiation_row(conn, negotiation_id)
            if neg["status"] == NegotiationPolicy.REJECTED: return {"negotiation": self._negotiation_view(conn, neg), "idempotent": True}
            if neg["status"] != NegotiationPolicy.OPEN: raise ApiError(409, "negotiation_closed", "协商单已结束")
            nows = iso(); items = Repository.negotiation_items(conn, negotiation_id)
            Repository.close_negotiation(conn, negotiation_id, NegotiationPolicy.REJECTED, reason, actor, nows)
            Repository.add_negotiation_event(conn, negotiation_id, actor, role, "negotiation_rejected", reason, {}, nows)
            Repository.audit(conn, neg["main_plan_id"], actor, role, "negotiation_rejected", {"reason": reason})
            self._notify_negotiation(conn, items, "negotiation_closed", f"冲突协商单 #{negotiation_id} 被拒绝：{reason}")
            return {"negotiation": self._negotiation_view(conn, Repository.negotiation_row(conn, negotiation_id)), "idempotent": False}

    def withdraw_negotiation(self, negotiation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "negotiation_forbidden", "只有运营方可以撤回协商单")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "撤回协商单必须填写原因")
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            neg = Repository.negotiation_row(conn, negotiation_id)
            if neg["status"] == NegotiationPolicy.WITHDRAWN: return {"negotiation": self._negotiation_view(conn, neg), "idempotent": True}
            if neg["status"] != NegotiationPolicy.OPEN: raise ApiError(409, "negotiation_closed", "协商单已结束")
            main = self._plan_row(conn, neg["main_plan_id"])
            if main["operator_id"] != operator: raise ApiError(403, "negotiation_forbidden", "只有主计划运营方可以撤回协商单")
            nows = iso(); items = Repository.negotiation_items(conn, negotiation_id)
            Repository.close_negotiation(conn, negotiation_id, NegotiationPolicy.WITHDRAWN, reason, actor, nows)
            Repository.add_negotiation_event(conn, negotiation_id, actor, role, "negotiation_withdrawn", reason, {}, nows)
            Repository.audit(conn, neg["main_plan_id"], actor, role, "negotiation_withdrawn", {"reason": reason})
            self._notify_negotiation(conn, items, "negotiation_closed", f"冲突协商单 #{negotiation_id} 已撤回：{reason}")
            return {"negotiation": self._negotiation_view(conn, Repository.negotiation_row(conn, negotiation_id)), "idempotent": False}

    def release_negotiation(self, negotiation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以放行协商单")
        offline_id, reason = str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        override = str(body.get("override_reason", "")).strip()
        if not offline_id or not reason: raise ApiError(400, "review_details_required", "offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            neg = Repository.negotiation_row(conn, negotiation_id)
            if neg["status"] == NegotiationPolicy.RELEASED:
                marker = conn.execute("SELECT detail_json FROM negotiation_events WHERE negotiation_id=? AND action='negotiation_released' ORDER BY id DESC LIMIT 1", (negotiation_id,)).fetchone()
                if marker and json.loads(marker["detail_json"]).get("offline_id") == offline_id:
                    return {"negotiation": self._negotiation_view(conn, neg), "idempotent": True, "released": []}
                raise ApiError(409, "offline_id_conflict", "协商单已放行，该离线审核编号与原放行不一致")
            if neg["status"] != NegotiationPolicy.OPEN: raise ApiError(409, "negotiation_closed", "协商单已结束，不能放行")
            prefix = f"{offline_id}:{negotiation_id}:"
            if conn.execute("SELECT 1 FROM approvals WHERE offline_id=? OR offline_id LIKE ? LIMIT 1", (offline_id, prefix + "%")).fetchone():
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经使用")
            hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []; adjacent: list[dict[str, Any]] = []
            for item in Repository.negotiation_items(conn, negotiation_id):
                plan = self._plan_row(conn, item["plan_id"]); report = self._conflict_report(conn, plan)
                if plan["status"] not in ACTIVE_STATUSES: raise ApiError(409, "plan_not_active", f"计划 {plan['callsign']} 状态不允许放行")
                if report["hard_violations"]: hard.append({"plan_id": item["plan_id"], "callsign": item["callsign"], "violations": report["hard_violations"]})
                traffic = [c for c in report["blocking_conflicts"] if c.get("code") == "adjacent_traffic"]
                others = [c for c in report["blocking_conflicts"] if c.get("code") != "adjacent_traffic"]
                if traffic: adjacent.append({"plan_id": item["plan_id"], "callsign": item["callsign"], "conflicts": traffic})
                if others: blocking.append({"plan_id": item["plan_id"], "callsign": item["callsign"], "conflicts": others})
            if hard: raise ApiError(409, "hard_constraint_violation", "协商计划违反不可覆盖的安全约束", {"violations": hard})
            if adjacent: raise ApiError(409, "adjacent_traffic_remains", "重新检查仍存在相邻交通冲突，不能放行", {"conflicts": adjacent})
            view = self._negotiation_view(conn, neg, with_reports=False)
            if not view["ready"]:
                raise ApiError(409, "negotiation_not_ready", "仍有运营方未确认或确认已失效", {"pending_operators": view["pending_operators"], "items": view["items"]})
            if blocking and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "协商计划仍有空域限制等阻断冲突", {"conflicts": blocking})
            nows = iso(); released: list[int] = []; items = Repository.negotiation_items(conn, negotiation_id)
            for item in items:
                plan = self._plan_row(conn, item["plan_id"])
                if plan["status"] == "approved": continue
                sub_offline = f"{prefix}{item['plan_id']}"
                had_other_blocking = any(g["plan_id"] == item["plan_id"] for g in blocking)
                conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                VALUES(?,?,?,?,?,?,?,?)""", (item["plan_id"], item["confirmed_revision"], actor, "approved", reason, sub_offline,
                                                            "emergency_authority" if had_other_blocking else None, nows))
                conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (nows, item["plan_id"]))
                Repository.audit(conn, item["plan_id"], actor, role, "plan_approved", {"revision": item["confirmed_revision"], "offline_id": sub_offline, "negotiation_id": negotiation_id})
                Repository.notify(conn, item["plan_id"], "approved", f"飞行计划 {plan['callsign']} 已随冲突协商单 #{negotiation_id} 放行")
                released.append(item["plan_id"])
            if blocking: Repository.audit(conn, neg["main_plan_id"], actor, role, "emergency_override_used", {"override_reason": override, "negotiation_id": negotiation_id, "conflicts": blocking})
            Repository.close_negotiation(conn, negotiation_id, NegotiationPolicy.RELEASED, reason, actor, nows)
            Repository.add_negotiation_event(conn, negotiation_id, actor, role, "negotiation_released", reason, {"offline_id": offline_id, "plan_ids": released, "override": bool(blocking)}, nows)
            Repository.audit(conn, neg["main_plan_id"], actor, role, "negotiation_released", {"offline_id": offline_id, "plan_ids": released})
            return {"negotiation": self._negotiation_view(conn, Repository.negotiation_row(conn, negotiation_id)), "idempotent": False, "released": released}

    def negotiation_board(self, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            self._auto_close_negotiations(conn)
            buckets: dict[str, list[dict[str, Any]]] = {"to_confirm": [], "approvable": [], "closed": []}
            for neg in Repository.list_negotiations(conn):
                items = Repository.negotiation_items(conn, neg["id"])
                if role == "operator" and not any(i["operator_id"] == operator for i in items): continue
                if neg["status"] != NegotiationPolicy.OPEN:
                    if role != "viewer" or neg["status"] == NegotiationPolicy.RELEASED:
                        buckets["closed"].append(self._negotiation_view(conn, neg, with_reports=False))
                    continue
                full = self._negotiation_view(conn, neg)
                if role == "operator":
                    mine_pending = any(v["state"] != "confirmed" and v["operator_id"] == operator for v in full["items"])
                    buckets["to_confirm" if mine_pending else "approvable"].append(full)
                elif role in {"airspace_reviewer", "commander", "auditor"}:
                    buckets["approvable" if full["ready"] else "to_confirm"].append(full)
            return {"negotiations": buckets, "server_time": iso()}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE (status='approved' AND ends_at<=?) OR (status='submitted' AND starts_at<=?)", (now, now)))
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
            closed = self._auto_close_negotiations(conn, actor=actor, role=role)
        return {"expired": len(rows), "negotiations_closed": closed}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        board = self.negotiation_board(role, operator)["negotiations"]
        return {"plans": plans, "restrictions": restrictions, "negotiations": board, "server_time": iso()}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/negotiations": return 200, self.service.negotiation_board(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        if len(parts) == 3 and parts[:2] == ["api", "negotiations"] and parts[2].isdigit(): return 200, self.service.get_negotiation(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
            }
            if action in routes: return 200, routes[action]()
        if len(parts) == 4 and parts[:2] == ["api", "negotiations"] and parts[2].isdigit():
            nid, action = int(parts[2]), parts[3]
            neg_routes = {
                "confirm": lambda: self.service.confirm_negotiation(nid, actor, role, operator, body),
                "reject": lambda: self.service.reject_negotiation(nid, actor, role, body),
                "withdraw": lambda: self.service.withdraw_negotiation(nid, actor, role, operator, body),
                "release": lambda: self.service.release_negotiation(nid, actor, role, body),
            }
            if len(parts) == 4 and action in neg_routes: return 200, neg_routes[action]()
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "negotiation":
            return 201, self.service.create_negotiation(int(parts[2]), actor, role, operator, body)
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path); handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
