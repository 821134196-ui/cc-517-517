"""页面视图：营位图、事件受理、现场记录、换位建议、复核、统计。"""
from __future__ import annotations

from datetime import datetime

from .database import neighbors
from .templates import (
    DISPOSITION_TEXT, STATUS_TEXT, base, esc, flash, status_badge,
    BLUE, BORDER, GREEN, MUTED, ORANGE, PANEL_2, TEXT, YELLOW, RED,
)
from .services import OFFICERS, SUPERVISOR

# 营位节点半宽（与 seed 坐标匹配）
NODE_W, NODE_H = 116, 64


def _now_local_input() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def _dt_input(s: str | None) -> str:
    if not s:
        return _now_local_input()
    return s.replace(" ", "T")[:16]


# ---------------- 营位图 ----------------
def render_map(svc, selected: int | None, flash_ok=None, flash_err=None) -> str:
    db = svc.db
    sites = db.query("SELECT * FROM sites ORDER BY id")
    edges = db.query("SELECT site_a, site_b FROM adjacency ORDER BY site_a, site_b")
    events = svc.list_events()
    # 每个营位的未结事件
    open_by_site: dict[int, list[dict]] = {}
    for e in events:
        if e["status"] in ("open", "assigned", "resolved", "appealed"):
            open_by_site.setdefault(e["reported_site_id"], []).append(e)

    zone = set()
    if selected:
        zone = set(neighbors(db, selected))

    edge_svg = []
    pos = {s["id"]: (s["x"], s["y"]) for s in sites}
    for ed in edges:
        x1, y1 = pos[ed["site_a"]]
        x2, y2 = pos[ed["site_b"]]
        # 只高亮“选中营位 — 直接相邻营位”的边
        hot = bool(selected) and selected in (ed["site_a"], ed["site_b"])
        color = ORANGE if hot else BORDER
        width = 3 if hot else 2
        edge_svg.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-width="{width}" opacity="0.9"/>')

    node_svg = []
    for s in sites:
        sid = s["id"]
        x, y = s["x"], s["y"]
        active = open_by_site.get(sid, [])
        is_selected = selected == sid
        in_zone = sid in zone
        if is_selected:
            stroke, sw = ORANGE, 3
        elif in_zone:
            stroke, sw = YELLOW, 2
        elif active:
            stroke, sw = ORANGE, 2
        else:
            stroke, sw = BORDER, 1.5
        fill = "#2b2e33" if not in_zone else "#33302a"
        x0, y0 = x - NODE_W / 2, y - NODE_H / 2
        node_svg.append(f'''
        <g>
          <rect x="{x0}" y="{y0}" width="{NODE_W}" height="{NODE_H}" rx="10"
                fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>
          <text x="{x - 42}" y="{y + 8}" font-size="22" font-weight="700"
                fill="{ORANGE if (is_selected or active) else TEXT}">{sid}</text>
          <text x="{x - 16}" y="{y - 6}" font-size="11" fill="{MUTED}">{esc(s['label'])}</text>
          <text x="{x - 16}" y="{y + 12}" font-size="10" fill="{MUTED}">
            安静 {esc(s['quiet_start'])}–{esc(s['quiet_end'])}</text>
          {f'<circle cx="{x + NODE_W/2 - 10}" cy="{y0 + 10}" r="5" fill="{ORANGE}"/>' if active else ''}
        </g>''')

    legend = f"""
    <div class="small muted" style="display:flex;gap:16px;margin-top:10px;flex-wrap:wrap">
      <span><span style="color:{ORANGE}">●</span> 有未结投诉 / 选中营位</span>
      <span><span style="color:{YELLOW}">●</span> 相邻营位（影响范围）</span>
      <span>灰线为相邻关系</span>
      <span>节点大数字为营位编号</span>
    </div>"""

    site_links = '<div class="row" style="flex-wrap:wrap;margin-bottom:12px">' + "".join(
        f'<a class="btn {"btn-warn" if sid==selected else ""}" '
        f'href="/?site={sid}">{sid} 号</a>'
        for sid in pos) + "</div>"

    detail_panel = ""
    if selected:
        nb = neighbors(db, selected)
        guests = db.query("SELECT name FROM guests WHERE site_id=?", (selected,))
        guest_txt = "、".join(esc(g["name"]) for g in guests) or "空闲"
        sug = svc.move_suggestions(selected)
        sug_txt = "、".join(f'{x["site"]["id"]} 号（{x["hops"]} 跳）' for x in sug) or "暂无合适空位"
        detail_panel = f"""
        <div class="card">
          <h2>{selected} 号营位 · 影响范围</h2>
          <dl class="kv">
            <dt>相邻营位</dt><dd>{("、".join(f'<a href="/?site={n}">{n} 号</a>' for n in nb)) or '无'}</dd>
            <dt>入住住客</dt><dd>{guest_txt}</dd>
            <dt>换位备选</dt><dd class="muted">{sug_txt}（相邻与已占用营位不推荐）</dd>
          </dl>
          <div class="row" style="margin-top:12px">
            <a class="btn btn-warn" href="/events/new?site={selected}">就 {selected} 号营位提交投诉</a>
            <a class="btn" href="/events?site={selected}">查看相关事件</a>
          </div>
        </div>"""

    svg = f"""
    <div class="card">
      <h2>营位图（编号与相邻关系）</h2>
      {site_links}
      <svg viewBox="0 0 720 380" width="100%" role="img"
           aria-label="营地营位与相邻关系图">
        {''.join(edge_svg)}
        {''.join(node_svg)}
      </svg>
      {legend}
    </div>"""
    body = flash(flash_ok, flash_err) + svg + detail_panel
    return base("营位图", body)


