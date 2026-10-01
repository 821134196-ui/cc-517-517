"""HTTP 界面（标准库 http.server）：营位图 / 事件受理 / 现场记录 /
换位建议 / 复核统计。

页面：
  /map                营位图（编号 + 相邻关系 + 实时状态）
  /incidents          事件受理列表 + 提交投诉
  /incidents/<id>     投诉详情：发生→到场→复核时间线、读数照片、处置/申诉/复核
  /incidents/<id>?view=respondent  被投诉方视图（匿名隔离自检入口）
  /relocate           换位建议
  /review             复核队列与统计
"""
from __future__ import annotations

import html
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db
from .meter import MockSoundMeter
from .services import CampService, ServiceError

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = "data/camp.db"
PHOTO_DIR = "data/readings"


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


STATUS_TEXT = {
    "open": "待派单", "dispatched": "已派单/到场中", "onsite": "现场处置中",
    "pending_review": "待主管复核", "appealed": "被申诉",
    "closed": "已结案", "dismissed": "已驳回/撤销", "withdrawn": "已撤回",
}


# ---------------------------------------------------------------------- #
# 页面片段
# ---------------------------------------------------------------------- #
def page(title: str, nav_active: str, body: str, flash: str = "") -> bytes:
    navs = [("/map", "营位图"), ("/incidents", "事件受理"),
            ("/relocate", "换位建议"), ("/review", "复核统计")]
    nav = "".join(
        f'<a href="{u}"{" class=active" if u==nav_active else ""}>{n}</a>'
        for u, n in navs)
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html><html lang=zh><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(title)} · 营地噪声处置</title>
<link rel=stylesheet href=/static/style.css></head><body>
<header><h1>营地噪声<span class=accent>·</span>投诉处置台</h1><nav>{nav}</nav></header>
<main>{flash_html}{body}</main></body></html>""".encode()


def badge(status: str) -> str:
    return f'<span class="badge {esc(status)}">{esc(STATUS_TEXT.get(status, status))}</span>'


# ---------------------------------------------------------------------- #
# 营位图
# ---------------------------------------------------------------------- #
def view_map(svc: CampService, qs) -> bytes:
    data = svc.map_data()
    impact = None
    if qs.get("incident"):
        try:
            impact = svc.impact_scope(int(qs["incident"][0]))
        except (ServiceError, ValueError):
            impact = None
    affected = set(impact["affected_codes"]) if impact else set()

    edges_svg = []
    pos = {s["code"]: (s["x"], s["y"]) for s in data["sites"]}
    for e in data["edges"]:
        x1, y1 = pos[e["a"]]; x2, y2 = pos[e["b"]]
        hot = impact and e["a"] in affected and e["b"] in affected
        edges_svg.append(
            f'<line class="edge" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{"#eb8230" if hot else "#3d424b"}" stroke-width="{3 if hot else 2}"/>')
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        edges_svg.append(f'<text class=edge-label x="{mx}" y="{my-3}">{e["d"]}m</text>')

    site_svg = []
    for s in data["sites"]:
        cls = "site"
        if s["active_incident"]:
            cls += " has-incident"
        if s["quiet_now"]:
            cls += " quiet"
        if impact and s["code"] in affected:
            cls += " has-incident"
        x, y = s["x"] - 52, s["y"] - 26
        ring = ""
        if impact and s["code"] in affected:
            ring = f'<rect x="{x-5}" y="{y-5}" width="114" height="62" rx="10" fill="none" stroke="#eb8230" stroke-dasharray="4 3"/>'
        sub = s["occupied_by"] or "空位"
        inc = f'<tspan class=sub>#{s["active_incident"]:03d} {esc(STATUS_TEXT.get(s["incident_status"], ""))}</tspan>' if s["active_incident"] else ""
        qm = " 🌙" if s["quiet_now"] else ""
        site_svg.append(
            f'''<g class="{cls}" onclick="location.href='/incidents?site={esc(s['code'])}'">
              {ring}<rect x="{x}" y="{y}" width="104" height="52" rx="8"/>
              <text class=code x="{x+8}" y="{y+19}">{esc(s["code"])}{qm}</text>
              <text class=sub x="{x+8}" y="{y+34}">{esc(sub)} {inc}</text>
            </g>''')

    impact_note = ""
    if impact:
        nbr = "、".join(n["code"] for n in impact["neighbors"]) or "（无相邻营位）"
        impact_note = (f'<div class=flash>影响范围（事件 #{int(qs["incident"][0]):03d}）：'
                       f'中心营位 <b>{esc(impact["center"]["code"])}</b>，'
                       f'相邻营位 {esc(nbr)}。相邻线以橙色标出。</div>')

    body = f"""
