"""冲突协商单判定逻辑：根据存储行与计划现状计算状态，纯函数、不接触数据库。

存储见 negotiation_store.py，业务编排见 negotiation_service.py。
"""
from __future__ import annotations

from typing import Any

# 参与方仍需履行义务的状态；declined/exited 为终态
ACTIVE_PARTY_STATUSES = ("pending", "confirmed", "stale")


def party_effective_status(party: dict[str, Any], plan: dict[str, Any] | None) -> str:
    """参与方当前展示状态：pending / confirmed / stale / declined / exited。

    确认后计划版本再变动即视为确认失效（stale，需重做）；
    计划取消、过期或被拒绝时参与方自动退出（exited）。
    """
    if party["party_status"] in ("declined", "exited"):
        return party["party_status"]
    if plan is None or plan["status"] in ("canceled", "expired", "rejected"):
        return "exited"
    if party["party_status"] == "confirmed" and party["confirmed_revision"] != plan["revision"]:
        return "stale"
    return party["party_status"]


def build_view(ticket: dict[str, Any], parties: list[dict[str, Any]], plans: dict[int, dict[str, Any]],
               recheck: dict[str, Any] | None = None) -> dict[str, Any]:
    """汇总一张协商单的判定结果。

    ticket/parties 为存储行，plans 为 plan_id -> flight_plans 行，
    recheck 为主计划的最新冲突检查报告（仅未结束单需要）。
    """
    party_views = []
    for party in parties:
        plan = plans.get(party["plan_id"])
        party_views.append({
            "plan_id": party["plan_id"],
            "callsign": plan["callsign"] if plan else None,
            "operator_id": party["operator_id"],
            "role_in_ticket": party["role_in_ticket"],
            "base_revision": party["base_revision"],
            "confirmed_revision": party["confirmed_revision"],
            "current_revision": plan["revision"] if plan else None,
            "party_status": party["party_status"],
            "effective_status": party_effective_status(party, plan),
            "confirmed_by": party["confirmed_by"],
            "confirmed_at": party["confirmed_at"],
            "exit_reason": party["exit_reason"],
        })
    view: dict[str, Any] = {
        "id": ticket["id"],
        "primary_plan_id": ticket["primary_plan_id"],
        "status": ticket["status"],
        "close_kind": ticket["close_kind"],
        "close_reason": ticket["close_reason"],
        "created_by": ticket["created_by"],
        "created_at": ticket["created_at"],
        "closed_at": ticket["closed_at"],
        "parties": party_views,
    }
    if ticket["status"] != "open":
        view.update(display_state="finished", releasable=False, pending_operators=[], redo_operators=[], release_blockers=[])
        return view
    pending = sorted({v["operator_id"] for v in party_views if v["effective_status"] == "pending"})
    redo = sorted({v["operator_id"] for v in party_views if v["effective_status"] == "stale"})
    blockers: list[str] = []
    if pending:
        blockers.append("等待运营方确认: " + "、".join(pending))
    if redo:
        blockers.append("确认已失效，需重做: " + "、".join(redo))
    adjacent_cleared = True
    if recheck is not None:
        adjacent_cleared = not any(c["code"] == "adjacent_traffic" for c in recheck["blocking_conflicts"])
        if not adjacent_cleared:
            blockers.append("重新检查仍存在相邻交通冲突")
        if recheck["hard_violations"]:
            blockers.append("主计划存在硬约束违规")
    releasable = not blockers
    view.update(
        display_state="approvable" if releasable else "pending_confirm",
        releasable=releasable,
        pending_operators=pending,
        redo_operators=redo,
        release_blockers=blockers,
        adjacent_conflict_cleared=adjacent_cleared,
    )
    return view
