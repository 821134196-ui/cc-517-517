"""匿名隔离：被投诉方视图不得出现投诉人姓名、营位、联系方式。"""
from datetime import datetime, timedelta

from tests.base import CampTestCase


class TestAnonymity(CampTestCase):
    def test_respondent_view_is_redacted(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02",
            content="音响太吵", anonymous=True, when=t)
        inc = r["incident_id"]

        staff = self.svc.incident_detail(inc, viewer="staff")
        respondent = self.svc.incident_detail(inc, viewer="respondent")

        # 内部视图可见
        names = [c["reporter"] for c in staff["complaints"]]
        self.assertIn("投诉人甲", names)
        self.assertTrue(any(c["contact"] for c in staff["complaints"]))

        # 被投诉方视图：身份字段全部脱敏
        for c in respondent["complaints"]:
            self.assertEqual(c["reporter"], "匿名住客")
            self.assertEqual(c["reporter_site"], "邻近营位")
            self.assertIsNone(c["contact"])

        blob = repr(respondent)
        for forbidden in ("投诉人甲", "13900001", "A-01"):
            self.assertNotIn(forbidden, blob,
                             f"被投诉方视图泄露了 {forbidden}")

    def test_messages_to_respondent_are_redacted(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02",
            content="音响太吵", anonymous=True, when=t)
        inc = r["incident_id"]
        self.svc.claim_incident(inc, 1, when=t + timedelta(minutes=2))
        msgs = self.svc.msg.log_for(inc)
        to_site = [m for m in msgs if m["to_site"] == "A-02"]
        self.assertTrue(to_site, "应向被投诉营位发送派单提醒")
        for m in to_site:
            self.assertTrue(m["redacted"])
            self.assertNotIn("投诉人甲", m["body"])
            self.assertNotIn("A-01", m["body"])

    def test_named_complaint_shows_identity_even_to_respondent(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02",
            content="我愿意实名", anonymous=False, when=t)
        v = self.svc.incident_detail(r["incident_id"], viewer="respondent")
        self.assertEqual(v["complaints"][0]["reporter"], "投诉人甲")
        # 实名也不向被投诉方暴露联系方式
        self.assertIsNone(v["complaints"][0]["contact"])

    def test_withdraw_requires_matching_token(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02",
            content="吵", anonymous=True, when=t)
        with self.assertRaises(Exception):
            self.svc.withdraw_complaint(r["complaint_id"], "tok-b", when=t)
