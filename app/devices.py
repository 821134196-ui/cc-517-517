"""模拟外部接口：分贝计、消息服务，以及音量读数"照片"本地存储。

均为无网络的本地模拟：
- DecibelMeter.read() 可返回正常读数或设备误报（device_ok=False）。
- MessageService.send() 写入 messages_log，绝不向被投诉方透露投诉人身份。
- save_reading_photo() 用标准库 zlib/struct 生成一张分贝计显示屏风格 PNG。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

from .database import Database

# ---------- 极简 5x7 点阵字体（仅照片渲染需要的字符） ----------
_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "d": ["00001", "00001", "01111", "10001", "10001", "10001", "01111"],
    "b": ["10000", "10000", "11110", "10001", "10001", "10001", "11110"],
    " ": ["00000"] * 7,
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
}


def _png(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    raw = b""
    for row in pixels:
        raw += b"\x00" + b"".join(struct.pack("BBB", *px) for px in row)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _draw_text(pixels, text: str, x0: int, y0: int, scale: int,
               color: tuple[int, int, int]) -> None:
    x = x0
    for ch in text:
        glyph = _FONT.get(ch, _FONT[" "])
        for ry, line in enumerate(glyph):
            for rx, bit in enumerate(line):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            px, py = x + rx * scale + sx, y0 + ry * scale + sy
                            if 0 <= py < len(pixels) and 0 <= px < len(pixels[0]):
                                pixels[py][px] = color
        x += 6 * scale


def save_reading_photo(directory: str | Path, reading_db: float, limit: float,
                       quiet: bool, stamp: str, event_id: int) -> str:
    """渲染 320x180 的分贝计显示屏 PNG，返回相对文件名。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    w, h = 320, 180
    bg = (24, 26, 28)       # 夜间深灰
    fg = (228, 228, 222)    # 浅色正文
    warn = (232, 126, 41)   # 有限的警示橙
    over = reading_db > limit
    bar_color = warn if over else (94, 168, 120)

    pixels = [[bg for _ in range(w)] for _ in range(h)]
    # 边框
    for x in range(w):
        pixels[4][x] = pixels[h - 5][x] = (70, 72, 76)
    for y in range(h):
        pixels[y][4] = pixels[y][w - 5] = (70, 72, 76)

    _draw_text(pixels, f"{reading_db:5.1f}", 18, 20, 3, warn if over else fg)
    _draw_text(pixels, "db", 150, 20, 3, fg)
    _draw_text(pixels, f"lim {limit:.0f}", 210, 30, 2, (150, 152, 156))
    _draw_text(pixels, "night" if quiet else "day  ", 18, 66, 2, (150, 152, 156))
    _draw_text(pixels, stamp[5:16].replace("-", "."), 130, 66, 2, (150, 152, 156))
    if over:
        _draw_text(pixels, "over", 250, 66, 2, warn)

    # 底部量条：0–100 dB
    bx0, bx1, by = 18, w - 18, h - 40
    for x in range(bx0, bx1):
        pixels[by][x] = pixels[by + 1][x] = pixels[by + 2][x] = (60, 62, 66)
    fill = int((bx1 - bx0) * min(max(reading_db, 0), 100) / 100)
    for x in range(bx0, bx0 + fill):
        pixels[by][x] = pixels[by + 1][x] = pixels[by + 2][x] = bar_color
    # 限值刻度
    lx = bx0 + int((bx1 - bx0) * limit / 100)
    for y in range(by - 6, by + 8):
        pixels[y][lx] = warn

    name = f"reading_event{event_id}_{stamp.replace(':', '').replace(' ', '_')}.png"
    (directory / name).write_bytes(_png(w, h, pixels))
    return name


# ---------- 分贝计模拟 ----------
class DecibelMeter:
    """模拟分贝计。force_fault=True 时返回设备误报读数。"""

    def __init__(self, fault_rate: float = 0.0):
        self.fault_rate = fault_rate
        self._calls = 0

    def read(self, ambient: float = 40.0, spike: float = 0.0,
             force_fault: bool = False) -> dict:
        self._calls += 1
        if force_fault or (self.fault_rate and (self._calls % 7 == 0)):
            return {
                "db": 99.9,
                "device_ok": False,
                "note": "设备自检失败：传感器无响应，读数不可采信（疑似误报）",
            }
        # 确定性伪抖动，避免引入随机状态
        jitter = ((self._calls * 13) % 7) - 3
        return {
            "db": round(ambient + spike + jitter * 0.4, 1),
            "device_ok": True,
            "note": "读数正常",
        }


# ---------- 消息服务模拟 ----------
class MessageService:
    """通知只发送处置进展，从不在面向被投诉方的消息里包含投诉人信息。"""

    def __init__(self, db: Database):
        self.db = db

    def send(self, target: str, body: str, channel: str = "sms") -> dict:
        msg = {
            "channel": channel,
            "target": target,
            "body": body,
        }
        self.db.execute(
            "INSERT INTO messages_log(channel, target, body, created_at) "
            "VALUES (?, ?, ?, datetime('now','localtime'))",
            (channel, target, body),
        )
        return msg

    def notify_officer(self, officer_phone: str, event_id: int, site_label: str) -> dict:
        return self.send(
            officer_phone,
            f"【营地处置】事件 #{event_id} 已派单至 {site_label}，请尽快到场记录。",
        )

    def notify_review_needed(self, supervisor_phone: str, event_id: int) -> dict:
        return self.send(
            supervisor_phone,
            f"【复核提醒】事件 #{event_id} 的处置已被申诉，等待值班主管复核结案。",
        )
