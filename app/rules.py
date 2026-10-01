"""领域规则：安静时段边界、事件合并、公共活动豁免、噪声阈值与升级、换位建议。

时间约定：全部使用本地时区 "YYYY-MM-DD HH:MM:SS" 文本，按字典序即可比较。
安静时段允许跨午夜（如 22:00–07:00）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .database import Database, neighbors

TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 分贝阈值（营地自定标准）
DAY_LIMIT_DB = 55.0
NIGHT_LIMIT_DB = 45.0
# 持续超限：安静时段内，间隔 ≤ 此值的两次读数均超限
PERSIST_WINDOW_MIN = 15
# 同一事件合并窗口（分钟）：同被投诉营位、时间接近的投诉合并
MERGE_WINDOW_MIN = 30


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s, TIME_FMT)


def now_str() -> str:
    return datetime.now().strftime(TIME_FMT)


def minutes_between(a: str, b: str) -> float:
    return abs((parse_dt(b) - parse_dt(a)).total_seconds()) / 60.0


def is_quiet_hours(ts: str, quiet_start: str = "22:00", quiet_end: str = "07:00") -> bool:
    """安静时段判定。22:00:00 起算，07:00:00 恰好解除（边界半开 [start, end)）。"""
    t = parse_dt(ts).time()
    sh, sm = map(int, quiet_start.split(":"))
    eh, em = map(int, quiet_end.split(":"))
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    cur = t.hour * 60 + t.minute + t.second / 60.0
    if start_min == end_min:
        return False
    if start_min < end_min:  # 不跨午夜
        return start_min <= cur < end_min
    return cur >= start_min or cur < end_min  # 跨午夜


def db_limit_at(quiet: bool) -> float:
    return NIGHT_LIMIT_DB if quiet else DAY_LIMIT_DB


def active_exemption(db: Database, reported_site_id: int, ts: str) -> dict | None:
    """公共活动豁免：全营地或覆盖该营位，且时间落在豁免区间内。"""
    for ex in db.query("SELECT * FROM exemptions ORDER BY id"):
        if ex["site_id"] is not None and ex["site_id"] != reported_site_id:
            continue
        # 豁免不跨夜设计；允许同日简单区间
        day = parse_dt(ts).strftime("%Y-%m-%d")
        start = f"{day} {ex['start_at']}:00"
        end = f"{day} {ex['end_at']}:00"
        if start <= ts < end:
            return dict(ex)
    return None


def find_mergeable_event(db: Database, reported_site_id: int, ts: str) -> dict | None:
    """查找可合并事件：同被投诉营位、未结案、首次投诉在合并窗口内。

    已撤回/误报关闭的事件不参与合并（那是独立结论）。
    """
    rows = db.query(
        """SELECT * FROM events
           WHERE reported_site_id = ?
             AND status IN ('open', 'assigned', 'resolved', 'appealed')
           ORDER BY id DESC""",
        (reported_site_id,),
    )
    for ev in rows:
        if minutes_between(ev["first_at"], ts) <= MERGE_WINDOW_MIN:
            return dict(ev)
    return None


def readings_over_limit(db: Database, event_id: int) -> list[dict]:
    out = []
    for r in db.query(
        "SELECT * FROM site_records WHERE event_id = ? AND device_ok = 1 ORDER BY reading_at",
        (event_id,),
    ):
        limit = db_limit_at(bool(r["quiet"]))
        out.append({**dict(r), "limit": limit, "exceeds": r["db_reading"] > limit})
    return out


def is_persistent(db: Database, event_id: int) -> bool:
    """持续超限：安静时段内存在两次有效（非误报设备）读数间隔 ≤ 窗口且均超夜间限值。"""
    night = [r for r in readings_over_limit(db, event_id) if r["quiet"] and r["exceeds"]]
    for i in range(1, len(night)):
        gap = minutes_between(night[i - 1]["reading_at"], night[i]["reading_at"])
        if gap <= PERSIST_WINDOW_MIN:
            return True
    return False


def recommend_moves(db: Database, reported_site_id: int, limit: int = 3) -> list[dict]:
    """换位建议：推荐与噪声源距离（营位图跳数）最远的空营位。

    距离 1（相邻）的营位不推荐。返回 [{site, hops, reason}]。
    """
    # BFS 跳数
    dist: dict[int, int] = {reported_site_id: 0}
    frontier = [reported_site_id]
    hops = 1
    while frontier:
        nxt = []
        for s in frontier:
            for n in neighbors(db, s):
                if n not in dist:
                    dist[n] = hops
                    nxt.append(n)
        frontier = nxt
        hops += 1

    occupied = {
        r["site_id"]
        for r in db.query("SELECT site_id FROM guests WHERE site_id IS NOT NULL")
    }
    suggestions = []
    for site in db.query("SELECT * FROM sites ORDER BY id"):
        sid = site["id"]
        d = dist.get(sid)
        if sid == reported_site_id or d is None or d <= 1:
            continue
        if sid in occupied:
            continue
        suggestions.append(
            {
                "site": dict(site),
                "hops": d,
                "reason": f"距被投诉营位 {d} 跳（不相邻），营位当前空闲，适合临时换位",
            }
        )
    suggestions.sort(key=lambda x: (-x["hops"], x["site"]["id"]))
    return suggestions[:limit]
