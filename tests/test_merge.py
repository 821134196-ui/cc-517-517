"""事件合并：同营位活跃事件在合并窗口内的多次投诉并入同一事件。"""
from datetime import datetime, timedelta

from tests.base import CampTestCase


class TestMerge(CampTestCase):
    def test_repeated_complaints_merge_into_one_incident(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r1 = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02",
            content="第一次", when=t)
        r2 = self.svc.submit_complaint(
            reporter_token="tok-b", reported_site="A-02",
            content="第二次", when=t + timedelta(minutes=10))
        r3 = self.svc.submit_complaint(
            reporter_token="tok-f", reported_site="A-02",
            content="第三次", when=t + timedelta(minutes=40))

        self.assertTrue(r2["merged"])
        self.assertTrue(r3["merged"])
        self.assertEqual(r1["incident_id"], r2["incident_id"] == r3["incident_id"])

        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (r1["incident_id"],)).fetchone()
        self.assertEqual(inc["merged_count"], 3)

        cmps = self.conn.execute(
            "SELECT id, is_duplicate FROM complaints WHERE incident_id=? ORDER BY id",
            (r1["incident_id"],)).fetchall()
        self.assertEqual([c["is_duplicate"] for c in cmps], [0, 1, 1])

    def test_complaints_after_window_open_new_incident(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r1 = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02",
            content="早", when=t)
        r2 = self.svc.submit_complaint(
            reporter_token="tok-b", reported_site="A-02",
            content="超过 90 分钟窗口", when=t + timedelta(minutes=91))
        self.assertNotEqual(r1["incident_id"], r2["incident_id"])

    def test_complaints_on_different_sites_never_merge(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r1 = self.svc.submit_complaint(
            reporter_token="tok-b", reported_site="A-01", content="x", when=t)
        r2 = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-03", content="y", when=t)
        self.assertNotEqual(r1["incident_id"], r2["incident_id"])

    def test_closed_incident_does_not_absorb_new_complaint(self):
        t = datetime(2026, 10, 1, 23, 0, 0)
        r1 = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="x", when=t)
        self.conn.execute(
            "UPDATE incidents SET status='closed' WHERE id=?", (r1["incident_id"],))
        self.conn.commit()
        r2 = self.svc.submit_complaint(
            reporter_token="tok-b", reported_site="A-02", content="y",
            when=t + timedelta(minutes=5))
        self.assertFalse(r2["merged"])
