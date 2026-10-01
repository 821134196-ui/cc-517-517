"""业务规则层：投诉受理/合并、接单竞争、现场处置、申诉复核、换位、脱敏视图、统计。

所有写操作都在一个 ``with self.conn`` 事务里；接单竞争用单条条件 UPDATE
依赖 SQLite 行锁，保证同一事件只有一名现场负责人。
"""
from __future__ import annotations

from datetime import timedelta

from . import timeutil
from .messaging import MessageService
from .meter import MockSoundMeter

MERGE_WINDOW = timedelta(minutes=90)   # 同营位活跃事件的投诉合并窗口

# 仍可接收新投诉合并的事件状态
ACTIVE_STATUSES = ("open", "dispatched", "onsite", "pending_review", "appealed")

# 处置结果 → 事件走向 / 记录是否自动关闭
# 提醒、警告、持续超限都是对被投诉方的不利处置：巡查员提交后进入主管复核队列；
# 误报、豁免、未发现声源不针对住客：事件直接驳回，记录自动关闭留痕。
RESULT_FLOW = {
    "reminded":    ("pending_review", 1, False),
    "warning":     ("pending_review", 2, False),
    "persistent":  ("pending_review", 3, False),
    "false_alarm": ("dismissed", 0, True),
    "exempt":      ("dismissed", 0, True),
    "no_source":   ("dismissed", 0, True),
}


class ServiceError(Exception):
    pass


class _Tx:
    """可重入事务：外层用连接的事务（commit/rollback），内层用 SAVEPOINT。"""

    def __init__(self, conn):
        self.conn = conn
        self.depth = 0

    def __enter__(self):
        if self.depth == 0:
            self.conn.execute("BEGIN")
        else:
            self.conn.execute(f"SAVEPOINT sp_{self.depth}")
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        if self.depth == 0:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
            return False
        sp = f"sp_{self.depth}"
        if exc_type is None:
            self.conn.execute(f"RELEASE SAVEPOINT {sp}")
        else:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            self.conn.execute(f"RELEASE SAVEPOINT {sp}")
        return False


