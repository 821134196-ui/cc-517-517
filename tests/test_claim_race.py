"""接单竞争：并发/重复接单只有一名现场负责人。"""
import threading
from datetime import datetime, timedelta

from app import db
from app.services import CampService
from tests.base import CampTestCase


class TestClaimRace(CampTestCase):
    def test_sequential_double_claim_only_one_wins(self):
        t = datetime(2026, 10, 1, 23, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="吵", when=t)
        inc = r["incident_id"]
        self.assertTrue(self.svc.claim_incident(inc, 1, when=t + timedelta(minutes=1)))
        self.assertFalse(self.svc.claim_incident(inc, 2, when=t + timedelta(minutes=2)))

        row = self.conn.execute(
            "SELECT assigned_officer_id FROM incidents WHERE id=?", (inc,)).fetchone()
        self.assertEqual(row["assigned_officer_id"], 1)

    def test_concurrent_claims_single_winner(self):
        t = datetime(2026, 10, 1, 23, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="吵", when=t)
        inc = r["incident_id"]
        self.conn.commit()

        results = {}
        barrier = threading.Barrier(3)

        def worker(officer_id):
            conn = db.connect(self.db_path)
            svc = CampService(conn)
            barrier.wait()  # 尽量让 3 个线程同时抢
            try:
                results[officer_id] = svc.claim_incident(inc, officer_id, when=t)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in (1, 2, 3)]  # 两名值班员 + 一名巡查员
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        winners = [k for k, ok in results.items() if ok]
        self.assertEqual(len(winners), 1, f"应有且仅有一名负责人，实际：{winners}")
        row = self.conn.execute(
            "SELECT assigned_officer_id, status FROM incidents WHERE id=?",
            (inc,)).fetchone()
        self.assertEqual(row["assigned_officer_id"], winners[0])
        self.assertEqual(row["status"], "dispatched")

    def test_non_officer_cannot_claim(self):
        t = datetime(2026, 10, 1, 23, 0)
        r = self.svc.submit_complaint(
            reporter_token="tok-a", reported_site="A-02", content="吵", when=t)
        # staff id 4 在夹具里是 supervisor；接单不限制角色以外的 active 员工，
        # 但停用员工不能接单
        self.conn.execute("UPDATE staff SET active=0 WHERE id=2")
        self.conn.commit()
        with self.assertRaises(Exception):
            self.svc.claim_incident(r["incident_id"], 2)
