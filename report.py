#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCI A1 抢机日报生成器

只读 grab.log，不修改 grab.sh、不额外写日志文件。
生成自包含的 HTML（内联 CSS/SVG，无外部依赖，可离线打开）。

用法:
  ./report.py                # 生成今天 + 昨天
  ./report.py --date 2026-11-09
  ./report.py --days 14      # 回填最近 14 天
  ./report.py --all          # 全部历史
  ./report.py --index-only   # 只刷新目录页
  ./report.py --notify       # 生成后把摘要推到微信（PushPlus）

节奏参数（INTERVAL/JITTER/REST_*）从同目录 config.sh 读取，
所以调整抢机节奏后报表的健康检查会自动跟着变，无需改本文件。
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "grab.log")
OUTDIR = os.path.join(BASE, "reports")
CFGFILE = os.path.join(BASE, "config.sh")


def load_rhythm():
    """从同目录 config.sh 读节奏参数。

    报表必须跟着配置走，不能把 600~660 这种数字写死在代码里——
    一旦调了 INTERVAL/JITTER，硬编码的「健康检查」就会把所有日子判成异常。
    读不到就退回旧默认值（600 ± 60，无长休）。
    """
    cfg = {"INTERVAL": 600, "JITTER": 60, "REST_PCT": 0,
           "REST_MIN": 0, "REST_MAX": 0}
    try:
        with open(CFGFILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                m = re.match(r"^([A-Z_]+)=(.*)$", line)
                if not m or m.group(1) not in cfg:
                    continue
                v = m.group(2).strip().strip('"').strip("'")
                try:
                    cfg[m.group(1)] = int(v)
                except ValueError:
                    pass
    except OSError:
        pass
    return cfg


RHYTHM = load_rhythm()
GAP_MIN = max(60, RHYTHM["INTERVAL"] - RHYTHM["JITTER"])   # 常规最短
GAP_MAX = RHYTHM["INTERVAL"] + RHYTHM["JITTER"]            # 常规最长
REST_CEIL = GAP_MAX + RHYTHM["REST_MAX"]                   # 长休上限
SLACK = 40                            # 调度误差容忍（秒）
LONG_GAP = GAP_MAX + SLACK            # 超过即不属于常规节奏

# 判定「这一轮是脚本主动长休」的休眠秒数下限。
# 取 REST_MIN 的一半做容差，避免把旧配置（600~660 秒）下的正常间隔
# 拿新阈值去量、批量误判成长休——历史日志不能按当前配置判。
REST_FLOOR = (GAP_MAX + max(120, RHYTHM["REST_MIN"] // 2)
              if RHYTHM["REST_PCT"] else None)

RE_ROUND = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\] 第 (\d+) 轮 → (.+?)"
    # 结尾可能带「（含长休）」后缀，必须吃掉，否则整条 nap 都解析不出来
    r"(?: ｜ 耗时 (\d+)s，休眠 (\d+)s(?:（含长休）)?)?$")
RE_AD = re.compile(r"PHX-AD-(\d+)=(\S+)")
RE_RL = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\] ⚠️ 触发限流")
RE_ERR = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\] AD=(\S+) 出现非容量错误：(.+)$")
RE_START = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\] ===== 抢机启动：(.+?) =====")