# ---------------- 事件列表 ----------------
def render_events(svc, site_filter: int | None = None,
                  flash_ok=None, flash_err=None) -> str:
    events = svc.list_events()
    if site_filter:
        events = [e for e in events if e["reported_site_id"] == site_filter]
    trs = []
    for e in events:
        disp = DISPOSITION_TEXT.get(e["disposition"] or "", "")
        trs.append(f"""<tr>
          <td><a href="/events/{e['id']}">#{e['id']}</a></td>
          <td><a href="/?site={e['reported_site_id']}">{e['reported_site_id']} 号</a>
              <div class="small muted">{esc(e['site_label'])}</div></td>
          <td>{'夜间' if e['quiet'] else '日间'}</td>
          <td>{e['n_complaints']}（有效 {e['n_active']}）
              {f'<div class="small muted">含 {e["n_complaints"]-1} 条合并投诉</div>' if e['n_complaints']>1 else ''}</td>
          <td>{e['n_visits']}</td>
          <td>{status_badge(e['status'])}
              {f'<div class="small muted">{esc(disp)}</div>' if disp else ''}</td>
          <td class="small muted">{esc(e['first_at'])}</td>
          <td>{f'<span class="small muted">{esc(e["assigned_to"])}</span>' if e['assigned_to'] else ''}</td>
        </tr>""")
    table = f"""
    <div class="card">
      <h2>事件受理{f' · {site_filter} 号营位' if site_filter else ''}
        {'<a class="btn" style="float:right" href="/events">全部</a>' if site_filter else ''}</h2>
      <table>
        <thead><tr><th>事件</th><th>被投诉营位</th><th>时段</th><th>投诉数</th>
        <th>到场</th><th>状态</th><th>首次发生</th><th>负责人</th></tr></thead>
        <tbody>{''.join(trs) or '<tr><td colspan="8" class="muted">暂无事件</td></tr>'}</tbody>
      </table>
    </div>"""
    body = flash(flash_ok, flash_err) + table + render_intake_form(svc, site_filter)
    return base("事件受理", body)


