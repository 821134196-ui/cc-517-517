"""营地初始化与演示数据：8 营位网格 + 相邻关系；生成重复投诉合并等样例。"""
from __future__ import annotations

from .database import Database
from .services import CampsiteService, OFFICERS, SUPERVISOR

# (id, 名称, 类型, x, y)
SITES = [
    (1, "A-01 林景台", "standard", 120, 110),
    (2, "A-02 松风位", "standard", 280, 110),
    (3, "A-03 溪畔位", "standard", 440, 110),
    (4, "A-04 观星位", "standard", 600, 110),
    (5, "B-01 桦影位", "standard", 120, 290),
    (6, "B-02 篝火边", "standard", 280, 290),
    (7, "B-03 草甸位", "standard", 440, 290),
    (8, "B-04 空位（机动）", "standard", 600, 290),
]

# 相邻营位关系（无向）
EDGES = [
    (1, 2), (2, 3), (3, 4),
    (5, 6), (6, 7), (7, 8),
    (1, 5), (2, 6), (3, 7), (4, 8),
    (2, 5), (3, 6),
]

GUESTS = [
    ("张伟", "13900000001", 1),
    ("李娜", "13900000002", 2),
    ("周磊", "13900000003", 3),
    ("赵强", "13900000004", 6),   # 常见被投诉方
    ("王芳", "13900000005", 5),
    ("陈晨", "13900000006", 7),
]


def build_camp(db: Database) -> None:
    if db.one("SELECT COUNT(*) n FROM sites")["n"]:
        return
    for sid, label, kind, x, y in SITES:
        db.execute(
            "INSERT INTO sites(id,label,kind,x,y,quiet_start,quiet_end) "
            "VALUES (?,?,?,?,?,'22:00','07:00')",
            (sid, label, kind, x, y),
        )
    for a, b in EDGES:
        db.execute("INSERT INTO adjacency(site_a,site_b) VALUES (?,?)", (a, b))
    for name, phone, site_id in GUESTS:
        db.execute("INSERT INTO guests(name,phone,site_id) VALUES (?,?,?)",
                   (name, phone, site_id))


def seed_demo(svc: CampsiteService) -> dict:
    """生成演示样例：合并投诉、持续超限+申诉、设备误报、撤回、豁免、待接单。"""
    db = svc.db
    build_camp(db)
    if db.one("SELECT COUNT(*) n FROM events")["n"]:
        return {}

    summary: dict = {"merged": None, "persistent": None, "false_alarm": None,
                     "withdrawn": None, "exempt": None, "open": None}

    # 1) 同一事件的多次投诉（22:10 / 22:20 / 22:31，合并窗口 30 分钟）
    r1 = svc.file_complaint(6, 5, 5, "B-02 音响重低音持续，孩子被吵醒", True,
                            "2026-10-01 22:10:00")
    svc.file_complaint(6, 6, 7, "6 号营位有人大声唱歌，已超过半小时", False,
                       "2026-10-01 22:20:00")
    svc.file_complaint(6, 5, 5, "还是很吵，音乐没有关小", True,
                       "2026-10-01 22:31:00")
    summary["merged"] = r1["event_id"]

    # 2) 持续超限：两次夜间读数均 >45dB 且间隔 ≤15 分钟 → 升级；随后被申诉待复核
    r2 = svc.file_complaint(3, 1, 1, "3 号营位划拳喧哗", True,
                            "2026-10-01 23:02:00")
    svc.claim_event(r2["event_id"], OFFICERS[0])
    svc.record_visit(r2["event_id"], OFFICERS[0], reading_db=47.2,
                     arrive_at="2026-10-01 23:08:00",
                     outcome="现场劝导调低音量",
                     reading_at="2026-10-01 23:09:00")
    svc.record_visit(r2["event_id"], OFFICERS[0], reading_db=48.6,
                     arrive_at="2026-10-01 23:20:00",
                     outcome="再次到场仍超限，书面警告并上报主管",
                     reading_at="2026-10-01 23:20:30")
    svc.appeal(r2["event_id"], "认为分贝计读数不准，客人称只是正常交谈")
    summary["persistent"] = r2["event_id"]

    # 3) 设备误报：到场后分贝计自检失败 → false_alarm
    r3 = svc.file_complaint(2, 3, 3, "2 号位有持续蜂鸣噪声", False,
                            "2026-10-01 23:40:00")
    svc.claim_event(r3["event_id"], OFFICERS[1])
    svc.record_visit(r3["event_id"], OFFICERS[1], reading_db=None,
                     arrive_at="2026-10-01 23:47:00",
                     outcome="排查为设备误报，现场无噪声",
                     meter_fault=True,
                     reading_at="2026-10-01 23:48:00")
    summary["false_alarm"] = r3["event_id"]

    # 4) 投诉撤回
    r4 = svc.file_complaint(1, 2, 2, "1 号位附近有狗叫", False,
                            "2026-10-02 00:15:00")
    cid = db.one("SELECT id FROM complaints WHERE event_id=? ORDER BY id DESC LIMIT 1",
                 (r4["event_id"],))["id"]
    svc.withdraw_complaint(cid, "狗已牵回室内，投诉人主动撤回")
    summary["withdrawn"] = r4["event_id"]

    # 5) 公共活动豁免：篝火晚会 20:00–21:30
    db.execute(
        "INSERT INTO exemptions(title, site_id, start_at, end_at, note) "
        "VALUES (?,?,?,?,?)",
        ("国庆篝火晚会", None, "20:00", "21:30", "已提前在营地公告，全营地豁免噪声限值"),
    )
    r5 = svc.file_complaint(4, None, 7, "广场音乐太响", True,
                            "2026-10-01 20:45:00")
    summary["exempt"] = r5["event_id"]

    # 6) 一条待接单的新投诉，便于演示接单竞争
    r6 = svc.file_complaint(7, 4, 4, "7 号位深夜打牌喧哗", False,
                            "2026-10-02 01:05:00")
    summary["open"] = r6["event_id"]

    return summary
