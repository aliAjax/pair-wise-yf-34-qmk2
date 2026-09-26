import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class NegotiationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, operator, callsign, route, start=None):
        start = start or self.start
        return self.svc.create_plan("u-" + operator, "operator", operator, {"callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route, "starts_at": iso(start), "ends_at": iso(start + timedelta(hours=1)),
            "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})

    def submit(self, p): return self.svc.submit(p["id"], "u-" + p["operator_id"], "operator", p["operator_id"], {})["plan"]

    def test_negotiation_confirm_change_invalidate_release(self):
        a = self.plan("OPA", "A1", [[116.10, 39.80], [116.30, 39.90]])
        b = self.plan("OPB", "B1", [[116.11, 39.81], [116.15, 39.84]])
        self.submit(a); self.submit(b)

        check = self.svc.check_conflicts(a["id"], "airspace_reviewer", "")
        self.assertFalse(check["approvable"])

        with self.assertRaises(ApiError) as ctx:
            self.svc.create_negotiation(999, "reviewer", "airspace_reviewer", "", {})
        self.assertEqual(ctx.exception.code, "plan_not_found")

        neg = self.svc.create_negotiation(a["id"], "reviewer", "airspace_reviewer", "", {"reason": "双方避让"})
        self.assertEqual(neg["status"], "open"); self.assertEqual({i["plan_id"] for i in neg["items"]}, {a["id"], b["id"]})
        self.assertEqual(neg["items"][0]["base_revision"], 1)
        # 无相邻冲突的计划不能再开单
        clean = self.plan("OPC", "C1", [[120.0, 30.0], [120.1, 30.1]]); self.submit(clean)
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_negotiation(clean["id"], "reviewer", "airspace_reviewer", "", {})
        self.assertEqual(ctx.exception.code, "no_adjacent_traffic")
        # 重复开单被拒
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_negotiation(a["id"], "reviewer", "airspace_reviewer", "", {})
        self.assertEqual(ctx.exception.code, "negotiation_already_open")

        # 双方按各自修订后的计划确认（先改航路拉开间隔）
        changed_a = self.svc.change(a["id"], "u-OPA", "operator", "OPA", {"expected_revision": 1, "route": [[116.10, 39.80], [116.105, 39.805]]})
        self.submit(changed_a)
        changed_b = self.svc.change(b["id"], "u-OPB", "operator", "OPB", {"expected_revision": 1, "route": [[116.20, 39.86], [116.30, 39.90]]})
        self.submit(changed_b)

        # A 的修改发生在开单后？这里开单在修改前，确认需与当前版本一致；旧 base 确认应被拒
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_negotiation(neg["id"], "u-OPA", "operator", "OPA", {"plan_id": a["id"], "confirmed_revision": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")

        self.svc.confirm_negotiation(neg["id"], "u-OPA", "operator", "OPA", {"plan_id": a["id"], "confirmed_revision": changed_a["revision"]})
        view = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        self.assertFalse(view["ready"]); self.assertEqual(view["pending_operators"], ["OPB"])

        # 看板：审核员视角未齐确认 -> to_confirm；OPB 视角自己待确认
        board = self.svc.negotiation_board("airspace_reviewer", "")["negotiations"]
        self.assertEqual([n["id"] for n in board["to_confirm"]], [neg["id"]]); self.assertEqual(board["approvable"], [])
        board_b = self.svc.negotiation_board("operator", "OPB")["negotiations"]
        self.assertEqual([n["id"] for n in board_b["to_confirm"]], [neg["id"]])

        self.svc.confirm_negotiation(neg["id"], "u-OPB", "operator", "OPB", {"plan_id": b["id"], "confirmed_revision": changed_b["revision"]})
        view = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        self.assertTrue(view["ready"]); self.assertEqual(view["pending_operators"], [])
        board = self.svc.negotiation_board("airspace_reviewer", "")["negotiations"]
        self.assertEqual([n["id"] for n in board["approvable"]], [neg["id"]])

        # 任一计划再改动：确认立即失效，页面指出需重做运营方
        again = self.svc.change(b["id"], "u-OPB", "operator", "OPB", {"expected_revision": changed_b["revision"], "route": [[116.108, 39.808], [116.12, 39.82]]})
        self.submit(again)
        view = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        stale = [i for i in view["items"] if i["state"] == "stale"]
        self.assertEqual([i["operator_id"] for i in stale], ["OPB"])
        self.assertFalse(view["ready"]); self.assertEqual(view["pending_operators"], ["OPB"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"offline_id": "off-rel-1", "reason": "放行"})
        self.assertEqual(ctx.exception.code, "negotiation_not_ready")

        # 重做确认后放行，两计划同时批准
        self.svc.confirm_negotiation(neg["id"], "u-OPB", "operator", "OPB", {"plan_id": b["id"], "confirmed_revision": again["revision"]})
        result = self.svc.release_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"offline_id": "off-rel-1", "reason": "避让到位"})
        self.assertEqual(set(result["released"]), {a["id"], b["id"]})
        self.assertEqual(self.svc.get_plan(a["id"], "airspace_reviewer", "")["status"], "approved")
        self.assertEqual(self.svc.get_plan(b["id"], "airspace_reviewer", "")["status"], "approved")
        detail = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        self.assertEqual(detail["status"], "released")
        # 幂等放行
        again_rel = self.svc.release_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"offline_id": "off-rel-1", "reason": "补传"})
        self.assertTrue(again_rel["idempotent"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"offline_id": "off-rel-x", "reason": "x"})
        self.assertEqual(ctx.exception.code, "offline_id_conflict")
        # 已结束不能再确认/拒绝
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_negotiation(neg["id"], "u-OPA", "operator", "OPA", {"plan_id": a["id"], "confirmed_revision": 3})
        self.assertEqual(ctx.exception.code, "negotiation_closed")

    def test_release_blocked_when_adjacent_conflict_remains(self):
        a = self.plan("OPA", "A2", [[116.10, 39.80], [116.30, 39.90]])
        b = self.plan("OPB", "B2", [[116.11, 39.81], [116.15, 39.84]])
        self.submit(a); self.submit(b)
        neg = self.svc.create_negotiation(a["id"], "u-OPA", "operator", "OPA", {"reason": "谈避让"})
        # 双方确认了当前版本，但航路没改，重新检查仍有相邻冲突
        self.svc.confirm_negotiation(neg["id"], "u-OPA", "operator", "OPA", {"plan_id": a["id"], "confirmed_revision": 1})
        self.svc.confirm_negotiation(neg["id"], "u-OPB", "operator", "OPB", {"plan_id": b["id"], "confirmed_revision": 1})
        view = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        self.assertFalse(view["ready"])  # 确认虽齐，但仍有相邻交通冲突
        self.assertEqual(view["pending_operators"], [])
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"offline_id": "off-rel-2", "reason": "放行"})
        self.assertEqual(ctx.exception.code, "adjacent_traffic_remains")
        # 指挥官也不能覆盖相邻交通
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_negotiation(neg["id"], "cmdr", "commander", {"offline_id": "off-rel-3", "reason": "紧急", "override_reason": "应急"})
        self.assertEqual(ctx.exception.code, "adjacent_traffic_remains")

    def test_reject_withdraw_require_reason_and_close(self):
        a = self.plan("OPA", "A3", [[116.10, 39.80], [116.30, 39.90]])
        b = self.plan("OPB", "B3", [[116.11, 39.81], [116.15, 39.84]])
        self.submit(a); self.submit(b)
        neg = self.svc.create_negotiation(a["id"], "reviewer", "airspace_reviewer", "", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"reason": "  "})
        self.assertEqual(ctx.exception.code, "reason_required")
        out = self.svc.reject_negotiation(neg["id"], "reviewer", "airspace_reviewer", {"reason": "避让方案不可接受"})
        self.assertFalse(out["idempotent"])
        self.assertEqual(self.svc.get_negotiation(neg["id"], "operator", "OPA")["status"], "rejected")

        a2 = self.plan("OPA", "A4", [[116.10, 39.80], [116.30, 39.90]])
        b2 = self.plan("OPB", "B4", [[116.11, 39.81], [116.15, 39.84]])
        self.submit(a2); self.submit(b2)
        neg2 = self.svc.create_negotiation(a2["id"], "reviewer", "airspace_reviewer", "", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.withdraw_negotiation(neg2["id"], "u-OPB", "operator", "OPB", {"reason": "我方撤回"})
        self.assertEqual(ctx.exception.code, "negotiation_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.withdraw_negotiation(neg2["id"], "u-OPA", "operator", "OPA", {"reason": ""})
        self.assertEqual(ctx.exception.code, "reason_required")
        self.svc.withdraw_negotiation(neg2["id"], "u-OPA", "operator", "OPA", {"reason": "我方任务取消"})
        self.assertEqual(self.svc.get_negotiation(neg2["id"], "airspace_reviewer", "")["status"], "withdrawn")

        board = self.svc.negotiation_board("operator", "OPA")["negotiations"]
        self.assertEqual({n["id"] for n in board["closed"]}, {neg["id"], neg2["id"]})

    def test_cancel_auto_exit(self):
        a = self.plan("OPA", "A5", [[116.10, 39.80], [116.30, 39.90]])
        b = self.plan("OPB", "B5", [[116.11, 39.81], [116.15, 39.84]])
        self.submit(a); self.submit(b)
        neg = self.svc.create_negotiation(a["id"], "reviewer", "airspace_reviewer", "", {})
        self.svc.cancel(b["id"], "u-OPB", "operator", "OPB", {"reason": "天气原因"})
        view = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        self.assertEqual(view["status"], "canceled")
        self.assertIn("自动退出", view["close_reason"])

    def test_expire_auto_exit_and_sweep(self):
        a = self.plan("OPA", "A6", [[116.10, 39.80], [116.30, 39.90]])
        b = self.plan("OPB", "B6", [[116.11, 39.81], [116.15, 39.84]])
        self.submit(a); self.submit(b)
        neg = self.svc.create_negotiation(a["id"], "reviewer", "airspace_reviewer", "", {})
        past = iso(utcnow() - timedelta(hours=1))
        self.svc.repo.conn.execute("UPDATE flight_plans SET starts_at=? WHERE id=?", (past, a["id"]))
        sweep = self.svc.expire_plans("reviewer", "airspace_reviewer")
        self.assertIn(neg["id"], sweep["negotiations_closed"])
        view = self.svc.get_negotiation(neg["id"], "airspace_reviewer", "")
        self.assertEqual(view["status"], "expired")
        self.assertEqual(self.svc.get_plan(a["id"], "airspace_reviewer", "")["status"], "expired")


if __name__ == "__main__": unittest.main()
