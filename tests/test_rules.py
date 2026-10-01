"""规则流：豁免、撤回、误报（设备故障）、持续超限升级、申诉与主管复核、换位。"""
from datetime import datetime, timedelta
from pathlib import Path

from app import db, timeutil
from app.services import ServiceError
from tests.base import CampTestCase


def t(hour=23, day=1, minute=0):
    return datetime(2026, 10, day, hour, minute)


class TestExemption(CampTestCase):
    def test_complaint_during_global_event_is_exempt(self):
        self.conn.execute(
            """INSERT INTO exemptions (scope,title,start_ts,end_ts,created_by)
               VALUES ('global','篝火晚会','2026-10-01 20:00:00','2026-10-01 21:30:00',4)""")
        self.conn.commit()
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="晚会音乐",
            when=t(20, 1, 20))
        self.assertTrue(r["exempt"])
        inc = self.conn.execute("SELECT * FROM incidents WHERE id=?",
                                (r["incident_id"],)).fetchone()
        self.assertEqual(inc["status"], "dismissed")
        self.assertIsNotNone(inc["exemption_id"])

    def test_exempt_event_does_not_merge_later_normal_complaint(self):
        self.conn.execute(
            """INSERT INTO exemptions (scope,title,start_ts,end_ts,created_by)
               VALUES ('global','晚会','2026-10-01 20:00:00','2026-10-01 21:30:00',4)""")
        self.conn.commit()
        r1 = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="晚会",
            when=t(20, 1, 10))
        r2 = self.svc.submit_complaint(
            reporter_token="tok-b", reported_site="A-02", content="深夜还在吵",
            when=t(23))
        self.assertNotEqual(r1["incident_id"], r2["incident_id"])


class TestWithdraw(CampTestCase):
    def test_single_complaint_withdraw_also_withdraws_incident(self):
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="吵", when=t())
        self.svc.withdraw_complaint(r["complaint_id"], "tok-a", "误会", when=t(23, 1, 5))
        inc = self.conn.execute("SELECT status FROM incidents WHERE id=?",
                                (r["incident_id"],)).fetchone()
        self.assertEqual(inc["status"], "withdrawn")

    def test_partial_withdraw_keeps_incident_open(self):
        r1 = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="1", when=t())
        self.svc.submit_complaint(
            reporter_token="tok-b", reported_site="A-02", content="2",
            when=t(23, 1, 5))
        self.svc.withdraw_complaint(r1["complaint_id"], "tok-a", when=t(23, 1, 6))
        inc = self.conn.execute("SELECT status FROM incidents WHERE id=?",
                                (r1["incident_id"],)).fetchone()
        self.assertEqual(inc["status"], "open")

    def test_cannot_withdraw_after_onsite_into_withdrawn(self):
        # 已派单到场后即便全部撤回，事件也不撤销，保留处置记录
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="吵", when=t())
        inc = r["incident_id"]
        self.svc.claim_incident(inc, 1, when=t(23, 1, 2))
        self.svc.mark_arrived(inc, 1, when=t(23, 1, 5))
        self.svc.add_reading(inc, base=50, seed=1, when=t(23, 1, 6))
        self.svc.complete_onsite(
            inc, 1, communication="已提醒", result="reminded", when=t(23, 1, 8))
        self.svc.withdraw_complaint(r["complaint_id"], "tok-a", when=t(23, 1, 9))
        inc_row = self.conn.execute("SELECT status FROM incidents WHERE id=?",
                                    (inc,)).fetchone()
        self.assertEqual(inc_row["status"], "pending_review")


class TestMeterAndFalseAlarm(CampTestCase):
    def test_faulty_reading_is_null_and_photo_saved(self):
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="嗡嗡", when=t())
        rd = self.svc.add_reading(r["incident_id"], fault=True, when=t(23, 1, 5))
        self.assertFalse(rd["meter_ok"])
        self.assertTrue(rd["photo"].endswith(".png"))
        # 照片保存在本次运行配置的本地目录
        saved = self.photo_dir / Path(rd["photo"]).name
        self.assertTrue(saved.exists())
        self.assertTrue(saved.read_bytes().startswith(b"\x89PNG"))
        j = self.svc.judge_overlimit(r["incident_id"])
        self.assertFalse(j["decidable"])

    def test_persistent_overlimit_needs_two_samples(self):
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="持续", when=t(22))
        inc = r["incident_id"]
        # 夜间限值 45：一次 50 → 单次超限但不“持续”
        self.svc.add_reading(inc, base=50, variance=0, seed=100, when=t(22, 1, 2))
        j1 = self.svc.judge_overlimit(inc)
        self.assertTrue(j1["overlimit"])
        self.assertFalse(j1["persistent"])
        self.svc.add_reading(inc, base=52, variance=0, seed=101, when=t(22, 1, 4))
        j2 = self.svc.judge_overlimit(inc)
        self.assertTrue(j2["persistent"])