<h2>营位图 <span class="muted small">编号 · 相邻关系 · 安静时段 🌙 · 活跃事件</span></h2>
{impact_note}
<div class=map-wrap>
  <svg class=map viewBox="0 0 540 430" role=img aria-label="营位分布图">
    {''.join(edges_svg)}{''.join(site_svg)}
  </svg>
</div>
<div class=legend>
  <span><i style="background:#2f333a;border:1px solid #4a505a"></i>普通营位</span>
  <span><i style="background:rgba(235,130,48,.15);border:1px solid #eb8230"></i>有活跃噪声事件</span>
  <span><i style="background:#2f333a;border:1px solid #4a6488"></i>处于安静时段</span>
  <span class=muted>边的数字为营位间距（米），相邻营位构成投诉影响范围</span>
</div>
<h3>营位与安静时段配置</h3>
<div class=panel><table><tr><th>编号</th><th>名称</th><th>分区</th><th>安静时段</th>
<th>夜间/白天上限</th><th>相邻营位</th><th>住客</th></tr>
{''.join(_site_row(svc, s) for s in data['sites'])}</table></div>
<p class="muted small">图中时间基准：{esc(data['now'])}（安静时段 22:00–07:00，左闭右开）</p>"""
    return page("营位图", "/map", body)


def _site_row(svc, s):
    nbrs = "、".join(
        f'{n["code"]}<span class=muted>({n["distance_m"]:g}m)</span>'
        for n in svc.neighbors(s["code"]))
    return (f'<tr><td><b>{esc(s["code"])}</b></td><td>{esc(s["name"])}</td>'
            f'<td>{esc(s["zone"])}</td>'
            f'<td>{"🌙 " if s["quiet_now"] else ""}{esc(s["quiet_start"])}–{esc(s["quiet_end"])}</td>'
            f'<td>{s["night_limit"]:g} / {s["day_limit"]:g} dB</td>'
            f'<td>{nbrs}</td><td>{esc(s["occupied_by"] or "—")}</td></tr>')


# ---------------------------------------------------------------------- #
# 事件受理
# ---------------------------------------------------------------------- #
def view_incidents(svc: CampService, qs) -> bytes:
    staff = svc.conn.execute("SELECT * FROM staff ORDER BY id").fetchall()
    guests = svc.conn.execute(
        "SELECT g.*, o.site_code FROM guests g JOIN occupancies o "
        "ON o.guest_id=g.id AND o.active=1 ORDER BY g.id").fetchall()
    sites = svc.conn.execute("SELECT code,name FROM sites ORDER BY code").fetchall()
    preselect = qs.get("site", [""])[0]

    guest_opts = "".join(
        f'<option value="{esc(g["anon_token"])}">{esc(g["name"])} '
        f'（{esc(g["site_code"])}，凭证 {esc(g["anon_token"])}）</option>'
        for g in guests)
    site_opts = "".join(
        f'<option value="{esc(s["code"])}"{" selected" if s["code"]==preselect else ""}>'
        f'{esc(s["code"])} {esc(s["name"])}</option>' for s in sites)

    rows = []
    for i in svc.list_incidents():
        quiet = '<span class="badge quiet">安静时段</span>' if i["quiet"] else ""
        dup = f'合并 {i["merged_count"]} 起' if i["merged_count"] > 1 else "单起"
        rows.append(
            f'<tr><td><a href="/incidents/{i["id"]}">#INC-{i["id"]:03d}</a></td>'
            f'<td><b>{esc(i["reported_site_code"])}</b> {esc(i["site_name"])}</td>'
            f'<td>{badge(i["status"])}</td><td>{quiet}</td>'
            f'<td>{esc(i["escalation_level"])} 级</td><td>{dup}</td>'
            f'<td>{esc(i.get("officer_name") or "—")}</td>'
            f'<td class="small muted">{esc(i["created_ts"])}</td></tr>')

    body = f"""