class CampService:
    def __init__(self, conn, meter: MockSoundMeter | None = None,
                 photo_dir: str = "data/readings"):
        self.conn = conn
        self._tx = _Tx(conn)
        self.meter = meter or MockSoundMeter(photo_dir)
        self.msg = MessageService(conn)

    def _transaction(self):
        return self._tx

    # ------------------------------------------------------------------ #
    # 辅助查询
    # ------------------------------------------------------------------ #
    def site(self, code: str):
        row = self.conn.execute("SELECT * FROM sites WHERE code=?", (code,)).fetchone()
        if not row:
            raise ServiceError(f"营位 {code} 不存在")
        return row

    def guest_by_token(self, token: str):
        return self.conn.execute(
            "SELECT * FROM guests WHERE anon_token=?", (token,)).fetchone()

    def occupancy_at(self, site_code: str, at=None):
        """某时刻住在该营位的住客（用于把被投诉营位解析到人）。"""
        t = timeutil.ts(at)
        return self.conn.execute(
            """SELECT o.*, g.name AS guest_name, g.contact, g.anon_token, g.id AS gid
               FROM occupancies o JOIN guests g ON g.id = o.guest_id
               WHERE o.site_code=? AND o.active=1 AND o.check_in<=?
                 AND (o.check_out IS NULL OR o.check_out>?)
               ORDER BY o.check_in DESC LIMIT 1""",
            (site_code, t, t),
        ).fetchone()

    def neighbors(self, site_code: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.*, a.distance_m FROM adjacency a
               JOIN sites s ON s.code = CASE WHEN a.a_code=? THEN a.b_code ELSE a.a_code END
               WHERE ? IN (a_code, b_code) ORDER BY a.distance_m""",
            (site_code, site_code),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 1) 投诉受理 + 同事件合并 + 豁免 + 夜间判定
    # ------------------------------------------------------------------ #
    def submit_complaint(self, *, reporter_token: str, reported_site: str,
                         content: str, anonymous: bool = True,
                         channel: str = "前台", when=None) -> dict:
        when = when or timeutil.now()
        reporter = self.guest_by_token(reporter_token)
        if not reporter:
            raise ServiceError("无效投诉凭证")
        site = self.site(reported_site)

        occ = self.conn.execute(
            """SELECT * FROM occupancies WHERE guest_id=? AND active=1
               AND check_in<=? AND (check_out IS NULL OR check_out>?)
               ORDER BY check_in DESC LIMIT 1""",
            (reporter["id"], timeutil.ts(when), timeutil.ts(when)),
        ).fetchone()
        reporter_site = occ["site_code"] if occ else "（已离场）"
        if reporter_site == reported_site:
            raise ServiceError("不能投诉自己所在营位")

        # 公共活动豁免：受理但直接免予处置，留痕备查
        exemption = timeutil.active_exemption(self.conn, when, reported_site)

        with self._transaction():
            # 合并判定：同被投诉营位、事件仍活跃、首诉在窗口内
            merge_target = self._find_merge_target(reported_site, when, exemption)

            if merge_target is None:
                cur = self.conn.execute(
                    """INSERT INTO incidents
                       (reported_site_code, noise_kind, status, quiet, exemption_id,
                        merged_count, created_ts)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (reported_site, "噪声扰民",
                     "dismissed" if exemption else "open",
                     1 if timeutil.in_quiet(when, site["quiet_start"],
                                            site["quiet_end"]) else 0,
                     exemption["id"] if exemption else None,
                     timeutil.ts(when)),
                )
                incident_id = cur.lastrowid
                cmp_id = self._insert_complaint(
                    incident_id, reporter, reporter_site, reported_site,
                    content, anonymous, channel, when, duplicate=False)
                self.conn.execute(
                    "UPDATE incidents SET first_complaint_id=? WHERE id=?",
                    (cmp_id, incident_id))
                if exemption:
                    self.conn.execute(
                        "UPDATE incidents SET close_reason=? WHERE id=?",
                        (f"公共活动豁免：{exemption['title']}", incident_id))
                    self.msg.send(to_guest_id=reporter["id"], kind="notify",
                                  incident_id=incident_id, redacted=False,
                                  body=f"您对 {reported_site} 的投诉已受理：该时段为"
                                       f"已报备公共活动「{exemption['title']}」，免予处置。")
                result = {"incident_id": incident_id, "complaint_id": cmp_id,
                          "merged": False, "exempt": bool(exemption)}
            else:
                incident_id = merge_target["id"]
                cmp_id = self._insert_complaint(
                    incident_id, reporter, reporter_site, reported_site,
                    content, anonymous, channel, when, duplicate=True)
                self.conn.execute(
                    "UPDATE incidents SET merged_count = merged_count + 1 WHERE id=?",
                    (incident_id,))
                self.msg.send(to_guest_id=reporter["id"], kind="notify",
                              incident_id=incident_id, redacted=False,
                              body=f"您对 {reported_site} 的投诉已并入正在处置的事件"
                                   f" #INC-{incident_id:03d}，无需重复致电。")
                result = {"incident_id": incident_id, "complaint_id": cmp_id,
                          "merged": True, "exempt": False}
        return result

    def _find_merge_target(self, reported_site, when, exemption):
        """同营位、活跃、首诉在合并窗口内的事件；豁免事件不参与合并。"""
        if exemption:
            return None
        rows = self.conn.execute(
            """SELECT i.* FROM incidents i WHERE i.reported_site_code=?
                 AND i.status IN ('open','dispatched','onsite','pending_review','appealed')
                 AND i.exemption_id IS NULL
               ORDER BY i.id DESC""",
            (reported_site,),
        ).fetchall()
        for row in rows:
            first = timeutil.parse(row["created_ts"])
            if when - first <= MERGE_WINDOW:
                return row
        return None

    def _insert_complaint(self, incident_id, reporter, reporter_site,
                          reported_site, content, anonymous, channel, when,
                          *, duplicate):
        cur = self.conn.execute(
            """INSERT INTO complaints
               (incident_id, reporter_guest_id, reporter_site_code, reported_site_code,
                anonymous, channel, content, status, is_duplicate, created_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (incident_id, reporter["id"], reporter_site, reported_site,
             1 if anonymous else 0, channel, content,
             1 if duplicate else 0, timeutil.ts(when)),
        )
        return cur.lastrowid

    # ------------------------------------------------------------------ #
    # 2) 投诉撤回（凭匿名 token，不向前台暴露身份之外的信息）
    # ------------------------------------------------------------------ #
    def withdraw_complaint(self, complaint_id: int, token: str,
                           reason: str = "", when=None) -> dict:
        when = when or timeutil.now()
        cmp = self.conn.execute(
            "SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not cmp:
            raise ServiceError("投诉不存在")
        guest = self.conn.execute(
            "SELECT * FROM guests WHERE id=?", (cmp["reporter_guest_id"],)).fetchone()
        if guest["anon_token"] != token:
            raise ServiceError("凭证不匹配，不能撤回他人投诉")
        if cmp["status"] == "withdrawn":
            raise ServiceError("该投诉已撤回")

        with self._transaction():
            self.conn.execute(
                """UPDATE complaints SET status='withdrawn', withdrawn_ts=?,
                   withdraw_reason=? WHERE id=?""",
                (timeutil.ts(when), reason, complaint_id))
            self.msg.send(kind="withdraw", incident_id=cmp["incident_id"],
                          to_guest_id=guest["id"], redacted=False,
                          body=f"您在事件 #INC-{cmp['incident_id']:03d} 中的投诉已撤回。")

            # 事件内全部投诉均撤回，且尚无现场处置 → 事件撤销
            active_left = self.conn.execute(
                "SELECT COUNT(*) c FROM complaints WHERE incident_id=? AND status='active'",
                (cmp["incident_id"],)).fetchone()["c"]
            has_onsite = self.conn.execute(
                "SELECT COUNT(*) c FROM onsite_records WHERE incident_id=?",
                (cmp["incident_id"],)).fetchone()["c"]
            incident = self.conn.execute(
                "SELECT * FROM incidents WHERE id=?",
                (cmp["incident_id"],)).fetchone()
            if active_left == 0 and has_onsite == 0 and incident["status"] in ACTIVE_STATUSES:
                self.conn.execute(
                    """UPDATE incidents SET status='withdrawn', closed_ts=?,
                       close_reason='全部投诉撤回' WHERE id=?""",
                    (timeutil.ts(when), cmp["incident_id"]))
                return {"incident_id": cmp["incident_id"], "incident_withdrawn": True}
        return {"incident_id": cmp["incident_id"], "incident_withdrawn": False}

    # ------------------------------------------------------------------ #
    # 3) 接单竞争：只有一人能成为现场负责人
    # ------------------------------------------------------------------ #
    def claim_incident(self, incident_id: int, officer_id: int, when=None) -> bool:
        """返回 True 表示抢占成功。并发下只有一个调用能拿到 True。"""
        when = when or timeutil.now()
        officer = self.conn.execute(
            "SELECT * FROM staff WHERE id=? AND active=1", (officer_id,)).fetchone()
        if not officer:
            raise ServiceError("值班员不存在或已停用")
        if officer["role"] not in ("officer", "patroller"):
            raise ServiceError("只有值班员/巡查员可以接单，主管不担任现场负责人")
        with self._transaction():
            cur = self.conn.execute(
                """UPDATE incidents
                      SET assigned_officer_id=?, assigned_ts=?, status='dispatched'
                    WHERE id=? AND assigned_officer_id IS NULL AND status='open'""",
                (officer_id, timeutil.ts(when), incident_id))
            if cur.rowcount != 1:
                return False  # 已被别人接走，或状态不再可接
            site = self.conn.execute(
                "SELECT reported_site_code FROM incidents WHERE id=?",
                (incident_id,)).fetchone()
            self.msg.send(to_site=site["reported_site_code"], kind="dispatch",
                          incident_id=incident_id, redacted=True,
                          body=f"【噪声提醒】关于本营位的投诉 #INC-{incident_id:03d} "
                               f"已派值班员到场，请降低音量。（投诉人信息保密）")
        return True

    def mark_arrived(self, incident_id: int, patroller_id: int, when=None) -> None:
        when = when or timeutil.now()
        with self._transaction():
            inc = self.conn.execute(
                "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not inc:
                raise ServiceError("事件不存在")
            if inc["assigned_officer_id"] and inc["assigned_officer_id"] != patroller_id:
                raise ServiceError("只有接单的现场负责人可以记录到场")
            self.conn.execute(
                "UPDATE incidents SET status='onsite' WHERE id=? AND status='dispatched'",
                (incident_id,))

    # ------------------------------------------------------------------ #
    # 4) 分贝读数（设备故障时读数为 NULL，不得作为超限依据）
    # ------------------------------------------------------------------ #
    def add_reading(self, incident_id: int, *, fault: bool = False,
                    base: float = 42.0, variance: float = 6.0,
                    seed: int | None = None, when=None, note: str = "") -> dict:
        when = when or timeutil.now()
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc:
            raise ServiceError("事件不存在")
        reading = self.meter.read(inc["reported_site_code"], fault=fault,
                                  base=base, variance=variance, seed=seed, when=when)
        with self._transaction():
            cur = self.conn.execute(
                """INSERT INTO readings (incident_id, ts, db_value, meter_ok,
                                         photo_path, note)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (incident_id, timeutil.ts(when), reading.db_value,
                 1 if reading.meter_ok else 0, reading.photo_path,
                 note or reading.note))
        return {"reading_id": cur.lastrowid, "db": reading.db_value,
                "meter_ok": reading.meter_ok, "photo": reading.photo_path}

    def judge_overlimit(self, incident_id: int) -> dict | None:
        """依据有效读数判定是否持续超限；设备故障/无读数不可判定。"""
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        site = self.site(inc["reported_site_code"])
        rows = self.conn.execute(
            "SELECT * FROM readings WHERE incident_id=? ORDER BY ts",
            (incident_id,)).fetchall()
        valid = [r for r in rows if r["meter_ok"] and r["db_value"] is not None]
        if not valid:
            return {"decidable": False, "overlimit": False,
                    "reason": "无有效读数（设备故障或未读数），不能认定超限"}
        over = []
        for r in valid:
            dt = timeutil.parse(r["ts"])
            limit, quiet = timeutil.applicable_limit(dt, site)
            if r["db_value"] > limit:
                over.append((r["db_value"], limit))
        # 两次及以上超限才算“持续”，单次为瞬时
        return {"decidable": True, "overlimit": len(over) >= 1,
                "persistent": len(over) >= 2, "samples": len(valid),
                "over_samples": over,
                "reason": ("持续超限" if len(over) >= 2 else
                           "单次超限" if over else "读数均未超限")}

    # ------------------------------------------------------------------ #
    # 5) 现场处置记录（巡查员只提交，不结案）
    # ------------------------------------------------------------------ #
    def complete_onsite(self, incident_id: int, patroller_id: int, *,
                        communication: str, result: str,
                        arrived_ts: str | None = None, when=None) -> int:
        when = when or timeutil.now()
        arrived = arrived_ts or timeutil.ts(when)
        if result not in RESULT_FLOW:
            raise ServiceError(f"未知处置结果 {result}")
        patroller = self.conn.execute(
            "SELECT * FROM staff WHERE id=?", (patroller_id,)).fetchone()
        if not patroller:
            raise ServiceError("巡查员不存在")

        # 到场时若正在进行已报备公共活动 → 只能按豁免处置
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        exemption = timeutil.active_exemption(self.conn, when,
                                              inc["reported_site_code"])

        with self._transaction():
            if exemption and result != "exempt":
                raise ServiceError("该营位此刻处于公共活动豁免期，应登记为 exempt")
            auto_close = RESULT_FLOW[result][2]
            rec_status = "closed" if auto_close else "submitted"
            cur = self.conn.execute(
                """INSERT INTO onsite_records
                   (incident_id, patroller_id, arrived_ts, communication,
                    result, status, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (incident_id, patroller_id, arrived, communication, result,
                 rec_status, timeutil.ts(when)))
            record_id = cur.lastrowid

            # 升级级别：参考该营位历史有效处置
            if result in ("warning", "persistent"):
                prior = self.conn.execute(
                    """SELECT COUNT(*) c FROM onsite_records r JOIN incidents i
                         ON i.id = r.incident_id
                       WHERE i.reported_site_code=? AND r.id<>?
                         AND r.result IN ('warning','persistent')""",
                    (inc["reported_site_code"], record_id)).fetchone()["c"]
                level = min(2 + prior + (1 if result == "persistent" else 0), 4)
            else:
                level = RESULT_FLOW[result][1]

            new_status = RESULT_FLOW[result][0]
            self.conn.execute(
                "UPDATE incidents SET status=?, escalation_level=? WHERE id=?",
                (new_status, level, incident_id))
            if auto_close:
                self.conn.execute(
                    "UPDATE incidents SET closed_ts=COALESCE(closed_ts, ?), "
                    "close_reason=COALESCE(close_reason, ?) WHERE id=?",
                    (timeutil.ts(when),
                     {"false_alarm": "现场读数未超限：误报，驳回",
                      "exempt": "公共活动豁免，免予处置",
                      "no_source": "到场未发现噪声源，驳回"}[result],
                     incident_id))

            # 持续超限达到 3 级：自动生成换位建议
            reloc_id = None
            if result == "persistent" and level >= 3:
                reloc_id = self._auto_relocation(incident_id, when)

            # 通知：误报/豁免 → 告知投诉链（不暴露给被投诉方）；警告/持续 → 通知被投诉方
            if result in ("false_alarm", "exempt", "no_source"):
                reason_text = {"false_alarm": "现场读数未超限，判定为误报",
                               "exempt": f"公共活动豁免：{exemption['title']}" if exemption else "公共活动豁免",
                               "no_source": "到场未发现噪声源"}[result]
                for c in self._chain_guests(incident_id):
                    self.msg.send(to_guest_id=c, kind="notify",
                                  incident_id=incident_id, redacted=False,
                                  body=f"事件 #INC-{incident_id:03d} 处置结果：{reason_text}。")
            else:
                self.msg.send(to_site=inc["reported_site_code"], kind="warning",
                              incident_id=incident_id, redacted=True,
                              body=f"【噪声处置 #INC-{incident_id:03d}】现场已记录"
                                   f"（{'口头提醒' if result=='reminded' else '书面警告/持续超限'}），"
                                   f"请遵守安静时段规定。投诉人信息依法保密。")
        return record_id

    def _chain_guests(self, incident_id):
        return [r["reporter_guest_id"] for r in self.conn.execute(
            "SELECT DISTINCT reporter_guest_id FROM complaints "
            "WHERE incident_id=? AND status='active'", (incident_id,))]

    # ------------------------------------------------------------------ #
    # 6) 申诉 + 值班主管复核结案
    # ------------------------------------------------------------------ #
    def appeal(self, record_id: int, token: str, note: str, when=None) -> None:
        """被投诉方凭自己的 anon_token 对处置记录申诉。"""
        when = when or timeutil.now()
        rec = self.conn.execute(
            "SELECT * FROM onsite_records WHERE id=?", (record_id,)).fetchone()
        if not rec:
            raise ServiceError("处置记录不存在")
        if rec["status"] in ("closed", "reviewed"):
            raise ServiceError("已复核记录不能再申诉")
        occ = self.occupancy_at(
            self.conn.execute("SELECT reported_site_code FROM incidents WHERE id=?",
                              (rec["incident_id"],)).fetchone()["reported_site_code"], when)
        if not occ or occ["anon_token"] != token:
            raise ServiceError("只有被投诉营位的当前住客可以申诉")
        with self._transaction():
            self.conn.execute(
                """UPDATE onsite_records SET status='appealed', appeal_ts=?,
                   appeal_note=? WHERE id=?""",
                (timeutil.ts(when), note, record_id))
            self.conn.execute(
                "UPDATE incidents SET status='appealed' WHERE id=?",
                (rec["incident_id"],))

    def review_close(self, *, record_id: int, supervisor_id: int,
                     decision: str, note: str = "", when=None) -> dict:
        """值班主管复核并结案。decision: approved(申诉成立)/rejected(维持)/followup。"""
        when = when or timeutil.now()
        if decision not in ("approved", "rejected", "followup"):
            raise ServiceError("无效复核决定")
        sup = self.conn.execute(
            "SELECT * FROM staff WHERE id=?", (supervisor_id,)).fetchone()
        if not sup or sup["role"] != "supervisor":
            raise ServiceError("只有值班主管可以复核结案")
        rec = self.conn.execute(
            "SELECT * FROM onsite_records WHERE id=?", (record_id,)).fetchone()
        if not rec:
            raise ServiceError("处置记录不存在")
        if rec["patroller_id"] == supervisor_id:
            raise ServiceError("复核人不能是处置记录人本人")
        if rec["status"] == "closed":
            raise ServiceError("记录已结案")

        with self._transaction():
            if decision == "followup":
                # 退回继续跟进：记录留复核痕，事件重新派给原负责人
                self.conn.execute(
                    """UPDATE onsite_records SET status='reviewed', reviewer_id=?,
                       review_ts=?, review_decision='followup', review_note=?
                     WHERE id=?""",
                    (supervisor_id, timeutil.ts(when), note, record_id))
                self.conn.execute(
                    "UPDATE incidents SET status='dispatched' WHERE id=?",
                    (rec["incident_id"],))
                return {"incident_id": rec["incident_id"], "closed": False}

            final_status = "closed"
            close_reason = "主管复核：维持处置"
            if decision == "approved":
                # 申诉成立 → 撤销处置
                final_status = "dismissed"
                close_reason = f"主管复核：申诉成立，撤销处置（{note}）"
            self.conn.execute(
                """UPDATE onsite_records SET status='closed', reviewer_id=?,
                   review_ts=?, review_decision=?, review_note=? WHERE id=?""",
                (supervisor_id, timeutil.ts(when), decision, note, record_id))
            self.conn.execute(
                """UPDATE incidents SET status=?, closed_ts=?, close_reason=?
                   WHERE id=?""",
                (final_status, timeutil.ts(when), close_reason, rec["incident_id"]))
            self.msg.send(to_site=self.conn.execute(
                              "SELECT reported_site_code FROM incidents WHERE id=?",
                              (rec["incident_id"],)).fetchone()["reported_site_code"],
                          kind="notify", incident_id=rec["incident_id"], redacted=True,
                          body=f"事件 #INC-{rec['incident_id']:03d} 已由值班主管复核"
                               f"{'维持处置并结案' if decision=='rejected' else '申诉成立，撤销处置'}。")
        return {"incident_id": rec["incident_id"], "closed": True,
                "final_status": final_status}

    def patrol_try_close(self, record_id: int, patroller_id: int) -> None:
        """巡查员自行结案 —— 始终被业务层拒绝（被申诉记录另有 DB 触发器兜底）。"""
        rec = self.conn.execute(
            "SELECT * FROM onsite_records WHERE id=?", (record_id,)).fetchone()
        if not rec:
            raise ServiceError("处置记录不存在")
        if rec["status"] == "appealed":
            raise ServiceError("被申诉的处置记录须经值班主管复核，巡查员不得自行结案")
        raise ServiceError("处置记录须提交值班主管复核结案")

    # ------------------------------------------------------------------ #
    # 7) 换营位与换位建议
    # ------------------------------------------------------------------ #
    def move_guest(self, guest_id: int, to_site: str, when=None) -> None:
        """住客换营位：旧入住关闭、新开一条，历史快照保留。"""
        when = when or timeutil.now()
        self.site(to_site)
        occ = self.conn.execute(
            "SELECT * FROM occupancies WHERE guest_id=? AND active=1",
            (guest_id,)).fetchone()
        if not occ:
            raise ServiceError("住客当前未入住")
        taken = self.occupancy_at(to_site, when)
        if taken:
            raise ServiceError(f"目标营位 {to_site} 已有人入住")
        with self._transaction():
            self.conn.execute(
                "UPDATE occupancies SET active=0, check_out=? WHERE id=?",
                (timeutil.ts(when), occ["id"]))
            self.conn.execute(
                "INSERT INTO occupancies (guest_id, site_code, check_in, active) "
                "VALUES (?, ?, ?, 1)", (guest_id, to_site, timeutil.ts(when)))

    def suggest_relocation(self, incident_id: int, to_site: str,
                           reason: str, when=None) -> int:
        when = when or timeutil.now()
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc:
            raise ServiceError("事件不存在")
        self.site(to_site)
        occ = self.occupancy_at(inc["reported_site_code"], when)
        with self._transaction():
            cur = self.conn.execute(
                """INSERT INTO relocation_suggestions
                   (incident_id, guest_id, from_site, to_site, reason, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (incident_id, occ["gid"] if occ else None,
                 inc["reported_site_code"], to_site, reason, timeutil.ts(when)))
            return cur.lastrowid

    def _auto_relocation(self, incident_id, when) -> int | None:
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        target = self._quiet_free_site(inc["reported_site_code"])
        if not target:
            return None
        rid = self.suggest_relocation(
            incident_id, target,
            "持续超限且升级至 3 级：建议噪声方调换至远离相邻营位的空位", when)
        self.conn.execute(
            "UPDATE incidents SET suggested_relocation_id=? WHERE id=?",
            (rid, incident_id))
        return rid

    def _quiet_free_site(self, away_from: str) -> str | None:
        """选择与问题营位不相邻、当前无人住、且无活跃投诉的空位。"""
        nbrs = {n["code"] for n in self.neighbors(away_from)}
        rows = self.conn.execute("SELECT code FROM sites ORDER BY code").fetchall()
        for r in rows:
            code = r["code"]
            if code == away_from or code in nbrs:
                continue
            if self.occupancy_at(code):
                continue
            busy = self.conn.execute(
                "SELECT 1 FROM incidents WHERE reported_site_code=? "
                "AND status IN ('open','dispatched','onsite','pending_review','appealed')",
                (code,)).fetchone()
            if not busy:
                return code
        return None

    def respond_relocation(self, suggestion_id: int, accept: bool, when=None) -> None:
        when = when or timeutil.now()
        sug = self.conn.execute(
            "SELECT * FROM relocation_suggestions WHERE id=?",
            (suggestion_id,)).fetchone()
        if not sug or sug["status"] != "proposed":
            raise ServiceError("建议不存在或已处理")
        with self._transaction():
            if accept:
                self.move_guest(sug["guest_id"], sug["to_site"], when)
                self.conn.execute(
                    "UPDATE relocation_suggestions SET status='accepted' WHERE id=?",
                    (suggestion_id,))
            else:
                self.conn.execute(
                    "UPDATE relocation_suggestions SET status='declined' WHERE id=?",
                    (suggestion_id,))

    # ------------------------------------------------------------------ #
    # 8) 脱敏视图与时间线
    # ------------------------------------------------------------------ #
    def incident_detail(self, incident_id: int, *, viewer: str = "staff") -> dict:
        """viewer='staff' 内部视图可见投诉人；viewer='respondent' 被投诉方视图脱敏。"""
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc:
            raise ServiceError("事件不存在")
        site = self.site(inc["reported_site_code"])
        complaints = []
        for c in self.conn.execute(
                "SELECT * FROM complaints WHERE incident_id=? ORDER BY id",
                (incident_id,)):
            g = self.conn.execute(
                "SELECT * FROM guests WHERE id=?", (c["reporter_guest_id"],)).fetchone()
            if viewer == "respondent" and c["anonymous"]:
                complaints.append({
                    "id": c["id"], "reporter": "匿名住客",
                    "reporter_site": "邻近营位", "contact": None,
                    "content": c["content"], "anonymous": True,
                    "status": c["status"], "is_duplicate": c["is_duplicate"],
                    "created_ts": c["created_ts"],
                    "withdrawn_ts": c["withdrawn_ts"],
                })
            else:
                complaints.append({
                    "id": c["id"],
                    "reporter": g["name"] if viewer == "staff" or not c["anonymous"]
                                else "匿名住客",
                    "reporter_site": c["reporter_site_code"]
                                if viewer == "staff" or not c["anonymous"]
                                else "邻近营位",
                    "contact": g["contact"] if viewer == "staff" else None,
                    "content": c["content"], "anonymous": bool(c["anonymous"]),
                    "status": c["status"], "is_duplicate": bool(c["is_duplicate"]),
                    "created_ts": c["created_ts"],
                    "withdrawn_ts": c["withdrawn_ts"],
                })

        readings = [dict(r) for r in self.conn.execute(
            "SELECT * FROM readings WHERE incident_id=? ORDER BY ts", (incident_id,))]
        records = []
        for r in self.conn.execute(
                "SELECT * FROM onsite_records WHERE incident_id=? ORDER BY id",
                (incident_id,)):
            d = dict(r)
            d["patroller_name"] = self.conn.execute(
                "SELECT name FROM staff WHERE id=?", (r["patroller_id"],)).fetchone()["name"]
            if r["reviewer_id"]:
                d["reviewer_name"] = self.conn.execute(
                    "SELECT name FROM staff WHERE id=?",
                    (r["reviewer_id"],)).fetchone()["name"]
            records.append(d)
        relocs = [dict(r) for r in self.conn.execute(
            "SELECT * FROM relocation_suggestions WHERE incident_id=? ORDER BY id",
            (incident_id,))]

        return {"incident": dict(inc), "site": dict(site),
                "complaints": complaints, "readings": readings,
                "records": records, "relocations": relocs,
                "judgement": self.judge_overlimit(incident_id),
                "timeline": self.timeline(incident_id, viewer=viewer)}

    def timeline(self, incident_id: int, *, viewer: str = "staff") -> list[dict]:
        """按 发生 → 到场（读数/沟通）→ 申诉/复核 排序的统一时间线。"""
        items: list[dict] = []
        for c in self.conn.execute(
                "SELECT * FROM complaints WHERE incident_id=? ORDER BY id",
                (incident_id,)):
            g = self.conn.execute(
                "SELECT name FROM guests WHERE id=?", (c["reporter_guest_id"],)).fetchone()
            who = g["name"]
            where = c["reporter_site_code"]
            if viewer == "respondent" and c["anonymous"]:
                who, where = "匿名住客", "邻近营位"
            items.append({
                "phase": "1-发生", "ts": c["created_ts"],
                "title": ("重复投诉并入" if c["is_duplicate"] else "投诉受理")
                         + ("（匿名）" if c["anonymous"] else ""),
                "detail": f"{who} @ {where}：{c['content']}"
                          + ("（已撤回）" if c["status"] == "withdrawn" else ""),
            })
        for r in self.conn.execute(
                "SELECT * FROM readings WHERE incident_id=? ORDER BY ts",
                (incident_id,)):
            items.append({
                "phase": "2-到场", "ts": r["ts"],
                "title": "分贝读数",
                "detail": (f"{r['db_value']} dB，快照 {r['photo_path']}"
                           if r["meter_ok"] else
                           f"设备故障，读数无效（快照 {r['photo_path']}）"),
            })
        for r in self.conn.execute(
                "SELECT * FROM onsite_records WHERE incident_id=? ORDER BY id",
                (incident_id,)):
            p = self.conn.execute("SELECT name FROM staff WHERE id=?",
                                  (r["patroller_id"],)).fetchone()["name"]
            items.append({
                "phase": "2-到场", "ts": r["arrived_ts"],
                "title": "值班员到场",
                "detail": f"现场负责人 {p}",
            })
            items.append({
                "phase": "2-到场", "ts": r["created_ts"],
                "title": f"沟通结果：{self._result_text(r['result'])}",
                "detail": r["communication"],
            })
            if r["appeal_ts"]:
                items.append({
                    "phase": "3-复核", "ts": r["appeal_ts"],
                    "title": "被投诉方申诉", "detail": r["appeal_note"],
                })
            if r["review_ts"]:
                s = self.conn.execute("SELECT name FROM staff WHERE id=?",
                                      (r["reviewer_id"],)).fetchone()["name"]
                items.append({
                    "phase": "3-复核", "ts": r["review_ts"],
                    "title": f"值班主管复核：{self._decision_text(r['review_decision'])}",
                    "detail": f"复核人 {s}；{r['review_note'] or ''}",
                })
        items.sort(key=lambda x: (x["ts"], x["phase"]))
        return items

    @staticmethod
    def _result_text(r):
        return {"reminded": "口头提醒", "warning": "书面警告",
                "persistent": "持续超限", "false_alarm": "设备/感知误报",
                "exempt": "公共活动豁免", "no_source": "未发现噪声源"}.get(r, r)

    @staticmethod
    def _decision_text(d):
        return {"approved": "申诉成立，撤销处置", "rejected": "维持处置，结案",
                "followup": "退回继续跟进"}.get(d, d or "")

    # ------------------------------------------------------------------ #
    # 9) 营位图与统计
    # ------------------------------------------------------------------ #
    def map_data(self, when=None) -> dict:
        when = when or timeutil.now()
        sites = []
        for s in self.conn.execute("SELECT * FROM sites ORDER BY code"):
            d = dict(s)
            d["quiet_now"] = timeutil.in_quiet(when, s["quiet_start"], s["quiet_end"])
            active = self.conn.execute(
                """SELECT i.id, i.status FROM incidents i
                   WHERE i.reported_site_code=? AND i.status IN
                     ('open','dispatched','onsite','pending_review','appealed')
                   ORDER BY i.id DESC LIMIT 1""", (s["code"],)).fetchone()
            d["active_incident"] = active["id"] if active else None
            d["incident_status"] = active["status"] if active else None
            occ = self.occupancy_at(s["code"], when)
            d["occupied_by"] = occ["guest_name"] if occ else None
            sites.append(d)
        edges = [dict(a=r["a_code"], b=r["b_code"], d=r["distance_m"])
                 for r in self.conn.execute(
                     "SELECT * FROM adjacency ORDER BY a_code, b_code")]
        return {"sites": sites, "edges": edges, "now": timeutil.ts(when)}

    def impact_scope(self, incident_id: int) -> dict:
        """营位图上的影响范围：被投诉营位 + 相邻营位。"""
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc:
            raise ServiceError("事件不存在")
        center = dict(self.site(inc["reported_site_code"]))
        nbrs = self.neighbors(inc["reported_site_code"])
        return {"center": center, "neighbors": nbrs,
                "affected_codes": [center["code"]] + [n["code"] for n in nbrs]}

    def list_incidents(self, status=None):
        sql = "SELECT * FROM incidents"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY id DESC"
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["site_name"] = self.site(r["reported_site_code"])["name"]
            if r["assigned_officer_id"]:
                d["officer_name"] = self.conn.execute(
                    "SELECT name FROM staff WHERE id=?",
                    (r["assigned_officer_id"],)).fetchone()["name"]
            out.append(d)
        return out

    def review_stats(self) -> dict:
        c = self.conn
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        by_status = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM incidents GROUP BY status")}
        # 到场响应时长（分钟）
        resp = [r[0] for r in c.execute(
            """SELECT (julianday(o.arrived_ts) - julianday(i.created_ts)) * 1440
               FROM onsite_records o JOIN incidents i ON i.id=o.incident_id""")
                if r[0] is not None]
        appeals = {r["d"]: r["n"] for r in c.execute(
            "SELECT COALESCE(review_decision,'pending') d, COUNT(*) n "
            "FROM onsite_records WHERE status IN ('appealed','closed','reviewed') "
            "AND (appeal_ts IS NOT NULL OR review_decision IS NOT NULL) GROUP BY d")}
        officer_load = [dict(r) for r in c.execute(
            """SELECT s.name, s.role, COUNT(i.id) n FROM staff s
               LEFT JOIN incidents i ON i.assigned_officer_id=s.id
               WHERE s.active=1 GROUP BY s.id ORDER BY n DESC""")]
        results = {r["result"]: r["n"] for r in c.execute(
            "SELECT result, COUNT(*) n FROM onsite_records GROUP BY result")}
        return {
            "total": one("SELECT COUNT(*) FROM incidents"),
            "by_status": by_status,
            "quiet_share": dict(c.execute(
                "SELECT quiet, COUNT(*) n FROM incidents GROUP BY quiet").fetchall()),
            "duplicate_complaints": one(
                "SELECT COUNT(*) FROM complaints WHERE is_duplicate=1"),
            "withdrawn": one("SELECT COUNT(*) FROM complaints WHERE status='withdrawn'"),
            "exempted": one("SELECT COUNT(*) FROM incidents WHERE exemption_id IS NOT NULL"),
            "false_alarms": results.get("false_alarm", 0),
            "onsite_results": results,
            "avg_response_min": round(sum(resp) / len(resp), 1) if resp else None,
            "appeals": appeals,
            "officer_load": officer_load,
            "escalation": {r["l"]: r["n"] for r in c.execute(
                "SELECT escalation_level l, COUNT(*) n FROM incidents GROUP BY l")},
            "pending_review_queue": [dict(r) for r in c.execute(
                """SELECT o.id record_id, o.incident_id, o.result, o.status,
                          o.patroller_id, s.name patroller_name, o.arrived_ts
                   FROM onsite_records o JOIN staff s ON s.id=o.patroller_id
                   WHERE o.status IN ('submitted','appealed')
                   ORDER BY o.appeal_ts IS NULL, o.arrived_ts""")],
        }
