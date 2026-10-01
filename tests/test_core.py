"""核心规则与服务测试：匿名隔离、事件合并、安静时段边界、接单竞争等。"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Database
from app.rules import is_quiet_hours
from app.seed import build_camp
from app.services import CampsiteService, OFFICERS, SUPERVISOR, ServiceError


class QuietHoursBoundaryTests(unittest.TestCase):
    def test_boundaries(self):
        # [22:00, 07:00) 半开区间：整点卡点
        self.assertFalse(is_quiet_hours("2026-10-01 21:59:59"))
        self.assertTrue(is_quiet_hours("2026-10-01 22:00:00"))
        self.assertTrue(is_quiet_hours("2026-10-01 23:30:00"))
        self.assertTrue(is_quiet_hours("2026-10-02 00:00:00"))
        self.assertTrue(is_quiet_hours("2026-10-02 06:59:59"))
        self.assertFalse(is_quiet_hours("2026-10-02 07:00:00"))
        self.assertFalse(is_quiet_hours("2026-10-02 12:00:00"))

    def test_evening_before_start_is_day(self):
        self.assertFalse(is_quiet_hours("2026-10-01 20:45:00"))


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))
        build_camp(self.db)
        self.svc = CampsiteService(self.db, os.path.join(self.tmp.name, "photos"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class MergeTests(ServiceTestBase):
    def test_repeated_complaints_merge_into_one_event(self):
        r1 = self.svc.file_complaint(6, 5, 5, "很吵", True, "2026-10-01 22:10:00")
        r2 = self.svc.file_complaint(6, 6, 7, "还在吵", False, "2026-10-01 22:20:00")
        r3 = self.svc.file_complaint(6, 5, 5, "没好转", True, "2026-10-01 22:31:00")
        self.assertEqual(r1["event_id"], r2["event_id"])
        self.assertEqual(r2["event_id"], r3["event_id"])
        self.assertFalse(r1["merged"])
        self.assertTrue(r2["merged"])
        chain = self.db.query("SELECT * FROM complaints WHERE event_id=?", (r1["event_id"],))
        self.assertEqual(len(chain), 3)

    def test_complaints_outside_window_create_new_event(self):
        r1 = self.svc.file_complaint(6, 5, 5, "吵", True, "2026-10-01 22:10:00")
        r2 = self.svc.file_complaint(6, 6, 7, "又吵", False, "2026-10-01 23:00:00")
        self.assertNotEqual(r1["event_id"], r2["event_id"])

    def test_different_sites_never_merge(self):
        r1 = self.svc.file_complaint(6, 5, 5, "吵", True, "2026-10-01 22:10:00")
        r2 = self.svc.file_complaint(7, 6, 6, "吵", True, "2026-10-01 22:11:00")
        self.assertNotEqual(r1["event_id"], r2["event_id"])


class AnonymityTests(ServiceTestBase):
    def test_anonymous_identity_hidden_in_public_view(self):
        r = self.svc.file_complaint(6, 5, 5, "匿名反映音响过大", True,
                                    "2026-10-01 22:10:00")
        detail = self.svc.event_detail(r["event_id"])
        c = detail["complaints"][0]
        self.assertTrue(c["anonymous"])
        self.assertNotIn("王芳", c["complainant_name"])
        self.assertIsNone(c["complainant_phone"])
        self.assertIsNone(c["site_id"])
        # 被投诉方视图序列化后也不允许出现投诉人姓名/电话
        blob = repr(detail)
        self.assertNotIn("王芳", blob)
        self.assertNotIn("13900000005", blob)

    def test_named_complaint_shows_identity_but_not_internal_id(self):
        r = self.svc.file_complaint(6, 6, 7, "实名反映", False,
                                    "2026-10-01 22:10:00")
        c = self.svc.event_detail(r["event_id"])["complaints"][0]
        self.assertEqual(c["complainant_name"], "陈晨")
        self.assertEqual(c["complainant_phone"], "13900000006")

    def test_anonymous_without_registered_guest_accepted(self):
        # 完全不登记投诉人/营位的匿名投诉也应受理
        r = self.svc.file_complaint(6, None, None, "匿名反映", True,
                                    "2026-10-01 22:10:00")
        cid = self.db.one("SELECT complainant_id, site_id FROM complaints "
                          "WHERE event_id=?", (r["event_id"],))
        self.assertIsNone(cid["complainant_id"])
        self.assertIsNone(cid["site_id"])

    def test_raw_chain_kept_for_internal_records(self):        # 投诉链仍在数据库中可追溯（内部用途），仅面向被投诉方视图脱敏
        r = self.svc.file_complaint(6, 5, 5, "匿名", True, "2026-10-01 22:10:00")
        raw = self.db.one("SELECT complainant_id FROM complaints WHERE event_id=?",
                          (r["event_id"],))
        self.assertEqual(raw["complainant_id"], 5)

    def test_messages_never_leak_complainant(self):
        r = self.svc.file_complaint(6, 5, 5, "匿名投诉", True, "2026-10-01 22:10:00")
        self.svc.claim_event(r["event_id"], OFFICERS[0])
        msgs = self.db.query("SELECT body FROM messages_log")
        for m in msgs:
            self.assertNotIn("王芳", m["body"])
            self.assertNotIn("13900000005", m["body"])


class ClaimRaceTests(ServiceTestBase):
    def test_concurrent_claims_only_one_winner(self):
        r = self.svc.file_complaint(6, 5, 5, "深夜喧哗", True, "2026-10-01 22:10:00")
        eid = r["event_id"]
        results = []
        barrier = threading.Barrier(2)

        def claim(officer):
            barrier.wait()
            results.append(self.svc.claim_event(eid, officer))

        t1 = threading.Thread(target=claim, args=(OFFICERS[0],))
        t2 = threading.Thread(target=claim, args=(OFFICERS[1],))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [x for x in results if x["won"]]
        losers = [x for x in results if not x["won"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        ev = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
        self.assertEqual(ev["status"], "assigned")
        self.assertEqual(ev["assigned_to"], winners[0]["assigned_to"])
        # 接单日志只允许一条成功记录
        logs = self.db.query("SELECT * FROM assignments_log WHERE event_id=?", (eid,))
        self.assertEqual(len(logs), 1)
        self.assertIn("已由", losers[0]["reason"])

    def test_stress_claims_many_threads_one_winner(self):
        r = self.svc.file_complaint(6, 5, 5, "深夜喧哗", True, "2026-10-01 22:10:00")
        eid = r["event_id"]
        winners = []
        barrier = threading.Barrier(8)

        def claim(i):
            officer = {"name": f"巡查员-{i}", "phone": f"139{i:09d}"}
            barrier.wait()
            if self.svc.claim_event(eid, officer)["won"]:
                winners.append(officer["name"])

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM assignments_log WHERE event_id=?",
                        (eid,))["n"],
            1)

    def test_cannot_record_without_being_assigned(self):
        r = self.svc.file_complaint(6, 5, 5, "吵", True, "2026-10-01 22:10:00")
        self.svc.claim_event(r["event_id"], OFFICERS[0])
        with self.assertRaises(ServiceError):
            self.svc.record_visit(r["event_id"], OFFICERS[1], reading_db=40.0,
                                  arrive_at="2026-10-01 22:15:00",
                                  outcome="测试", reading_at="2026-10-01 22:15:00")


class DispositionTests(ServiceTestBase):
    def _claimed_event(self, site=3, ts="2026-10-01 23:02:00"):
        r = self.svc.file_complaint(site, 1, 1, "喧哗", True, ts)
        self.svc.claim_event(r["event_id"], OFFICERS[0])
        return r["event_id"]

    def test_persistent_over_limit_escalates(self):
        eid = self._claimed_event()
        a = self.svc.record_visit(eid, OFFICERS[0], reading_db=47.2,
                                  arrive_at="2026-10-01 23:08:00",
                                  outcome="劝导", reading_at="2026-10-01 23:09:00")
        self.assertTrue(a["over"])
        self.assertFalse(a["persistent"])
        b = self.svc.record_visit(eid, OFFICERS[0], reading_db=48.6,
                                  arrive_at="2026-10-01 23:20:00",
                                  outcome="警告", reading_at="2026-10-01 23:20:30")
        self.assertTrue(b["persistent"])
        ev = self.db.one("SELECT status, disposition FROM events WHERE id=?", (eid,))
        self.assertEqual(ev["status"], "resolved")
        self.assertEqual(ev["disposition"], "escalated_persistent")

    def test_day_limit_is_55_night_limit_45(self):
        eid = self._claimed_event(ts="2026-10-01 15:00:00")
        res = self.svc.record_visit(eid, OFFICERS[0], reading_db=50.0,
                                    arrive_at="2026-10-01 15:05:00",
                                    outcome="正常交谈", reading_at="2026-10-01 15:05:00")
        self.assertFalse(res["quiet"])
        self.assertFalse(res["over"])

    def test_device_false_alarm_closes_event_and_ignores_reading(self):
        eid = self._claimed_event(site=2, ts="2026-10-01 23:40:00")
        res = self.svc.record_visit(eid, OFFICERS[0], reading_db=None,
                                    arrive_at="2026-10-01 23:47:00",
                                    outcome="无噪声", meter_fault=True,
                                    reading_at="2026-10-01 23:48:00")
        self.assertFalse(res["device_ok"])
        self.assertEqual(res["closed_as"], "false_alarm")
        ev = self.db.one("SELECT status, disposition FROM events WHERE id=?", (eid,))
        self.assertEqual(ev["status"], "false_alarm")
        self.assertIsNone(res["photo"])  # 误报不保存读数照片

    def test_reading_photo_saved_locally(self):
        eid = self._claimed_event()
        res = self.svc.record_visit(eid, OFFICERS[0], reading_db=42.0,
                                    arrive_at="2026-10-01 23:08:00",
                                    outcome="提醒", reading_at="2026-10-01 23:09:00")
        path = os.path.join(self.tmp.name, "photos", res["photo"])
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")


class AppealReviewTests(ServiceTestBase):
    def _resolved_event(self):
        r = self.svc.file_complaint(3, 1, 1, "喧哗", True, "2026-10-01 23:02:00")
        eid = r["event_id"]
        self.svc.claim_event(eid, OFFICERS[0])
        self.svc.record_visit(eid, OFFICERS[0], 47.2, "2026-10-01 23:08:00",
                              "劝导", reading_at="2026-10-01 23:09:00")
        self.svc.record_visit(eid, OFFICERS[0], 48.6, "2026-10-01 23:20:00",
                              "警告", reading_at="2026-10-01 23:20:30")
        return eid

    def test_officer_cannot_close_appealed_record(self):
        eid = self._resolved_event()
        self.svc.appeal(eid, "读数有异议")
        officer = {"name": "巡查员·阿岭", "role": "officer"}
        with self.assertRaises(ServiceError):
            self.svc.review(eid, officer, "upheld", "维持")
        ev = self.db.one("SELECT status FROM events WHERE id=?", (eid,))
        self.assertEqual(ev["status"], "appealed")

    def test_supervisor_review_closes_case(self):
        eid = self._resolved_event()
        self.svc.appeal(eid, "读数有异议")
        sup = {"name": SUPERVISOR["name"], "role": "supervisor"}
        self.svc.review(eid, sup, "upheld", "两次读数证据充分，维持升级处置")
        ev = self.db.one("SELECT status, reviewer FROM events WHERE id=?", (eid,))
        self.assertEqual(ev["status"], "closed")
        self.assertEqual(ev["reviewer"], SUPERVISOR["name"])

    def test_review_downgrade_and_reopen(self):
        eid = self._resolved_event()
        self.svc.appeal(eid, "异议")
        sup = {"name": SUPERVISOR["name"], "role": "supervisor"}
        self.svc.review(eid, sup, "downgraded_false_alarm", "发现传感器故障")
        self.assertEqual(self.db.one("SELECT status FROM events WHERE id=?", (eid,))["status"],
                         "false_alarm")

    def test_cannot_appeal_unresolved_event(self):
        r = self.svc.file_complaint(3, 1, 1, "吵", True, "2026-10-01 23:02:00")
        with self.assertRaises(ServiceError):
            self.svc.appeal(r["event_id"], "太早了")


class WithdrawExemptionMoveTests(ServiceTestBase):
    def test_withdraw_last_complaint_closes_event(self):
        r = self.svc.file_complaint(1, 2, 2, "狗叫", False, "2026-10-02 00:15:00")
        cid = self.db.one("SELECT id FROM complaints WHERE event_id=?", (r["event_id"],))["id"]
        self.svc.withdraw_complaint(cid, "误会")
        ev = self.db.one("SELECT status FROM events WHERE id=?", (r["event_id"],))
        self.assertEqual(ev["status"], "withdrawn")

    def test_withdraw_one_of_multiple_keeps_event_open(self):
        r = self.svc.file_complaint(6, 5, 5, "吵", True, "2026-10-01 22:10:00")
        self.svc.file_complaint(6, 6, 7, "同吵", False, "2026-10-01 22:12:00")
        cid = self.db.one(
            "SELECT id FROM complaints WHERE event_id=? AND anonymous=1", (r["event_id"],))["id"]
        self.svc.withdraw_complaint(cid)
        ev = self.db.one("SELECT status FROM events WHERE id=?", (r["event_id"],))
        self.assertEqual(ev["status"], "open")

    def test_public_event_exemption(self):
        self.db.execute(
            "INSERT INTO exemptions(title, site_id, start_at, end_at, note) "
            "VALUES ('篝火晚会', NULL, '20:00', '21:30', '公告豁免')")
        r = self.svc.file_complaint(4, None, 7, "音乐响", True, "2026-10-01 20:45:00")
        self.assertTrue(r["exempt"])
        ev = self.db.one("SELECT status, disposition, exempt FROM events WHERE id=?",
                         (r["event_id"],))
        self.assertEqual(ev["status"], "closed")
        self.assertEqual(ev["disposition"], "public_exempt")
        self.assertEqual(ev["exempt"], 1)

    def test_exemption_does_not_apply_after_window(self):
        self.db.execute(
            "INSERT INTO exemptions(title, site_id, start_at, end_at, note) "
            "VALUES ('篝火晚会', NULL, '20:00', '21:30', '')")
        r = self.svc.file_complaint(4, None, 7, "音乐响", True, "2026-10-01 21:30:00")
        self.assertFalse(r["exempt"])

    def test_move_suggestions_exclude_neighbors_and_occupied(self):
        # 噪声源为 6 号营位：相邻 2,3,5,7；8 号空闲、距离 2 跳
        sug = self.svc.move_suggestions(6)
        ids = {s["site"]["id"] for s in sug}
        self.assertNotIn(6, ids)
        self.assertNotIn(2, ids)   # 相邻不推荐
        self.assertNotIn(5, ids)
        for s in sug:
            self.assertGreaterEqual(s["hops"], 2)
        self.assertIn(8, ids)

    def test_relocate_guest_blocks_occupied_site(self):
        with self.assertRaises(ServiceError):
            self.svc.relocate_guest(1, 3)  # 3 号住着周磊
        self.svc.relocate_guest(1, 8)
        self.assertEqual(self.db.one("SELECT site_id FROM guests WHERE id=1")["site_id"], 8)

    def test_timeline_order_occur_arrive_review(self):
        r = self.svc.file_complaint(3, 1, 1, "喧哗", True, "2026-10-01 23:02:00")
        eid = r["event_id"]
        self.svc.claim_event(eid, OFFICERS[0])
        self.svc.record_visit(eid, OFFICERS[0], 47.2, "2026-10-01 23:08:00",
                              "劝导", reading_at="2026-10-01 23:09:00")
        self.svc.record_visit(eid, OFFICERS[0], 48.6, "2026-10-01 23:20:00",
                              "警告", reading_at="2026-10-01 23:20:30")
        self.svc.appeal(eid, "异议")
        self.svc.review(eid, {"name": SUPERVISOR["name"], "role": "supervisor"},
                        "upheld", "维持")
        phases = [t["phase"] for t in self.svc.event_detail(eid)["timeline"]]
        self.assertEqual(phases[0], "发生")
        self.assertIn("到场", phases)
        self.assertEqual(phases[-1], "复核")


if __name__ == "__main__":
    unittest.main(verbosity=2)
