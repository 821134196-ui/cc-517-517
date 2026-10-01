"""SQLite 持久层：投诉链、现场处置记录、复核与消息留痕。

设计要点
--------
* ``complaints`` 为投诉链：同一事件的多次投诉全部挂在同一个 ``incident_id``
  下，后到的重复投诉标记 ``is_duplicate=1``，绝不覆盖先到投诉。
* 投诉人身份（``reporter_guest_id`` / ``reporter_occupancy_id``）只供内部追责
  使用；被投诉方视图通过服务层脱敏，触发器只负责结构约束。
* 现场记录申诉后必须由值班主管（supervisor）复核才能置为 closed：
  用触发器兜底，服务层再做角色校验。
* 接单竞争用单条条件 UPDATE 抢占，``assigned_officer_id`` 即现场负责人，
  数据库行锁保证只分配一人。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 员工 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    role   TEXT NOT NULL CHECK (role IN ('officer', 'patroller', 'supervisor')),
    phone  TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

-- 营位与相邻关系 -------------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
    code        TEXT PRIMARY KEY,          -- 营位编号，如 A-01
    name        TEXT NOT NULL,
    zone        TEXT NOT NULL,
    x           REAL NOT NULL,             -- 营位图坐标（米）
    y           REAL NOT NULL,
    quiet_start TEXT NOT NULL DEFAULT '22:00',
    quiet_end   TEXT NOT NULL DEFAULT '07:00',
    day_limit   REAL NOT NULL DEFAULT 55,  -- 白天分贝上限
    night_limit REAL NOT NULL DEFAULT 45   -- 夜间安静时段上限
);

CREATE TABLE IF NOT EXISTS adjacency (
    a_code     TEXT NOT NULL REFERENCES sites(code) ON DELETE CASCADE,
    b_code     TEXT NOT NULL REFERENCES sites(code) ON DELETE CASCADE,
    distance_m REAL NOT NULL DEFAULT 8,
    PRIMARY KEY (a_code, b_code),
    CHECK (a_code < b_code)                -- 相邻关系无向，只存一行
);

-- 住客与入住（换营位 = 新开一条 occupancy，旧记录保留） ---------------
CREATE TABLE IF NOT EXISTS guests (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    contact    TEXT,                        -- 手机号/消息账号，仅内部可见
    anon_token TEXT NOT NULL               -- 撤回等自助操作凭证
);

CREATE TABLE IF NOT EXISTS occupancies (
    id         INTEGER PRIMARY KEY,
    guest_id   INTEGER NOT NULL REFERENCES guests(id),
    site_code  TEXT NOT NULL REFERENCES sites(code),
    check_in   TEXT NOT NULL,
    check_out  TEXT,
    active     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_occ_site ON occupancies(site_code, active);

-- 公共活动豁免（campfire 晚会等），scope=global 时全营生效 -------------
CREATE TABLE IF NOT EXISTS exemptions (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL CHECK (scope IN ('site', 'global')),
    site_code  TEXT REFERENCES sites(code),
    title      TEXT NOT NULL,
    start_ts   TEXT NOT NULL,
    end_ts     TEXT NOT NULL,
    created_by INTEGER REFERENCES staff(id),
    note       TEXT
);

-- 事件（一次噪声事件 = 一条主记录，投诉挂在其下） ---------------------
CREATE TABLE IF NOT EXISTS incidents (
    id                  INTEGER PRIMARY KEY,
    reported_site_code  TEXT NOT NULL REFERENCES sites(code), -- 被投诉营位
    noise_kind          TEXT,
    status              TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'dispatched', 'onsite',
                          'pending_review', 'appealed',
                          'closed', 'dismissed', 'withdrawn')),
    quiet               INTEGER NOT NULL,          -- 首次投诉是否落在安静时段
    exemption_id        INTEGER REFERENCES exemptions(id), -- 命中的豁免
    first_complaint_id  INTEGER,
    merged_count        INTEGER NOT NULL DEFAULT 1,-- 合并进来的投诉总数
    assigned_officer_id INTEGER REFERENCES staff(id),     -- 唯一现场负责人
    assigned_ts         TEXT,
    created_ts          TEXT NOT NULL,
    closed_ts           TEXT,
    close_reason        TEXT,
    escalation_level    INTEGER NOT NULL DEFAULT 0
        CHECK (escalation_level BETWEEN 0 AND 4),  -- 0无 1提醒 2警告 3换位 4上报
    suggested_relocation_id INTEGER
);

-- 投诉链 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS complaints (
    id                  INTEGER PRIMARY KEY,
    incident_id         INTEGER NOT NULL REFERENCES incidents(id),
    reporter_guest_id   INTEGER NOT NULL REFERENCES guests(id),
    reporter_site_code  TEXT NOT NULL,   -- 提交时所在营位（快照，换营位也可追溯）
    reported_site_code  TEXT NOT NULL REFERENCES sites(code),
    anonymous           INTEGER NOT NULL DEFAULT 1, -- 对被投诉方匿名
    channel             TEXT NOT NULL DEFAULT '前台',
    content             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'withdrawn')),
    is_duplicate        INTEGER NOT NULL DEFAULT 0,
    created_ts          TEXT NOT NULL,
    withdrawn_ts        TEXT,
    withdraw_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmp_incident ON complaints(incident_id);
CREATE INDEX IF NOT EXISTS idx_cmp_reported ON complaints(reported_site_code, created_ts);

-- 分贝读数流水（一次到场可能多次读数；误报依据） -----------------------
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    ts          TEXT NOT NULL,
    db_value    REAL,                    -- 设备故障时为 NULL
    meter_ok    INTEGER NOT NULL,
    photo_path  TEXT,                    -- 本地快照照片（相对路径）
    note        TEXT
);

-- 现场处置记录 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS onsite_records (
    id               INTEGER PRIMARY KEY,
    incident_id      INTEGER NOT NULL REFERENCES incidents(id),
    patroller_id     INTEGER NOT NULL REFERENCES staff(id),
    arrived_ts       TEXT NOT NULL,
    communication    TEXT NOT NULL,      -- 沟通结果
    result           TEXT NOT NULL CHECK (result IN
                         ('reminded', 'warning', 'false_alarm',
                          'persistent', 'exempt', 'no_source')),
    status           TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'appealed', 'reviewed', 'closed')),
    appeal_ts        TEXT,
    appeal_note      TEXT,
    reviewer_id      INTEGER REFERENCES staff(id),
    review_ts        TEXT,
    review_decision  TEXT CHECK (review_decision IN ('approved', 'rejected', 'followup')),
    review_note      TEXT,
    created_ts       TEXT NOT NULL
);

-- 被申诉的处置记录必须经主管复核才能结案；复核人不能是记录人本人。
-- 普通记录的复核要求由服务层流程保证（所有提醒/警告/持续处置均入复核队列），
-- 误报/豁免/未发现声源类记录可直接关闭，故触发器只拦截 appealed → closed。
CREATE TRIGGER IF NOT EXISTS trg_appeal_review_required
BEFORE UPDATE OF status ON onsite_records
WHEN NEW.status = 'closed' AND OLD.status = 'appealed'
     AND (NEW.reviewer_id IS NULL
          OR NEW.reviewer_id = NEW.patroller_id)
BEGIN
    SELECT RAISE(ABORT, '被申诉的处置记录须由值班主管（非记录人）复核后才能结案');
END;

-- 换位建议 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relocation_suggestions (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    guest_id    INTEGER REFERENCES guests(id),  -- NULL = 建议被投诉方换位
    from_site   TEXT NOT NULL REFERENCES sites(code),
    to_site     TEXT NOT NULL REFERENCES sites(code),
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'declined')),
    created_ts  TEXT NOT NULL
);

-- 模拟消息服务发送留痕 --------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id),
    to_guest_id INTEGER REFERENCES guests(id),
    to_site     TEXT,
    kind        TEXT NOT NULL,   -- dispatch / notify / warning / relocation / withdraw
    channel     TEXT NOT NULL DEFAULT '消息服务(模拟)',
    body        TEXT NOT NULL,
    redacted    INTEGER NOT NULL DEFAULT 0, -- 发给被投诉方时是否隐去投诉人
    sent_ts     TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 1
);
"""


def connect(db_path: str | Path = "data/camp.db") -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False,
                           isolation_level=None)  # 显式事务管理（BEGIN/COMMIT）
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
