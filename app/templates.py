"""服务端渲染模板：夜间深灰主题，浅色正文，警示橙仅用于告警状态。"""
from __future__ import annotations

import html

# 夜间调色板（低亮度可读）
BG = "#17181a"
PANEL = "#1f2124"
PANEL_2 = "#25282c"
BORDER = "#31343a"
TEXT = "#e7e7e0"
MUTED = "#9aa0a6"
BLUE = "#3987e5"
GREEN = "#2faf78"
ORANGE = "#e87e29"   # 有限的警示橙
YELLOW = "#c9a13c"
RED = "#e06464"

STATUS_STYLE = {
    "open":       (ORANGE, "●"),
    "assigned":   (BLUE, "◉"),
    "resolved":   (GREEN, "✓"),
    "appealed":   (YELLOW, "⚠"),
    "closed":     (MUTED, "■"),
    "withdrawn":  (MUTED, "↩"),
    "false_alarm":(MUTED, "◇"),
}
STATUS_TEXT = {
    "open": "待接单", "assigned": "处置中", "resolved": "已处置",
    "appealed": "申诉待复核", "closed": "已结案", "withdrawn": "已撤回",
    "false_alarm": "设备误报",
}
DISPOSITION_TEXT = {
    "escalated_persistent": "持续超限·升级",
    "within_limit": "到场未超限",
    "device_false_alarm": "设备误报",
    "review_false_alarm": "复核改判误报",
    "public_exempt": "公共活动豁免",
    "withdrawn": "投诉撤回",
}


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def base(title: str, body: str, role: str = "officer") -> str:
    role_label = {"officer": "巡查员视图", "supervisor": "值班主管视图",
                  "guest": "住客视图"}.get(role, "巡查员视图")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 营地噪声处置</title>
<style>
:root {{
  --bg:{BG}; --panel:{PANEL}; --panel2:{PANEL_2}; --border:{BORDER};
  --text:{TEXT}; --muted:{MUTED}; --blue:{BLUE}; --green:{GREEN};
  --orange:{ORANGE}; --yellow:{YELLOW}; --red:{RED};
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--bg); color:var(--text);
  font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}}
a {{ color:var(--blue); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
header {{
  display:flex; align-items:center; gap:18px; padding:12px 22px;
  background:var(--panel); border-bottom:1px solid var(--border);
  position:sticky; top:0; z-index:10;
}}
header h1 {{ font-size:16px; margin:0; font-weight:600; }}
header nav {{ display:flex; gap:14px; }}
header nav a {{ color:var(--muted); }}
header nav a.active {{ color:var(--text); }}
.role {{ margin-left:auto; display:flex; gap:6px; align-items:center; color:var(--muted); }}
.role select {{ background:var(--panel2); color:var(--text); border:1px solid var(--border);
  border-radius:6px; padding:3px 8px; }}
main {{ max-width:1180px; margin:0 auto; padding:22px; }}
.grid {{ display:grid; gap:18px; }}
.cols-2 {{ grid-template-columns: 1fr 1fr; }}
@media (max-width:900px) {{ .cols-2 {{ grid-template-columns:1fr; }} }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px 18px; }}
.card h2 {{ font-size:14px; margin:0 0 12px; color:var(--text); font-weight:600; }}
.muted {{ color:var(--muted); }}
.small {{ font-size:12px; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); vertical-align:top; }}
th {{ color:var(--muted); font-weight:500; font-size:12px; }}
tr:last-child td {{ border-bottom:none; }}
.badge {{ display:inline-flex; align-items:center; gap:5px; padding:1px 9px;
  border-radius:20px; font-size:12px; border:1px solid var(--border); white-space:nowrap; }}
.btn {{ display:inline-block; background:var(--panel2); color:var(--text);
  border:1px solid var(--border); border-radius:7px; padding:6px 14px; cursor:pointer;
  font-size:13px; }}