<h2>事件受理</h2>
<div class="grid two">
  <div class=panel>
    <h3>进行中 / 历史事件</h3>
    <table><tr><th>事件</th><th>被投诉营位</th><th>状态</th><th>时段</th>
    <th>升级</th><th>投诉链</th><th>现场负责人</th><th>首诉时间</th></tr>
    {''.join(rows)}</table>
  </div>
  <div class=panel>
    <h3>提交噪声投诉</h3>
    <form method=post action=/api/complaints>
      <label>投诉住客（身份仅内部可见）</label>
      <select name=reporter_token>{guest_opts}</select>
      <label>被投诉营位</label><select name=reported_site>{site_opts}</select>
      <label>情况描述</label><textarea name=content placeholder="时间、声音类型、持续情况"></textarea>
      <div class=row>
        <div><label>渠道</label>
          <select name=channel><option>前台电话</option><option>小程序</option><option>前台</option></select></div>
        <div><label>对被投诉方</label>
          <select name=anonymous><option value=1 selected>匿名展示</option>
          <option value=0>实名展示</option></select></div>
      </div>
      <button type=submit>提交投诉（自动合并同事件）</button>
    </form>
    <hr>
    <h3>撤回投诉</h3>
    <form method=post action=/api/withdraw class=row>
      <div><label>投诉编号</label><input name=complaint_id required placeholder="如 2"></div>
      <div><label>住客凭证</label><input name=token required placeholder="tok-xxxx"></div>
      <div style="flex:2"><label>原因</label><input name=reason placeholder="选填"></div>
      <button class=ghost type=submit>撤回</button>
    </form>
  </div>