def render_intake_form(svc, site_filter: int | None = None) -> str:
    sites = svc.db.query("SELECT * FROM sites ORDER BY id")
    guests = svc.db.query("SELECT * FROM guests ORDER BY id")
    options = "".join(
        f'<option value="{s["id"]}" {"selected" if site_filter==s["id"] else ""}>'
        f'{s["id"]} 号 · {esc(s["label"])}</option>' for s in sites)
    goptions = '<option value="">— 不登记（仅匿名）—</option>' + "".join(
        f'<option value="{g["id"]}">{esc(g["name"])}（{esc(g["phone"])}）</option>'
        for g in guests)
    exemptions = svc.db.query("SELECT * FROM exemptions ORDER BY id")
    ex_txt = "、".join(f'{esc(e["title"])} {esc(e["start_at"])}–{esc(e["end_at"])}'
                      for e in exemptions)
    return f"""
    <div class="card" style="margin-top:18px">
      <h2>提交噪声投诉</h2>
      {f'<p class="small muted">当前公共活动豁免：{ex_txt}（豁免时段内的投诉受理但直接归档，不派单）</p>' if ex_txt else ''}
      <form method="post" action="/complaints">
        <div class="row">
          <div><label>被投诉营位</label><select name="reported_site_id">{options}</select></div>
          <div><label>投诉人（内部登记）</label><select name="complainant_id">{goptions}</select></div>
        </div>
        <label>情况描述</label>
        <textarea name="detail" placeholder="例如：22:10 起音响重低音持续，孩子被吵醒" required></textarea>
        <div class="checkbox">
          <input type="checkbox" id="anon" name="anonymous" value="1" checked>
          <label for="anon" style="margin:0">对被投诉方匿名展示（系统仍保留内部投诉链，但不向其公开姓名/电话/营位）</label>
        </div>
        <button class="btn btn-warn" type="submit">提交投诉</button>
        <span class="hint">同一营位 30 分钟内的再次投诉将自动合并到既有事件</span>
      </form>
    </div>"""


