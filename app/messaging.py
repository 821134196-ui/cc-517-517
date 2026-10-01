"""模拟消息服务：所有“发送”落库留痕，不真正外联。

关键脱敏行为：向被投诉方发送通知时 redacted=1，正文只含事件与处置内容，
不含投诉人姓名、营位、联系方式；服务层负责保证匿名投诉不进正文。
"""
from __future__ import annotations

from . import timeutil


class MessageService:
    def __init__(self, conn):
        self.conn = conn

    def send(self, *, to_guest_id=None, to_site=None, kind: str,
             body: str, incident_id=None, redacted: bool = False) -> int:
        cur = self.conn.execute(
            """INSERT INTO messages
               (incident_id, to_guest_id, to_site, kind, body, redacted, sent_ts, delivered)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (incident_id, to_guest_id, to_site, kind, body,
             1 if redacted else 0, timeutil.ts()),
        )
        # 不在此处 commit：由服务层事务统一提交/回滚
        return cur.lastrowid

    def log_for(self, incident_id: int):
        return self.conn.execute(
            "SELECT * FROM messages WHERE incident_id=? ORDER BY id",
            (incident_id,),
        ).fetchall()