</div>"""
    return page("事件受理", "/incidents", body)


# ---------------------------------------------------------------------- #
# 投诉详情
# ---------------------------------------------------------------------- #
def view_detail(svc: CampService, incident_id: int, qs) -> bytes:
    viewer = "respondent" if qs.get("view", [""])[0] == "respondent" else "staff"
    d = svc.incident_detail(incident_id, viewer=viewer)
    inc, site = d["incident"], d["site"]
    staff = svc.conn.execute("SELECT * FROM staff ORDER BY id").fetchall()

    view_toggle = (
        f'<a href="/incidents/{incident_id}">内部视图</a> · '
        f'<a href="/incidents/{incident_id}?view=respondent"><b>被投诉方视图（匿名隔离自检）</b></a>'
        if viewer == "respondent" else
        f'<a href="/incidents/{incident_id}?view=respondent">被投诉方视图（匿名隔离自检）</a>')

    # 投诉链表
    cmp_rows = []
    for c in d["complaints"]:
        anon = '<span class="badge">匿名</span>' if c["anonymous"] else '<span class="badge quiet">实名</span>'
        dup = '<span class="badge open">重复并入</span>' if c["is_duplicate"] else ""
        st = '<span class="muted">已撤回</span>' if c["status"] == "withdrawn" else ""
        contact = f'<div class="small muted">{esc(c["contact"])}</div>' if c.get("contact") else ""
        cmp_rows.append(
            f'<tr><td>#{c["id"]}</td><td>{esc(c["reporter"])} {anon} {dup}</td>'
            f'<td>{esc(c["reporter_site"])}{contact}</td>'
            f'<td>{esc(c["content"])} {st}</td>'
            f'<td class="small muted">{esc(c["created_ts"])}</td></tr>')

    # 时间线
    tl = []
    for it in d["timeline"]:
        pcls = {"1-发生": "p1", "2-到场": "p2", "3-复核": "p3"}[it["phase"]]
        tl.append(f'<li class={pcls}><span class=when>{esc(it["ts"])}</span>'
                  f'<span class="phase">{esc(it["phase"])}</span>'
                  f'<span class=title>{esc(it["title"])}</span>'
                  f'<div class=detail>{esc(it["detail"])}</div></li>')

    # 读数与照片
    rd = []
    for r in d["readings"]:
        if r["meter_ok"]:
            limit = site["night_limit"] if inc["quiet"] else site["day_limit"]
            over = r["db_value"] > limit
            val = f'<b class="{"red" if over else "green"}">{r["db_value"]} dB</b>'
        else:
            val = '<b class=red>设备故障·读数无效</b>'
        rd.append(f'<tr><td class=small>{esc(r["ts"])}</td><td>{val}</td>'
                  f'<td class=small muted>{esc(r["note"])}</td>'
                  f'<td><a href="/{esc(r["photo_path"])}" target=_blank>'
                  f'<img class=photo-thumb width=200 src="/{esc(r["photo_path"])}"></a></td></tr>')

    # 现场记录 + 申诉/复核
    rec_html = []
    for r in d["records"]:
        review = ""
        if r["review_ts"]:
            review = (f'<div class="small">复核：{esc(r.get("reviewer_name"))} '
                      f'于 {esc(r["review_ts"])} —— '
                      f'<b>{esc(CampService._decision_text(r["review_decision"]))}</b>'
                      f' {esc(r["review_note"] or "")}</div>')
        appeal = ""
        if r["appeal_ts"]:
            appeal = f'<div class="small orange">申诉（{esc(r["appeal_ts"])}）：{esc(r["appeal_note"])}</div>'
        rec_html.append(
            f'<div class=panel style="margin-bottom:10px">'
            f'<div><b>{esc(CampService._result_text(r["result"]))}</b> {badge(r["status"])}</div>'
            f'<div class=small muted>到场 {esc(r["arrived_ts"])} · '
            f'巡查员 {esc(r["patroller_name"])}</div>'
            f'<div>{esc(r["communication"])}</div>{appeal}{review}</div>')

    # 操作区
    actions = []
    occ = svc.occupancy_at(inc["reported_site_code"])
    respondent_token = occ["anon_token"] if occ else ""
    if inc["status"] == "open":
        opts = "".join(f'<option value={s["id"]}>{esc(s["name"])}（{s["role"]}）</option>'
                       for s in staff if s["role"] in ("officer", "patroller"))
        actions.append(f"""
          <h3>值班员接单（竞争：仅一人成为现场负责人）</h3>
          <form method=post action=/api/claim class=row>
            <input type=hidden name=incident_id value={incident_id}>
            <div style="flex:2"><label>接单值班员</label><select name=officer_id>{opts}</select></div>
            <button type=submit>接单</button></form>""")
    if inc["status"] in ("dispatched", "onsite"):
        opts = "".join(f'<option value={s["id"]}>{esc(s["name"])}</option>'
                       for s in staff if s["role"] in ("officer", "patroller"))
        actions.append(f"""
          <h3>现场记录（到场时间 · 分贝读数 · 沟通结果）</h3>
          <form method=post action=/api/reading class=row>
            <input type=hidden name=incident_id value={incident_id}>
            <div><label>读数类型</label><select name=fault>
              <option value=0>正常读数</option><option value=1>设备故障（无效）</option></select></div>
            <div><label>基准分贝</label><input name=base value=48></div>
            <div><label>随机种子</label><input name=seed value=1></div>
            <button class=ghost type=submit>采集读数（生成照片）</button>
          </form>
          <form method=post action=/api/onsite>
            <input type=hidden name=incident_id value={incident_id}>
            <div class=row>
              <div style="flex:2"><label>到场巡查员</label><select name=patroller_id>{opts}</select></div>
              <div><label>到场时间</label><input name=arrived_ts placeholder="留空=现在" value=""></div>
            </div>
            <label>处置结果</label>
            <select name=result>
              <option value=reminded>口头提醒（提醒后达标→结案）</option>
              <option value=warning>书面警告（持续噪声，进入复核）</option>
              <option value=persistent>持续超限（升级，自动换位建议）</option>
              <option value=false_alarm>误报（读数未超限/设备故障）</option>
              <option value=exempt>公共活动豁免</option>
              <option value=no_source>未发现噪声源</option>
            </select>
            <label>沟通结果</label><textarea name=communication required></textarea>
            <button type=submit>提交现场记录（巡查员不能自行结案）</button>
          </form>""")
    # 复核队列中的记录：主管复核
    reviewable = [r for r in d["records"] if r["status"] in ("submitted", "appealed")]
    if reviewable:
        sups = "".join(f'<option value={s["id"]}>{esc(s["name"])}</option>'
                       for s in staff if s["role"] == "supervisor")
        items = "".join(f'<option value={r["id"]}>记录 #{r["id"]} · '
                        f'{esc(CampService._result_text(r["result"]))} · {esc(r["status"])}</option>'
                        for r in reviewable)
        actions.append(f"""
          <h3>值班主管复核结案</h3>
          <form method=post action=/api/review class=row>
            <input type=hidden name=incident_id value={incident_id}>
            <div style="flex:2"><label>待复核记录</label><select name=record_id>{items}</select></div>
            <div><label>主管</label><select name=supervisor_id>{sups}</select></div>
            <div><label>决定</label><select name=decision>
              <option value=rejected>维持处置并结案</option>
              <option value=followup>退回继续跟进</option>
              <option value=approved>撤销原处置（申诉/异议成立）</option></select></div>
            <div style="flex:2"><label>复核意见</label><input name=note></div>
            <button type=submit>复核</button>
          </form>
          <p class="muted small">规则：被申诉记录必须由主管（不得是记录人本人）复核；
          巡查员本人无法关闭自己的处置记录。</p>""")

    # 被投诉方对已提交、尚未结案/复核的处置记录申诉
    appeallable = [r for r in d["records"] if r["status"] in ("submitted",)]
    if occ and appeallable and inc["status"] in ("pending_review", "appealed"):
        opts = "".join(f'<option value={r["id"]}>记录 #{r["id"]} · '
                       f'{esc(CampService._result_text(r["result"]))}</option>'
                       for r in appeallable)
        actions.append(f"""
          <form method=post action=/api/appeal>
            <h3>被投诉方申诉</h3>
            <input type=hidden name=token value="{esc(respondent_token)}">
            <label>申诉针对的记录</label><select name=record_id>{opts}</select>
            <label>申诉理由</label><textarea name=note required></textarea>
            <button class=ghost type=submit>提交申诉（进入主管复核）</button>
          </form>""")

    # 换位建议
    reloc_rows = []
    for r in d["relocations"]:
        reloc_rows.append(
            f'<tr><td>{esc(r["from_site"])} → <b>{esc(r["to_site"])}</b></td>'
            f'<td>{esc(r["reason"])}</td><td>{badge(r["status"])}</td>'
            f'<td class=small>{esc(r["created_ts"])}</td></tr>')

    judge = d["judgement"] or {}
    judge_txt = judge.get("reason", "—")
    if judge.get("decidable"):
        judge_txt += f'（{judge["samples"]} 次有效读数，{len(judge.get("over_samples", []))} 次超限）'

    body = f"""