# ---------------- 事件详情 ----------------
def render_event_detail(svc, event_id: int, role: str,
                        flash_ok=None, flash_err=None) -> str:
    d = svc.event_detail(event_id)
    ev, site = d["event"], d["site"]

    # 投诉链（合并展示，匿名脱敏）
    comp_rows = []
    for i, c in enumerate(d["complaints"], 1):
        who = esc(c["complainant_name"]) if c["complainant_name"] else "未登记住客"
        if c["anonymous"]:
            who = f'<span style="color:{YELLOW}">{who}（匿名）</span>'
        if c["withdrawn"]:
            action_cell = '<span class="muted">已撤回</span>'
        else:
            action_cell = (
                f'<form method="post" action="/complaints/{c["id"]}/withdraw" '
                'onsubmit="return confirm(&quot;确认撤回该投诉？&quot;)">'
                '<button class="btn small">撤回</button></form>')
        comp_rows.append(f"""<tr>
          <td>{i}</td>
          <td>{who}</td>
          <td class="small">{esc(c['created_at'])}</td>
          <td>{esc(c['detail'])}</td>
          <td>{action_cell}</td>
        </tr>""")
    complaints_card = f"""
    <div class="card">
      <h2>投诉链（{len(comp_rows)} 条，同一事件合并）</h2>
      <table><thead><tr><th>#</th><th>投诉人</th><th>发生时间</th><th>内容</th><th></th></tr></thead>
      <tbody>{''.join(comp_rows)}</tbody></table>
      <p class="hint">被投诉方仅可见“匿名投诉人 N”；内部数据库保留可追溯投诉链。</p>
    </div>"""

    # 时间线：发生 → 到场 → 复核
    tl_items = []
    for t in d["timeline"]:
        tl_items.append(f"""<li class="ph-{t['phase']}">
          <span class="dot"></span>
          <div><span class="phase-tag">{t['phase']}</span>
          <span class="small muted">{esc(t['at'])}</span></div>
          <div>{esc(t['title'])}</div>
          {f'<div class="small muted">{esc(t["detail"])}</div>' if t['detail'] else ''}
        </li>""")
    timeline_card = f"""
    <div class="card">
      <h2>处置时间线（发生 → 到场 → 复核）</h2>
      <ul class="timeline">{''.join(tl_items) or '<li class="muted">暂无记录</li>'}</ul>
    </div>"""

    # 现场记录
    rec_rows = []
    for r in d["records"]:
        if not r["device_ok"]:
            reading = f'<span style="color:{MUTED}">误报，读数不采信</span>'
        else:
            lim = 45 if r["quiet"] else 55
            color = ORANGE if r["over_limit"] else GREEN
            reading = (f'<b style="color:{color}">{r["db_reading"]:.1f} dB</b>'
                       f'<span class="small muted"> / 限值 {lim}</span>')
        photo = (f'<a href="/photos/{esc(r["photo_path"])}" target="_blank">'
                 f'<img class="photo" src="/photos/{esc(r["photo_path"])}" '
                 f'width="280" alt="分贝读数照片"></a>') if r["photo_path"] else ""
        rec_rows.append(f"""<tr>
          <td class="small">{esc(r['arrive_at'])}<div class="muted">读数 {esc(r['reading_at'])}</div></td>
          <td>{esc(r['officer'])}</td><td>{reading}</td>
          <td>{'夜间' if r['quiet'] else '日间'}</td>
          <td>{esc(r['outcome'])}{f'<div class="small muted">{esc(r["device_note"])}</div>' if not r['device_ok'] else ''}</td>
          <td>{photo}</td>
        </tr>""")
    records_card = f"""
    <div class="card">
      <h2>现场记录</h2>
      {('<table><thead><tr><th>到场/读数时间</th><th>负责人</th><th>分贝读数</th>'
        '<th>时段</th><th>沟通结果</th><th>读数照片</th></tr></thead>'
        f'<tbody>{"".join(rec_rows)}</tbody></table>') if rec_rows else '<p class="muted">暂无到场记录</p>'}
    </div>"""

    # 操作区（按状态 + 视角）
    actions = render_actions(svc, d, role)

    head = f"""
    <div class="card">
      <div class="row" style="align-items:flex-start">
        <div>
          <h2 style="margin-bottom:6px">事件 #{event_id} · 被投诉营位
            <a href="/?site={ev['reported_site_id']}">{ev['reported_site_id']} 号</a>
            <span class="small muted">{esc(site['label'])}</span></h2>
          <div class="small muted">首次发生 {esc(ev['first_at'])} ·
            {'安静时段（限值 45dB）' if ev['quiet'] else '日间（限值 55dB）'} ·
            相邻：{"、".join(f'<a href="/?site={n}">{n}</a>' for n in d['neighbors']) or '无'}</div>
        </div>
        <div style="text-align:right">{status_badge(ev['status'])}
          {f'<div class="small muted" style="margin-top:6px">{esc(DISPOSITION_TEXT.get(ev["disposition"], ""))}</div>' if ev['disposition'] else ''}
        </div>
      </div>
      {f'<p class="hint" style="margin-bottom:0">{esc(ev["resolution_note"])}</p>' if ev['resolution_note'] else ''}
      {f'<p class="hint" style="color:var(--yellow);margin-bottom:0">申诉理由：{esc(ev["appeal_note"])}</p>' if ev['appeal_note'] else ''}
      {f'<p class="hint" style="margin-bottom:0">复核：{esc(ev["review_note"])}（{esc(ev["reviewer"])}，{esc(ev["reviewed_at"])}）</p>' if ev['review_note'] else ''}
    </div><br>"""

    body = flash(flash_ok, flash_err) + head
    body += '<div class="grid cols-2">' + timeline_card + complaints_card + '</div><br>'
    body += actions
    body += records_card + "<br>" + render_moves(svc, event_id)
    return base("事件详情", body, role)