def parse_log(path):
    """把 grab.log 解析成按日期分组的结构"""
    days = {}
    if not os.path.exists(path):
        sys.exit("找不到日志：%s" % path)

    def bucket(ds):
        if ds not in days:
            days[ds] = {"rounds": [], "ratelimits": [], "errors": [], "starts": []}
        return days[ds]

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = RE_ROUND.match(line)
            if m:
                ds, hh, mm, ss, no, body, cost, nap = m.groups()
                ads = [(int(a), b) for a, b in RE_AD.findall(body)]
                bucket(ds)["rounds"].append({
                    "t": datetime(int(ds[:4]), int(ds[5:7]), int(ds[8:10]),
                                  int(hh), int(mm), int(ss)),
                    "no": int(no),
                    "ads": ads,
                    "cost": int(cost) if cost else None,
                    "nap": int(nap) if nap else None,
                })
                continue
            m = RE_RL.match(line)
            if m:
                ds, hh, mm, ss = m.groups()
                bucket(ds)["ratelimits"].append(
                    datetime(int(ds[:4]), int(ds[5:7]), int(ds[8:10]),
                             int(hh), int(mm), int(ss)))
                continue
            m = RE_ERR.match(line)
            if m:
                ds, hh, mm, ss, ad, msg = m.groups()
                bucket(ds)["errors"].append({
                    "t": datetime(int(ds[:4]), int(ds[5:7]), int(ds[8:10]),
                                  int(hh), int(mm), int(ss)),
                    "ad": ad, "msg": msg,
                })
                continue
            m = RE_START.match(line)
            if m:
                ds, hh, mm, ss, desc = m.groups()
                bucket(ds)["starts"].append({
                    "t": datetime(int(ds[:4]), int(ds[5:7]), int(ds[8:10]),
                                  int(hh), int(mm), int(ss)),
                    "desc": desc,
                })

    for d in days.values():
        d["rounds"].sort(key=lambda r: r["t"])
    return days


STATUS_MAP = {
    "无容量": ("CAP", "容量不足", "gray"),
    "限流": ("429", "触发限流", "orange"),
    "异常": ("ERR", "请求异常", "red"),
    "成功": ("OK", "抢到实例", "green"),
}


def norm_status(raw):
    for k, v in STATUS_MAP.items():
        if raw.startswith(k):
            return v
    return ("UNK", raw, "gray")