class TestReviewFlow(CampTestCase):
    def _persistent_incident(self):
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="唱K", when=t(23))
        inc = r["incident_id"]
        self.svc.claim_incident(inc, 1, when=t(23, 1, 2))
        self.svc.mark_arrived(inc, 1, when=t(23, 1, 7))
        self.svc.add_reading(inc, base=52, seed=1, when=t(23, 1, 8))
        self.svc.add_reading(inc, base=53, seed=2, when=t(23, 1, 10))
        rec = self.svc.complete_onsite(
            inc, 3, communication="关小后仍反复", result="persistent",
            when=t(23, 1, 12))
        return inc, rec

    def test_patroller_cannot_close_appealed_record(self):
        inc, rec = self._persistent_incident()
        self.svc.appeal(rec, "tok-z", "不是我们的声音", when=t(23, 1, 20))
        with self.assertRaises(ServiceError):
            self.svc.patrol_try_close(rec, 3)
        # 直接改库也会被触发器拒绝
        with self.assertRaises(Exception):
            with self.conn:
                self.conn.execute(
                    "UPDATE onsite_records SET status='closed' WHERE id=?", (rec,))

    def test_supervisor_review_closes_case(self):
        inc, rec = self._persistent_incident()
        self.svc.appeal(rec, "tok-z", "申诉", when=t(23, 1, 20))
        out = self.svc.review_close(
            record_id=rec, supervisor_id=4, decision="rejected",
            note="读数充分，维持警告", when=t(23, 1, 40))
        self.assertTrue(out["closed"])
        self.assertEqual(out["final_status"], "closed")
        row = self.conn.execute("SELECT status FROM incidents WHERE id=?",
                                (inc,)).fetchone()
        self.assertEqual(row["status"], "closed")

    def test_reviewer_must_be_supervisor_and_not_self(self):
        inc, rec = self._persistent_incident()
        with self.assertRaises(ServiceError):
            self.svc.review_close(record_id=rec, supervisor_id=3,
                                  decision="rejected")  # 记录人本人
        with self.assertRaises(ServiceError):
            self.svc.review_close(record_id=rec, supervisor_id=1,
                                  decision="rejected")  # 值班员不是主管

    def test_approved_appeal_dismisses_incident(self):
        inc, rec = self._persistent_incident()
        self.svc.appeal(rec, "tok-z", "音箱来自营区外", when=t(23, 1, 20))
        out = self.svc.review_close(
            record_id=rec, supervisor_id=4, decision="approved",
            note="声源在营区外，撤销", when=t(23, 1, 40))
        self.assertEqual(out["final_status"], "dismissed")

    def test_persistent_escalation_creates_relocation(self):
        inc, rec = self._persistent_incident()
        inc_row = self.conn.execute("SELECT escalation_level FROM incidents WHERE id=?",
                                    (inc,)).fetchone()
        self.assertGreaterEqual(inc_row["escalation_level"], 3)
        sug = self.conn.execute(
            "SELECT * FROM relocation_suggestions WHERE incident_id=?",
            (inc,)).fetchone()
        self.assertIsNotNone(sug, "持续超限 3 级应自动生成换位建议")
        self.assertNotIn(sug["to_site"], {"A-02"} |
                         {n["code"] for n in self.svc.neighbors("A-02")})

    def test_accept_relocation_moves_guest_and_keeps_history(self):
        inc, rec = self._persistent_incident()
        sug = self.conn.execute(
            "SELECT id FROM relocation_suggestions WHERE incident_id=?",
            (inc,)).fetchone()
        self.svc.respond_relocation(sug["id"], True, when=t(23, 1, 30))
        occ = self.svc.occupancy_at(sug_target(self.conn, sug["id"]),
                                    t(23, 1, 31))
        self.assertIsNotNone(occ)
        self.assertEqual(occ["anon_token"], "tok-z")
        # 旧营位历史入住仍在
        old = self.conn.execute(
            "SELECT COUNT(*) c FROM occupancies WHERE site_code='A-02'").fetchone()["c"]
        self.assertGreaterEqual(old, 1)

    def test_false_alarm_flow(self):
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="异响", when=t())
        inc = r["incident_id"]
        self.svc.claim_incident(inc, 1, when=t(23, 1, 2))
        self.svc.mark_arrived(inc, 1, when=t(23, 1, 6))
        self.svc.add_reading(inc, fault=True, when=t(23, 1, 7))
        rec = self.svc.complete_onsite(
            inc, 3, communication="设备故障，复测安静", result="false_alarm",
            when=t(23, 1, 10))
        row = self.conn.execute("SELECT status FROM incidents WHERE id=?",
                                (inc,)).fetchone()
        self.assertEqual(row["status"], "dismissed")
        recrow = self.conn.execute("SELECT status FROM onsite_records WHERE id=?",
                                   (rec,)).fetchone()
        self.assertEqual(recrow["status"], "closed")


def sug_target(conn, sid):
    return conn.execute("SELECT to_site FROM relocation_suggestions WHERE id=?",
                        (sid,)).fetchone()["to_site"]
