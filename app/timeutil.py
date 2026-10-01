"""时间与安静时段判定（HH:MM 边界采用左闭右开，22:00:00 起算夜间）。"""
from __future__ import annotations

from datetime import datetime, time, timedelta

FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now()


def ts(dt: datetime | None = None) -> str:
    return (dt or now()).strftime(FMT)


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet(dt: datetime, quiet_start: str, quiet_end: str) -> bool:
    """是否处于安静时段。支持跨午夜（如 22:00–07:00）。

    边界规则：左闭右开 [start, end)。22:00:00 属于安静时段，
    07:00:00 整点已离开安静时段。
    """
    t = dt.time()
    s, e = parse_hhmm(quiet_start), parse_hhmm(quiet_end)
    if s <= e:                       # 不跨午夜，如 13:00–15:00
        return s <= t < e
    return t >= s or t < e          # 跨午夜


def applicable_limit(dt: datetime, site) -> tuple[float, bool]:
    """返回 (适用分贝上限, 是否夜间安静时段)。"""
    quiet = in_quiet(dt, site["quiet_start"], site["quiet_end"])
    return (site["night_limit"] if quiet else site["day_limit"]), quiet


def active_exemption(conn, dt: datetime, site_code: str):
    """查找该时刻适用于该营位的公共活动豁免（global 或指定营位）。"""
    cur = conn.execute(
        """
        SELECT * FROM exemptions
        WHERE ? BETWEEN start_ts AND end_ts
          AND (scope = 'global' OR site_code = ?)
        ORDER BY scope DESC LIMIT 1
        """,
        (ts(dt), site_code),
    )
    return cur.fetchone()