def aggregate(day):
    """单日统计"""
    rounds = day["rounds"]
    st = {
        "rounds": len(rounds),
        "attempts": 0,
        "status": {},
        "ads": {},
        "intervals": [],
        "long_gaps": [],
        "rests": [],
        "cost": [],
        "nap": [],
        "hourly": [0] * 24,
        "first": None,
        "last": None,
        "success": 0,
    }
    if not rounds:
        return st

    st["first"] = rounds[0]["t"]
    st["last"] = rounds[-1]["t"]

    for r in rounds:
        st["hourly"][r["t"].hour] += 1
        for ad_no, raw in r["ads"]:
            st["attempts"] += 1
            code, label, color = norm_status(raw)
            st["status"][label] = st["status"].get(label, 0) + 1
            key = "PHX-AD-%d" % ad_no
            slot = st["ads"].setdefault(key, {"total": 0, "CAP": 0, "429": 0,
                                              "ERR": 0, "OK": 0})
            slot["total"] += 1
            slot[code] = slot.get(code, 0) + 1
            if code == "OK":
                st["success"] += 1
        if r["cost"]:
            st["cost"].append(r["cost"])
        if r["nap"]:
            st["nap"].append(r["nap"])

    # 判定阈值按当天自己的数据分布来定，不写死：
    # 历史日志（旧配置 600~660）和今天的新配置（480~600 + 长休）混在一份日志里，
    # 用固定的绝对阈值必然误判其中一边。
    if st["intervals"]:
        srt = sorted(st["intervals"])
        median_gap = srt[len(srt) // 2]
    else:
        median_gap = GAP_MAX
    lo_thresh = max(LONG_GAP, median_gap * 1.5)
    st["gap_threshold"] = lo_thresh

    for a, b in zip(rounds, rounds[1:]):
        gap = (b["t"] - a["t"]).total_seconds()
        st["intervals"].append(gap)
        if gap <= lo_thresh:
            continue
        # 超过常规上限了，还要分清「脚本主动长休」和「真卡住了」。
        # 只认该轮日志里明写的 nap：早期版本没记 nap，用间隔反推会把卡顿
        # 算成长休（2026-09-22 实测就多算出 2 次），宁可存疑也不假阴性。
        nap = a["nap"]
        if nap and REST_FLOOR and REST_FLOOR <= nap <= REST_CEIL:
            st["rests"].append((a["t"], b["t"], gap))
        else:
            st["long_gaps"].append((a["t"], b["t"], gap))

    return st


# ---------------------------------------------------------------- HTML 模板

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2937;
padding:28px 20px 48px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto}
header{margin-bottom:22px}
h1{font-size:23px;font-weight:650;letter-spacing:-.2px;color:#111827}
.sub{color:#6b7280;font-size:13px;margin-top:6px}
.badge{display:inline-block;padding:3px 10px;border-radius:11px;font-size:12px;
font-weight:600;vertical-align:middle;margin-left:8px}
.b-wait{background:#fef3c7;color:#92400e}
.b-ok{background:#dcfce7;color:#166534}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;
margin-bottom:14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card h2{font-size:13px;font-weight:600;color:#6b7280;letter-spacing:.4px;
text-transform:uppercase;margin-bottom:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px}
.kpi{background:#fafbfc;border:1px solid #eef0f3;border-radius:8px;padding:13px 14px}
.kpi .n{font-size:24px;font-weight:640;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.kpi .l{font-size:12px;color:#6b7280;margin-top:3px}
.kpi .x{font-size:11px;color:#9ca3af;margin-top:4px}
.c-blue{color:#2563eb}.c-green{color:#15803d}.c-orange{color:#c2410c}
.c-gray{color:#4b5563}.c-red{color:#b91c1c}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#6b7280;font-weight:500;font-size:12px;padding:7px 10px;
border-bottom:1px solid #e5e7eb;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #f3f4f6;
font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#fafbfc}
.num{text-align:right}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
.t-cap{background:#f3f4f6;color:#4b5563}
.t-429{background:#ffedd5;color:#c2410c}
.t-err{background:#fee2e2;color:#b91c1c}
.t-ok{background:#dcfce7;color:#15803d}
ul.notes{list-style:none;font-size:13.5px}
ul.notes li{padding:6px 0 6px 20px;position:relative;border-bottom:1px solid #f6f7f9}
ul.notes li:last-child{border-bottom:none}
ul.notes li:before{content:"";position:absolute;left:6px;top:14px;width:5px;height:5px;
border-radius:50%;background:#d1d5db}
.ok{color:#15803d;font-weight:600}
.warn{color:#c2410c;font-weight:600}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
footer{color:#9ca3af;font-size:12px;text-align:center;margin-top:24px;line-height:1.8}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
.bars{width:100%;height:auto;display:block}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def fmt_int(n):
    return "{:,}".format(n)


def hourly_svg(hourly, peak):
    """24 小时轮次分布，纯 SVG"""
    W, H = 720, 168
    left, right, top, bottom = 34, 8, 14, 26
    pw, ph = W - left - right, H - top - bottom
    bw = pw / 24.0
    maxv = max(peak, 1)
    out = []
    # y 轴网格
    for i in range(4):
        y = top + ph - ph * i / 3.0
        val = maxv * i / 3.0
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#eef0f3" '
                   'stroke-width="1"/>' % (left, y, W - right, y))
        out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#9ca3af" '
                   'text-anchor="end">%d</text>' % (left - 6, y + 3, round(val)))
    for h, v in enumerate(hourly):
        x = left + h * bw
        bh = ph * v / maxv if maxv else 0
        y = top + ph - bh
        fill = "#bfdbfe" if v else "#f3f4f6"
        if v and v == maxv:
            fill = "#3b82f6"
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" '
                   'fill="%s"/>' % (x + bw * 0.14, y, bw * 0.72, max(bh, 1 if v else 1), fill))
        if h % 3 == 0:
            out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#9ca3af" '
                       'text-anchor="middle">%02d</text>' % (x + bw / 2, H - 8, h))
    return ('<svg class="bars" viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet">'
            % (W, H)) + "".join(out) + "</svg>"


def render_day(ds, day, st, cfg_hint):
    dt = datetime.strptime(ds, "%Y-%m-%d")
    weekday = "一二三四五六日"[dt.weekday()]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if st["rounds"] == 0:
        body = ('<div class="card"><h2>当日无数据</h2>'
                '<p style="color:#6b7280;font-size:13.5px">这一天没有轮询记录。'
                '可能是脚本没在运行，或者服务刚好在这天重启。'
                '同日的启动记录见下方。</p></div>')
        starts = ""
        if day["starts"]:
            rows = "".join(
                '<tr><td class="mono">%s</td><td>%s</td></tr>'
                % (s["t"].strftime("%H:%M:%S"), esc(s["desc"]))
                for s in day["starts"])
            starts = ('<div class="card"><h2>脚本启动记录</h2><table>'
                      '<thead><tr><th>时间</th><th>启动参数</th></tr></thead>'
                      '<tbody>%s</tbody></table></div>' % rows)
        html_doc = _page(ds, weekday, generated, body + starts)
        return html_doc, st

    attempts = st["attempts"]
    cap = st["status"].get("容量不足", 0)
    rl = st["status"].get("触发限流", 0)
    err = st["status"].get("请求异常", 0)
    ok = st["status"].get("抢到实例", 0)

    avg_gap = sum(st["intervals"]) / len(st["intervals"]) if st["intervals"] else 0
    span_h = (st["last"] - st["first"]).total_seconds() / 3600.0
    span_txt = "%.1f 小时" % span_h if span_h >= 1 else "%d 分钟" % (span_h * 60)
    eff = (cap / attempts * 100) if attempts else 0
    avg_cost = sum(st["cost"]) / len(st["cost"]) if st["cost"] else 0
    long_waste = sum(g[2] for g in st["long_gaps"])

    # 顶部结论
    if ok:
        verdict = '<span class="badge b-ok">已抢到 %d 次</span>' % ok
    else:
        verdict = '<span class="badge b-wait">未抢到</span>'

    kpi = ['<div class="kpis">']
    items = [
        ("轮询轮次", fmt_int(st["rounds"]), "次循环", "c-blue",
         "平均 %.1f 轮/天" % (st["rounds"] / max(span_h / 24.0, 0.01))),
        ("请求次数", fmt_int(attempts), "次 launch 调用", "c-blue",
         "每轮 %d 个可用域" % (attempts // st["rounds"] if st["rounds"] else 0)),
        ("抢到实例", str(ok), "次", "c-green" if ok else "c-gray",
         "0 表示全部落空"),
        ("触发限流", str(rl), "次", "c-orange" if rl else "c-gray",
         "有则说明该降频"),
        ("请求异常", str(err), "次", "c-red" if err else "c-gray",
         "非容量类报错"),
        ("平均间隔", "%.0f 秒" % avg_gap, "≈ %.1f 分钟" % (avg_gap / 60), "c-gray",
         "常规 %d~%d 秒" % (GAP_MIN, GAP_MAX)),
    ]
    for label, val, unit, cls, sub in items:
        kpi.append('<div class="kpi"><div class="n %s">%s</div>'
                   '<div class="l">%s</div><div class="x">%s</div></div>'
                   % (cls, val, label, esc(sub)))
    kpi.append("</div>")
    kpi = "".join(kpi)

    # 时段分布
    peak = max(st["hourly"]) if st["hourly"] else 0
    peak_h = st["hourly"].index(peak) if peak else 0
    chart = hourly_svg(st["hourly"], peak)

    # 可用域表
    ad_rows = []
    for key in sorted(st["ads"]):
        a = st["ads"][key]
        pct = a["total"] / attempts * 100 if attempts else 0
        ad_rows.append(
            '<tr><td class="mono">%s</td><td class="num">%d</td>'
            '<td class="num"><span class="tag t-cap">%d</span></td>'
            '<td class="num">%d</td><td class="num">%d</td><td class="num">%d</td>'
            '<td class="num">%.1f%%</td></tr>'
            % (key, a["total"], a.get("CAP", 0), a.get("429", 0),
               a.get("ERR", 0), a.get("OK", 0), pct))

    # 结果分布
    st_rows = []
    for label, cnt in sorted(st["status"].items(), key=lambda x: -x[1]):
        cls = {"容量不足": "t-cap", "触发限流": "t-429",
               "请求异常": "t-err", "抢到实例": "t-ok"}.get(label, "t-cap")
        pct = cnt / attempts * 100 if attempts else 0
        st_rows.append('<tr><td><span class="tag %s">%s</span></td>'
                       '<td class="num">%d</td><td class="num">%.2f%%</td></tr>'
                       % (cls, esc(label), cnt, pct))

    # 健康检查
    exp_rest = ((RHYTHM["REST_MIN"] + RHYTHM["REST_MAX"]) / 2.0
                if RHYTHM["REST_PCT"] else 0)
    exp_gap = RHYTHM["INTERVAL"] + RHYTHM["REST_PCT"] / 100.0 * exp_rest
    notes = []
    if avg_gap and abs(avg_gap - exp_gap) <= max(60, exp_gap * 0.12):
        notes.append('<li>轮询节奏正常：平均间隔 %.0f 秒，与设计均值 %.0f 秒相符 '
                     '<span class="ok">✓</span></li>' % (avg_gap, exp_gap))
    elif avg_gap:
        notes.append('<li>轮询节奏偏离设计值：平均 %.0f 秒，设计均值 %.0f 秒 '
                     '<span class="warn">需关注</span></li>' % (avg_gap, exp_gap))

    # 只在真的发生长休时才写这一行：历史日报跑的是旧配置（没有长休机制），
    # 在那几天硬写「今日未触发长休」属于编造。
    if st["rests"]:
        rest_total = sum(g[2] for g in st["rests"])
        notes.append('<li>主动长休 %d 次，合计 %.1f 分钟（设计为 %d%% 概率、'
                     '每次 %d~%d 秒，用于打破过于机械的节奏）</li>'
                     % (len(st["rests"]), rest_total / 60, RHYTHM["REST_PCT"],
                        RHYTHM["REST_MIN"], RHYTHM["REST_MAX"]))

    if st["long_gaps"]:
        worst = max(st["long_gaps"], key=lambda g: g[2])
        notes.append('<li>出现 %d 次超长间隔（&gt;%d 秒），最长一次 %.1f 分钟'
                     '（%s，白等合计 %.1f 分钟）<span class="warn">需关注</span></li>'
                     % (len(st["long_gaps"]), st.get("gap_threshold", LONG_GAP),
                        worst[2] / 60,
                        worst[0].strftime("%H:%M"), long_waste / 60))
    else:
        notes.append('<li>除计划内长休外，无异常间隔 <span class="ok">✓</span></li>')

    if rl == 0:
        notes.append('<li>全天零限流，请求频率安全 <span class="ok">✓</span></li>')
    else:
        notes.append('<li>触发限流 %d 次，已按退避机制额外等待 5 分钟；'
                     '若持续出现建议调大 INTERVAL <span class="warn">注意</span></li>' % rl)

    if err:
        msgs = "；".join("%s %s" % (e["t"].strftime("%H:%M"), esc(e["msg"][:70]))
                        for e in day["errors"][:3])
        notes.append('<li>非容量类异常 %d 次：%s</li>' % (err, msgs))
    else:
        notes.append('<li>无异常报错 <span class="ok">✓</span></li>')

    notes.append('<li>运行时段 %s → %s，持续 %s；单轮平均耗时 %.1f 秒</li>'
                 % (st["first"].strftime("%H:%M:%S"), st["last"].strftime("%H:%M:%S"),
                    span_txt, avg_cost))

    if day["starts"]:
        for s in day["starts"]:
            notes.append('<li>脚本于 %s 重启，参数：%s</li>'
                         % (s["t"].strftime("%H:%M:%S"), esc(s["desc"])))

    # 最近轮次明细
    recent = day["rounds"][-12:]
    rec_rows = []
    for r in reversed(recent):
        tags = "".join(
            '<span class="tag %s">AD-%d %s</span> '
            % ({"CAP": "t-cap", "429": "t-429", "ERR": "t-err",
                "OK": "t-ok"}.get(norm_status(raw)[0], "t-cap"), n,
               esc(norm_status(raw)[1]))
            for n, raw in r["ads"])
        rec_rows.append(
            '<tr><td class="mono">%s</td><td class="num">%d</td><td>%s</td>'
            '<td class="num">%s</td><td class="num">%s</td></tr>'
            % (r["t"].strftime("%H:%M:%S"), r["no"], tags,
               ("%ds" % r["cost"]) if r["cost"] else "—",
               ("%ds" % r["nap"]) if r["nap"] else "—"))

    parts = []
    parts.append('<div class="card"><h2>今日概览</h2>%s</div>' % kpi)
    parts.append('<div class="card"><h2>轮询时段分布</h2>%s'
                 '<p style="font-size:12px;color:#9ca3af;margin-top:8px">'
                 '每根柱子代表该小时完成的轮次数；峰值 %d 轮在 %02d 点。</p></div>'
                 % (chart, peak, peak_h))
    parts.append('<div class="card"><h2>轮询目标（区域 / 可用域）</h2>'
                 '<table><thead><tr><th>可用域</th><th class="num">请求数</th>'
                 '<th class="num">无容量</th><th class="num">限流</th>'
                 '<th class="num">异常</th><th class="num">成功</th>'
                 '<th class="num">占比</th></tr></thead><tbody>%s</tbody></table>'
                 '<p style="font-size:12px;color:#9ca3af;margin-top:10px">'
                 '区域 us-phoenix-1，规格 %s，每轮依次尝试全部可用域。</p></div>'
                 % ("".join(ad_rows), esc(cfg_hint)))
    parts.append('<div class="card"><h2>结果分布</h2>'
                 '<table><thead><tr><th>结果</th><th class="num">次数</th>'
                 '<th class="num">占比</th></tr></thead><tbody>%s</tbody></table>'
                 '</div>' % "".join(st_rows))
    parts.append('<div class="card"><h2>运行健康检查</h2>'
                 '<ul class="notes">%s</ul></div>' % "".join(notes))
    parts.append('<div class="card"><h2>最近 12 轮明细</h2>'
                 '<table><thead><tr><th>时间</th><th class="num">轮次</th>'
                 '<th>各可用域结果</th><th class="num">耗时</th>'
                 '<th class="num">休眠</th></tr></thead><tbody>%s</tbody></table>'
                 '<p style="font-size:12px;color:#9ca3af;margin-top:10px">'
                 '「休眠」是本轮结束后实际等待的秒数：常规 %d~%d 秒，'
                 '触发长休时会明显变长（最多到 %d 秒）。</p>'
                 '</div>' % ("".join(rec_rows), GAP_MIN, GAP_MAX, REST_CEIL))

    return _page(ds, weekday, generated, "".join(parts), verdict), st


def _page(ds, weekday, generated, body, verdict=""):
    title = "OCI A1 抢机日报 · %s" % ds
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<style>%s</style>
</head>
<body>
<div class="wrap">
<header>
<h1>OCI A1 抢机日报%s</h1>
<div class="sub">%s（周%s）&nbsp;·&nbsp;<a href="index.html">← 返回目录</a></div>
</header>
%s
<footer>
共 3 个可用域 · 数据来源 grab.log · 生成于 %s<br>
本报告仅统计单日数据，不改变抢机脚本的任何行为
</footer>
</div>
</body>
</html>
""" % (title, CSS, verdict, ds, weekday, body, generated)


def render_index(entries):
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    total_r = total_a = total_ok = total_rl = 0
    for e in entries:
        total_r += e["rounds"]
        total_a += e["attempts"]
        total_ok += e["ok"]
        total_rl += e["rl"]
        rl_tag = ('<span class="tag t-429">%d</span>' % e["rl"]) if e["rl"] else "0"
        ok_tag = ('<span class="tag t-ok">%d</span>' % e["ok"]) if e["ok"] else "0"
        rows.append(
            '<tr><td><a href="report-%s.html">%s</a></td>'
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%.0f 秒</td></tr>'
            % (e["date"], e["date"], fmt_int(e["rounds"]), fmt_int(e["attempts"]),
               ok_tag, rl_tag, e["avg_gap"]))

    body = """
<div class="card"><h2>累计</h2><div class="kpis">
<div class="kpi"><div class="n c-blue">%s</div><div class="l">总轮次</div></div>
<div class="kpi"><div class="n c-blue">%s</div><div class="l">总请求次数</div></div>
<div class="kpi"><div class="n %s">%s</div><div class="l">抢到</div></div>
<div class="kpi"><div class="n %s">%s</div><div class="l">触发限流</div></div>
</div></div>
<div class="card"><h2>日报列表（最新在前）</h2>
<table><thead><tr><th>日期</th><th class="num">轮次</th><th class="num">请求</th>
<th class="num">抢到</th><th class="num">限流</th><th class="num">平均间隔</th>
</tr></thead><tbody>%s</tbody></table></div>
""" % (fmt_int(total_r), fmt_int(total_a),
       "c-green" if total_ok else "c-gray", total_ok,
       "c-orange" if total_rl else "c-gray", total_rl,
       "".join(rows))

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCI A1 抢机日报 · 目录</title>
<style>%s</style>
</head>
<body>
<div class="wrap">
<header>
<h1>OCI A1 抢机日报</h1>
<div class="sub">数据来源 grab.log · 生成于 %s</div>
</header>
%s
<footer>点击日期查看当天详情 · 报告自包含，可离线打开</footer>
</div>
</body>
</html>
""" % (CSS, generated, body)


# ---------------------------------------------------------------- 微信推送

def push_pushplus(title, html):
    """用 PushPlus 把日报摘要推到微信。凭据读同目录 pushplus_token。

    返回 (成功与否, 说明)。任何失败都只记日志、不抛异常——
    推不出去不能连累报表生成。
    """
    tokfile = os.path.join(BASE, "pushplus_token")
    try:
        with open(tokfile, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        return False, "未找到 %s，跳过推送" % tokfile
    if not tok:
        return False, "pushplus_token 为空，跳过推送"

    payload = json.dumps({"token": tok, "title": title,
                          "content": html, "template": "html"}).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send", data=payload,
        headers={"Content-Type": "application/json"})
    # 显式禁用代理：这台机器上也没配代理，但避免继承到奇怪的 http_proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as exc:
        return False, "请求失败：%s" % exc
    try:
        j = json.loads(body)
    except ValueError:
        return False, "响应非 JSON：%s" % body[:120]
    if j.get("code") == 200:
        return True, "code=200"
    return False, "code=%s msg=%s" % (j.get("code"), j.get("msg"))


def build_digest(ds, st):
    """把单日统计压成一条适合微信看的短摘要（HTML）"""
    ok = st["success"]
    rl = st["status"].get("触发限流", 0)
    err = st["status"].get("请求异常", 0)
    cap = st["status"].get("容量不足", 0)
    gaps = st["intervals"]
    avg = sum(gaps) / len(gaps) if gaps else 0
    rests = st["rests"]

    if ok:
        head = "<b>🎉 抢到了 %d 次</b>" % ok
    elif st["rounds"] == 0:
        head = "<b>⚠️ 当天没有轮询记录</b>"
    else:
        head = "<b>仍未抢到</b>"

    ad_rows = "".join(
        "<tr><td>%s</td><td align=right>%d</td></tr>"
        % (k, v["total"]) for k, v in sorted(st["ads"].items()))

    body = (
        "%s<br>"
        "%s 共 %d 轮 / %d 次请求<br>"
        "无容量 %d · 限流 %d · 异常 %d<br>"
        "平均间隔 %.0f 秒（常规 %d~%d 秒），长休 %d 次<br>"
        "<table cellpadding=2 cellspacing=0>%s</table>"
        % (head, ds, st["rounds"], st["attempts"], cap, rl, err,
           avg, GAP_MIN, GAP_MAX, len(rests), ad_rows))

    if rl:
        body += "<br><span style='color:#c2410c'>出现限流，建议调大 INTERVAL</span>"
    if err:
        body += "<br><span style='color:#b91c1c'>有 %d 次非容量异常，详见日报</span>" % err
    return body


# ---------------------------------------------------------------- 主流程

def build_one(ds, day, cfg_hint):
    st = aggregate(day)
    doc, st = render_day(ds, day, st, cfg_hint)
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "report-%s.html" % ds)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    return path, st


def guess_cfg_hint(days):
    """从最近一次启动记录里取规格描述"""
    best = None
    for ds in sorted(days):
        for s in days[ds]["starts"]:
            if best is None or s["t"] > best["t"]:
                best = s
    if best:
        # "VM.Standard.A1.Flex 1OCPU/6GB，间隔 600s(+0~60s)"
        return best["desc"].split("，")[0]
    return "VM.Standard.A1.Flex"


def main():
    ap = argparse.ArgumentParser(description="生成 OCI A1 抢机日报")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD")
    ap.add_argument("--days", type=int, help="回填最近 N 天")
    ap.add_argument("--all", action="store_true", help="回填全部历史")
    ap.add_argument("--index-only", action="store_true", help="只刷新目录页")
    ap.add_argument("--notify", action="store_true",
                    help="生成后把首份日报摘要推到微信（读同目录 pushplus_token）")
    ap.add_argument("--log", default=LOG, help="日志路径")
    args = ap.parse_args()

    days = parse_log(args.log)
    cfg_hint = guess_cfg_hint(days)
    os.makedirs(OUTDIR, exist_ok=True)

    targets = []
    if args.date:
        targets = [args.date]
    elif args.all:
        targets = sorted(days)
    elif args.days:
        today = datetime.now().date()
        targets = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
                   for i in range(args.days - 1, -1, -1)]
    elif args.index_only:
        targets = []
    else:
        today = datetime.now().date()
        targets = [(today - timedelta(days=1)).strftime("%Y-%m-%d"),
                   today.strftime("%Y-%m-%d")]

    written = []
    for ds in targets:
        day = days.get(ds, {"rounds": [], "ratelimits": [], "errors": [], "starts": []})
        path, st = build_one(ds, day, cfg_hint)
        written.append((ds, st, path))
        print("%s  %3d 轮 / %4d 次 / 抢到 %d / 限流 %d  → %s"
              % (ds, st["rounds"], st["attempts"], st.get("success", 0),
                 st["status"].get("触发限流", 0), path))

    # 推送放在重建目录页之前：目录页写坏了也不该影响"今天有没有消息"
    if args.notify and written:
        ds, st, _ = written[0]
        title = "OCI A1 日报 %s" % ds[5:]
        if st["success"]:
            title += " · 抢到了"
        ok, info = push_pushplus(title, build_digest(ds, st))
        print("推送 %s：%s（%s）" % ("成功" if ok else "失败", title, info))

    # 重建目录页（扫描现有报告文件）
    entries = []
    for fn in sorted(os.listdir(OUTDIR)):
        m = re.match(r"report-(\d{4}-\d{2}-\d{2})\.html$", fn)
        if not m:
            continue
        ds = m.group(1)
        day = days.get(ds, {"rounds": [], "ratelimits": [], "errors": [], "starts": []})
        st = aggregate(day)
        gaps = st["intervals"]
        entries.append({
            "date": ds,
            "rounds": st["rounds"],
            "attempts": st["attempts"],
            "ok": st["success"],
            "rl": st["status"].get("触发限流", 0),
            "avg_gap": (sum(gaps) / len(gaps)) if gaps else 0,
        })
    entries.sort(key=lambda e: e["date"], reverse=True)

    idx = os.path.join(OUTDIR, "index.html")
    with open(idx, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_index(entries))
    print("\n目录页：%s（共 %d 份日报）" % (idx, len(entries)))


if __name__ == "__main__":
    main()
