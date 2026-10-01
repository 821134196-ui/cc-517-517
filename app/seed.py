"""样例数据：重置数据库并生成一条含重复投诉的完整投诉链。

被 ``run.sh`` 调用；也可单独执行：python3 -m app.seed
"""
from __future__ import annotations

from datetime import timedelta

from . import db, timeutil
from .meter import MockSoundMeter
from .services import CampService

STAFF = [
    ("值班员-周磊", "officer", "13800000001"),
    ("值班员-陈岚", "officer", "13800000002"),
    ("巡查员-高源", "patroller", "13800000003"),
    ("主管-方勤", "supervisor", "13800000004"),
]

# code, 名称, 分区, x, y
SITES = [
    ("A-01", "松林 1 号", "A 松林", 80, 90),
    ("A-02", "松林 2 号", "A 松林", 200, 90),
    ("A-03", "松林 3 号", "A 松林", 320, 90),
    ("B-01", "湖畔 1 号", "B 湖畔", 80, 220),
    ("B-02", "湖畔 2 号", "B 湖畔", 200, 220),
    ("B-03", "湖畔 3 号", "B 湖畔", 320, 220),
    ("C-01", "篝火场旁", "C 活动", 200, 350),
    ("C-02", "活动边位", "C 活动", 320, 350),
    ("D-01", "安静林 1 号", "D 静区", 460, 120),
    ("D-02", "安静林 2 号", "D 静区", 460, 250),
]

EDGES = [
    ("A-01", "A-02", 6), ("A-02", "A-03", 6),
    ("A-01", "B-01", 9), ("A-02", "B-02", 9), ("A-03", "B-03", 9),
    ("B-01", "B-02", 7), ("B-02", "B-03", 7),
    ("B-02", "C-01", 11), ("B-03", "C-02", 10),
    ("C-01", "C-02", 8), ("A-03", "D-01", 12),
    ("B-03", "D-02", 12), ("C-02", "D-02", 14),
]

GUESTS = [
    # name, contact, token, site, 入住时刻偏移（小时；主链事件发生在昨晚 23:00）
    ("赵一鸣", "13911110001", "tok-zhao", "A-02", -30),
    ("钱多多", "13911110002", "tok-qian", "A-01", -30),
    ("孙小眠", "13911110003", "tok-sun", "B-02", -29),
    ("李听安", "13911110004", "tok-li", "A-03", -29),
    ("周望远", "13911110005", "tok-zhou", "C-01", -27),
    ("吴静舒", "13911110006", "tok-wu", "B-01", -29),
]