def render_actions(svc, d: dict, role: str) -> str:
    ev = d["event"]
    blocks = []

    # 接单（巡查员视角；并提供双人并发模拟按钮）
    if ev["status"] == "open" and role in ("officer", "supervisor"):
        opts = "".join(f'<option value="{esc(o["name"])}">{esc(o["name"])}</option>'
                       for o in OFFICERS)
        blocks.append(f"""
        <div class="card">
          <h2>派单接单</h2>
          <form method="post" action="/events/{ev['id']}/claim" class="row">
            <select name="officer">{opts}</select>
            <button class="btn btn-primary" type="submit">接单（唯一现场负责人）</button>
          </form>
          <button class="btn" style="margin-top:10px" onclick="raceClaim({ev['id']})">
            ⚡ 模拟两名巡查员同时并发接单</button>
          <p id="race-result" class="hint"></p>
          <script>
          async function raceClaim(id) {{
            const btn = document.getElementById('race-result');
            btn.textContent = '并发请求中…';
            const go = (officer) => fetch('/events/' + id + '/claim', {{
              method:'POST',
              headers:{{'Content-Type':'application/x-www-form-urlencoded',
                       'X-Requested-With':'fetch'}},
              body: new URLSearchParams({{officer}})
            }}).then(r => r.json());
            const [a, b] = await Promise.all([
              go('{esc(OFFICERS[0]["name"])}'), go('{esc(OFFICERS[1]["name"])}')
            ]);
            const line = (x) => x.won
              ? ('✅ ' + x.assigned_to + ' 接单成功')
              : ('⛔ 接单失败：' + (x.reason || ''));
            btn.textContent = '结果：' + line(a) + ' ｜ ' + line(b);
            setTimeout(()=>location.reload(), 1800);
          }}
          </script>
        </div>""")

    # 现场记录（仅本事件负责人）
    if ev["status"] == "assigned" and role in ("officer", "supervisor"):
        mine = True
        officer_opts = "".join(
            f'<option {"selected" if ev["assigned_to"]==o["name"] else ""} '
            f'value="{esc(o["name"])}">{esc(o["name"])}</option>' for o in OFFICERS)
        blocks.append(f"""
        <div class="card">
          <h2>现场记录{'' if mine else '（非本事件负责人不可提交）'}</h2>
          <form method="post" action="/events/{ev['id']}/records">
            <input type="hidden" name="officer" value="{esc(ev['assigned_to'])}">
            <div class="row">
              <div><label>到场时间</label>
                <input type="datetime-local" name="arrive_at" value="{_now_local_input()}"></div>
              <div><label>读数时间</label>
                <input type="datetime-local" name="reading_at" value="{_now_local_input()}"></div>
            </div>
            <div class="row">
              <div><label>分贝读数（留空使用模拟分贝计）</label>
                <input name="reading_db" type="number" step="0.1" placeholder="自动读取"></div>
              <div style="flex:0 0 200px"><label>模拟设备状态</label>
                <select name="meter_fault"><option value="0">设备正常</option>
                <option value="1">设备误报（自检失败）</option></select></div>
            </div>
            <label>沟通结果</label>
            <textarea name="outcome" required placeholder="例如：现场劝导，对方同意关闭音响"></textarea>
            <div class="hint" style="margin:8px 0">
              规则：夜间两次有效超限读数间隔 ≤15 分钟 → 自动升级处置；
              设备误报读数不采信，事件按误报登记。
            </div>
            <button class="btn btn-primary" type="submit">提交现场记录</button>
          </form>
        </div>""")

    # 申诉（住客视角或处置完成后）
    if ev["status"] in ("resolved", "false_alarm"):
        blocks.append(f"""
        <div class="card">
          <h2>被投诉方申诉</h2>
          <form method="post" action="/events/{ev['id']}/appeal">
            <textarea name="note" required placeholder="申诉理由：将提交值班主管复核，巡查员不能自行结案"></textarea>
            <button class="btn btn-warn" style="margin-top:8px" type="submit">提交申诉</button>
          </form>
        </div>""")

    # 复核（仅主管）
    if ev["status"] == "appealed":
        if role == "supervisor":
            blocks.append(f"""
            <div class="card" style="border-color:{YELLOW}66">
              <h2>值班主管复核（只有主管可结案）</h2>
              <form method="post" action="/events/{ev['id']}/review">
                <div class="row">
                  <select name="result">
                    <option value="upheld">维持原处置 → 结案</option>
                    <option value="downgraded_false_alarm">改判设备误报</option>
                    <option value="reopen">发回重查</option>
                  </select>
                </div>
                <label>复核意见</label>
                <textarea name="note" required></textarea>
                <button class="btn btn-primary" style="margin-top:8px" type="submit">提交复核结论</button>
              </form>
            </div>""")
        else:
            blocks.append(f"""
            <div class="card" style="border-color:{YELLOW}66">
              <h2>申诉待复核</h2>
              <p class="muted">该处置已被申诉。巡查员无权关闭被申诉记录，
              请切换到 <a href="#" onclick="document.querySelector('[name=role]').value='supervisor';document.querySelector('.role').submit()">值班主管视角</a> 复核结案。</p>
            </div>""")

    if not blocks:
        return ""
    return '<div class="grid cols-2">' + "".join(blocks) + "</div><br>"


