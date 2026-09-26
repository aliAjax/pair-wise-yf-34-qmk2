"""冲突协商单业务编排：权限、事务、通知与自动退出钩子。

判定规则见 negotiation_rules.py，SQL 见 negotiation_store.py，HTTP 入口见 app.py。
本模块只负责把三者组合起来，并复用主服务的计划读取、冲突检查与通知审计。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from common import ApiError, iso
from negotiation_store import NegotiationStore
import negotiation_rules

REVIEW_ROLES = {"airspace_reviewer", "commander"}
# 计划状态事件 -> 主计划协商单结束方式
EVENT_CLOSE_KIND = {"approved": "released", "rejected": "rejected", "canceled": "canceled", "expired": "expired"}
EVENT_LABEL = {"approved": "主计划已批准", "rejected": "主计划已被拒绝", "canceled": "主计划已取消", "expired": "主计划已过期"}
EXIT_LABEL = {"rejected": "被拒绝", "canceled": "已取消", "expired": "已过期"}
KIND_LABEL = {"released": "已放行", "declined": "协商被拒绝", "withdrawn": "已撤回",
              "canceled": "主计划已取消", "expired": "已过期", "rejected": "主计划被拒绝"}


class NegotiationService:
    def __init__(self, main: Any):
        self.main = main  # DroneAirspaceService：复用 _plan_row / _conflict_report / get_plan / repo

    @property
    def repo(self):
        return self.main.repo

    # ---------- 视图 ----------
    def _ticket_view(self, conn: sqlite3.Connection, ticket: sqlite3.Row, recheck: bool = True) -> dict[str, Any]:
        parties = [dict(r) for r in NegotiationStore.list_parties(conn, ticket["id"])]
        plans: dict[int, dict[str, Any]] = {}
        for pid in {ticket["primary_plan_id"], *(p["plan_id"] for p in parties)}:
            row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (pid,)).fetchone()
            if row:
                plans[pid] = dict(row)
        primary = plans.get(ticket["primary_plan_id"])
        report = None
        if recheck and ticket["status"] == "open" and primary is not None:
            report = self.main._conflict_report(conn, primary)
        view = negotiation_rules.build_view(dict(ticket), parties, plans, report)
        view["primary_callsign"] = primary["callsign"] if primary else None
        if report is not None:
            view["recheck"] = {"approvable": report["approvable"], "hard_violations": report["hard_violations"],
                               "blocking_conflicts": report["blocking_conflicts"]}
        return view

    def _get_ticket(self, conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
        ticket = NegotiationStore.get_ticket(conn, ticket_id)
        if not ticket:
            raise ApiError(404, "negotiation_not_found", "协商单不存在")
        return ticket

    def _open_ticket(self, conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
        ticket = self._get_ticket(conn, ticket_id)
        if ticket["status"] != "open":
            raise ApiError(409, "negotiation_closed", "协商单已结束", {"ticket": self._ticket_view(conn, ticket, recheck=False)})
        primary = self.main._plan_row(conn, ticket["primary_plan_id"])
        if primary["status"] in ("canceled", "expired"):  # 兜底：主计划取消/过期时整单自动结束
            label = "已取消" if primary["status"] == "canceled" else "已过期"
            self._close_ticket(conn, ticket, primary["status"], f"主计划{label}", "system", "system")
            raise ApiError(409, "negotiation_closed", f"主计划{label}，协商单自动结束")
        return ticket

    def _close_ticket(self, conn: sqlite3.Connection, ticket: sqlite3.Row, close_kind: str, reason: str, actor: str, role: str) -> None:
        status = "released" if close_kind == "released" else "closed"
        NegotiationStore.close_ticket(conn, ticket["id"], status, close_kind, reason, iso())
        self.repo.audit(conn, ticket["primary_plan_id"], actor, role, "negotiation_closed",
                        {"ticket_id": ticket["id"], "close_kind": close_kind, "reason": reason})
        label = KIND_LABEL.get(close_kind, "已结束")
        for party in NegotiationStore.list_parties(conn, ticket["id"]):
            self.repo.notify(conn, party["plan_id"], "negotiation_closed", f"协商单 #{ticket['id']} {label}：{reason}")

    # ---------- 创建 ----------
    def create_for_plan(self, plan_id: int, actor: str, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self.main._plan_row(conn, plan_id)
            if role == "operator":
                if plan["operator_id"] != operator:
                    raise ApiError(403, "plan_forbidden", "不能为其他运营方的计划发起协商")
            elif role not in REVIEW_ROLES:
                raise ApiError(403, "negotiation_forbidden", "只有运营方、空域审核员或指挥官可以发起协商")
            if plan["status"] != "submitted":
                raise ApiError(409, "invalid_transition", "只有已提交待审核的计划可以发起冲突协商")
            existing = NegotiationStore.get_open_ticket_for_plan(conn, plan_id)
            if existing:
                return {"ticket": self._ticket_view(conn, existing), "idempotent": True}
            report = self.main._conflict_report(conn, plan)
            adjacent = next((c for c in report["blocking_conflicts"] if c["code"] == "adjacent_traffic"), None)
            if not adjacent:
                raise ApiError(409, "no_adjacent_conflict", "当前没有相邻交通冲突，无需协商")
            ticket_id = NegotiationStore.create_ticket(conn, plan_id, actor, role, iso())
            NegotiationStore.add_party(conn, ticket_id, plan_id, plan["operator_id"], "primary", plan["revision"])
            related_ids = []
            for item in adjacent["plans"]:
                other = self.main._plan_row(conn, item["plan_id"])
                related_ids.append(other["id"])
                NegotiationStore.add_party(conn, ticket_id, other["id"], other["operator_id"], "related", other["revision"])
                self.repo.notify(conn, other["id"], "negotiation_invited",
                                 f"计划 {other['callsign']} 与 {plan['callsign']} 存在相邻交通冲突，已加入协商单 #{ticket_id}，请调整或确认")
            self.repo.notify(conn, plan_id, "negotiation_created", f"计划 {plan['callsign']} 的冲突协商单 #{ticket_id} 已创建")
            self.repo.audit(conn, plan_id, actor, role, "negotiation_created", {"ticket_id": ticket_id, "related_plan_ids": related_ids})
            return {"ticket": self._ticket_view(conn, self._get_ticket(conn, ticket_id)), "idempotent": False}

    # ---------- 参与方动作 ----------
    def _operator_party(self, conn: sqlite3.Connection, ticket_id: int, operator: str, body: dict[str, Any]) -> sqlite3.Row:
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, int):
            raise ApiError(400, "plan_required", "plan_id 必填，用于指明操作哪一份计划")
        party = NegotiationStore.get_party(conn, ticket_id, plan_id)
        if not party:
            raise ApiError(404, "party_not_found", "该计划不在协商单中")
        if party["operator_id"] != operator:
            raise ApiError(403, "party_forbidden", "只能以本运营方的计划参与协商")
        if party["party_status"] in ("declined", "exited"):
            raise ApiError(409, "party_closed", "该参与方已退出协商")
        return party

    def confirm(self, ticket_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator":
            raise ApiError(403, "confirm_forbidden", "只有运营方可以确认协商结果")
        with self.repo.tx() as conn:
            ticket = self._open_ticket(conn, ticket_id)
            party = self._operator_party(conn, ticket_id, operator, body)
            plan = self.main._plan_row(conn, party["plan_id"])
            if plan["status"] in ("canceled", "expired", "rejected"):
                raise ApiError(409, "party_closed", "计划已取消、过期或被拒绝，不能确认")
            if party["party_status"] == "confirmed" and party["confirmed_revision"] == plan["revision"]:
                return {"ticket": self._ticket_view(conn, ticket), "idempotent": True}
            NegotiationStore.confirm_party(conn, party["id"], plan["revision"], actor, iso())
            self.repo.audit(conn, ticket["primary_plan_id"], actor, role, "negotiation_confirmed",
                            {"ticket_id": ticket_id, "plan_id": plan["id"], "revision": plan["revision"]})
            self.repo.notify(conn, ticket["primary_plan_id"], "negotiation_confirmed",
                             f"协商单 #{ticket_id}：运营方 {operator} 已确认计划 {plan['callsign']} 的版本 {plan['revision']}")
            return {"ticket": self._ticket_view(conn, ticket), "idempotent": False}

    def decline(self, ticket_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator":
            raise ApiError(403, "decline_forbidden", "只有运营方可以拒绝协商")
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ApiError(400, "reason_required", "拒绝协商必须填写原因")
        with self.repo.tx() as conn:
            ticket = self._open_ticket(conn, ticket_id)
            party = self._operator_party(conn, ticket_id, operator, body)
            NegotiationStore.set_party_status(conn, party["id"], "declined")
            self._close_ticket(conn, ticket, "declined", f"运营方 {operator} 拒绝协商：{reason}", actor, role)
            self.repo.audit(conn, ticket["primary_plan_id"], actor, role, "negotiation_declined",
                            {"ticket_id": ticket_id, "plan_id": party["plan_id"], "reason": reason})
            return {"ticket": self._ticket_view(conn, self._get_ticket(conn, ticket_id), recheck=False), "idempotent": False}

    def withdraw(self, ticket_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ApiError(400, "reason_required", "撤回协商必须填写原因")
        with self.repo.tx() as conn:
            ticket = self._open_ticket(conn, ticket_id)
            primary = self.main._plan_row(conn, ticket["primary_plan_id"])
            allowed = role in REVIEW_ROLES or actor == ticket["created_by"] or (role == "operator" and primary["operator_id"] == operator)
            if not allowed:
                raise ApiError(403, "withdraw_forbidden", "只有发起方、主计划运营方或审核角色可以撤回协商单")
            self._close_ticket(conn, ticket, "withdrawn", reason, actor, role)
            return {"ticket": self._ticket_view(conn, self._get_ticket(conn, ticket_id), recheck=False), "idempotent": False}

    # ---------- 放行 ----------
    def release(self, ticket_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in REVIEW_ROLES:
            raise ApiError(403, "release_forbidden", "只有空域审核员或指挥官可以放行")
        reason = str(body.get("reason", "")).strip()
        offline_id = str(body.get("offline_id", "")).strip()
        override = str(body.get("override_reason", "")).strip()
        if not reason or not offline_id:
            raise ApiError(400, "review_details_required", "reason 和 offline_id 必填")
        with self.repo.tx() as conn:
            ticket = self._get_ticket(conn, ticket_id)
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == ticket["primary_plan_id"] and prior["decision"] == "approved":
                    return {"ticket": self._ticket_view(conn, ticket, recheck=False), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            if ticket["status"] != "open":
                raise ApiError(409, "negotiation_closed", "协商单已结束", {"ticket": self._ticket_view(conn, ticket, recheck=False)})
            view = self._ticket_view(conn, ticket)  # 判定：确认版本一致 + 重新检查无相邻冲突
            if not view["releasable"]:
                raise ApiError(409, "negotiation_not_ready", "协商单尚未满足放行条件",
                               {"blockers": view["release_blockers"], "ticket": view})
            primary = self.main._plan_row(conn, ticket["primary_plan_id"])
            if primary["status"] != "submitted":
                raise ApiError(409, "invalid_transition", "主计划当前不在待审核状态，不能放行")
            approval_id, override_kind = self.main._apply_approval(conn, primary, actor, role, reason, offline_id, override)
            self._close_ticket(conn, ticket, "released", reason, actor, role)
            self.repo.audit(conn, primary["id"], actor, role, "negotiation_released",
                            {"ticket_id": ticket_id, "approval_id": approval_id, "offline_id": offline_id})
            return {"ticket": self._ticket_view(conn, self._get_ticket(conn, ticket_id), recheck=False),
                    "plan": self.main.get_plan(primary["id"], role, ""),
                    "approval_id": approval_id, "override_kind": override_kind, "idempotent": False}

    # ---------- 查询 ----------
    def get_view(self, ticket_id: int, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        ticket = self._get_ticket(conn, ticket_id)
        if role == "operator":
            operators = {p["operator_id"] for p in NegotiationStore.list_parties(conn, ticket_id)}
            if operator not in operators:
                raise ApiError(403, "negotiation_forbidden", "只有协商参与方可以查看该协商单")
        elif role not in {"airspace_reviewer", "commander", "auditor"}:
            raise ApiError(403, "negotiation_forbidden", "当前角色不能查看协商单")
        return self._ticket_view(conn, ticket)

    def state_for(self, role: str, operator: str) -> dict[str, Any]:
        """首页三栏：待确认 / 可批准 / 已结束，按角色过滤。"""
        buckets: dict[str, Any] = {"pending_confirm": [], "approvable": [], "finished": []}
        conn = self.repo.conn
        if role == "operator":
            tickets = NegotiationStore.tickets_for_operator(conn, operator)
        elif role in {"airspace_reviewer", "commander", "auditor"}:
            tickets = NegotiationStore.list_tickets(conn)
        else:
            return buckets
        for ticket in tickets:
            view = self._ticket_view(conn, ticket)
            if role == "operator":
                mine = [p for p in view["parties"] if p["operator_id"] == operator]
                view["my_action_needed"] = view["status"] == "open" and any(p["effective_status"] in ("pending", "stale") for p in mine)
            buckets[view["display_state"]].append(view)
        return buckets

    # ---------- 计划状态钩子：取消、过期、被拒绝自动退出 ----------
    def sync_plan_event(self, conn: sqlite3.Connection, plan: sqlite3.Row, event: str) -> None:
        """计划批准/被拒绝/取消/过期时调用：主计划结束整单，相关计划自动退出。"""
        ticket = NegotiationStore.get_open_ticket_for_plan(conn, plan["id"])
        if ticket:
            self._close_ticket(conn, ticket, EVENT_CLOSE_KIND[event], EVENT_LABEL[event], "system", "system")
        if event == "approved":
            return  # 相关计划被批准不影响其他协商单
        for row in NegotiationStore.open_related_tickets_for_plan(conn, plan["id"]):
            party = NegotiationStore.get_party(conn, row["id"], plan["id"])
            if not party or party["party_status"] not in ("pending", "confirmed"):
                continue
            NegotiationStore.set_party_status(conn, party["id"], "exited", event)
            self.repo.audit(conn, row["primary_plan_id"], "system", "system", "negotiation_party_exited",
                            {"ticket_id": row["id"], "plan_id": plan["id"], "event": event})
            self.repo.notify(conn, row["primary_plan_id"], "negotiation_party_exited",
                             f"协商单 #{row['id']}：计划 {plan['callsign']} {EXIT_LABEL[event]}，自动退出协商")

    def sweep_expired(self, conn: sqlite3.Connection, now: str) -> None:
        """到期处理：主计划飞行窗口结束则整单过期，相关计划窗口结束则自动退出。"""
        for ticket in NegotiationStore.list_open_tickets(conn):
            primary = self.main._plan_row(conn, ticket["primary_plan_id"])
            if primary["ends_at"] <= now:
                self._close_ticket(conn, ticket, "expired", "主计划飞行窗口已结束", "system", "system")
                continue
            for party in NegotiationStore.list_parties(conn, ticket["id"]):
                if party["party_status"] not in ("pending", "confirmed"):
                    continue
                plan = self.main._plan_row(conn, party["plan_id"])
                if plan["ends_at"] <= now:
                    NegotiationStore.set_party_status(conn, party["id"], "exited", "expired")
                    self.repo.audit(conn, ticket["primary_plan_id"], "system", "system", "negotiation_party_exited",
                                    {"ticket_id": ticket["id"], "plan_id": plan["id"], "event": "expired"})
                    self.repo.notify(conn, ticket["primary_plan_id"], "negotiation_party_exited",
                                     f"协商单 #{ticket['id']}：计划 {plan['callsign']} 的飞行窗口已结束，自动退出协商")
