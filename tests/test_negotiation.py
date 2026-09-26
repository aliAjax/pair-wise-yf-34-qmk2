import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class NegotiationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, operator, callsign, route=None):
        return self.svc.create_plan("op-user", "operator", operator, {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})

    def submitted_pair(self):
        a = self.plan("OP1", "D100")
        b = self.plan("OP2", "D200", route=[[116.11, 39.81], [116.15, 39.84]])
        self.svc.submit(a["id"], "op1", "operator", "OP1", {})
        self.svc.submit(b["id"], "op2", "operator", "OP2", {})
        return a, b

    def create_ticket(self, plan):
        return self.svc.negotiations.create_for_plan(plan["id"], "reviewer", "airspace_reviewer", "")["ticket"]

    def move_away_and_resubmit(self, plan, revision=1):
        self.svc.change(plan["id"], "op2", "operator", "OP2", {"expected_revision": revision, "route": [[117.0, 40.0], [117.2, 40.1]]})
        self.svc.submit(plan["id"], "op2", "operator", "OP2", {})

    def test_create_collects_plans_and_revisions(self):
        a, b = self.submitted_pair()
        ticket = self.create_ticket(a)
        self.assertEqual(ticket["status"], "open"); self.assertEqual(ticket["display_state"], "pending_confirm")
        parties = {p["plan_id"]: p for p in ticket["parties"]}
        self.assertEqual(parties[a["id"]]["role_in_ticket"], "primary")
        self.assertEqual(parties[b["id"]]["role_in_ticket"], "related")
        self.assertEqual(parties[a["id"]]["base_revision"], 1)
        self.assertEqual(set(ticket["pending_operators"]), {"OP1", "OP2"})
        again = self.svc.negotiations.create_for_plan(a["id"], "reviewer", "airspace_reviewer", "")
        self.assertTrue(again["idempotent"]); self.assertEqual(again["ticket"]["id"], ticket["id"])
        far = self.plan("OP3", "D300", route=[[117.0, 40.0], [117.1, 40.1]])
        self.svc.submit(far["id"], "op3", "operator", "OP3", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.create_for_plan(far["id"], "reviewer", "airspace_reviewer", "")
        self.assertEqual(ctx.exception.code, "no_adjacent_conflict")

    def test_confirm_and_release(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        self.move_away_and_resubmit(b)
        confirmed = self.svc.negotiations.confirm(tid, "op2", "operator", "OP2", {"plan_id": b["id"]})
        self.assertFalse(confirmed["idempotent"])
        party = next(p for p in confirmed["ticket"]["parties"] if p["plan_id"] == b["id"])
        self.assertEqual(party["confirmed_revision"], 2)
        self.assertTrue(self.svc.negotiations.confirm(tid, "op2", "operator", "OP2", {"plan_id": b["id"]})["idempotent"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.release(tid, "reviewer", "airspace_reviewer", {"reason": "放行", "offline_id": "rel-1"})
        self.assertEqual(ctx.exception.code, "negotiation_not_ready")
        self.assertIn("OP1", ctx.exception.details["blockers"][0])
        self.svc.negotiations.confirm(tid, "op1", "operator", "OP1", {"plan_id": a["id"]})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertTrue(view["releasable"]); self.assertEqual(view["display_state"], "approvable")
        done = self.svc.negotiations.release(tid, "reviewer", "airspace_reviewer", {"reason": "协商一致", "offline_id": "rel-1"})
        self.assertEqual(done["plan"]["status"], "approved")
        self.assertEqual(done["ticket"]["status"], "released"); self.assertEqual(done["ticket"]["display_state"], "finished")
        dup = self.svc.negotiations.release(tid, "reviewer", "airspace_reviewer", {"reason": "补传", "offline_id": "rel-1"})
        self.assertTrue(dup["idempotent"])

    def test_change_invalidates_confirmation_and_marks_redo_operator(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        self.svc.negotiations.confirm(tid, "op2", "operator", "OP2", {"plan_id": b["id"]})
        self.svc.change(b["id"], "op2", "operator", "OP2", {"expected_revision": 1, "route": [[117.0, 40.0], [117.2, 40.1]]})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertEqual(view["redo_operators"], ["OP2"])
        party = next(p for p in view["parties"] if p["plan_id"] == b["id"])
        self.assertEqual(party["effective_status"], "stale")
        self.assertFalse(view["releasable"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.release(tid, "reviewer", "airspace_reviewer", {"reason": "放行", "offline_id": "rel-2"})
        self.assertEqual(ctx.exception.code, "negotiation_not_ready")
        self.svc.submit(b["id"], "op2", "operator", "OP2", {})
        self.svc.negotiations.confirm(tid, "op2", "operator", "OP2", {"plan_id": b["id"]})
        self.svc.negotiations.confirm(tid, "op1", "operator", "OP1", {"plan_id": a["id"]})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertEqual(view["redo_operators"], []); self.assertTrue(view["releasable"])

    def test_decline_requires_reason_and_closes_ticket(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.decline(tid, "op2", "operator", "OP2", {"plan_id": b["id"], "reason": ""})
        self.assertEqual(ctx.exception.code, "reason_required")
        done = self.svc.negotiations.decline(tid, "op2", "operator", "OP2", {"plan_id": b["id"], "reason": "无法调整时刻"})
        self.assertEqual(done["ticket"]["status"], "closed"); self.assertEqual(done["ticket"]["close_kind"], "declined")
        self.assertIn("无法调整时刻", done["ticket"]["close_reason"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.confirm(tid, "op1", "operator", "OP1", {"plan_id": a["id"]})
        self.assertEqual(ctx.exception.code, "negotiation_closed")

    def test_withdraw_requires_reason_and_permission(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.withdraw(tid, "op1", "operator", "OP1", {"reason": ""})
        self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.withdraw(tid, "op2", "operator", "OP2", {"reason": "相关方不能撤回"})
        self.assertEqual(ctx.exception.code, "withdraw_forbidden")
        done = self.svc.negotiations.withdraw(tid, "op1", "operator", "OP1", {"reason": "任务取消，重新规划"})
        self.assertEqual(done["ticket"]["close_kind"], "withdrawn")
        self.assertEqual(done["ticket"]["close_reason"], "任务取消，重新规划")

    def test_cancel_related_plan_exits_automatically(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        self.svc.cancel(b["id"], "op2", "operator", "OP2", {"reason": "任务取消"})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        party = next(p for p in view["parties"] if p["plan_id"] == b["id"])
        self.assertEqual(party["effective_status"], "exited"); self.assertEqual(party["exit_reason"], "canceled")
        self.assertEqual(view["status"], "open")
        self.svc.negotiations.confirm(tid, "op1", "operator", "OP1", {"plan_id": a["id"]})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertTrue(view["releasable"])

    def test_cancel_primary_closes_ticket(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        self.svc.cancel(a["id"], "op1", "operator", "OP1", {"reason": "取消任务"})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertEqual(view["status"], "closed"); self.assertEqual(view["close_kind"], "canceled")

    def test_expire_closes_ticket(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        past = iso(utcnow() - timedelta(hours=1))
        self.svc.repo.conn.execute("UPDATE flight_plans SET ends_at=? WHERE id IN (?,?)", (past, a["id"], b["id"]))
        self.svc.expire_plans("reviewer", "airspace_reviewer")
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertEqual(view["status"], "closed"); self.assertEqual(view["close_kind"], "expired")

    def test_reject_related_plan_exits_and_reject_primary_closes(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        self.svc.reject(b["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "rej-1", "reason": "风险过高"})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        party = next(p for p in view["parties"] if p["plan_id"] == b["id"])
        self.assertEqual(party["effective_status"], "exited"); self.assertEqual(party["exit_reason"], "rejected")
        self.assertEqual(view["status"], "open")
        self.svc.reject(a["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "rej-2", "reason": "无法协调"})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertEqual(view["status"], "closed"); self.assertEqual(view["close_kind"], "rejected")

    def test_regular_approve_closes_ticket(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        self.svc.approve(a["id"], "cmd", "commander", {"expected_revision": 1, "offline_id": "ov-1", "reason": "紧急任务", "override_reason": "应急救援"})
        view = self.svc.negotiations.get_view(tid, "airspace_reviewer", "")
        self.assertEqual(view["status"], "released"); self.assertEqual(view["close_kind"], "released")

    def test_state_buckets_by_role(self):
        a, b = self.submitted_pair()
        tid = self.create_ticket(a)["id"]
        op_state = self.svc.state("operator", "OP1")["negotiations"]
        self.assertEqual([t["id"] for t in op_state["pending_confirm"]], [tid])
        self.assertTrue(op_state["pending_confirm"][0]["my_action_needed"])
        reviewer_state = self.svc.state("airspace_reviewer", "")["negotiations"]
        self.assertEqual([t["id"] for t in reviewer_state["pending_confirm"]], [tid])
        other = self.svc.state("operator", "OP9")["negotiations"]
        self.assertEqual(other["pending_confirm"], [])
        viewer_state = self.svc.state("viewer", "")["negotiations"]
        self.assertEqual(viewer_state, {"pending_confirm": [], "approvable": [], "finished": []})
        self.move_away_and_resubmit(b)
        self.svc.negotiations.confirm(tid, "op2", "operator", "OP2", {"plan_id": b["id"]})
        self.svc.negotiations.confirm(tid, "op1", "operator", "OP1", {"plan_id": a["id"]})
        reviewer_state = self.svc.state("airspace_reviewer", "")["negotiations"]
        self.assertEqual([t["id"] for t in reviewer_state["approvable"]], [tid])
        self.svc.negotiations.release(tid, "reviewer", "airspace_reviewer", {"reason": "放行", "offline_id": "rel-3"})
        reviewer_state = self.svc.state("airspace_reviewer", "")["negotiations"]
        self.assertEqual([t["id"] for t in reviewer_state["finished"]], [tid])
        self.assertEqual(reviewer_state["approvable"], [])

    def test_permissions(self):
        a, b = self.submitted_pair()
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.create_for_plan(a["id"], "op2", "operator", "OP2")
        self.assertEqual(ctx.exception.code, "plan_forbidden")
        tid = self.create_ticket(a)["id"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.confirm(tid, "op2", "operator", "OP2", {"plan_id": a["id"]})
        self.assertEqual(ctx.exception.code, "party_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.release(tid, "op1", "operator", {"reason": "x", "offline_id": "y"})
        self.assertEqual(ctx.exception.code, "release_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.negotiations.get_view(tid, "operator", "OP9")
        self.assertEqual(ctx.exception.code, "negotiation_forbidden")


if __name__ == "__main__": unittest.main()
