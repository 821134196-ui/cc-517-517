"""模拟分贝计：读数 + 本地快照照片（手写 PNG，零依赖）。

真实部署时替换为驱动同一接口的硬件适配层即可：
    meter.read(site_code) -> Reading(db, photo_path)
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import timeutil

# ---- 极简 PNG 编码（RGB，逐行） ----------------------------------------

def _png(width: int, height: int, rows: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _canvas(w: int, h: int, bg: tuple[int, int, int]):
    return [bytearray(bg * w) for _ in range(h)]


def _rect(rows, x0, y0, x1, y1, color):
    for y in range(max(0, y0), min(len(rows), y1)):
        row = rows[y]
        for x in range(max(0, x0), min(len(row) // 3, x1)):
            row[3 * x:3 * x + 3] = bytes(color)


def _text(rows, x, y, text: str, color, scale: int = 2):
    """5x7 点阵字体，仅含本界面用到的字符。"""
    FONT = {
        "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
        "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
        "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
        "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
        "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
        "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
        "6": ["01110", "10000", "11110", "10001", "10001", "10001", "01110"],
        "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
        "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
        "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
        ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
        ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
        "-": ["00000", "00000", "00000", "01110", "00000", "00000", "00000"],
        "d": ["00000", "00000", "01101", "01001", "01001", "01001", "01110"],
        "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
        "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
        "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
        "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
        "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
        "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
        "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
        "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
        "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
        "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
        "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
        "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
        "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
        "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
        " ": ["00000"] * 7,
        "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
        "?": ["01110", "10001", "00001", "00110", "00100", "00000", "00100"],
        "!": ["00100", "00100", "00100", "00100", "00100", "00000", "00100"],
        "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    }
    cx = x
    for ch in text:
        glyph = FONT.get(ch, FONT["?"])
        for ry, rowbits in enumerate(glyph):
            for rx, bit in enumerate(rowbits):
                if bit == "1":
                    _rect(rows, cx + rx * scale, y + ry * scale,
                          cx + (rx + 1) * scale, y + (ry + 1) * scale, color)
        cx += 6 * scale


@dataclass
class MeterReading:
    db_value: float | None
    meter_ok: bool
    photo_path: str
    note: str


# 夜间深灰底 / 浅色正文 / 警示橙，与 Web 界面一致
_BG = (28, 30, 34)
_PANEL = (40, 43, 48)
_TEXT = (229, 231, 235)
_ORANGE = (235, 130, 48)
_GREEN = (110, 170, 110)
_RED = (200, 80, 70)


class MockSoundMeter:
    """模拟分贝计。

    fault=True 时模拟设备故障：返回 NULL 读数并在快照上标注 FAULT，
    此时不能据此判定噪声超限（误报规则）。
    用 seed 让脚本生成的样例可复现。
    """

    def __init__(self, photo_dir: str | Path = "data/readings"):
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def read(self, site_code: str, *, fault: bool = False,
             base: float = 42.0, variance: float = 6.0, seed: int | None = None,
             when=None) -> MeterReading:
        self._seq += 1
        if fault:
            value, ok, note = None, False, "设备故障：无有效读数"
        else:
            if seed is not None:
                # 简单确定性伪随机
                v = ((seed * 1103515245 + self._seq * 12345) & 0x7fffffff) / 0x7fffffff
            else:
                import random
                v = random.random()
            value = round(base + (v - 0.3) * variance, 1)
            ok, note = True, "正常读数"

        dt = when or timeutil.now()
        stamp = dt.strftime("%Y%m%d_%H%M%S")
        import random
        nonce = f"{random.randrange(0x100000):06x}"
        fname = f"meter_{site_code.replace('-', '')}_{stamp}_{nonce}.png"
        path = self.photo_dir / fname
        path.write_bytes(self._snapshot_png(site_code, value, ok, dt))
        return MeterReading(value, ok, f"data/readings/{fname}", note)

    def _snapshot_png(self, site_code, value, ok, dt) -> bytes:
        W, H = 320, 120
        rows = _canvas(W, H, _BG)
        _rect(rows, 8, 8, W - 8, H - 8, _PANEL)
        _text(rows, 16, 16, "SOUND METER", _TEXT)
        _text(rows, 16, 38, site_code, _TEXT)
        _text(rows, 120, 38, dt.strftime("%Y-%m-%d %H:%M"), _TEXT)
        if ok:
            over = value >= 50
            _text(rows, 16, 66, f"{value:.1f} dB", _ORANGE if over else _GREEN, scale=3)
        else:
            _text(rows, 16, 66, "FAULT", _RED, scale=3)
        _text(rows, 16, 102, "CAMP NOISE MONITOR", _TEXT)
        return _png(W, H, rows)