def reset_and_seed(db_path: str = "data/camp.db", photo_dir: str = "data/readings") -> CampService:
    import os, shutil
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(photo_dir):
        shutil.rmtree(photo_dir)
    os.makedirs(photo_dir, exist_ok=True)

    conn = db.connect(db_path)
    db.init_db(conn)
    svc = CampService(conn, MockSoundMeter(photo_dir))
    base = timeutil.now().replace(minute=0, second=0, microsecond=0)

    # 员工
    for name, role, phone in STAFF:
        conn.execute("INSERT INTO staff (name, role, phone) VALUES (?,?,?)",
                     (name, role, phone))
    # 营位 + 相邻
    for code, nm, zone, x, y in SITES:
        conn.execute(
            """INSERT INTO sites (code,name,zone,x,y,quiet_start,quiet_end,
                                  day_limit,night_limit)
               VALUES (?,?,?,?,?, '22:00','07:00',55,45)""",
            (code, nm, zone, x, y))
    for a, b, dist in EDGES:
        conn.execute(
            "INSERT INTO adjacency (a_code,b_code,distance_m) VALUES (?,?,?)",
            (a, b, dist))
    # 住客入住
    for name, contact, token, site, off in GUESTS:
        cur = conn.execute(
            "INSERT INTO guests (name,contact,anon_token) VALUES (?,?,?)",
            (name, contact, token))
        conn.execute(
            "INSERT INTO occupancies (guest_id,site_code,check_in,active) "
            "VALUES (?,?,?,1)",
            (cur.lastrowid, site, timeutil.ts(base + timedelta(hours=off))))
    conn.commit()

    # 今晚 20:00–21:30 篝火晚会（全营公共活动豁免）
    fire_start = base.replace(hour=20)
    conn.execute(
        """INSERT INTO exemptions (scope,title,start_ts,end_ts,created_by,note)
           VALUES ('global', ?, ?, ?, 4, '每周例行篝火晚会')""",
        ("篝火晚会", timeutil.ts(fire_start),
         timeutil.ts(fire_start + timedelta(hours=1, minutes=30))))
    conn.commit()

    # ---------- 样例主链：A-02 深夜音响，多次投诉合并 ----------
    t0 = base  # 整点，作为相对基准
    night = (t0 - timedelta(days=1)).replace(hour=23)  # 昨晚 23:00（安静时段内）

    r1 = svc.submit_complaint(
        reporter_token="tok-qian", reported_site="A-02",
        content="A-02 开着户外音箱唱 K，低音很震，孩子没法睡",
        anonymous=True, channel="前台电话", when=night)
    inc = r1["incident_id"]
    # 8 分钟后第二位相邻住客投诉（并入同事件）
    svc.submit_complaint(
        reporter_token="tok-sun", reported_site="A-02",
        content="B-02 听到 A-02 音响，已关窗仍超过 50 分贝的体感",
        anonymous=True, channel="小程序", when=night + timedelta(minutes=8))
    # 20 分钟后第三位住客投诉（再次并入）
    svc.submit_complaint(
        reporter_token="tok-li", reported_site="A-02",
        content="第三次了，麻烦派人过来看一下",
        anonymous=False, channel="前台电话", when=night + timedelta(minutes=20))

    # 值班员周磊接单（陈岚随后尝试接单将失败 —— 测试接单竞争）
    svc.claim_incident(inc, 1, when=night + timedelta(minutes=21))
    svc.mark_arrived(inc, 1, when=night + timedelta(minutes=28))
    svc.add_reading(inc, base=52, variance=6, seed=7,
                    when=night + timedelta(minutes=29), note="帐篷外 1 米")
    svc.add_reading(inc, base=51, variance=5, seed=11,
                    when=night + timedelta(minutes=33), note="沟通后复测")
    svc.complete_onsite(
        inc, 3, communication="到场关闭音箱，住户口头答应不再开机",
        result="persistent", arrived_ts=timeutil.ts(night + timedelta(minutes=28)),
        when=night + timedelta(minutes=35))
    # 被投诉方申诉（token 必须匹配 A-02 住客赵一鸣）
    svc.appeal(
        conn.execute("SELECT id FROM onsite_records WHERE incident_id=?",
                     (inc,)).fetchone()["id"],
        token="tok-zhao", note="音箱不是我们开的，是隔壁营地传过来的",
        when=night + timedelta(minutes=50))

    # ---------- 样例旁支：C-01 在篝火晚会期间被投诉 → 豁免 ----------
    svc.submit_complaint(
        reporter_token="tok-wu", reported_site="C-01",
        content="篝火场音乐太响", anonymous=True,
        when=fire_start + timedelta(minutes=20))

    # ---------- 样例旁支：B-01 误报（设备故障 + 无超限读数） ----------
    r3 = svc.submit_complaint(
        reporter_token="tok-zhou", reported_site="B-01",
        content="好像有持续嗡嗡声", anonymous=True, when=night + timedelta(minutes=40))
    inc3 = r3["incident_id"]
    svc.claim_incident(inc3, 2, when=night + timedelta(minutes=42))
    svc.mark_arrived(inc3, 2, when=night + timedelta(minutes=48))
    svc.add_reading(inc3, fault=True, when=night + timedelta(minutes=49))
    svc.add_reading(inc3, base=38, variance=3, seed=3,
                    when=night + timedelta(minutes=52))
    svc.complete_onsite(
        inc3, 3, communication="现场安静，分贝计先故障后复测 38-41dB，未超限",
        result="false_alarm",
        arrived_ts=timeutil.ts(night + timedelta(minutes=48)),
        when=night + timedelta(minutes=54))

    # ---------- 一条已复核结案的历史事件（用于统计） ----------
    old_t = base - timedelta(days=1, hours=2)
    r4 = svc.submit_complaint(
        reporter_token="tok-wu", reported_site="B-03",
        content="昨晚 B-03 大声放电影", anonymous=True, when=old_t)
    inc4 = r4["incident_id"]
    svc.claim_incident(inc4, 2, when=old_t + timedelta(minutes=5))
    svc.mark_arrived(inc4, 2, when=old_t + timedelta(minutes=12))
    svc.add_reading(inc4, base=49, variance=4, seed=5,
                    when=old_t + timedelta(minutes=13))
    rec4 = svc.complete_onsite(
        inc4, 3, communication="提醒后关小，复测达标",
        result="reminded",
        arrived_ts=timeutil.ts(old_t + timedelta(minutes=12)),
        when=old_t + timedelta(minutes=18))
    conn.execute("UPDATE onsite_records SET status='closed', reviewer_id=4, "
                 "review_ts=?, review_decision='rejected', review_note='抽查复核通过' "
                 "WHERE id=?",
                 (timeutil.ts(old_t + timedelta(minutes=40)), rec4))
    conn.execute("UPDATE incidents SET status='closed', closed_ts=?, "
                 "close_reason='提醒后整改，复核结案' WHERE id=?",
                 (timeutil.ts(old_t + timedelta(minutes=40)), inc4))
    conn.commit()

    return svc


if __name__ == "__main__":
    s = reset_and_seed()
    print("样例数据已生成：data/camp.db")
    print("事件列表：")
    for i in s.list_incidents():
        print(f"  #INC-{i['id']:03d} {i['reported_site_code']} "
              f"{i['status']:<14} 合并投诉 {i['merged_count']} 起")
