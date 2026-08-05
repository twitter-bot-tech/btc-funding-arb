"""Build a trading-terminal style board for the BTC confluence strategy."""
from pathlib import Path
import json
import math

import pandas as pd

from backtest_confluence_range import add_features, load, score_row

ROOT = Path(__file__).parent
DATA = ROOT / "data"
OUT = ROOT / "strategy_board.html"
DEPLOY_OUT = ROOT / "deploy" / "strategy.html"


def money(v):
    return f"${v:,.2f}"


def pct(v):
    return f"{v * 100:.2f}%"


def pct_raw(v):
    return f"{v:.1f}%"


def tone(v):
    return "good" if v >= 0 else "bad"


def svg_points(values, width=620, height=170):
    vals = list(values)
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    pts = []
    for i, val in enumerate(vals):
        x = i * width / max(1, len(vals) - 1)
        y = height - ((val - lo) / span) * (height - 12) - 6
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def main():
    combo = json.loads((DATA / "combined_range_summary.json").read_text())
    confluence = json.loads((DATA / "confluence_range_summary.json").read_text())
    daily = pd.read_csv(DATA / "confluence_range_daily.csv")
    trades = pd.read_csv(DATA / "confluence_range_trades.csv")
    market = add_features(load())

    params = confluence["selected_params"]
    s90 = confluence["last_90d"]
    s30 = confluence["last_30d"]
    old90 = combo["last_90d"]
    po90 = combo["profit_only_last_90d"]

    latest = market.dropna(subset=["range_hi", "range_lo", "pos_in_range", "range_width"]).iloc[-1]
    latest_ts_sgt = pd.Timestamp(latest["ts"]).tz_convert("Asia/Singapore")
    score, signals = score_row(latest, rsi_max=params["rsi_max"])
    range_ok = latest["range_width"] >= params["min_width"] and latest["pos_in_range"] <= params["buy_q"]
    buy_ready = range_ok and score >= params["min_score"]
    decision = "WAIT" if not buy_ready else "BUY WATCH"
    decision_cn = "等待低位共振" if not buy_ready else "可观察入场"
    decision_class = "wait" if not buy_ready else "go"

    model_score = min(100, max(0, 42 + score * 6 + (14 if range_ok else -10)))
    pass_rate = score / 6 * 100
    advantage = model_score - 50
    pos = min(1, max(0, float(latest["pos_in_range"])))
    marker_pos = min(99.2, max(0, float(latest["pos_in_range"]) * 100))
    range_state = "突破区间上沿" if latest["pos_in_range"] > 1 else ("低吸区" if latest["pos_in_range"] <= params["buy_q"] else "区间中上部")
    buy_zone = latest["range_lo"] + (latest["range_hi"] - latest["range_lo"]) * params["buy_q"]
    sell_zone = latest["range_lo"] + (latest["range_hi"] - latest["range_lo"]) * params["sell_q"]
    stop_zone = latest["range_lo"] * (1 - params["stop_break"])

    signal_defs = [
        ("RSI", "RSI(14) 低位回升", signals["rsi_rebound"], f"{latest['rsi']:.1f}"),
        ("M15", "15m 动量转正", signals["mom_15m_pos"], pct(latest["mom_15m"])),
        ("H1", "1h 动量不弱", signals["mom_1h_not_bad"], pct(latest["mom_1h"])),
        ("VWAP", "4h VWAP 修复", signals["vwap_ok"], pct(latest["vwap_dev"])),
        ("SMA", "SMA8/21 结构", signals["sma_ok"], pct(latest["sma_gap"])),
        ("REGIME", "非单边下跌", signals["not_one_way_down"], f"8h {pct(latest['ret_8h'])}"),
    ]
    signal_tiles = "\n".join(
        f"<div class='sig {'on' if ok else 'off'}'><div class='sigTop'><b>{code}</b><span>{'ON' if ok else 'OFF'}</span></div>"
        f"<strong>{value}</strong><small>{label}</small></div>"
        for code, label, ok, value in signal_defs
    )

    scan_rows = []
    recent_bars = market.dropna(subset=["range_width", "pos_in_range"]).tail(12)
    for r in recent_bars.itertuples(index=False):
        sc, sigs = score_row(r, rsi_max=params["rsi_max"])
        rz = float(r.range_width) >= params["min_width"] and float(r.pos_in_range) <= params["buy_q"]
        state = "READY" if rz and sc >= params["min_score"] else ("WATCH" if sc >= params["min_score"] else "SKIP")
        scan_rows.append(
            f"<tr><td>{str(r.ts_sgt)[11:16]}</td><td>{sc}/6</td><td>{pct(float(r.pos_in_range))}</td>"
            f"<td>{pct(float(r.range_width))}</td><td><span class='tag {state.lower()}'>{state}</span></td></tr>"
        )

    reason_map = {
        "SELL_RANGE": "RANGE",
        "TAKE_PCT": "TAKE",
        "STOP_BREAK": "STOP",
        "TIME": "TIME",
        "FINAL": "FINAL",
    }
    trade_rows = []
    for r in trades.tail(7).itertuples(index=False):
        trade_rows.append(
            f"<tr><td>{str(r.entry_ts)[5:16]}</td><td>{money(r.entry)}</td><td>{money(r.exit)}</td>"
            f"<td>{reason_map.get(r.reason, r.reason)}</td><td>{int(r.hold_bars)}</td>"
            f"<td class='num {tone(r.pnl)}'>{money(r.pnl)}</td></tr>"
        )

    daily_rows = []
    for r in daily.tail(10).iloc[::-1].itertuples(index=False):
        daily_rows.append(
            f"<tr><td>{r.date_sgt}</td><td class='num {tone(float(r.pnl))}'>{money(float(r.pnl))}</td>"
            f"<td class='num'>{money(float(r.equity))}</td></tr>"
        )

    pnl_bars = []
    pnl_grid = []
    recent_pnl = daily[["date_sgt", "pnl", "equity"]].tail(30).reset_index(drop=True)
    pnl_min = float(recent_pnl["pnl"].min())
    pnl_max = float(recent_pnl["pnl"].max())
    y_min = min(-50, math.floor(pnl_min / 50) * 50)
    y_max = max(50, math.ceil(pnl_max / 50) * 50)
    pnl_chart_w, pnl_chart_h = 620, 205
    pnl_l, pnl_r, pnl_t, pnl_b = 62, 10, 14, 34
    plot_w = pnl_chart_w - pnl_l - pnl_r
    plot_h = pnl_chart_h - pnl_t - pnl_b
    y_span = y_max - y_min or 1

    def pnl_y(v):
        return pnl_t + (y_max - v) / y_span * plot_h

    for tick in range(int(y_min), int(y_max) + 1, 50):
        y = pnl_y(tick)
        pnl_grid.append(
            f"<line x1='{pnl_l}' y1='{y:.1f}' x2='{pnl_chart_w - pnl_r}' y2='{y:.1f}' class='gridLine'/>"
            f"<text x='{pnl_l - 10}' y='{y + 4:.1f}' class='axisText' text-anchor='end'>${tick}</text>"
        )

    zero_y = pnl_y(0)
    slot = plot_w / max(1, len(recent_pnl))
    bar_w = max(5, slot * 0.58)
    for i, r in enumerate(recent_pnl.itertuples(index=False)):
        v = float(r.pnl)
        x = pnl_l + i * slot + (slot - bar_w) / 2
        y = pnl_y(max(v, 0))
        h = max(2, abs(pnl_y(v) - zero_y))
        if v < 0:
            y = zero_y
        active = " active" if i == len(recent_pnl) - 1 else ""
        pnl_bars.append(
            f"<rect class='pnlBarRect {tone(v)}{active}' x='{x:.1f}' y='{y:.1f}' width='{bar_w:.1f}' height='{h:.1f}' rx='3' "
            f"data-date='{r.date_sgt}' data-pnl='{money(v)}' data-equity='{money(float(r.equity))}'/>"
        )
    axis_indexes = [0, 7, 14, 21, len(recent_pnl) - 1]
    pnl_axis = []
    for idx in axis_indexes:
        if idx < 0 or idx >= len(recent_pnl):
            continue
        label = str(recent_pnl.iloc[idx]["date_sgt"])[5:]
        pnl_axis.append(f"<span>{label}</span>")
    nonzero_days = int((recent_pnl["pnl"].abs() >= 0.01).sum())
    best_day = float(recent_pnl["pnl"].max())
    worst_day = float(recent_pnl["pnl"].min())
    selected_pnl = recent_pnl.iloc[-1]

    recent_equity = daily[["date_sgt", "equity"]].tail(90).reset_index(drop=True)
    equity_pts = svg_points(recent_equity["equity"], 620, 170)
    equity_axis = []
    equity_axis_indexes = [0, len(recent_equity) // 4, len(recent_equity) // 2, len(recent_equity) * 3 // 4, len(recent_equity) - 1]
    for idx in equity_axis_indexes:
        if idx < 0 or idx >= len(recent_equity):
            continue
        label = str(recent_equity.iloc[idx]["date_sgt"])[5:]
        equity_axis.append(f"<span>{label}</span>")
    equity_start = float(recent_equity["equity"].iloc[0])
    equity_end = float(recent_equity["equity"].iloc[-1])
    equity_high = float(recent_equity["equity"].max())
    equity_low = float(recent_equity["equity"].min())
    source_ts = pd.Timestamp(confluence["source_last_ts"]).tz_convert("Asia/Singapore").strftime("%m-%d %H:%M")
    generated = pd.Timestamp(confluence["generated_at"]).strftime("%m-%d %H:%M")
    day_pnl = float(daily.iloc[-1]["pnl"])
    target_progress = max(0, min(100, day_pnl / 50 * 100))
    session_on = 13 <= latest_ts_sgt.hour < 16
    session_text = "SG 主窗口内" if session_on else "非 13:00-16:00"
    stale_hours = (pd.Timestamp.now(tz="Asia/Singapore") - latest_ts_sgt).total_seconds() / 3600
    freshness = "数据偏旧" if stale_hours > 6 else "数据正常"
    avg_hold = float(trades["hold_bars"].mean()) if len(trades) else 0
    advice_text = (
        "当前先用共振版做观察主策略：只在低位、低波动、非单边下跌且信号共振时出手。"
        "回测显示它不能稳定每天 $50，重点应放在低回撤验证和减少无效交易。"
    )
    observer_path = DATA / "bitget_observer_state.json"
    observer = json.loads(observer_path.read_text(encoding="utf-8")) if observer_path.exists() else {}
    obs_signal = observer.get("signal", {})
    obs_plan = observer.get("plan", {})
    obs_account = observer.get("account", {})
    obs_blockers = observer.get("blockers") or ["观察状态未生成"]
    obs_time = observer.get("local_time", "未运行")
    obs_price = observer.get("live_price", "")
    obs_action = observer.get("action", "NO DATA")
    obs_action_class = "good" if obs_action == "BUY_WATCH" else "bad" if obs_action == "NO DATA" else ""
    obs_blocker_rows = "".join(f"<div class='gap'><span>阻断</span><b>{b}</b></div>" for b in obs_blockers)
    paper_path = DATA / "paper_state.json"
    paper = json.loads(paper_path.read_text(encoding="utf-8")) if paper_path.exists() else {}
    paper_action = paper.get("last_action", "NO DATA")
    paper_action_class = "good" if paper_action in ("BUY", "SELL") else "bad" if paper_action == "NO DATA" else ""
    paper_equity = float(paper.get("equity", paper.get("capital", 10000)) or 10000)
    paper_capital = float(paper.get("capital", 10000) or 10000)
    paper_unreal = float(paper.get("unrealized_pnl", 0) or 0)
    paper_realized = float(paper.get("realized_pnl", 0) or 0)
    paper_trades_path = DATA / "paper_trades.csv"
    paper_trade_rows = []
    if paper_trades_path.exists():
        paper_trades = pd.read_csv(paper_trades_path)
        for r in paper_trades.tail(5).iloc[::-1].itertuples(index=False):
            paper_trade_rows.append(
                f"<tr><td>{str(r.exit_ts)[5:16]}</td><td>{money(float(r.entry_price))}</td>"
                f"<td>{money(float(r.exit_price))}</td><td>{r.reason}</td>"
                f"<td class='num {tone(float(r.pnl))}'>{money(float(r.pnl))}</td></tr>"
            )
    if not paper_trade_rows:
        paper_trade_rows.append("<tr><td colspan='5'>暂无模拟成交</td></tr>")

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC AI Signal Terminal</title>
<style>
:root {{
  --bg:#050608; --panel:#0b0f14; --panel2:#10161d; --line:#1e2a35; --ink:#eef6ff;
  --muted:#7f8b99; --green:#28d17c; --red:#ff5663; --amber:#f5b642; --blue:#4ca3ff;
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; min-height:100%; background:var(--bg); color:var(--ink); }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif; font-size:13px; line-height:1.35; }}
.screen {{ max-width:1440px; margin:0 auto; padding:14px; }}
.ticker {{ height:42px; display:flex; align-items:center; justify-content:space-between; border:1px solid var(--line); background:#080c11; border-radius:8px; padding:0 12px; }}
.ticker b {{ font-size:16px; }}
.ticker span {{ color:var(--muted); margin-left:14px; }}
.ticker strong {{ color:var(--green); }}
.liveMeta {{ color:var(--muted); margin-left:14px; }}
.tickerActions {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }}
.refreshButton {{ border:1px solid var(--line); background:#101923; color:var(--ink); border-radius:7px; padding:6px 10px; font:inherit; font-size:12px; font-weight:800; cursor:pointer; }}
.refreshButton:hover {{ border-color:var(--blue); color:#fff; }}
.refreshCountdown {{ color:var(--muted); font-size:11px; }}
.grid {{ display:grid; grid-template-columns:360px minmax(0,1fr) 330px; gap:10px; margin-top:10px; align-items:start; }}
.grid > * {{ min-width:0; }}
.panel {{ background:linear-gradient(180deg, var(--panel2), var(--panel)); border:1px solid var(--line); border-radius:8px; overflow:hidden; min-width:0; }}
.head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; height:38px; padding:0 12px; border-bottom:1px solid var(--line); color:#c9d4e3; }}
.head b {{ font-size:13px; letter-spacing:.02em; }}
.head span {{ color:var(--muted); font-size:11px; }}
.body {{ padding:12px; }}
.hero {{ min-height:250px; display:grid; grid-template-columns:1fr 230px; gap:12px; }}
.decision {{ padding:14px; border:1px solid #263644; border-radius:8px; background:#0a1118; min-height:220px; }}
.kicker {{ color:var(--muted); font-size:11px; font-weight:800; letter-spacing:.12em; }}
.decision h1 {{ margin:10px 0 4px; font-size:43px; line-height:1; letter-spacing:0; }}
.decision h1.go {{ color:var(--green); }}
.decision h1.wait {{ color:var(--amber); }}
.decision p {{ margin:0; color:#9aa7b8; }}
.prob {{ margin-top:18px; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }}
.prob div,.metric,.rule,.sig,.mini {{ border:1px solid var(--line); border-radius:8px; background:#080d13; padding:10px; }}
.prob span,.metric span,.mini span,.rule span,.sig small {{ display:block; color:var(--muted); font-size:11px; }}
.prob b,.metric b,.mini b {{ display:block; margin-top:4px; font-size:20px; font-variant-numeric:tabular-nums; }}
.gauge {{ border:1px solid var(--line); border-radius:8px; background:#080d13; display:grid; place-items:center; min-height:220px; }}
.ring {{ width:168px; height:168px; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--green) {model_score:.1f}%, #202934 0); box-shadow:inset 0 0 0 18px #080d13; }}
.ring b {{ font-size:38px; }}
.ring span {{ display:block; text-align:center; color:var(--muted); font-size:11px; }}
.signals {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
.sigTop {{ display:flex; justify-content:space-between; align-items:center; }}
.sigTop b {{ font-size:12px; }}
.sigTop span {{ font-size:10px; color:var(--muted); }}
.sig {{ min-width:0; }}
.sig strong {{ display:block; margin:11px 0 2px; font-size:20px; overflow-wrap:anywhere; }}
.sig small {{ overflow-wrap:anywhere; }}
.sig.on {{ border-color:rgba(40,209,124,.5); }}
.sig.on strong,.good {{ color:var(--green); }}
.sig.off {{ border-color:rgba(255,86,99,.45); }}
.sig.off strong,.bad {{ color:var(--red); }}
.range {{ position:relative; height:74px; border:1px solid var(--line); border-radius:8px; background:#070b10; overflow:hidden; }}
.range .buy {{ position:absolute; left:0; top:0; bottom:0; width:{params["buy_q"] * 100:.1f}%; background:rgba(40,209,124,.24); }}
.range .sell {{ position:absolute; right:0; top:0; bottom:0; width:{(1 - params["sell_q"]) * 100:.1f}%; background:rgba(76,163,255,.24); }}
.range .mark {{ position:absolute; left:{marker_pos:.1f}%; top:9px; bottom:9px; width:3px; background:#fff; border-radius:3px; }}
.metricGrid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-top:8px; }}
.rules {{ display:grid; gap:8px; }}
.rule b {{ display:block; margin-bottom:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th,td {{ padding:8px 7px; border-bottom:1px solid var(--line); white-space:nowrap; text-align:left; }}
th {{ color:var(--muted); font-size:10px; background:#080d13; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.tag {{ display:inline-flex; padding:3px 7px; border-radius:999px; font-size:10px; font-weight:800; }}
.tag.ready {{ color:#062915; background:var(--green); }}
.tag.watch {{ color:#201400; background:var(--amber); }}
.tag.skip {{ color:#d4dde9; background:#293241; }}
.spark {{ width:100%; height:190px; background:#080d13; border:1px solid var(--line); border-radius:8px; }}
.pnlSvg {{ width:100%; height:205px; display:block; background:#080d13; border:1px solid var(--line); border-radius:8px; }}
.gridLine {{ stroke:#263442; stroke-width:1; }}
.zeroLine {{ stroke:#536170; stroke-width:1.4; }}
.axisText {{ fill:#9aa7b8; font-size:13px; font-weight:700; font-variant-numeric:tabular-nums; }}
.pnlBarRect {{ cursor:pointer; opacity:.88; }}
.pnlBarRect.good {{ fill:var(--green); }}
.pnlBarRect.bad {{ fill:var(--red); }}
.pnlBarRect:hover,.pnlBarRect.active {{ opacity:1; stroke:#eef6ff; stroke-width:2; }}
.barAxis {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; padding:7px 2px 0; color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }}
.barAxis span:nth-child(2),.barAxis span:nth-child(3),.barAxis span:nth-child(4) {{ text-align:center; }}
.barAxis span:last-child {{ text-align:right; }}
.chartAxis {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; padding:7px 2px 0; color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }}
.chartAxis span:nth-child(2),.chartAxis span:nth-child(3),.chartAxis span:nth-child(4) {{ text-align:center; }}
.chartAxis span:last-child {{ text-align:right; }}
.chartHead {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:12px; }}
.chartHead .chartTitle {{ margin:0; }}
.chartHead span {{ color:var(--muted); font-size:11px; white-space:nowrap; }}
.chartStats,.pnlStats {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; margin-top:8px; }}
.chartStats div,.pnlStats div {{ border:1px solid var(--line); border-radius:7px; background:#080d13; padding:7px 8px; }}
.chartStats span,.pnlStats span {{ display:block; color:var(--muted); font-size:10px; }}
.chartStats b,.pnlStats b {{ display:block; margin-top:2px; font-size:13px; font-variant-numeric:tabular-nums; }}
.pnlReadout {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px; margin-top:8px; }}
.pnlReadout div {{ border:1px solid var(--line); border-radius:7px; background:#080d13; padding:8px; }}
.pnlReadout span {{ display:block; color:var(--muted); font-size:10px; }}
.pnlReadout b {{ display:block; margin-top:2px; font-size:15px; font-variant-numeric:tabular-nums; }}
.miniGrid {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
.miniGrid.wide {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
.rightStack {{ display:grid; gap:10px; align-content:start; }}
.mainStack {{ display:grid; gap:10px; min-height:0; }}
.rightStack.side {{ align-content:start; }}
.foot {{ margin-top:10px; color:#9facbd; }}
.tableWrap {{ width:100%; overflow-x:auto; }}
.bottomGrid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; align-items:start; min-height:0; }}
.chartTitle {{ font-size:22px; margin:0 0 12px; letter-spacing:0; }}
.chartPanel {{ min-height:315px; }}
.chartPanel .spark {{ height:230px; }}
.dailyTable th,.dailyTable td {{ font-size:13px; padding:10px 10px; }}
.dailyTable th {{ font-size:11px; }}
.advice {{ color:#c7d0df; font-size:16px; line-height:1.5; padding:18px; }}
.leftStack {{ display:grid; gap:10px; align-content:start; min-height:0; }}
.fillBody {{ display:grid; align-content:start; gap:8px; }}
.sideList {{ display:grid; gap:8px; }}
.sideItem {{ border:1px solid var(--line); border-radius:8px; background:#080d13; padding:9px 10px; }}
.sideItem span {{ display:block; color:var(--muted); font-size:11px; }}
.sideItem b {{ display:block; margin-top:4px; font-size:16px; }}
.sideItem strong {{ color:var(--amber); }}
.gapList {{ display:grid; gap:8px; }}
.gap {{ display:flex; justify-content:space-between; gap:10px; border:1px solid var(--line); border-radius:8px; background:#080d13; padding:9px 10px; }}
.gap span {{ color:var(--muted); }}
.gap b {{ color:var(--amber); }}
.progress {{ height:10px; border-radius:999px; background:#202934; overflow:hidden; margin-top:8px; }}
.progress i {{ display:block; height:100%; width:{target_progress:.1f}%; background:var(--green); }}
.observeGrid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
.observeWide {{ grid-column:1 / -1; }}
@media(max-width:1240px) {{ .grid {{ grid-template-columns:330px minmax(0,1fr); }} .grid > aside.side {{ grid-column:1 / -1; grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
@media(max-width:900px) {{ .grid,.hero,.metricGrid,.signals,.prob,.miniGrid,.grid > aside.side,.bottomGrid,.observeGrid {{ grid-template-columns:1fr; }} .observeWide {{ grid-column:auto; }} .leftStack,.mainStack,.bottomGrid {{ grid-template-rows:auto; height:auto; }} .decision h1 {{ font-size:34px; }} }}
</style>
</head>
<body>
<div class="screen">
  <div class="ticker">
    <div><b>BTC/USDT</b><span>AI Signal Terminal</span><span>Bitget Spot 15m / 4h 区间</span></div>
    <div class="tickerActions">
      <div><span>LAST</span> <strong id="livePrice">{money(latest["close"])}</strong><span id="livePriceMeta" class="liveMeta">live 待刷新</span><span>DATA {source_ts}</span><span>{freshness}</span></div>
      <button id="manualRefresh" class="refreshButton" type="button">刷新</button>
      <span id="refreshCountdown" class="refreshCountdown">60s</span>
    </div>
  </div>

  <section class="grid">
    <aside class="leftStack">
      <section class="panel">
        <div class="head"><b>SIGNAL STACK</b><span>{score}/6 passed</span></div>
        <div class="body"><div class="signals">{signal_tiles}</div></div>
      </section>
      <section class="panel">
        <div class="head"><b>STRATEGY CONFIG</b><span>spot only</span></div>
        <div class="body sideList">
          <div class="sideItem"><span>本金 / 观察仓位</span><b>$10,000 / $5,500</b></div>
          <div class="sideItem"><span>入场区</span><b>4h 底部 {pct(params["buy_q"])}</b></div>
          <div class="sideItem"><span>共振门槛</span><b>{params["min_score"]}/6 signals</b></div>
          <div class="sideItem"><span>止盈规则</span><b>{pct(params["take_profit"])} 或区间 {pct(params["sell_q"])}</b></div>
          <div class="sideItem"><span>SG 主窗口</span><b>13:00-16:00</b></div>
          <div class="sideItem"><span>更新频率</span><b>现价 1s / 信号 15m / 回测 1h</b></div>
        </div>
      </section>
      <section class="panel">
        <div class="head"><b>NO TRADE FILTER</b><span>hard blocks</span></div>
        <div class="body sideList">
          <div class="sideItem"><span>价格不在低吸区</span><b><strong>{'当前触发' if not range_ok else '未触发'}</strong></b></div>
          <div class="sideItem"><span>8h 单边下跌</span><b>{'通过' if signals["not_one_way_down"] else '禁止开仓'}</b></div>
          <div class="sideItem"><span>浮亏红线</span><b>1% / 2% / 3.5%</b></div>
          <div class="sideItem"><span>数据完整性</span><b>{freshness}</b></div>
        </div>
      </section>
      <section class="panel">
        <div class="head"><b>OBSERVATION LOG</b><span>live notes</span></div>
        <div class="body fillBody">
          <div class="sideItem"><span>当前动作</span><b>{decision_cn}</b></div>
          <div class="sideItem"><span>主要阻断</span><b>{range_state}</b></div>
          <div class="sideItem"><span>复核顺序</span><b>区间 -> 共振 -> 风控 -> 成交</b></div>
          <div class="sideItem"><span>下一步</span><b>等待价格回到低吸区</b></div>
        </div>
      </section>
      <section class="panel">
        <div class="head"><b>DATA GAPS</b><span>未纳入回测</span></div>
        <div class="body gapList">
          <div class="gap"><span>1m / 5m 动量</span><b>缺历史数据</b></div>
          <div class="gap"><span>每日 288 个 5m 窗口</span><b>未扫描</b></div>
          <div class="gap"><span>订单簿失衡</span><b>缺 L2 回放</b></div>
          <div class="gap"><span>模型概率校准</span><b>未训练</b></div>
        </div>
      </section>
      <section class="panel">
        <div class="head"><b>RECENT FILLS</b><span>last 7</span></div>
        <div class="tableWrap"><table><thead><tr><th>入场</th><th>买价</th><th>卖价</th><th>退出</th><th>持仓</th><th class="num">PnL</th></tr></thead><tbody>{''.join(trade_rows)}</tbody></table></div>
      </section>
    </aside>

    <main class="mainStack">
      <section class="panel">
        <div class="head"><b>BTC 走势预测</b><span>评分映射，不是已校准概率</span></div>
        <div class="body hero">
          <div class="decision">
            <div class="kicker">CURRENT DECISION</div>
            <h1 class="{decision_class}">{decision}</h1>
            <p>{decision_cn}。当前分数够，但价格不在 4h 底部低吸区，所以不追涨。</p>
            <div class="prob">
              <div><span>模型评分</span><b>{pct_raw(model_score)}</b></div>
              <div><span>信号通过率</span><b>{pct_raw(pass_rate)}</b></div>
              <div><span>评分优势</span><b class="{tone(advantage)}">{pct_raw(advantage)}</b></div>
            </div>
          </div>
          <div class="gauge"><div class="ring"><div><b>{pct_raw(model_score)}</b><span>SCORE</span></div></div></div>
        </div>
      </section>

      <section class="panel">
        <div class="head"><b>RANGE MAP</b><span>低买高卖执行区</span></div>
        <div class="body">
          <div class="range"><div class="buy"></div><div class="sell"></div><div class="mark"></div></div>
          <div class="metricGrid">
            <div class="metric"><span>低吸线</span><b>{money(buy_zone)}</b></div>
            <div class="metric"><span>现价位置</span><b>{pct(float(latest["pos_in_range"]))}</b></div>
            <div class="metric"><span>止盈区</span><b>{money(sell_zone)}</b></div>
            <div class="metric"><span>止损线</span><b>{money(stop_zone)}</b></div>
            <div class="metric"><span>区间状态</span><b>{range_state}</b></div>
            <div class="metric"><span>SG 主窗口</span><b>13:00-16:00</b></div>
            <div class="metric"><span>单笔资金</span><b>$5,500</b></div>
            <div class="metric"><span>成本假设</span><b>0.12%</b></div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="head"><b>MARKET CONTEXT</b><span>已纳入/可辅助判断</span></div>
        <div class="body miniGrid wide">
          <div class="mini"><span>8h 涨跌</span><b class="{tone(latest["ret_8h"])}">{pct(latest["ret_8h"])}</b></div>
          <div class="mini"><span>24h 涨跌</span><b class="{tone(latest["ret_24h"])}">{pct(latest["ret_24h"])}</b></div>
          <div class="mini"><span>24h 宽度</span><b>{pct(latest["width_24h"])}</b></div>
          <div class="mini"><span>最新 15m 成交</span><b>{int(latest["trades"]):,}</b></div>
        </div>
      </section>

      <section class="bottomGrid">
        <div class="panel chartPanel">
          <div class="body">
            <div class="chartHead"><h2 class="chartTitle">90 天净值</h2><span>SGT 日期 / 账户净值</span></div>
            <svg class="spark" viewBox="0 0 620 190" preserveAspectRatio="none">
              <polyline points="{equity_pts}" fill="none" stroke="#4ca3ff" stroke-width="3"/>
            </svg>
            <div class="chartAxis">{''.join(equity_axis)}</div>
            <div class="chartStats">
              <div><span>起始</span><b>{money(equity_start)}</b></div>
              <div><span>当前</span><b class="{tone(equity_end - equity_start)}">{money(equity_end)}</b></div>
              <div><span>最高</span><b>{money(equity_high)}</b></div>
              <div><span>最低</span><b>{money(equity_low)}</b></div>
            </div>
          </div>
        </div>
        <div class="panel chartPanel">
          <div class="body">
            <div class="chartHead"><h2 class="chartTitle">30 天每日盈亏</h2><span>点击柱子查看单日</span></div>
            <svg class="pnlSvg" viewBox="0 0 {pnl_chart_w} {pnl_chart_h}" preserveAspectRatio="none">
              {''.join(pnl_grid)}
              <line x1="{pnl_l}" y1="{zero_y:.1f}" x2="{pnl_chart_w - pnl_r}" y2="{zero_y:.1f}" class="zeroLine"/>
              {''.join(pnl_bars)}
            </svg>
            <div class="barAxis">{''.join(pnl_axis)}</div>
            <div class="pnlReadout">
              <div><span>选中日期</span><b id="pnlDate">{selected_pnl["date_sgt"]}</b></div>
              <div><span>当日盈亏</span><b id="pnlValue" class="{tone(float(selected_pnl["pnl"]))}">{money(float(selected_pnl["pnl"]))}</b></div>
              <div><span>当日净值</span><b id="pnlEquity">{money(float(selected_pnl["equity"]))}</b></div>
            </div>
            <div class="pnlStats">
              <div><span>今日</span><b class="{tone(day_pnl)}">{money(day_pnl)}</b></div>
              <div><span>最高日</span><b class="{tone(best_day)}">{money(best_day)}</b></div>
              <div><span>最低日</span><b class="{tone(worst_day)}">{money(worst_day)}</b></div>
              <div><span>有交易日</span><b>{nonzero_days}/30</b></div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="body">
            <h2 class="chartTitle">最近每日结果</h2>
            <div class="tableWrap"><table class="dailyTable"><thead><tr><th>日期</th><th class="num">盈亏</th><th class="num">净值</th></tr></thead><tbody>{''.join(daily_rows)}</tbody></table></div>
          </div>
        </div>
        <div class="panel">
          <div class="body advice">
            <h2 class="chartTitle">执行建议</h2>
            <div class="sideList">
              <div class="sideItem"><span>主判断</span><b>当前价格在区间上沿，不追涨。</b></div>
              <div class="sideItem"><span>允许开仓</span><b>回到 4h 底部 {pct(params["buy_q"])} 且至少 {params["min_score"]}/6 共振。</b></div>
              <div class="sideItem"><span>出场</span><b>盈利 {pct(params["take_profit"])} 立即卖，或到区间 {pct(params["sell_q"])}。</b></div>
              <div class="sideItem"><span>今日目标</span><b>不能稳定每天 $50，先观察回撤和无效交易次数。</b></div>
              <div class="sideItem"><span>资金</span><b>$5,500 策略资金，$4,500 留现金。</b></div>
            </div>
          </div>
        </div>
      </section>
    </main>

    <aside class="rightStack side">
      <section class="panel">
        <div class="head"><b>BACKTEST</b><span>$10,000 spot</span></div>
        <div class="body miniGrid">
          <div class="mini"><span>近 30 天</span><b class="{tone(s30["total"])}">{money(s30["total"])}</b></div>
          <div class="mini"><span>近 90 天</span><b class="{tone(s90["total"])}">{money(s90["total"])}</b></div>
          <div class="mini"><span>胜率</span><b>{pct_raw(s90["win"])}</b></div>
          <div class="mini"><span>最大回撤</span><b class="bad">{pct(s90["max_dd"])}</b></div>
          <div class="mini"><span>交易次数</span><b>{s90["trades"]}</b></div>
          <div class="mini"><span>Profit Factor</span><b>{s90["profit_factor"]:.2f}</b></div>
          <div class="mini"><span>Sharpe</span><b>{s90["sharpe"]:.2f}</b></div>
          <div class="mini"><span>平均每笔</span><b class="{tone(s90["avg_trade"])}">{money(s90["avg_trade"])}</b></div>
          <div class="mini"><span>最好单笔</span><b class="good">{money(s90["best"])}</b></div>
          <div class="mini"><span>最差单笔</span><b class="bad">{money(s90["worst"])}</b></div>
          <div class="mini"><span>达 $50 天数</span><b>{pct_raw(s90["hit50"])}</b></div>
          <div class="mini"><span>亏 $50 天数</span><b>{pct_raw(s90["loss50"])}</b></div>
        </div>
      </section>

      <section class="panel">
        <div class="head"><b>TODAY / SESSION</b><span>{latest_ts_sgt.strftime("%m-%d %H:%M")} SGT</span></div>
        <div class="body">
          <div class="miniGrid">
            <div class="mini"><span>今日 PnL</span><b class="{tone(day_pnl)}">{money(day_pnl)}</b></div>
            <div class="mini"><span>$50 目标</span><b>{pct_raw(target_progress)}</b></div>
            <div class="mini"><span>交易时段</span><b>{session_text}</b></div>
            <div class="mini"><span>数据状态</span><b>{freshness}</b></div>
          </div>
          <div class="progress"><i></i></div>
        </div>
      </section>

      <section class="panel">
        <div class="head"><b>PAPER TRADING</b><span>{str(paper.get("last_update", "未运行"))[5:16] if paper else "未运行"}</span></div>
        <div class="body">
          <div class="miniGrid">
            <div class="mini"><span>模拟动作</span><b class="{paper_action_class}">{paper_action}</b></div>
            <div class="mini"><span>模拟净值</span><b class="{tone(paper_equity - paper_capital)}">{money(paper_equity)}</b></div>
            <div class="mini"><span>现金</span><b>{money(float(paper.get("cash", paper_capital) or paper_capital))}</b></div>
            <div class="mini"><span>BTC 持仓</span><b>{paper.get("btc", "0")}</b></div>
            <div class="mini"><span>浮动盈亏</span><b class="{tone(paper_unreal)}">{money(paper_unreal)}</b></div>
            <div class="mini"><span>已实现盈亏</span><b class="{tone(paper_realized)}">{money(paper_realized)}</b></div>
            <div class="mini"><span>入场价</span><b>{money(float(paper["entry_price"])) if paper.get("entry_price") else "-"}</b></div>
            <div class="mini"><span>模拟成交</span><b>{paper.get("trade_count", 0)}</b></div>
          </div>
          <div class="gapList" style="margin-top:8px">
            <div class="gap"><span>最近原因</span><b>{paper.get("last_reason", "未运行")}</b></div>
          </div>
          <div class="tableWrap" style="margin-top:8px"><table><thead><tr><th>退出</th><th>买价</th><th>卖价</th><th>原因</th><th class="num">PnL</th></tr></thead><tbody>{''.join(paper_trade_rows)}</tbody></table></div>
        </div>
      </section>

      <section class="panel">
        <div class="head"><b>BITGET OBSERVE</b><span>{obs_time[5:16] if len(obs_time) >= 16 else obs_time}</span></div>
        <div class="body">
          <div class="observeGrid">
            <div class="mini"><span>观察动作</span><b class="{obs_action_class}">{obs_action}</b></div>
            <div class="mini"><span>Bitget 实时价</span><b>{money(float(obs_price)) if obs_price else "未读取"}</b></div>
            <div class="mini"><span>UTA 可用</span><b>{obs_account.get("uta_usdt_available", "0")} USDT</b></div>
            <div class="mini"><span>计划金额</span><b>{obs_plan.get("quote_usdt", "0")} USDT</b></div>
            <div class="mini"><span>计划 BTC</span><b>{obs_plan.get("planned_size_btc", "-")}</b></div>
            <div class="mini"><span>观察止盈</span><b>{obs_plan.get("take_profit_pct", "-")}</b></div>
            <div class="mini"><span>低吸上限</span><b>${obs_plan.get("buy_zone_max", "-")}</b></div>
            <div class="mini"><span>止损线</span><b>${obs_plan.get("stop_zone", "-")}</b></div>
            <div class="mini observeWide"><span>信号状态</span><b>{obs_signal.get("score", "-")} / {obs_signal.get("pos_in_range", "-")} / {'可观察' if obs_signal.get("buy_ready") else '等待'}</b></div>
          </div>
          <div class="gapList" style="margin-top:8px">{obs_blocker_rows}</div>
        </div>
      </section>

      <section class="panel">
        <div class="head"><b>SCAN WINDOWS</b><span>最近 12 根 15m</span></div>
        <div class="tableWrap"><table><thead><tr><th>SGT</th><th>信号</th><th>位置</th><th>振幅</th><th>状态</th></tr></thead><tbody>{''.join(scan_rows)}</tbody></table></div>
      </section>

      <section class="panel">
        <div class="head"><b>RISK</b><span>live rule</span></div>
        <div class="body rules">
          <div class="rule"><b>开仓</b><span>4h 底部 {pct(params["buy_q"])} + 至少 {params["min_score"]}/6 共振。</span></div>
          <div class="rule"><b>止盈</b><span>盈利 {pct(params["take_profit"])} 立即卖，或到区间 {pct(params["sell_q"])}。</span></div>
          <div class="rule"><b>亏损</b><span>1% 停止加仓，2% 不开新仓，3.5% 必须处理。</span></div>
          <div class="rule"><b>执行</b><span>信号用已完成 15m K，下一根开盘成交；回测含 0.12% 往返成本。</span></div>
          <div class="rule"><b>持仓</b><span>最长 16 根 15m K，90 天平均持仓 {avg_hold:.1f} 根 K。</span></div>
          <div class="rule"><b>盘口</b><span>历史 L2 未接入，订单簿失衡暂不计分，避免假回测。</span></div>
        </div>
      </section>

    </aside>
  </section>

  <div class="foot">对比：旧组合 90 天 {money(old90["final_vs_initial"])} / 87 笔 / 回撤 {pct(old90["max_dd"])}；只盈利平仓实验 90 天 {money(po90["final_vs_initial"])}，回撤 {pct(po90["max_dd"])}，不建议默认使用。</div>
</div>
<script>
const livePrice = document.getElementById("livePrice");
const livePriceMeta = document.getElementById("livePriceMeta");
const fmtPrice = value => "$" + Number(value).toLocaleString("en-US", {{minimumFractionDigits:2, maximumFractionDigits:2}});
async function refreshLivePrice() {{
  try {{
    const response = await fetch("https://api.bitget.com/api/v2/spot/market/tickers?symbol=BTCUSDT", {{cache:"no-store"}});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    const row = Array.isArray(data.data) ? data.data[0] : data.data;
    livePrice.textContent = fmtPrice(row.lastPr || row.last || row.close);
    livePriceMeta.textContent = "live " + new Date().toLocaleTimeString("zh-CN", {{hour12:false}});
    livePriceMeta.style.color = "#28d17c";
  }} catch (error) {{
    livePriceMeta.textContent = "live 暂不可用";
    livePriceMeta.style.color = "#f5b642";
  }}
}}
refreshLivePrice();
setInterval(refreshLivePrice, 1000);

const pnlDate = document.getElementById("pnlDate");
const pnlValue = document.getElementById("pnlValue");
const pnlEquity = document.getElementById("pnlEquity");
document.querySelectorAll(".pnlBarRect").forEach(bar => {{
  bar.addEventListener("click", () => {{
    document.querySelectorAll(".pnlBarRect.active").forEach(active => active.classList.remove("active"));
    bar.classList.add("active");
    pnlDate.textContent = bar.dataset.date;
    pnlValue.textContent = bar.dataset.pnl;
    pnlValue.className = bar.classList.contains("bad") ? "bad" : "good";
    pnlEquity.textContent = bar.dataset.equity;
  }});
}});

let refreshLeft = 60;
const refreshCountdown = document.getElementById("refreshCountdown");
document.getElementById("manualRefresh").addEventListener("click", () => {{
  location.reload();
}});
setInterval(() => {{
  refreshLeft -= 1;
  refreshCountdown.textContent = refreshLeft + "s";
  if (refreshLeft <= 0) location.reload();
}}, 1000);
</script>
</body>
</html>
"""
    OUT.write_text(html, encoding="utf-8")
    DEPLOY_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT.name} and {DEPLOY_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