<div class=row style="justify-content:space-between">
  <h2>事件 #INC-{incident_id:03d} · {esc(site['code'])} {esc(site['name'])}
      {badge(inc['status'])}</h2>
  <span class=small>{view_toggle}</span>
</div>
<div class="kv small" style="margin-bottom:12px">
  被投诉营位 <b>{esc(site['code'])}</b> ·
  首诉时段 <b>{("夜间安静时段 🌙" if inc["quiet"] else "白天时段")}</b> ·
  升级 <b>{inc["escalation_level"]} 级</b> ·
  合并投诉 <b>{inc["merged_count"]}</b> 起 ·
  读数判定 <b class="orange">{esc(judge_txt)}</b> ·
  <a href="/map?incident={incident_id}">在营位图查看影响范围</a>
  {f'<br>结案说明：{esc(inc["close_reason"])}' if inc["close_reason"] else ''}
</div>

<div class="grid side">
  <div>
    <div class=panel>
      <h3>① 投诉链（同一事件多次投诉合并）</h3>
      <table><tr><th>#</th><th>投诉人</th><th>所在营位</th><th>内容</th><th>时间</th></tr>
      {''.join(cmp_rows)}</table>
    </div>
    <h3>时间线：发生 → 到场 → 复核</h3>
    <ul class=timeline>{''.join(tl)}</ul>
    <div class=panel>
      <h3>② 分贝读数与现场照片（本地存储）</h3>
      <table><tr><th>时间</th><th>读数</th><th>备注</th><th>快照照片</th></tr>{''.join(rd)}</table>
    </div>
    <h3>② 现场处置记录</h3>
    {''.join(rec_html) or '<p class=muted>暂无</p>'}
    <div class=panel>
      <h3>换位建议</h3>
      {f'<table><tr><th>方案</th><th>原因</th><th>状态</th><th>时间</th></tr>{"".join(reloc_rows)}</table>'
       if reloc_rows else '<p class=muted>暂无（持续超限升级至 3 级时自动生成）</p>'}
    </div>
  </div>
  <div class=panel>
    <h3>操作</h3>
    {''.join(actions) or '<p class=muted>当前状态无可执行操作</p>'}
    <hr>
    <h3>消息留痕（模拟消息服务）</h3>
    {_messages(svc, incident_id)}
  </div>
