"""标准库 HTTP 服务：路由、表单处理、静态照片，零第三方依赖。"""
from __future__ import annotations

import json
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .database import Database
from .seed import seed_demo
from .services import CampsiteService, SUPERVISOR, ServiceError
from . import views

VALID_ROLES = {"officer", "supervisor", "guest"}


class App:
    def __init__(self, db_path: str = "data/camp.db", photo_dir: str = "data/photos"):
        self.db = Database(db_path)
        self.svc = CampsiteService(self.db, photo_dir)
        self.photo_dir = photo_dir


def make_handler(app: App):
    svc = app.svc

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静一点
            pass

        # ---------- 基础工具 ----------
        def _role(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            role = cookie.get("role")
            if role and role.value in VALID_ROLES:
                return role.value
            return "officer"

        def _send(self, content: str, status: int = 200, ctype: str = "text/html; charset=utf-8"):
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, obj, status: int = 200):
            self._send(json.dumps(obj, ensure_ascii=False), status,
                       "application/json; charset=utf-8")

        def _redirect(self, path: str):
            from urllib.parse import quote
            # Location 头只能是 latin-1：路径保留，查询值做百分号编码
            if "?" in path:
                base, query = path.split("?", 1)
                parts = []
                for kv in query.split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        parts.append(f"{k}={quote(v, safe='')}")
                    else:
                        parts.append(quote(kv, safe=""))
                path = base + "?" + "&".join(parts)
            self.send_response(303)
            self.send_header("Location", path)
            self.end_headers()

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _officer_by_name(self, name: str) -> dict:
            from .services import OFFICERS
            for o in OFFICERS:
                if o["name"] == name:
                    return o
            raise ServiceError("未知巡查员")

        def _wants_json(self) -> bool:
            return self.headers.get("X-Requested-With", "") == "fetch"

        # ---------- GET ----------
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            role = self._role()
            try:
                if p == "/":
                    site = int(q["site"][0]) if q.get("site") else None
                    self._send(views.render_map(
                        svc, site, q.get("ok", [None])[0], q.get("err", [None])[0]))
                elif p == "/events":
                    site = int(q["site"][0]) if q.get("site") else None
                    self._send(views.render_events(
                        svc, site, q.get("ok", [None])[0], q.get("err", [None])[0]))
                elif p == "/events/new":
                    site = int(q["site"][0]) if q.get("site") else None
                    self._send(views.render_events(
                        svc, site, q.get("ok", [None])[0], q.get("err", [None])[0]))
                elif p == "/review":
                    self._send(views.render_review_queue(
                        svc, role, q.get("ok", [None])[0], q.get("err", [None])[0]))
                elif p == "/stats":
                    self._send(views.render_stats(svc, role))
                elif (m := re.fullmatch(r"/events/(\d+)", p)):
                    self._send(views.render_event_detail(
                        svc, int(m.group(1)), role,
                        q.get("ok", [None])[0], q.get("err", [None])[0]))
                elif (m := re.fullmatch(r"/photos/(.+\.png)", p)):
                    self._photo(m.group(1))
                else:
                    self._send("未找到", 404, "text/plain; charset=utf-8")
            except ServiceError as e:
                self._send(f"错误：{e}", 400, "text/plain; charset=utf-8")

        def _photo(self, name: str):
            from pathlib import Path
            path = Path(app.photo_dir) / Path(name).name  # 防目录穿越
            if not path.exists():
                self._send("照片不存在", 404, "text/plain; charset=utf-8")
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------- POST ----------
        def do_POST(self):
            p = urlparse(self.path).path
            role = self._role()
            form = self._form()
            try:
                if p == "/role":
                    r = form.get("role", "officer")
                    if r not in VALID_ROLES:
                        r = "officer"
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"role={r}; Path=/; Max-Age=86400")
                    self.send_header("Location", self.headers.get("Referer", "/"))
                    self.end_headers()

                elif p == "/complaints":
                    rid = int(form["reported_site_id"])
                    cid_raw = form.get("complainant_id", "")
                    cid = int(cid_raw) if cid_raw else None
                    guest_site = None
                    if cid:
                        g = svc.db.one("SELECT site_id FROM guests WHERE id=?", (cid,))
                        guest_site = g["site_id"] if g else None
                    r = svc.file_complaint(rid, cid, guest_site,
                                           form.get("detail", ""),
                                           form.get("anonymous") == "1")
                    msg = ("投诉已提交" + ("，因处于公共活动豁免时段，受理后直接归档，不派单"
                           if r["exempt"] else
                           ("，已合并到现有事件 #" + str(r["event_id"])
                            if r["merged"] else "，已创建事件 #" + str(r["event_id"]))))
                    self._redirect(f"/events/{r['event_id']}?ok={msg}")

                elif (m := re.fullmatch(r"/complaints/(\d+)/withdraw", p)):
                    cid = int(m.group(1))
                    r = svc.withdraw_complaint(cid, form.get("note", ""))
                    self._redirect(f"/events/{r['event_id']}?ok=投诉已撤回")

                elif (m := re.fullmatch(r"/events/(\d+)/claim", p)):
                    eid = int(m.group(1))
                    officer = self._officer_by_name(form.get("officer", ""))
                    r = svc.claim_event(eid, officer)
                    if self._wants_json():
                        self._send_json(r)
                    else:
                        self._redirect(f"/events/{eid}")

                elif (m := re.fullmatch(r"/events/(\d+)/records", p)):
                    eid = int(m.group(1))
                    officer = self._officer_by_name(form.get("officer", ""))
                    reading_raw = form.get("reading_db", "").strip()
                    reading = float(reading_raw) if reading_raw else None
                    arrive = form["arrive_at"].replace("T", " ") + ":00"
                    reading_at = form["reading_at"].replace("T", " ") + ":00"
                    r = svc.record_visit(
                        eid, officer, reading, arrive, form.get("outcome", ""),
                        meter_fault=form.get("meter_fault") == "1",
                        reading_at=reading_at)
                    note = {
                        "escalated_persistent": "已记录：安静时段持续超限，自动升级处置",
                        "within_limit": "已记录：到场读数未超限，口头提醒后归档",
                        "false_alarm": "已记录：设备误报，读数不采信，事件按误报登记",
                    }.get(r["closed_as"] or "", "已记录现场情况（单次超限，继续观察）")
                    self._redirect(f"/events/{eid}?ok={note}")

                elif (m := re.fullmatch(r"/events/(\d+)/appeal", p)):
                    eid = int(m.group(1))
                    svc.appeal(eid, form.get("note", ""))
                    self._redirect(f"/events/{eid}?ok=申诉已提交，等待值班主管复核")

                elif (m := re.fullmatch(r"/events/(\d+)/review", p)):
                    eid = int(m.group(1))
                    if role != "supervisor":
                        raise ServiceError("只有值班主管可以复核结案")
                    svc.review(eid, {"name": SUPERVISOR["name"], "role": "supervisor"},
                               form.get("result", "upheld"), form.get("note", ""))
                    self._redirect(f"/review?ok=事件 #{eid} 复核完成")

                elif (m := re.fullmatch(r"/events/(\d+)/moves", p)):
                    eid = int(m.group(1))
                    svc.log_move_suggestion(eid, int(form["to_site"]), form.get("reason", ""))
                    self._redirect(f"/events/{eid}?ok=换位建议已记录")

                else:
                    self._send("未找到", 404, "text/plain; charset=utf-8")
            except ServiceError as e:
                if self._wants_json():
                    self._send_json({"error": str(e)}, 400)
                else:
                    referer = self.headers.get("Referer", "/events")
                    self._redirect(referer.split("?")[0] + f"?err={e}")
            except (ValueError, KeyError) as e:
                self._send(f"表单参数错误：{e}", 400, "text/plain; charset=utf-8")

    return Handler


def run(host: str = "127.0.0.1", port: int = 8000,
        db_path: str = "data/camp.db", photo_dir: str = "data/photos",
        seed: bool = True) -> None:
    app = App(db_path, photo_dir)
    if seed:
        seed_demo(app.svc)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"🏕️  营地噪声处置台已启动：http://{host}:{port}")
    print("    视角切换在右上角（巡查员 / 值班主管 / 住客）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
