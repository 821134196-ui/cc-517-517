"""命令行入口：python3 -m app.main [--reset] [--port 8000] [--host 127.0.0.1]

--reset 时删除旧库并重新生成重复投诉合并等演示样例。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .database import Database
from .seed import build_camp, seed_demo
from .server import run
from .services import CampsiteService


def main() -> None:
    ap = argparse.ArgumentParser(description="营地噪声投诉处置系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/camp.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reset", action="store_true", help="清空旧数据并生成演示样例")
    ap.add_argument("--no-seed", action="store_true", help="不写入演示数据")
    args = ap.parse_args()

    if args.reset and Path(args.db).exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(args.db + suffix)
            if p.exists():
                p.unlink()
        print(f"已重置数据库：{args.db}")

    # 先建库 + 种子（保证“启动即有重复投诉样例”）
    db = Database(args.db)
    svc = CampsiteService(db, args.photos)
    build_camp(db)
    if not args.no_seed:
        summary = seed_demo(svc)
        if summary:
            print("已生成演示样例：")
            print(f"  · 事件 #{summary['merged']}：3 条投诉合并（含 2 条匿名）")
            print(f"  · 事件 #{summary['persistent']}：持续超限升级，已申诉待主管复核")
            print(f"  · 事件 #{summary['false_alarm']}：分贝计设备误报")
            print(f"  · 事件 #{summary['withdrawn']}：投诉撤回")
            print(f"  · 事件 #{summary['exempt']}：公共活动豁免归档")
            print(f"  · 事件 #{summary['open']}：新投诉待接单（可演示并发竞争）")

    run(args.host, args.port, args.db, args.photos, seed=False)


if __name__ == "__main__":
    main()