</div>"""
    return page(f"事件 {incident_id}", "/incidents", body)


def _messages(svc, incident_id):
    rows = svc.msg.log_for(incident_id)
    if not rows:
        return '<p class=muted>暂无</p>'
    out = []
    for m in rows:
        tag = '<span class="badge">已脱敏</span>' if m["redacted"] else ""
        out.append(f'<div class=small style="margin-bottom:8px">'
                   f'<div class=muted>{esc(m["sent_ts"])} · {esc(m["kind"])} {tag}</div>'
                   f'{esc(m["body"])}</div>')
    return "".join(out)


# ---------------------------------------------------------------------- #
# 换位建议
# ---------------------------------------------------------------------- #
def view_relocate(svc: CampService) -> bytes:
    rows = svc.conn.execute(
        """SELECT r.*, g.name guest_name FROM relocation_suggestions r
           LEFT JOIN guests g ON g.id=r.guest_id ORDER BY r.id DESC""").fetchall()
    cards = []
    for r in rows:
        actions = ""
        if r["status"] == "proposed":
            actions = (f'<form method=post action=/api/relocation/accept class=row style="margin-top:8px">'
                       f'<input type=hidden name=suggestion_id value={r["id"]}>'
                       f'<button type=submit>接受并换位</button></form>'
                       f'<form method=post action=/api/relocation/decline>'
                       f'<input type=hidden name=suggestion_id value={r["id"]}>'
                       f'<button class=ghost type=submit>婉拒</button></form>')
        cards.append(
            f'<div class=panel style="margin-bottom:10px">'
            f'<div><a href="/incidents/{r["incident_id"]}">#INC-{r["incident_id"]:03d}</a> '
            f'{esc(r["from_site"])} → <b class=orange>{esc(r["to_site"])}</b> '
            f'{badge(r["status"])}</div>'
            f'<div class=small muted>建议对象：{esc(r["guest_name"] or "—")} · {esc(r["created_ts"])}</div>'
            f'<div>{esc(r["reason"])}</div>{actions}</div>')

    # 空位参考
    free = svc.conn.execute(
        """SELECT s.code, s.name FROM sites s WHERE NOT EXISTS
           (SELECT 1 FROM occupancies o WHERE o.site_code=s.code AND o.active=1)
           ORDER BY s.code""").fetchall()
    body = f"""
<h2>换位建议</h2>
<p class=muted>持续超限升级至 3 级时自动生成：目标营位与问题营位<b>不相邻</b>、当前无人入住、
且无活跃投诉。接受后旧营位退房、新营位入住，历史入住记录保留（可追溯换营位）。</p>
<div class=grid two><div>{''.join(cards) or '<p class=muted>暂无建议</p>'}</div>
<div class=panel><h3>当前空位</h3>
<table><tr><th>营位</th><th>名称</th></tr>
{''.join(f'<tr><td>{esc(s["code"])}</td><td>{esc(s["name"])}</td></tr>' for s in free)}
</table></div></div>"""
    return page("换位建议", "/relocate", body)


# ---------------------------------------------------------------------- #
# 复核统计
# ---------------------------------------------------------------------- #
def view_review(svc: CampService) -> bytes:
    st = svc.review_stats()
    bs = st["by_status"]
    stat = lambda n, v, hot=False: (
        f'<div class=stat><div class="n {"orange" if hot else ""}">{v}</div>'
        f'<div class=l>{n}</div></div>')
    status_cards = "".join(
        stat(STATUS_TEXT.get(k, k), v, k in ("open", "pending_review", "appealed"))
        for k, v in sorted(bs.items()))
    result_names = CampService._result_text
    result_rows = "".join(
        f'<tr><td>{esc(result_names(k))}</td><td>{v}</td></tr>'
        for k, v in st["onsite_results"].items()) or '<tr><td colspan=2>无</td></tr>'
    appeal_rows = "".join(
        f'<tr><td>{esc(CampService._decision_text(k) if k!="pending" else "待复核")}</td><td>{v}</td></tr>'
        for k, v in st["appeals"].items()) or '<tr><td colspan=2>无</td></tr>'
    load_rows = "".join(
        f'<tr><td>{esc(o["name"])}</td><td>{esc(o["role"])}</td><td>{o["n"]}</td></tr>'
        for o in st["officer_load"])
    queue_rows = "".join(
        f'<tr><td><a href="/incidents/{q["incident_id"]}">#INC-{q["incident_id"]:03d}</a></td>'
        f'<td>记录 {q["record_id"]}</td><td>{esc(result_names(q["result"]))}</td>'
        f'<td>{badge(q["status"])}</td><td>{esc(q["patroller_name"])}</td>'
        f'<td class=small muted>{esc(q["arrived_ts"])}</td></tr>'
        for q in st["pending_review_queue"]) or '<tr><td colspan=6 class=muted>队列已清空</td></tr>'

    body = f"""
