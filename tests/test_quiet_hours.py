"""安静时段边界：22:00–07:00 跨午夜，左闭右开，限值按时段切换。"""
from datetime import datetime

from app import timeutil
from tests.base import CampTestCase


class TestQuietHours(CampTestCase):
    def test_boundary_start_inclusive(self):
        site = self.svc.site("A-02")
        self.assertTrue(timeutil.in_quiet(
            datetime(2026, 10, 1, 22, 0, 0), site["quiet_start"], site["quiet_end"]))

    def test_boundary_end_exclusive(self):
        site = self.svc.site("A-02")
        # 07:00:00 整点已离开安静时段
        self.assertFalse(timeutil.in_quiet(
            datetime(2026, 10, 2, 7, 0, 0), site["quiet_start"], site["quiet_end"]))
        # 06:59:59 仍在安静时段
        self.assertTrue(timeutil.in_quiet(
            datetime(2026, 10, 2, 6, 59, 59), site["quiet_start"], site["quiet_end"]))

    def test_midnight_and_daytime(self):
        site = self.svc.site("A-02")
        self.assertTrue(timeutil.in_quiet(
            datetime(2026, 10, 2, 0, 0, 0), site["quiet_start"], site["quiet_end"]))
        self.assertTrue(timeutil.in_quiet(
            datetime(2026, 10, 2, 3, 30, 0), site["quiet_start"], site["quiet_end"]))
        self.assertFalse(timeutil.in_quiet(
            datetime(2026, 10, 1, 12, 0, 0), site["quiet_start"], site["quiet_end"]))
        self.assertFalse(timeutil.in_quiet(
            datetime(2026, 10, 1, 21, 59, 59), site["quiet_start"], site["quiet_end"]))

    def test_limit_switches_at_boundary(self):
        site = self.svc.site("A-02")
        night_limit, quiet = timeutil.applicable_limit(
            datetime(2026, 10, 1, 22, 0, 0), site)
        self.assertTrue(quiet)
        self.assertEqual(night_limit, 45)
        day_limit, quiet = timeutil.applicable_limit(
            datetime(2026, 10, 1, 7, 0, 0), site)
        self.assertFalse(quiet)
        self.assertEqual(day_limit, 55)

    def test_complaint_flags_quiet_on_incident(self):
        night = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="夜间",
            when=datetime(2026, 10, 1, 23, 30))
        day = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-03", content="白天",
            when=datetime(2026, 10, 1, 12, 0))
        qn = self.conn.execute("SELECT quiet FROM incidents WHERE id=?",
                               (night["incident_id"],)).fetchone()[0]
        qd = self.conn.execute("SELECT quiet FROM incidents WHERE id=?",
                               (day["incident_id"],)).fetchone()[0]
        self.assertEqual(qn, 1)
        self.assertEqual(qd, 0)
