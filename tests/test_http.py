"""HTTP 端到端集成测试：真实线程服务器上的匿名隔离、并发接单、角色复核等。"""
import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.seed import build_camp
from app.server import App, make_handler

BASE = None  # 测试启动时赋值


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


def make_opener(role=None, no_redirect=True):
    cj = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(cj)]
    if no_redirect:
        handlers.append(_NoRedirect())
    op = urllib.request.build_opener(*handlers)
    if role:
        op.addheaders.append(("Cookie", f"role={role}"))
    return op


def post(path, data, op=None, want_json=False):
    body = urllib.parse.urlencode(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if want_json:
        headers["X-Requested-With"] = "fetch"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method="POST")
    try:
        resp = (op or make_opener()).open(req)
        raw = resp.read().decode()
        return resp.status, raw, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Location", "")


def get(path, op=None):
    resp = (op or urllib.request).urlopen(BASE + path)
    return resp.status, resp.read().decode()


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "http.db")
        cls.photo_dir = os.path.join(cls.tmp.name, "photos")
        global BASE
        from app.server import App as _App
        cls.app = _App(cls.db_path, cls.photo_dir)
        build_camp(cls.app.db)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.httpd.server_address[1]
        BASE = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.app.db.close()
        cls.tmp.cleanup()

    def _file(self, site=6, detail="很吵", anon=True, cid=""):
        return post("/complaints", {
            "reported_site_id": site, "complainant_id": cid,
            "detail": detail, "anonymous": "1" if anon else ""})

    def test_01_pages_render(self):
        for p in ("/", "/events", "/review", "/stats"):
            status, _ = get(p)
            self.assertEqual(status, 200)

    def test_02_complaint_created_and_anonymous_isolated(self):
        status, _, loc = self._file(site=6, detail="音响太大", anon=True, cid=5)
        self.assertEqual(status, 303)
        eid = loc.rstrip("/").split("/events/")[1].split("?")[0]
        status, html = get(f"/events/{eid}")
        self.assertEqual(status, 200)
        # 投诉人是 guest id=5 王芳/13900000005，页面不得暴露
        self.assertNotIn("王芳", html)
        self.assertNotIn("13900000005", html)
        self.assertIn("匿名投诉人", html)

    def test_03_merge_over_http(self):
        _, _, loc1 = self._file(site=3, detail="第一次", anon=False, cid=1)
        e1 = loc1.split("/events/")[1].split("?")[0]
        _, _, loc2 = self._file(site=3, detail="窗口内第二次", anon=True, cid=2)
        e2 = loc2.split("/events/")[1].split("?")[0]
        self.assertEqual(e1, e2)

    def test_04_concurrent_claims_single_winner(self):
        _, _, loc = self._file(site=7, detail="深夜打牌", anon=True)
        eid = loc.split("/events/")[1].split("?")[0]
        results, barrier = [], threading.Barrier(4)

        def claim(name):
            barrier.wait()
            _, raw, _ = post(f"/events/{eid}/claim", {"officer": name}, want_json=True)
            results.append(json.loads(raw))

        ts = [threading.Thread(target=claim, args=(n,))
              for n in ("巡查员·阿岭", "巡查员·阿杉", "巡查员·阿野", "巡查员·阿溪")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sum(1 for r in results if r["won"]), 1)
        n = self.app.db.one(
            "SELECT COUNT(*) c FROM assignments_log WHERE event_id=?", (eid,))["c"]
        self.assertEqual(n, 1)

    def test_05_officer_cannot_review_supervisor_can(self):
        _, _, loc = self._file(site=2, detail="喧哗待复核", anon=True)
        eid = loc.split("/events/")[1].split("?")[0]
        # 接单 + 两次夜间超限读数
        post(f"/events/{eid}/claim", {"officer": "巡查员·阿岭"})
        post(f"/events/{eid}/records", {
            "officer": "巡查员·阿岭", "reading_db": "47.0",
            "arrive_at": "2026-10-01T23:08", "reading_at": "2026-10-01T23:09",
            "outcome": "劝导", "meter_fault": "0"})
        post(f"/events/{eid}/records", {
            "officer": "巡查员·阿岭", "reading_db": "48.0",
            "arrive_at": "2026-10-01T23:19", "reading_at": "2026-10-01T23:20",
            "outcome": "警告", "meter_fault": "0"})
        post(f"/events/{eid}/appeal", {"note": "有异议"})
        self.assertEqual(self.app.db.one(
            "SELECT status FROM events WHERE id=?", (eid,))["status"], "appealed")

        # 巡查员复核被拒
        op_officer = make_opener("officer")
        post(f"/events/{eid}/review", {"result": "upheld", "note": "自己结案"},
             op=op_officer)
        self.assertEqual(self.app.db.one(
            "SELECT status FROM events WHERE id=?", (eid,))["status"], "appealed")

        # 主管复核成功结案
        op_sup = make_opener("supervisor")
        post(f"/events/{eid}/review", {"result": "upheld", "note": "维持"}, op=op_sup)
        row = self.app.db.one(
            "SELECT status, reviewer FROM events WHERE id=?", (eid,))
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["reviewer"], "值班主管·陆队")

    def test_06_device_fault_over_http(self):
        _, _, loc = self._file(site=4, detail="疑似蜂鸣", anon=False, cid=3)
        eid = loc.split("/events/")[1].split("?")[0]
        post(f"/events/{eid}/claim", {"officer": "巡查员·阿杉"})
        status, _, _ = post(f"/events/{eid}/records", {
            "officer": "巡查员·阿杉", "reading_db": "",
            "arrive_at": "2026-10-01T23:47", "reading_at": "2026-10-01T23:48",
            "outcome": "无噪声", "meter_fault": "1"})
        self.assertEqual(self.app.db.one(
            "SELECT status FROM events WHERE id=?", (eid,))["status"], "false_alarm")
        # 误报不产生照片
        n = self.app.db.one(
            "SELECT COUNT(*) c FROM site_records WHERE event_id=? AND photo_path IS NOT NULL",
            (eid,))["c"]
        self.assertEqual(n, 0)

    def test_07_timeline_order_on_page(self):
        _, _, loc = self._file(site=1, detail="时间线", anon=True)
        eid = loc.split("/events/")[1].split("?")[0]
        post(f"/events/{eid}/claim", {"officer": "巡查员·阿岭"})
        post(f"/events/{eid}/records", {
            "officer": "巡查员·阿岭", "reading_db": "40.0",
            "arrive_at": "2026-10-01T22:20", "reading_at": "2026-10-01T22:21",
            "outcome": "提醒", "meter_fault": "0"})
        _, html = get(f"/events/{eid}")
        i_occur = html.find("发生")
        i_arrive = html.find("到场")
        self.assertGreater(i_arrive, i_occur)


if __name__ == "__main__":
    unittest.main(verbosity=2)
