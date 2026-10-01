"""应用服务：事件受理与合并、匿名投诉、接单竞争、现场记录、申诉/复核、换位、统计。"""
from __future__ import annotations

from .database import Database, neighbors
from .devices import DecibelMeter, MessageService, save_reading_photo
from .rules import (
    active_exemption,
    db_limit_at,
    find_mergeable_event,
    is_persistent,
    is_quiet_hours,
    minutes_between,
    now_str,
    readings_over_limit,
    recommend_moves,
)

OFFICERS = [
    {"name": "巡查员·阿岭", "phone": "13800000001"},
    {"name": "巡查员·阿杉", "phone": "13800000002"},
    {"name": "巡查员·阿野", "phone": "13800000004"},
    {"name": "巡查员·阿溪", "phone": "13800000005"},
]
SUPERVISOR = {"name": "值班主管·陆队", "phone": "13800000003"}


class ServiceError(Exception):
    pass


class CampsiteService:
    def __init__(self, db: Database, photo_dir: str = "data/photos"):
        self.db = db
        self.photo_dir = photo_dir
        self.meter = DecibelMeter()
        self.messages = MessageService(db)

    # ---------------- 营位与影响范围 ----------------
    def impact_zone(self, site_id: int) -> dict:
        return {
            "site_id": site_id,
            "neighbors": neighbors(self.db, site_id),
        }

    def move_suggestions(self, reported_site_id: int) -> list[dict]:
        return recommend_moves(self.db, reported_site_id)

    # ---------------- 投诉受理（合并 + 匿名 + 豁免） ----------------
    def file_complaint(self, reported_site_id: int, complainant_id: int | None,
                       complainant_site_id: int | None, detail: str,
                       anonymous: bool, ts: str | None = None) -> dict:
        ts = ts or now_str()
        site = self.db.one("SELECT * FROM sites WHERE id = ?", (reported_site_id,))
        if site is None:
            raise ServiceError("被投诉营位不存在")
        quiet = is_quiet_hours(ts, site["quiet_start"], site["quiet_end"])

        # 公共活动豁免：受理但标记，不进入噪声处置派单
        exemption = active_exemption(self.db, reported_site_id, ts)

        merged = find_mergeable_event(self.db, reported_site_id, ts)
        if merged:
            event_id = merged["id"]
            merged_into = event_id
        else:
            cur = self.db.execute(
                "INSERT INTO events(reported_site_id, first_at, quiet, status, exempt) "
                "VALUES (?, ?, ?, 'open', ?)",
                (reported_site_id, ts, int(quiet), int(bool(exemption))),
            )
            event_id = cur.lastrowid
            merged_into = None

        self.db.execute(
            "INSERT INTO complaints(event_id, complainant_id, site_id, anonymous, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, complainant_id, complainant_site_id, int(anonymous), detail.strip(), ts),
        )

        if exemption and merged_into is None:
            # 豁免事件直接以 closed 归档，处置说明写明豁免来源；仍保留投诉链
            self.db.execute(
                "UPDATE events SET status='closed', disposition='public_exempt', "
                "resolution_note=?, resolved_at=?, reviewer=? WHERE id=?",
                (f"公共活动豁免：{exemption['title']}（{exemption['note']}）",
                 ts, "系统-豁免规则", event_id),
            )

        return {
            "event_id": event_id,
            "merged": merged_into is not None,
            "quiet": quiet,
            "exempt": bool(exemption),
            "exemption": exemption,
        }

    def withdraw_complaint(self, complaint_id: int, note: str = "") -> dict:
        """投诉撤回：撤回单条投诉；若事件下再无有效投诉，则事件关闭为 withdrawn。"""
        c = self.db.one("SELECT * FROM complaints WHERE id = ?", (complaint_id,))
        if c is None:
            raise ServiceError("投诉不存在")
        if c["withdrawn"]:
            raise ServiceError("该投诉已撤回")
        self.db.execute(
            "UPDATE complaints SET withdrawn=1, withdrawn_at=? WHERE id=?",
            (now_str(), complaint_id),
        )
        active = self.db.one(
            "SELECT COUNT(*) AS n FROM complaints WHERE event_id=? AND withdrawn=0",
            (c["event_id"],),
        )["n"]
        ev = self.db.one("SELECT status FROM events WHERE id=?", (c["event_id"],))
        if active == 0 and ev["status"] in ("open",):
            self.db.execute(
                "UPDATE events SET status='withdrawn', disposition='withdrawn', "
                "resolution_note=?, resolved_at=? WHERE id=?",
                (f"全部投诉人撤回（最后撤回 #{complaint_id}）。{note}".strip(),
                 now_str(), c["event_id"],),
            )
        return {"event_id": c["event_id"], "remaining": active}

    # ---------------- 接单（并发竞争，只一名负责人） ----------------
    def claim_event(self, event_id: int, officer: dict, ts: str | None = None) -> dict:
        """原子接单：BEGIN IMMEDIATE 取写锁 + 条件 UPDATE，保证只分配一名现场负责人。"""
        ts = ts or now_str()
        conn = self.db.begin()
        try:
            row = conn.execute(
                "SELECT id, status, assigned_to FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                self.db.rollback()
                raise ServiceError("事件不存在")
            if row["status"] != "open":
                # 已被接单（或已进入后续状态）——必须先结束事务释放写锁
                assigned_to = row["assigned_to"]
                self.db.rollback()
                return {
                    "won": False,
                    "event_id": event_id,
                    "assigned_to": assigned_to,
                    "reason": f"事件已由 {assigned_to or '他人'} 接单",
                }
            conn.execute(
                "UPDATE events SET status='assigned', assigned_to=?, assigned_at=? "
                "WHERE id=? AND status='open'",
                (officer["name"], ts, event_id),
            )
            conn.execute(
                "INSERT INTO assignments_log(event_id, officer, claimed_at) VALUES (?, ?, ?)",
                (event_id, officer["name"], ts),
            )
            self.db.commit()
        except Exception:
            # 仅当事务仍处于打开状态时回滚（rollback 分支之后的异常不再重复释放）
            try:
                self.db.rollback()
            except Exception:
                pass
            raise

        self.messages.notify_officer(officer["phone"], event_id,
                                     self._site_label(self.db.one(
                                         "SELECT s.label AS l FROM events e "
                                         "JOIN sites s ON s.id=e.reported_site_id WHERE e.id=?",
                                         (event_id,))["l"]))
        return {"won": True, "event_id": event_id, "assigned_to": officer["name"]}

    # ---------------- 现场记录（分贝、到场、误报、持续超限） ----------------
    def record_visit(self, event_id: int, officer: dict, reading_db: float | None,
                     arrive_at: str, outcome: str, duration_s: int | None = None,
                     meter_fault: bool = False, ambient: float = 40.0,
                     spike: float = 0.0, reading_at: str | None = None) -> dict:
        ev = self.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        if ev is None:
            raise ServiceError("事件不存在")
        if ev["assigned_to"] != officer["name"]:
            raise ServiceError("只有该事件的现场负责人才可提交记录")
        reading_at = reading_at or now_str()
        site = self.db.one(
            "SELECT s.quiet_start AS qs, s.quiet_end AS qe FROM sites s "
            "JOIN events e ON e.reported_site_id=s.id WHERE e.id=?", (event_id,))
        quiet = is_quiet_hours(reading_at, site["qs"], site["qe"])

        if meter_fault:
            sample = self.meter.read(force_fault=True)
        else:
            sample = self.meter.read(ambient=ambient, spike=spike)
        device_ok = bool(sample["device_ok"])
        value = reading_db if reading_db is not None else sample["db"]
        over = bool(device_ok and value > db_limit_at(quiet))

        photo = None
        if device_ok:
            photo = save_reading_photo(
                self.photo_dir, value, db_limit_at(quiet), quiet, reading_at, event_id)

        cur = self.db.execute(
            "INSERT INTO site_records(event_id, officer, db_reading, quiet, over_limit, "
            "reading_at, arrive_at, duration_s, outcome, photo_path, device_ok, device_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, officer["name"], value, int(quiet), int(over), reading_at, arrive_at,
             duration_s, outcome, photo, int(device_ok), sample["note"]),
        )

        result = {"record_id": cur.lastrowid, "db": value, "quiet": quiet,
                  "over": over, "device_ok": device_ok, "photo": photo,
                  "persistent": False, "closed_as": None}

        # 设备误报：读数不采信；本次到场按误报登记，事件转 false_alarm
        if not device_ok:
            self.db.execute(
                "UPDATE events SET status='false_alarm', disposition='device_false_alarm', "
                "resolution_note=?, resolved_at=? WHERE id=?",
                (f"分贝计设备误报（{sample['note']}），读数不予采信；现场沟通：{outcome}",
                 now_str(), event_id),
            )
            result["closed_as"] = "false_alarm"
            return result

        # 持续超限：安静时段两次有效超限读数间隔 ≤15 分钟 → 升级处置
        persistent = is_persistent(self.db, event_id)
        result["persistent"] = persistent
        if persistent:
            self.db.execute(
                "UPDATE events SET status='resolved', disposition='escalated_persistent', "
                "resolution_note=?, resolved_at=? WHERE id=?",
                (f"安静时段持续超限：升级处置（书面警告并上报主管）；现场沟通：{outcome}",
                 now_str(), event_id),
            )
            result["closed_as"] = "escalated_persistent"
        elif not over:
            self.db.execute(
                "UPDATE events SET status='resolved', disposition='within_limit', "
                "resolution_note=?, resolved_at=? WHERE id=?",
                (f"到场读数 {value:.1f}dB 未超限值，口头提醒；现场沟通：{outcome}",
                 now_str(), event_id),
            )
            result["closed_as"] = "within_limit"
        else:
            # 单次超限：保持 assigned，记录后待再次到场或视沟通结果处置
            self.db.execute(
                "UPDATE events SET resolution_note=? WHERE id=?",
                (f"单次超限读数 {value:.1f}dB，已现场干预：{outcome}（持续观察中）", event_id),
            )
        return result

    # ---------------- 申诉与主管复核（巡查员不能自行关闭被申诉记录） ----------------
    def appeal(self, event_id: int, note: str, ts: str | None = None) -> dict:
        ev = self.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        if ev is None:
            raise ServiceError("事件不存在")
        if ev["status"] not in ("resolved", "false_alarm"):
            raise ServiceError("仅已处置（含误报判定）的事件可被申诉")
        ts = ts or now_str()
        self.db.execute(
            "UPDATE events SET status='appealed', appeal_note=?, appealed_at=? WHERE id=?",
            (note.strip(), ts, event_id),
        )
        self.messages.notify_review_needed(SUPERVISOR["phone"], event_id)
        return {"event_id": event_id, "status": "appealed"}

    def review(self, event_id: int, reviewer: dict, result: str, note: str) -> dict:
        """值班主管复核后才可结案。result: upheld / downgraded_false_alarm / reopen。"""
        ev = self.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        if ev is None:
            raise ServiceError("事件不存在")
        if ev["status"] != "appealed":
            raise ServiceError("只有被申诉、待复核的事件可复核")
        # 硬性隔离：巡查员角色不得复核
        if reviewer.get("role") == "officer":
            raise ServiceError("巡查员不能复核被申诉的处置记录，须由值班主管结案")

        self.db.execute(
            "INSERT INTO reviews(event_id, reviewer, result, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, reviewer["name"], result, note.strip(), now_str()),
        )
        if result == "upheld":
            disposition = ev["disposition"] or "upheld"
            self.db.execute(
                "UPDATE events SET status='closed', reviewer=?, review_note=?, reviewed_at=? "
                "WHERE id=?",
                (reviewer["name"], f"维持原处置（{disposition}）。{note}".strip(),
                 now_str(), event_id),
            )
        elif result == "downgraded_false_alarm":
            self.db.execute(
                "UPDATE events SET status='false_alarm', disposition='review_false_alarm', "
                "reviewer=?, review_note=?, reviewed_at=? WHERE id=?",
                (reviewer["name"], f"复核改判：设备误报/证据不足。{note}".strip(),
                 now_str(), event_id),
            )
        elif result == "reopen":
            self.db.execute(
                "UPDATE events SET status='assigned', reviewer=?, review_note=?, reviewed_at=? "
                "WHERE id=?",
                (reviewer["name"], f"复核发回重查。{note}".strip(), now_str(), event_id),
            )
        else:
            raise ServiceError("未知复核结论")
        return {"event_id": event_id, "result": result}

    # ---------------- 住客换营位 ----------------
    def relocate_guest(self, guest_id: int, to_site_id: int) -> dict:
        g = self.db.one("SELECT * FROM guests WHERE id=?", (guest_id,))
        if g is None:
            raise ServiceError("住客不存在")
        occupied = self.db.one(
            "SELECT id FROM guests WHERE site_id=? AND id<>?", (to_site_id, guest_id))
        if occupied:
            raise ServiceError("目标营位已有人入住")
        self.db.execute("UPDATE guests SET site_id=? WHERE id=?", (to_site_id, guest_id))
        return {"guest_id": guest_id, "from_site": g["site_id"], "to_site": to_site_id}

    def log_move_suggestion(self, event_id: int, to_site_id: int, reason: str) -> int:
        ev = self.db.one("SELECT reported_site_id FROM events WHERE id=?", (event_id,))
        cur = self.db.execute(
            "INSERT INTO move_suggestions(event_id, from_site, to_site, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, ev["reported_site_id"], to_site_id, reason, now_str()),
        )
        return cur.lastrowid

    # ---------------- 查询视图（匿名隔离在此统一收口） ----------------
    def public_complaints(self, event_id: int) -> list[dict]:
        """面向被投诉方/巡查界面的投诉视图：匿名投诉不暴露身份。"""
        rows = self.db.query(
            "SELECT c.id, c.event_id, c.site_id, c.anonymous, c.detail, c.created_at, "
            "c.withdrawn, c.withdrawn_at, g.name AS complainant_name, g.phone AS complainant_phone "
            "FROM complaints c LEFT JOIN guests g ON g.id=c.complainant_id "
            "WHERE c.event_id=? ORDER BY c.id",
            (event_id,),
        )
        out = []
        anon_index = 0
        for r in rows:
            d = dict(r)
            if d["anonymous"]:
                anon_index += 1
                d["complainant_name"] = f"匿名投诉人 {anon_index}"
                d["complainant_phone"] = None
                d["complainant_id"] = None
                d["site_id"] = None  # 营位也可能反推身份，匿名时一并隐藏
            else:
                d["complainant_id"] = None  # 公开视图不暴露内部住客 id
            out.append(d)
        return out

    def event_detail(self, event_id: int, include_identities: bool = False) -> dict:
        ev = self.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        if ev is None:
            raise ServiceError("事件不存在")
        ev = dict(ev)
        site = self.db.one("SELECT * FROM sites WHERE id=?", (ev["reported_site_id"],))
        complaints = self.public_complaints(event_id)
        records = [dict(r) for r in self.db.query(
            "SELECT * FROM site_records WHERE event_id=? ORDER BY reading_at, id", (event_id,))]
        reviews = [dict(r) for r in self.db.query(
            "SELECT * FROM reviews WHERE event_id=? ORDER BY id", (event_id,))]
        timeline = self._timeline(ev, complaints, records, reviews)
        ev["limit_at_first"] = db_limit_at(bool(ev["quiet"]))
        return {
            "event": ev,
            "site": dict(site),
            "neighbors": neighbors(self.db, ev["reported_site_id"]),
            "complaints": complaints,
            "records": records,
            "reviews": reviews,
            "readings": readings_over_limit(self.db, event_id),
            "timeline": timeline,
        }

    def _timeline(self, ev, complaints, records, reviews) -> list[dict]:
        """投诉详情按 发生 → 到场 → 复核 顺序排列。"""
        items = []
        for c in complaints:
            items.append({
                "phase": "发生", "at": c["created_at"],
                "title": ("匿名投诉" if c["anonymous"] else f"{c['complainant_name']} 的投诉")
                         + ("（已撤回）" if c["withdrawn"] else ""),
                "detail": c["detail"],
            })
        if ev["assigned_at"]:
            items.append({"phase": "派单", "at": ev["assigned_at"],
                          "title": f"现场负责人接单：{ev['assigned_to']}", "detail": ""})
        for r in records:
            tag = "设备误报" if not r["device_ok"] else (
                f"{r['db_reading']:.1f}dB " + ("超限" if r["over_limit"] else "未超限"))
            items.append({"phase": "到场", "at": r["arrive_at"],
                          "title": f"{r['officer']} 到场（{tag}）",
                          "detail": f"读数时刻 {r['reading_at']}；沟通结果：{r['outcome']}"})
        if ev["appealed_at"]:
            items.append({"phase": "申诉", "at": ev["appealed_at"],
                          "title": "被投诉方申诉", "detail": ev["appeal_note"]})
        for rv in reviews:
            result_text = {"upheld": "维持原处置", "downgraded_false_alarm": "改判误报",
                           "reopen": "发回重查"}.get(rv["result"], rv["result"])
            items.append({"phase": "复核", "at": rv["created_at"],
                          "title": f"主管复核：{result_text}（{rv['reviewer']}）",
                          "detail": rv["note"]})
        order = {"发生": 0, "派单": 1, "到场": 2, "申诉": 3, "复核": 4}
        items.sort(key=lambda x: (order.get(x["phase"], 9), x["at"]))
        return items

    def list_events(self) -> list[dict]:
        rows = self.db.query(
            """SELECT e.*, s.label AS site_label,
                      (SELECT COUNT(*) FROM complaints c WHERE c.event_id=e.id) AS n_complaints,
                      (SELECT COUNT(*) FROM complaints c WHERE c.event_id=e.id AND c.withdrawn=0)
                        AS n_active,
                      (SELECT COUNT(*) FROM site_records r WHERE r.event_id=e.id) AS n_visits
               FROM events e JOIN sites s ON s.id=e.reported_site_id
               ORDER BY e.id DESC""")
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        total = self.db.one("SELECT COUNT(*) n FROM events")["n"]
        by_status = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) n FROM events GROUP BY status")}
        merged = self.db.one(
            "SELECT COUNT(*) n FROM complaints c JOIN events e ON e.id=c.event_id "
            "WHERE c.id NOT IN (SELECT MIN(id) FROM complaints GROUP BY event_id)")["n"]
        anon = self.db.one("SELECT COUNT(*) n FROM complaints WHERE anonymous=1")["n"]
        withdrawn = self.db.one("SELECT COUNT(*) n FROM complaints WHERE withdrawn=1")["n"]
        false_alarms = self.db.one(
            "SELECT COUNT(*) n FROM site_records WHERE device_ok=0")["n"]
        persistent = self.db.one(
            "SELECT COUNT(*) n FROM events WHERE disposition='escalated_persistent'")["n"]
        exempt = self.db.one("SELECT COUNT(*) n FROM events WHERE exempt=1")["n"]
        night = self.db.one("SELECT COUNT(*) n FROM events WHERE quiet=1")["n"]
        review_rows = self.db.query(
            "SELECT result, COUNT(*) n FROM reviews GROUP BY result")
        return {
            "total_events": total,
            "by_status": by_status,
            "merged_complaints": merged,
            "anonymous_complaints": anon,
            "withdrawn_complaints": withdrawn,
            "device_false_alarms": false_alarms,
            "persistent_escalations": persistent,
            "exempt_events": exempt,
            "night_events": night,
            "reviews": {r["result"]: r["n"] for r in review_rows},
        }

    # ---------------- 帮助 ----------------
    def _site_label(self, label: str) -> str:
        return label