def render_moves(svc, event_id: int) -> str:
    suggestions = svc.move_suggestions(
        svc.db.one("SELECT reported_site_id AS r FROM events WHERE id=?",
                   (event_id,))["r"])
    rows = []
    for s in suggestions:
        rows.append(f"""<tr>
          <td>{s['site']['id']} 号 · {esc(s['site']['label'])}</td>
          <td>{s['hops']} 跳（非相邻）</td>
          <td class="small muted">{esc(s['reason'])}</td>
          <td>{'<span class="small muted">已记录建议</span>' if svc.db.one(
              "SELECT id FROM move_suggestions WHERE event_id=? AND to_site=?",
              (event_id, s['site']['id'])) else
              f'<form method="post" action="/events/{event_id}/moves">'
              f'<input type="hidden" name="to_site" value="{s["site"]["id"]}">'
              f'<input type="hidden" name="reason" value="{esc(s["reason"])}">'
              '<button class="btn small">采用建议</button></form>'}</td>
        </tr>""")
    return f"""
    <div class="card">
      <h2>换位建议（按营位图距离，避开相邻与占用营位）</h2>
      <table><thead><tr><th>推荐营位</th><th>距离</th><th>理由</th><th></th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="4" class="muted">暂无可推荐的空营位</td></tr>'}</tbody></table>
    </div>"""


# ---------------- 复核队列 ----------------
def render_review_queue(svc, role: str, flash_ok=None, flash_err=None) -> str:
    appealed = [e for e in svc.list_events() if e["status"] == "appealed"]
    rows = []
    for e in appealed:
        rows.append(f"""<tr>
          <td><a href="/events/{e['id']}">#{e['id']}</a></td>
          <td>{e['reported_site_id']} 号</td>
          <td>{esc(e['assigned_to'])}</td>
          <td class="small">{esc(DISPOSITION_TEXT.get(e['disposition'], ''))}</td>
          <td>{status_badge(e['status'])}</td>
          <td>{'<a class="btn btn-primary" href="/events/' + str(e['id']) + '">前往复核</a>'
               if role == 'supervisor' else '<span class="small muted">主管视角可操作</span>'}</td>
        </tr>""")
    queue = f"""
    <div class="card">
      <h2>待复核队列（被申诉处置）</h2>
      <table><thead><tr><th>事件</th><th>营位</th><th>原负责人</th><th>原处置</th>
      <th>状态</th><th></th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="6" class="muted">暂无待复核事件</td></tr>'}</tbody></table>
      <p class="hint">制度要求：巡查员不能自己关闭被申诉的处置记录，值班主管复核后才可结案。</p>
    </div>"""

    done_rows = []
    for r in svc.db.query(
            """SELECT r.*, e.reported_site_id FROM reviews r JOIN events e ON e.id=r.event_id
               ORDER BY r.id DESC LIMIT 20"""):
        result_txt = {"upheld": "维持原处置", "downgraded_false_alarm": "改判误报",
                      "reopen": "发回重查"}.get(r["result"], r["result"])
        color = {"upheld": GREEN, "downgraded_false_alarm": MUTED,
                 "reopen": ORANGE}.get(r["result"], MUTED)
        done_rows.append(f"""<tr>
          <td><a href="/events/{r['event_id']}">#{r['event_id']}</a></td>
          <td>{r['reported_site_id']} 号</td>
          <td style="color:{color}">{result_txt}</td>
          <td class="small">{esc(r['note'])}</td>
          <td class="small muted">{esc(r['reviewer'])} · {esc(r['created_at'])}</td>
        </tr>""")
    done = f"""
    <div class="card" style="margin-top:18px">
      <h2>近期复核结论</h2>
      <table><thead><tr><th>事件</th><th>营位</th><th>结论</th><th>意见</th><th>复核人/时间</th></tr></thead>
      <tbody>{''.join(done_rows) or '<tr><td colspan="5" class="muted">暂无</td></tr>'}</tbody></table>
    </div>"""
    return base("复核", flash(flash_ok, flash_err) + queue + done, role)