<h2>复核队列与统计</h2>
<div class=stat-row>{status_cards}
  {stat("合并的重复投诉", st["duplicate_complaints"], True)}
  {stat("撤回投诉", st["withdrawn"])}
  {stat("豁免事件", st["exempted"])}
  {stat("误报处置", st["false_alarms"])}
  {stat("平均到场(分钟)", st["avg_response_min"] if st["avg_response_min"] is not None else "—")}
</div>
<div class="grid two" style="margin-top:16px">
  <div class=panel><h3>待复核队列（被申诉记录须主管处理）</h3>
    <table><tr><th>事件</th><th>记录</th><th>结果</th><th>状态</th><th>巡查员</th><th>到场时间</th></tr>
    {queue_rows}</table></div>
  <div>
    <div class=panel><h3>现场处置结果分布</h3>
      <table>{result_rows}</table></div>
    <div class=panel style="margin-top:12px"><h3>申诉复核结果</h3>
      <table>{appeal_rows}</table></div>
    <div class=panel style="margin-top:12px"><h3>人员接单量（现场负责人唯一性核查）</h3>
      <table><tr><th>姓名</th><th>角色</th><th>接单数</th></tr>{load_rows}</table></div>
  </div>
</div>"""
    return page("复核统计", "/review", body)


# ---------------------------------------------------------------------- #
# HTTP Handler
# ---------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    # 每请求独立连接，避免多线程共享同一个 sqlite3.Connection；
    # 接单竞争因此真正由 SQLite 的行锁裁决。
    @property
    def svc(self) -> CampService:
        conn = db.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=15000")
        return CampService(conn, MockSoundMeter(PHOTO_DIR))

    def log_message(self, fmt, *args):
        pass

    # -- 工具 --
    def _send(self, content: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, path: str, flash: str = ""):
        if flash:
            path += ("&" if "?" in path else "?") + "flash=" + urllib.parse.quote(flash)
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _qs(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    # -- GET --
    def do_GET(self):
        path, qs = self._qs()
        flash = qs.get("flash", [""])[0]
        try:
            if path == "/" or path == "/map":
                self._send(view_map(self.svc, qs))
            elif path == "/incidents":
                self._send(view_incidents(self.svc, qs))
            elif path.startswith("/incidents/"):
                inc_id = int(path.rsplit("/", 1)[1])
                self._send(view_detail(self.svc, inc_id, qs))
            elif path == "/relocate":
                self._send(view_relocate(self.svc))
            elif path == "/review":
                self._send(view_review(self.svc))
            elif path.startswith("/static/") or path.startswith("/data/"):
                self._serve_file(path)
            else:
                self._send(b"not found", "text/plain", 404)
        except ServiceError as e:
            self._send(page("错误", "", f'<div class=flash>{esc(e)}</div>'), code=400)
        except (ValueError, FileNotFoundError):
            self._send(b"bad request", "text/plain", 400)

    def _serve_file(self, path):
        target = (ROOT / path.lstrip("/")).resolve()
        if not str(target).startswith(str(ROOT.resolve())) or not target.is_file():
            self._send(b"forbidden", "text/plain", 403)
            return
        ctype = ("image/png" if target.suffix == ".png" else
                 "text/css" if target.suffix == ".css" else "application/octet-stream")
        self._send(target.read_bytes(), ctype)

    # -- POST --
    def do_POST(self):
        path, _ = self._qs()
        f = self._form()
        s = self.svc
        try:
            if path == "/api/complaints":
                r = s.submit_complaint(
                    reporter_token=f["reporter_token"],
                    reported_site=f["reported_site"], content=f["content"],
                    anonymous=f.get("anonymous", "1") == "1",
                    channel=f.get("channel", "前台"))
                if r["exempt"]:
                    flash = "该营位此刻处于已报备公共活动时段，投诉已受理但免予处置（豁免留痕）"
                elif r["merged"]:
                    flash = f"该营位已有正在处置的事件，投诉已并入 #INC-{r['incident_id']:03d}（投诉链合并且不暴露投诉人）"
                else:
                    flash = f"已创建事件 #INC-{r['incident_id']:03d}"
                self._redirect(f"/incidents/{r['incident_id']}", flash)
            elif path == "/api/withdraw":
                r = s.withdraw_complaint(int(f["complaint_id"]), f["token"],
                                         f.get("reason", ""))
                self._redirect(f"/incidents/{r['incident_id']}",
                               "投诉已撤回" + ("，事件内全部投诉撤回，事件撤销"
                                              if r["incident_withdrawn"] else "（事件继续处置）"))
            elif path == "/api/claim":
                ok = s.claim_incident(int(f["incident_id"]), int(f["officer_id"]))
                self._redirect(f"/incidents/{f['incident_id']}",
                               "接单成功，您是唯一现场负责人" if ok
                               else "接单失败：该事件已被其他值班员接走（仅分配一名负责人）")
            elif path == "/api/reading":
                r = s.add_reading(int(f["incident_id"]),
                                  fault=f.get("fault") == "1",
                                  base=float(f.get("base") or 48),
                                  seed=int(f["seed"]) if f.get("seed") else None)
                self._redirect(f"/incidents/{f['incident_id']}",
                               f"已采集读数：{r['db'] if r['meter_ok'] else '设备故障·无效'}（照片已本地保存）")
            elif path == "/api/onsite":
                rid = s.complete_onsite(
                    int(f["incident_id"]), int(f["patroller_id"]),
                    communication=f["communication"], result=f["result"],
                    arrived_ts=f.get("arrived_ts") or None)
                self._redirect(f"/incidents/{f['incident_id']}",
                               f"现场记录 #{rid} 已提交，进入后续流程（巡查员不可自行结案）")
            elif path == "/api/appeal":
                s.appeal(int(f["record_id"]), f["token"], f["note"])
                self._redirect(f"/incidents/{f.get('incident_id', '')}", "申诉已受理，等待值班主管复核")
            elif path == "/api/review":
                r = s.review_close(record_id=int(f["record_id"]),
                                   supervisor_id=int(f["supervisor_id"]),
                                   decision=f["decision"], note=f.get("note", ""))
                self._redirect(f"/incidents/{r['incident_id']}",
                               "已复核结案" if r.get("closed") else "已退回继续跟进")
            elif path in ("/api/relocation/accept", "/api/relocation/decline"):
                s.respond_relocation(int(f["suggestion_id"]),
                                     accept=(path.endswith("accept")))
                self._redirect("/relocate",
                               "已接受换位建议并完成换营位" if path.endswith("accept")
                               else "已婉拒换位建议")
            else:
                self._send(b"not found", "text/plain", 404)
        except ServiceError as e:
            back = f.get("incident_id")
            self._redirect(f"/incidents/{back}" if back else "/incidents", f"操作被拒绝：{e}")
        except (KeyError, ValueError) as e:
            self._redirect("/incidents", f"表单参数错误：{e}")


def make_server(host: str = "127.0.0.1", port: int = 8000,
                db_path: str = "data/camp.db") -> ThreadingHTTPServer:
    global DB_PATH
    DB_PATH = db_path
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.close()
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = make_server(port=p)
    print(f"营地噪声投诉处置台已启动：http://127.0.0.1:{p}")
    httpd.serve_forever()