.btn:hover {{ border-color:var(--muted); text-decoration:none; }}
.btn-primary {{ background:var(--blue); border-color:var(--blue); color:#fff; }}
.btn-warn {{ background:var(--orange); border-color:var(--orange); color:#1a1208; }}
.btn:disabled {{ opacity:.45; cursor:not-allowed; }}
input,select,textarea {{
  background:var(--panel2); color:var(--text); border:1px solid var(--border);
  border-radius:7px; padding:7px 10px; font:inherit; width:100%;
}}
textarea {{ min-height:64px; resize:vertical; }}
label {{ display:block; margin:10px 0 4px; color:var(--muted); font-size:12px; }}
.row {{ display:flex; gap:10px; align-items:center; }}
.row > * {{ flex:1; }}
.checkbox {{ display:flex; align-items:center; gap:8px; margin:10px 0; }}
.checkbox input {{ width:auto; }}
.flash {{ padding:9px 14px; border-radius:8px; margin-bottom:14px; font-size:13px; }}
.flash-ok {{ background:rgba(47,175,120,.13); border:1px solid rgba(47,175,120,.4); }}
.flash-err {{ background:rgba(224,100,100,.12); border:1px solid rgba(224,100,100,.4); }}
/* 时间线：发生 → 到场 → 复核 */
.timeline {{ list-style:none; margin:0; padding:0; }}
.timeline li {{ position:relative; padding:0 0 16px 30px; border-left:2px solid var(--border);
  margin-left:8px; }}
.timeline li:last-child {{ border-left-color:transparent; padding-bottom:0; }}
.timeline .dot {{ position:absolute; left:-8px; top:2px; width:14px; height:14px;
  border-radius:50%; background:var(--panel2); border:2px solid var(--muted); }}
.timeline .ph-发生 .dot {{ border-color:var(--blue); }}
.timeline .ph-到场 .dot {{ border-color:var(--green); }}
.timeline .ph-复核 .dot {{ border-color:var(--yellow); }}
.timeline .ph-申诉 .dot {{ border-color:var(--orange); }}
.phase-tag {{ font-size:11px; color:var(--muted); border:1px solid var(--border);
  border-radius:4px; padding:0 6px; margin-right:8px; }}
.stat-tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
.tile {{ background:var(--panel2); border:1px solid var(--border); border-radius:10px; padding:14px; }}
.tile .num {{ font-size:26px; font-weight:600; }}
.tile .lbl {{ color:var(--muted); font-size:12px; }}
.bar-row {{ display:grid; grid-template-columns:110px 1fr 42px; gap:10px; align-items:center;
  margin:7px 0; font-size:12px; }}
.bar {{ height:14px; background:var(--panel2); border-radius:4px; overflow:hidden; }}
.bar > span {{ display:block; height:100%; border-radius:4px; }}
.site-cell {{ display:flex; gap:8px; align-items:baseline; }}
.photo {{ border:1px solid var(--border); border-radius:8px; margin-top:8px; max-width:300px; }}
.hint {{ font-size:12px; color:var(--muted); margin-top:6px; }}
.kv {{ display:grid; grid-template-columns:92px 1fr; gap:4px 12px; font-size:13px; }}
.kv dt {{ color:var(--muted); }}
.kv dd {{ margin:0; }}
</style>
</head>
<body>
<header>
  <h1>🏕️ 营地噪声处置台</h1>
  <nav>
    <a href="/" class="{'active' if title=='营位图' else ''}">营位图</a>
    <a href="/events" class="{'active' if title=='事件受理' else ''}">事件受理</a>
    <a href="/review" class="{'active' if title=='复核' else ''}">复核</a>
    <a href="/stats" class="{'active' if title=='统计' else ''}">统计</a>
  </nav>
  <form class="role" method="post" action="/role">
    <span>视角</span>
    <select name="role" onchange="this.form.submit()">
      <option value="officer" {'selected' if role=='officer' else ''}>巡查员</option>
      <option value="supervisor" {'selected' if role=='supervisor' else ''}>值班主管</option>
      <option value="guest" {'selected' if role=='guest' else ''}>住客</option>
    </select>
  </form>
</header>
<main>
{body}
</main>
</body></html>"""


def status_badge(status: str) -> str:
    color, icon = STATUS_STYLE.get(status, (MUTED, "·"))
    text = STATUS_TEXT.get(status, status)
    return (f'<span class="badge" style="color:{color};border-color:{color}55">'
            f'<span>{icon}</span>{esc(text)}</span>')


def flash(ok: str | None = None, err: str | None = None) -> str:
    out = ""
    if ok:
        out += f'<div class="flash flash-ok">{esc(ok)}</div>'
    if err:
        out += f'<div class="flash flash-err">{esc(err)}</div>'
    return out