# ---------------- 统计 ----------------
def render_stats(svc, role: str) -> str:
    s = svc.stats()
    tiles = [
        (s["total_events"], "事件总数", TEXT),
        (s["merged_complaints"], "合并的重复投诉", BLUE),
        (s["anonymous_complaints"], "匿名投诉", YELLOW),
        (s["persistent_escalations"], "持续超限升级", ORANGE),
        (s["device_false_alarms"], "设备误报次数", MUTED),
        (s["withdrawn_complaints"], "撤回投诉", MUTED),
        (s["exempt_events"], "公共活动豁免", GREEN),
        (s["night_events"], "夜间事件", TEXT),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="num" style="color:{c}">{n}</div>'
        f'<div class="lbl">{esc(l)}</div></div>' for n, l, c in tiles)

    total = max(s["total_events"], 1)
    bar_colors = {
        "open": ORANGE, "assigned": BLUE, "resolved": GREEN, "appealed": YELLOW,
        "closed": MUTED, "withdrawn": MUTED, "false_alarm": MUTED,
    }
    bars = []
    for st, n in sorted(s["by_status"].items(), key=lambda x: -x[1]):
        pct = round(n / total * 100)
        bars.append(f"""<div class="bar-row">
          <span>{STATUS_TEXT.get(st, st)}</span>
          <div class="bar"><span style="width:{pct}%;background:{bar_colors.get(st, MUTED)}"></span></div>
          <span>{n}</span></div>""")

    reviews = s["reviews"]
    review_txt = " · ".join(
        f'{ {"upheld":"维持","downgraded_false_alarm":"改判误报","reopen":"发回重查"}.get(k,k) } {v}'
        for k, v in reviews.items()) or "暂无复核"

    # 消息送达日志
    msgs = svc.db.query("SELECT * FROM messages_log ORDER BY id DESC LIMIT 6")
    msg_rows = "".join(
        f'<tr><td class="small">{esc(m["created_at"])}</td><td>{esc(m["channel"])}</td>'
        f'<td class="small">{esc(m["target"])}</td><td class="small">{esc(m["body"])}</td></tr>'
        for m in msgs) or '<tr><td colspan="4" class="muted">暂无通知</td></tr>'

    body = f"""
    <div class="card">
      <h2>处置统计</h2>
      <div class="stat-tiles">{tile_html}</div>
    </div>
    <div class="grid cols-2" style="margin-top:18px">
      <div class="card">
        <h2>事件状态分布</h2>
        {''.join(bars)}
        <p class="hint">复核结论：{esc(review_txt)}</p>
      </div>
      <div class="card">
        <h2>通知发送记录（模拟消息服务）</h2>
        <table><thead><tr><th>时间</th><th>渠道</th><th>接收</th><th>内容</th></tr></thead>
        <tbody>{msg_rows}</tbody></table>
        <p class="hint">所有通知均不含投诉人姓名或电话。</p>
      </div>
    </div>"""
    return base("统计", body, role)
