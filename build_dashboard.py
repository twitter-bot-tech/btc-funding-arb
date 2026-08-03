"""Pro-grade quant dashboard generator.

Produces a single self-contained dashboard.html with:
  · Live ticker (BTC price refreshed via Binance public API every 15s in browser)
  · Today's scorecard with risk gauges
  · Strategy comparison metrics table (Sharpe/Sortino/Calmar/WinRate/ProfitFactor/MaxDD/UlcerIndex)
  · Monthly returns heatmap (year x month, color-coded)
  · Drawdown underwater chart
  · BTC price chart with Donchian channels + trade entry/exit markers
  · Funding rate term structure (3 exchanges, 3d/7d/30d APY)
  · Basis curve with regime shading
  · Volatility cone (14d/30d/60d/90d percentiles)
  · Decision history with allocation evolution
  · Reasoning log
  · Tabbed UI: Live | Strategies | Risk | History

Reads same data files as before.
"""
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"


# ============ Metrics ============

def compute_metrics(returns, equity, daily_pnl, positions, label):
    """Comprehensive performance metrics."""
    r = returns.fillna(0).values
    n = len(r)
    years = n / 365.0
    total_ret = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total_ret) ** (1 / max(years, 1e-9)) - 1
    mean_d = r.mean()
    std_d = r.std()
    sharpe = (mean_d * 365) / (std_d * math.sqrt(365)) if std_d > 0 else 0
    # Sortino: downside std only
    downside = r[r < 0]
    sortino = (mean_d * 365) / (downside.std() * math.sqrt(365)) if len(downside) > 1 and downside.std() > 0 else 0
    # Drawdown
    eq_arr = equity.values
    peak = np.maximum.accumulate(eq_arr)
    dd = eq_arr / peak - 1
    max_dd = dd.min()
    # Calmar = CAGR / |MaxDD|
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0
    # Ulcer index = RMS of drawdowns
    ulcer = math.sqrt((dd ** 2).mean())
    # Win rate (only on days with non-zero PnL = active days)
    active = daily_pnl[daily_pnl != 0]
    win_rate = (active > 0).sum() / max(len(active), 1)
    # Profit factor
    gross_win = active[active > 0].sum()
    gross_loss = abs(active[active < 0].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    # Avg win/loss
    avg_win = active[active > 0].mean() if (active > 0).any() else 0
    avg_loss = active[active < 0].mean() if (active < 0).any() else 0
    # Longest losing streak (consecutive losing days)
    streaks_l = 0
    cur = 0
    for x in active:
        if x < 0:
            cur += 1
            streaks_l = max(streaks_l, cur)
        else:
            cur = 0
    # Max drawdown duration (days from peak to recovery)
    max_dd_days = 0
    cur = 0
    last_peak = eq_arr[0]
    for v in eq_arr:
        if v >= last_peak:
            last_peak = v
            cur = 0
        else:
            cur += 1
            max_dd_days = max(max_dd_days, cur)
    # Time in market
    pct_in_market = (positions > 0).mean() if positions is not None else 1.0
    # Number of trades (position changes)
    trades = 0
    if positions is not None:
        trades = int((positions.diff().abs() > 0.01).sum() / 2)  # each trade has entry+exit

    return {
        "label": label,
        "years": round(years, 2),
        "total_return": float(total_ret),
        "cagr": float(cagr),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_dd": float(max_dd),
        "max_dd_days": int(max_dd_days),
        "calmar": float(calmar),
        "ulcer": float(ulcer),
        "win_rate": float(win_rate),
        "profit_factor": float(pf) if pf != float("inf") else 99.0,
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "longest_loss_streak": int(streaks_l),
        "pct_in_market": float(pct_in_market),
        "n_trades": trades,
        "vol_annual": float(std_d * math.sqrt(365)),
    }


def monthly_returns_matrix(dates, daily_pnl, capital_start=100000):
    """Return list of {year, month, return_pct} for heatmap."""
    df = pd.DataFrame({"date": pd.to_datetime(dates, utc=True), "pnl": daily_pnl.values})
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    # equity at start of each month
    df["equity"] = capital_start + df["pnl"].cumsum()
    # monthly return = (last_eq - first_eq) / first_eq for the month
    out = []
    for (y, m), g in df.groupby(["year", "month"]):
        if len(g) == 0:
            continue
        eq_start = g["equity"].iloc[0] - g["pnl"].iloc[0]  # equity at month-start
        eq_end = g["equity"].iloc[-1]
        ret = (eq_end - eq_start) / max(eq_start, 1)
        out.append({"year": int(y), "month": int(m), "ret": float(ret)})
    return out


def drawdown_series(dates, equity):
    eq = equity.values
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak - 1) * 100
    return {"labels": [d.strftime("%Y-%m-%d") for d in pd.to_datetime(dates, utc=True)],
            "dd": dd.round(3).tolist()}


def trade_markers(dates, positions, prices):
    """Find position transitions."""
    p = positions.fillna(0).values
    entries = []
    exits = []
    prev = 0
    for i, cur in enumerate(p):
        if prev <= 0.01 and cur > 0.01:
            entries.append({"x": dates.iloc[i].strftime("%Y-%m-%d"), "y": float(prices.iloc[i])})
        elif prev > 0.01 and cur <= 0.01:
            exits.append({"x": dates.iloc[i].strftime("%Y-%m-%d"), "y": float(prices.iloc[i])})
        prev = cur
    return {"entries": entries[-50:], "exits": exits[-50:]}  # last 50 trades


def volatility_cone(dates, closes):
    """Compute realized vol distribution across multiple windows."""
    s = pd.Series(closes.values, index=pd.to_datetime(dates, utc=True))
    rets = s.pct_change().dropna()
    windows = [7, 14, 30, 60, 90]
    cone = []
    for w in windows:
        if len(rets) < w + 1:
            continue
        rolling = rets.rolling(w).std() * math.sqrt(365) * 100
        rolling = rolling.dropna()
        cone.append({
            "window": w,
            "current": float(rolling.iloc[-1]) if len(rolling) > 0 else 0,
            "p10": float(rolling.quantile(0.10)),
            "p25": float(rolling.quantile(0.25)),
            "p50": float(rolling.quantile(0.50)),
            "p75": float(rolling.quantile(0.75)),
            "p90": float(rolling.quantile(0.90)),
        })
    return cone


def downsample(df, n=600):
    if len(df) <= n: return df
    return df.iloc[::max(1, len(df)//n)].reset_index(drop=True)


def load_macro():
    """Load macro + sentiment frames; returns dict for dashboard."""
    macro = {}
    fng_p = DATA / "fng.csv"
    if fng_p.exists():
        fng = pd.read_csv(fng_p)
        fng["date"] = pd.to_datetime(fng["date"])
        fng = fng.tail(730).reset_index(drop=True)
        macro["fng"] = {
            "labels": [d.strftime("%Y-%m-%d") for d in fng["date"]],
            "values": fng["fng"].tolist(),
            "latest": int(fng["fng"].iloc[-1]),
            "classification": str(fng["classification"].iloc[-1]),
            "p10": float(fng["fng"].quantile(0.10)),
            "p25": float(fng["fng"].quantile(0.25)),
            "p50": float(fng["fng"].quantile(0.50)),
            "p75": float(fng["fng"].quantile(0.75)),
            "p90": float(fng["fng"].quantile(0.90)),
        }

    ma_p = DATA / "macro_all.csv"
    if ma_p.exists():
        ma = pd.read_csv(ma_p)
        ma["date"] = pd.to_datetime(ma["date"])
        # forward-fill weekends/holidays
        ma = ma.sort_values("date").ffill().dropna(subset=["eth_close"]).tail(500).reset_index(drop=True)
        macro["assets"] = {
            "labels": [d.strftime("%Y-%m-%d") for d in ma["date"]],
            "eth": ma["eth_close"].round(2).fillna(0).tolist(),
            "spx": ma["spx_close"].round(2).fillna(0).tolist() if "spx_close" in ma else [],
            "dxy": ma["dxy_close"].round(3).fillna(0).tolist() if "dxy_close" in ma else [],
            "gold": ma["gold_close"].round(2).fillna(0).tolist() if "gold_close" in ma else [],
            "vix": ma["vix_close"].round(2).fillna(0).tolist() if "vix_close" in ma else [],
        }
        macro["_raw"] = ma  # for correlation computation

    dom_p = DATA / "btc_dominance.json"
    if dom_p.exists():
        macro["dominance"] = json.loads(dom_p.read_text())
    return macro


def correlation_matrix(macro_raw, btc_daily):
    """Compute correlations between BTC and macro assets at 30d/90d windows."""
    df = macro_raw.copy()
    df = df.merge(btc_daily.rename(columns={"date": "date", "close": "btc_close"}), on="date", how="inner")
    cols = ["btc_close", "eth_close", "spx_close", "dxy_close", "gold_close", "vix_close"]
    cols = [c for c in cols if c in df.columns]
    rets = df[cols].pct_change().dropna()
    if len(rets) < 30:
        return None
    # Full-period correlation
    full_corr = rets.corr().round(3).fillna(0)
    # 30d rolling correlation of BTC vs each
    win30 = 30
    rolling = {}
    for c in cols:
        if c == "btc_close": continue
        roll = rets["btc_close"].rolling(win30).corr(rets[c])
        rolling[c] = {
            "current": float(roll.iloc[-1]) if not roll.empty else 0,
            "history": roll.dropna().round(3).tolist(),
        }
    labels = [c.replace("_close", "").upper() for c in cols]
    matrix = []
    for i, ri in enumerate(cols):
        row = []
        for j, rj in enumerate(cols):
            row.append(float(full_corr.iloc[i, j]))
        matrix.append({"label": labels[i], "values": row})
    return {"labels": labels, "matrix": matrix, "rolling_30d": rolling,
            "rolling_dates": [d.strftime("%Y-%m-%d") for d in df["date"].iloc[win30:]]}


def monte_carlo_forward(returns_series, n_sims=1000, n_days=365, capital=100000):
    """Bootstrap historical daily returns to project forward equity paths."""
    r = returns_series.dropna().values
    if len(r) < 100:
        return None
    rng = np.random.default_rng(42)
    # Sample n_days returns n_sims times
    paths = np.zeros((n_sims, n_days + 1))
    paths[:, 0] = capital
    samples = rng.choice(r, size=(n_sims, n_days), replace=True)
    paths[:, 1:] = capital * np.cumprod(1 + samples, axis=1)
    # Percentile bands
    p5 = np.percentile(paths, 5, axis=0)
    p25 = np.percentile(paths, 25, axis=0)
    p50 = np.percentile(paths, 50, axis=0)
    p75 = np.percentile(paths, 75, axis=0)
    p95 = np.percentile(paths, 95, axis=0)
    # Final stats
    final = paths[:, -1]
    return {
        "days": list(range(n_days + 1)),
        "p5": p5.round(0).tolist(),
        "p25": p25.round(0).tolist(),
        "p50": p50.round(0).tolist(),
        "p75": p75.round(0).tolist(),
        "p95": p95.round(0).tolist(),
        "final_p5": float(p5[-1]),
        "final_p25": float(p25[-1]),
        "final_p50": float(p50[-1]),
        "final_p75": float(p75[-1]),
        "final_p95": float(p95[-1]),
        "prob_profit": float((final > capital).mean()),
        "prob_double": float((final > 2 * capital).mean()),
        "prob_lose_half": float((final < capital / 2).mean()),
        "expected_cagr": float((final.mean() / capital) - 1),
        "median_cagr": float((p50[-1] / capital) - 1),
    }


def risk_metrics(returns_series, capital=100000):
    """Kelly, VaR, CVaR."""
    r = returns_series.dropna()
    if len(r) < 30:
        return None
    mu = r.mean()
    var = r.var()
    # Continuous Kelly (Thorp formula approximation): f* = mu / sigma^2
    kelly_full = mu / var if var > 0 else 0
    kelly_half = kelly_full / 2  # half-Kelly is industry standard
    # VaR (historical)
    var_95 = float(r.quantile(0.05))
    var_99 = float(r.quantile(0.01))
    # CVaR (expected shortfall = mean of losses beyond VaR)
    cvar_95 = float(r[r <= var_95].mean()) if (r <= var_95).any() else var_95
    cvar_99 = float(r[r <= var_99].mean()) if (r <= var_99).any() else var_99
    return {
        "kelly_full": float(kelly_full),
        "kelly_half": float(kelly_half),
        "kelly_recommendation_pct": float(min(max(kelly_half, 0), 1) * 100),
        "var_95_pct": var_95 * 100,
        "var_99_pct": var_99 * 100,
        "var_95_usd": var_95 * capital,
        "var_99_usd": var_99 * capital,
        "cvar_95_pct": cvar_95 * 100,
        "cvar_99_pct": cvar_99 * 100,
        "mean_daily_pct": float(mu * 100),
        "std_daily_pct": float(r.std() * 100),
    }


def rolling_sharpe(returns_series, window=90):
    """90-day rolling Sharpe."""
    r = returns_series.dropna()
    rolling_mean = r.rolling(window).mean() * 365
    rolling_std = r.rolling(window).std() * np.sqrt(365)
    sharpe = (rolling_mean / rolling_std).dropna()
    return sharpe


def main():
    # Load
    scorecard = json.loads((ROOT / "scorecard.json").read_text())
    pl = pd.read_csv(ROOT / "portfolio_ledger.csv")
    if "alloc_yield" not in pl.columns:
        pl["alloc_yield"] = 0
    pl = pl.drop_duplicates("date", keep="last").reset_index(drop=True)

    # ===== Donchian backtest (combo file has positions + equity) =====
    don_full = pd.read_csv(DATA / "combo_equity.csv")
    don_full["date"] = pd.to_datetime(don_full["date"], utc=True, format="mixed")
    don_full = don_full.sort_values("date").reset_index(drop=True)
    # Keep two versions:
    #   don_full     = 2020-2026 full history (for monthly heatmap, drawdown)
    #   don          = 2023-2026 OOS-style window (removes 2020-2021 bull market bias from MC)
    don = don_full[don_full["date"] >= pd.Timestamp("2023-01-01", tz="UTC")].reset_index(drop=True)

    # Re-derive a pure Donchian curve (ignore overlay 0.5 positions, only full=1)
    # Use full history for monthly heatmap + DD, but flag MC source as 2023-2026 only
    don_pure_full = don_full.copy()
    don_pure_full["pos_pure"] = (don_pure_full["pos"] >= 1).astype(float)
    don_pure_full["pnl_pct_pure"] = don_pure_full["pos_pure"].shift(1).fillna(0) * don_pure_full["close"].pct_change().fillna(0)
    don_pure_full["equity_pure"] = 100000 * (1 + don_pure_full["pnl_pct_pure"]).cumprod()
    don_pure_full["daily_pnl_pure"] = don_pure_full["equity_pure"].diff().fillna(0)
    don_pure = don_pure_full  # alias for downstream code

    # OOS-window pure series (for Monte Carlo)
    don_pure_oos = don_pure_full[don_pure_full["date"] >= pd.Timestamp("2023-01-01", tz="UTC")].reset_index(drop=True)

    # BuyHold
    don["bh_eq"] = 100000 * (1 + don["close"].pct_change().fillna(0)).cumprod()
    don["bh_daily"] = don["bh_eq"].diff().fillna(0)
    don["bh_ret"] = don["close"].pct_change().fillna(0)

    # Metrics
    m_donchian = compute_metrics(don_pure["pnl_pct_pure"], don_pure["equity_pure"],
                                  don_pure["daily_pnl_pure"], don_pure["pos_pure"], "Donchian 突破")
    m_combo = compute_metrics(don["pnl_pct"], don["equity"], don["daily_pnl"], don["pos"], "Donchian+震荡")
    m_bh = compute_metrics(don["bh_ret"], don["bh_eq"], don["bh_daily"],
                            pd.Series(np.ones(len(don)), index=don.index), "买入持有")

    # Funding arb metrics
    fund_eq = pd.read_csv(DATA / "equity_curve.csv")
    fund_eq["ts"] = pd.to_datetime(fund_eq["ts"], utc=True, format="mixed")
    # daily groupby
    fund_eq["date"] = fund_eq["ts"].dt.floor("1D")
    fund_daily = fund_eq.groupby("date").agg(equity=("equity", "last"),
                                              net_pnl=("net_pnl", "sum")).reset_index()
    fund_daily["ret"] = fund_daily["equity"].pct_change().fillna(0)
    fund_daily["daily_pnl"] = fund_daily["equity"].diff().fillna(0)
    m_funding = compute_metrics(fund_daily["ret"], fund_daily["equity"], fund_daily["daily_pnl"],
                                 pd.Series(np.ones(len(fund_daily)), index=fund_daily.index), "资金费率套利")

    # Basis trade metrics
    basis_eq = pd.read_csv(DATA / "basis_equity.csv")
    basis_eq["date"] = pd.to_datetime(basis_eq["date"], utc=True, format="mixed")
    m_basis = compute_metrics(basis_eq["net_pnl"], 100000 * basis_eq["equity"],
                               (100000 * basis_eq["equity"]).diff().fillna(0),
                               pd.Series(np.ones(len(basis_eq)), index=basis_eq.index), "季度基差套利")

    # Portfolio policy metrics
    portfolio_eq = pd.read_csv(DATA / "portfolio_equity.csv")
    portfolio_eq["date"] = pd.to_datetime(portfolio_eq["date"], utc=True, format="mixed")
    m_portfolio = compute_metrics(portfolio_eq["portfolio_ret"], portfolio_eq["equity"],
                                  portfolio_eq["daily_pnl"],
                                  1 - portfolio_eq["w_cash"], "Policy Portfolio")

    sg_day = None
    sg_daily = None
    sg_trades = None
    sg_summary_p = DATA / "sg_day_range_summary.json"
    sg_daily_p = DATA / "sg_day_range_daily.csv"
    sg_trades_p = DATA / "sg_day_range_trades.csv"
    if sg_summary_p.exists() and sg_daily_p.exists():
        sg_day = json.loads(sg_summary_p.read_text())
        sg_daily = pd.read_csv(sg_daily_p)
        sg_daily["date"] = pd.to_datetime(sg_daily["date_sgt"], format="mixed")
        sg_daily["equity"] = sg_daily["equity"].astype(float)
        sg_daily["ret"] = sg_daily["equity"].pct_change().fillna(0)
        sg_daily["pnl"] = sg_daily["equity"].diff().fillna(sg_daily["equity"] - 10000)
        m_sg = compute_metrics(sg_daily["ret"], sg_daily["equity"], sg_daily["pnl"],
                               sg_daily.get("position", pd.Series(np.zeros(len(sg_daily)))),
                               "SG 白天区间")
        m_sg["n_trades"] = int(sg_day.get("full_90d", {}).get("trades", m_sg["n_trades"]))
        if sg_trades_p.exists():
            sg_trades = pd.read_csv(sg_trades_p).tail(12).to_dict(orient="records")
    else:
        m_sg = None

    combined_range = None
    combined_daily = None
    combined_summary_p = DATA / "combined_range_summary.json"
    combined_daily_p = DATA / "combined_range_daily.csv"
    if combined_summary_p.exists() and combined_daily_p.exists():
        combined_range = json.loads(combined_summary_p.read_text())
        combined_daily = pd.read_csv(combined_daily_p)
        combined_daily["date"] = pd.to_datetime(combined_daily["date_sgt"], format="mixed")
        combined_daily["equity"] = combined_daily["equity"].astype(float)
        combined_daily["ret"] = combined_daily["equity"].pct_change().fillna(0)
        combined_daily["pnl"] = combined_daily["equity"].diff().fillna(combined_daily["equity"] - 10000)
        m_combined_range = compute_metrics(
            combined_daily["ret"],
            combined_daily["equity"],
            combined_daily["pnl"],
            combined_daily.get("position", pd.Series(np.zeros(len(combined_daily)))),
            "Range 组合",
        )
        m_combined_range["n_trades"] = int(combined_range.get("last_90d", {}).get("trades", m_combined_range["n_trades"]))
    else:
        m_combined_range = None

    strategies_metrics = [m_portfolio, m_donchian, m_combo]
    if m_combined_range:
        strategies_metrics.append(m_combined_range)
    strategies_metrics.extend([m_funding, m_basis])
    if m_sg:
        strategies_metrics.append(m_sg)
    strategies_metrics.append(m_bh)

    # ===== Monthly returns heatmap (use Donchian pure) =====
    monthly = monthly_returns_matrix(don_pure["date"], don_pure["daily_pnl_pure"])

    # ===== Drawdown series =====
    dd = drawdown_series(don_pure["date"], don_pure["equity_pure"])

    # ===== Trade markers =====
    markers = trade_markers(don_pure["date"], don_pure["pos_pure"], don_pure["close"])

    # ===== BTC price + Donchian channels (last 720d) =====
    spot = pd.read_csv(DATA / "spot_8h_btcusdt.csv")
    spot["ts"] = pd.to_datetime(spot["openTime"], utc=True, format="mixed")
    spot["date"] = spot["ts"].dt.floor("1D")
    daily = spot.groupby("date").agg(high=("high", "max"), low=("low", "min"),
                                      close=("close", "last"), volume=("volume", "sum")).reset_index()
    daily["d20_high"] = daily["high"].rolling(20).max().shift(1)
    daily["d20_low"] = daily["low"].rolling(20).min().shift(1)
    daily = daily.dropna().tail(720)

    # ===== Volatility cone =====
    vol_cone = volatility_cone(daily["date"], daily["close"])

    # ===== Funding 3-exchange =====
    fme = pd.read_csv(DATA / "funding_multi_exchange.csv")
    fme["ts"] = pd.to_datetime(fme["ts"], utc=True, format="mixed")
    fme["date"] = fme["ts"].dt.floor("1D")
    fmd = fme.groupby("date").mean(numeric_only=True).reset_index().tail(720)
    # Funding APY rolling 7d
    for col in ["fr_binance", "fr_bybit", "fr_okx"]:
        if col in fmd.columns:
            fmd[col + "_apy7d"] = fmd[col].rolling(7, min_periods=1).mean() * 365 * 3 / 2 * 100  # on capital

    # ===== Basis =====
    bc = pd.read_csv(DATA / "basis_curve.csv")
    bc["date"] = pd.to_datetime(bc["date"], utc=True, format="mixed")
    bc_daily = bc.groupby(bc["date"].dt.floor("1D"))["ann_basis"].mean().reset_index().tail(720)

    # ===== Backtest curves (normalized) =====
    bt_curves = {}
    for name, df_, col in [
        ("Policy Portfolio", portfolio_eq, "equity"),
        ("Donchian 突破", don_pure, "equity_pure"),
        ("Donchian+震荡", don, "equity"),
        *([("Range 组合", combined_daily, "equity")] if combined_daily is not None else []),
        *([("SG 白天区间", sg_daily, "equity")] if sg_daily is not None else []),
        ("买入持有", don, "bh_eq"),
        ("资金费率套利", fund_daily, "equity"),
        ("季度基差套利", basis_eq.assign(equity=basis_eq["equity"]*100000), "equity"),
    ]:
        s = downsample(df_[["date" if "date" in df_.columns else "ts", col]].dropna(), 500)
        date_col = "date" if "date" in s.columns else "ts"
        bt_curves[name] = {
            "labels": [d.strftime("%Y-%m-%d") for d in pd.to_datetime(s[date_col], utc=True)],
            "equity": (s[col] / s[col].iloc[0]).round(4).tolist(),
        }

    # ===== Macro / Sentiment / Correlation / Monte Carlo / Risk =====
    # Hunter data
    hunter_snap = None
    hunter_daily = None
    spreads_ts = None
    yield_snap = None
    yp = ROOT / "yield_snapshot.json"
    if yp.exists():
        try: yield_snap = json.loads(yp.read_text())
        except Exception: yield_snap = None
    try:
        sp = ROOT / "hunter_snapshot.json"
        dp = ROOT / "hunter_daily.json"
        spc = ROOT / "spreads.csv"
        if sp.exists(): hunter_snap = json.loads(sp.read_text())
        if dp.exists(): hunter_daily = json.loads(dp.read_text())
        if spc.exists():
            sdf = pd.read_csv(spc)
            sdf["ts"] = pd.to_datetime(sdf["ts"], utc=True, format="mixed")
            # Last 48h, 6 main coins
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=48)
            sdf = sdf[sdf["ts"] >= cutoff]
            target_coins = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
            spreads_ts = {"labels": [], "series": {}, "summary": {}}
            for c in target_coins:
                csub = sdf[sdf["coin"] == c].sort_values("ts")
                if len(csub) < 2: continue
                if not spreads_ts["labels"]:
                    spreads_ts["labels"] = [t.strftime("%H:%M") for t in csub["ts"]]
                spreads_ts["series"][c] = csub["gross_bps"].round(2).tolist()
                spreads_ts["summary"][c] = {
                    "last": float(csub["gross_bps"].iloc[-1]),
                    "mean": float(csub["gross_bps"].mean()),
                    "max": float(csub["gross_bps"].max()),
                    "min": float(csub["gross_bps"].min()),
                    "best_buy_mode": str(csub["best_buy"].mode().iloc[0]) if len(csub) else "-",
                    "best_sell_mode": str(csub["best_sell"].mode().iloc[0]) if len(csub) else "-",
                    "n_samples": len(csub),
                }
    except Exception as e:
        print(f"Hunter data skipped: {e}")

    macro = load_macro()
    corr = None
    if "_raw" in macro:
        btc_daily_for_corr = daily[["date", "close"]].copy()
        btc_daily_for_corr["date"] = pd.to_datetime(btc_daily_for_corr["date"]).dt.tz_localize(None)
        macro_raw = macro["_raw"].copy()
        macro_raw["date"] = pd.to_datetime(macro_raw["date"])
        corr = correlation_matrix(macro_raw, btc_daily_for_corr)
        del macro["_raw"]

    # Monte Carlo using OOS (2023-2026) returns to remove 2020-2021 bull-market bias
    # Use $10K capital to match target framing
    mc = monte_carlo_forward(don_pure_oos["pnl_pct_pure"], capital=10000)
    mc_bh = monte_carlo_forward(don["bh_ret"], capital=10000)

    # Risk metrics
    risk_donchian = risk_metrics(don_pure["pnl_pct_pure"])
    risk_bh = risk_metrics(don["bh_ret"])

    # Rolling Sharpe
    rs = rolling_sharpe(don_pure["pnl_pct_pure"])
    rs_dates = [d.strftime("%Y-%m-%d") for d in don_pure["date"].iloc[-len(rs):]]

    # ===== Bundle =====
    bundle = {
        "scorecard": scorecard,
        "metrics": strategies_metrics,
        "monthly_returns": monthly,
        "drawdown": dd,
        "btc": {
            "labels": [d.strftime("%Y-%m-%d") for d in daily["date"]],
            "close": daily["close"].round(2).tolist(),
            "d20_high": daily["d20_high"].round(2).tolist(),
            "d20_low": daily["d20_low"].round(2).tolist(),
            "volume": daily["volume"].round(0).tolist(),
        },
        "trade_markers": markers,
        "vol_cone": vol_cone,
        "funding": {
            "labels": [d.strftime("%Y-%m-%d") for d in fmd["date"]],
            "binance_apy": fmd.get("fr_binance_apy7d", pd.Series([0]*len(fmd))).fillna(0).round(2).tolist(),
            "bybit_apy": fmd.get("fr_bybit_apy7d", pd.Series([0]*len(fmd))).fillna(0).round(2).tolist(),
            "okx_apy": fmd.get("fr_okx_apy7d", pd.Series([0]*len(fmd))).fillna(0).round(2).tolist(),
        },
        "basis": {
            "labels": [d.strftime("%Y-%m-%d") for d in bc_daily["date"]],
            "ann_pct": (bc_daily["ann_basis"] * 100).round(2).tolist(),
        },
        "backtests": bt_curves,
        "history": {
            "labels": pl["date"].tolist(),
            "btc_close": pl["btc_close"].tolist(),
            "alloc_btc": pl["alloc_btc"].tolist(),
            "alloc_funding": pl["alloc_funding"].tolist(),
            "alloc_basis": pl["alloc_basis"].tolist(),
            "alloc_yield": pl["alloc_yield"].tolist(),
            "alloc_cash": pl["alloc_cash"].tolist(),
            "expected_daily": pl["expected_daily"].tolist(),
            "expected_apy": pl["expected_apy"].tolist(),
            "best_funding_apy": pl["best_funding_apy_7d"].tolist(),
            "ann_basis": pl["ann_basis"].tolist(),
            "vol_14d": pl["vol_14d"].tolist(),
        },
        "macro": macro,
        "correlation": corr,
        "monte_carlo": {"donchian": mc, "buyhold": mc_bh},
        "risk": {"donchian": risk_donchian, "buyhold": risk_bh},
        "rolling_sharpe": {"labels": rs_dates, "values": rs.round(3).tolist()},
        "hunter": {"snapshot": hunter_snap, "daily": hunter_daily, "spreads": spreads_ts},
        "yield": yield_snap,
        "sg_day": {"summary": sg_day, "recent_trades": sg_trades},
        "combined_range": combined_range,
    }

    html = HTML_TEMPLATE.replace("__BUNDLE__", json.dumps(bundle, ensure_ascii=False))
    (ROOT / "dashboard.html").write_text(html, encoding="utf-8")
    print(f"Wrote dashboard.html ({len(html)/1024:.1f} KB)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>BTC Quant Terminal · Super Agent</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root{
  --bg:#06060d;--panel:#0d0f17;--panel2:#13161f;--border:#1f2330;--text:#e6edf3;--muted:#7d8590;--dim:#484f58;
  /* Neon palette */
  --accent:#ff3d8a;     /* magenta — primary */
  --accent2:#00d9ff;    /* cyan — secondary */
  --accent3:#39ff14;    /* lime — tertiary */
  --green:#39ff14;--red:#ff3366;--blue:#00d9ff;--yellow:#ffd700;--purple:#b87fff;
  --grid:#161b25;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue",sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
.app{display:flex;min-height:100vh}

/* Sidebar */
.sb{width:220px;background:var(--panel);border-right:1px solid var(--border);position:fixed;top:0;bottom:0;left:0;display:flex;flex-direction:column;z-index:10}
.sb-logo{padding:18px 20px;border-bottom:1px solid var(--border)}
.sb-logo .ttl{font-size:13px;font-weight:800;color:var(--accent);letter-spacing:2px;text-shadow:0 0 14px var(--accent)}
.sb-logo .sub{font-size:10px;color:var(--accent2);margin-top:4px;letter-spacing:2px;text-shadow:0 0 8px var(--accent2)}
.sb-logo .sub{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:.5px}
.sb-nav{flex:1;padding:8px 0;overflow-y:auto}
.sb-nav a{display:flex;align-items:center;padding:10px 20px;color:var(--muted);text-decoration:none;font-size:13px;border-left:2px solid transparent}
.sb-nav a:hover{color:var(--text);background:rgba(255,61,138,.04)}
.sb-nav a.on{color:var(--text);background:rgba(255,61,138,.08);border-left-color:var(--accent)}
.sb-nav .grp{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;padding:14px 20px 6px}
.sb-foot{padding:14px 20px;border-top:1px solid var(--border);font-size:10px;color:var(--muted)}
.sb-foot .dot{display:inline-block;width:6px;height:6px;background:var(--green);border-radius:50%;margin-right:6px;box-shadow:0 0 8px var(--green);animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}
@keyframes neon-flicker{0%,18%,22%,25%,53%,57%,100%{opacity:1}20%,24%,55%{opacity:.7}}
@keyframes scroll-log{from{transform:translateY(0)}to{transform:translateY(-100%)}}

/* Glow utility */
.glow{text-shadow:0 0 12px currentColor,0 0 24px currentColor}
.glow-soft{text-shadow:0 0 8px currentColor}

/* LIVE badge */
.live-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border:1px solid var(--accent);border-radius:3px;font-size:10px;font-weight:700;color:var(--accent);letter-spacing:1px;text-transform:uppercase;background:rgba(255,61,138,.08);box-shadow:0 0 14px rgba(255,61,138,.3)}
.live-badge::before{content:"";width:6px;height:6px;background:var(--accent);border-radius:50%;box-shadow:0 0 8px var(--accent);animation:pulse 1.2s infinite}

/* Main */
.main{flex:1;margin-left:220px}

/* Top ticker */
.tick{background:var(--panel);border-bottom:1px solid var(--border);padding:12px 32px;display:flex;align-items:center;gap:32px;position:sticky;top:0;z-index:9}
.tick .item{display:flex;flex-direction:column;gap:2px}
.tick .item .lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.tick .item .val{font-size:20px;font-weight:700;font-feature-settings:"tnum";text-shadow:0 0 12px currentColor;letter-spacing:-.3px}
.tick .item .val.green{color:var(--green)} .tick .item .val.red{color:var(--red)} .tick .item .val.accent{color:var(--accent)}
.tick .item .chg{font-size:11px;color:var(--muted);font-feature-settings:"tnum"}
.tick .item .chg.green{color:var(--green)} .tick .item .chg.red{color:var(--red)}
.tick .sep{width:1px;background:var(--border);height:34px}
.tick .right{margin-left:auto;font-size:11px;color:var(--muted)}

/* Content */
.content{padding:24px 32px 64px}
section{margin-bottom:48px;scroll-margin-top:96px}
section h2{font-size:18px;margin:0 0 4px;font-weight:700;display:flex;align-items:center;gap:10px;letter-spacing:-.3px;text-transform:uppercase}
section h2 .pill{font-size:9px;font-weight:700;padding:3px 10px;border-radius:2px;background:transparent;color:var(--accent);border:1px solid var(--accent);letter-spacing:1.5px;box-shadow:0 0 12px rgba(255,61,138,.2)}
section .sub{color:var(--muted);font-size:12px;margin-bottom:16px}

/* Cards */
.grid{display:grid;gap:14px}
.g-4{grid-template-columns:repeat(4,1fr)}
.g-3{grid-template-columns:repeat(3,1fr)}
.g-2{grid-template-columns:repeat(2,1fr)}
.g-mix{grid-template-columns:2fr 1fr}

/* Mobile menu trigger - hidden on desktop */
.menu-btn{display:none;position:fixed;top:14px;left:14px;z-index:30;width:42px;height:42px;background:var(--panel2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:20px;cursor:pointer;align-items:center;justify-content:center}
.menu-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:19}

/* Tablet (≤ 1100px) */
@media(max-width:1100px){
  .g-4{grid-template-columns:repeat(2,1fr)}
  .g-3{grid-template-columns:repeat(2,1fr)}
  .g-mix{grid-template-columns:1fr}
  .content{padding:24px}
  .tick{padding:12px 24px;gap:24px}
}

/* Mobile (≤ 768px) — drawer sidebar */
@media(max-width:768px){
  html,body,.app{overflow-x:hidden;max-width:100vw}
  .app{width:100vw}
  .sb{transform:translateX(-100%);transition:transform .25s ease;width:240px;z-index:25;box-shadow:4px 0 20px rgba(0,0,0,.5)}
  .sb.open{transform:translateX(0)}
  .sb-nav a{font-size:14px}
  .sb-nav .grp{font-size:10px;padding:14px 20px 6px}
  .menu-btn{display:flex}
  .menu-overlay.show{display:block}
  /* CRITICAL: min-width:0 lets flex child shrink below content size, enabling inner overflow */
  .main{margin-left:0;min-width:0;width:100%;flex:1 1 0;max-width:100vw}
  .g-4,.g-3,.g-2,.g-mix{grid-template-columns:1fr}
  .content{padding:14px 12px 48px;max-width:100%}

  /* Top ticker: wrap items */
  .tick{padding:60px 12px 12px;gap:12px;flex-wrap:wrap;position:relative;top:0}
  .tick .item{flex:1 1 calc(50% - 6px);min-width:0}
  .tick .item .lbl{font-size:9px}
  .tick .item .val{font-size:14px}
  .tick .item .chg{font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tick .sep{display:none}
  .tick .right{display:none}

  /* Sections */
  section{margin-bottom:32px;scroll-margin-top:80px}
  section h2{font-size:16px;flex-wrap:wrap}
  section h2 .pill{font-size:9px}
  section .sub{font-size:11px;margin-bottom:12px}

  /* Cards */
  .card{padding:14px}
  .card .val{font-size:18px}
  .card .lbl{font-size:9px}
  .card .delta{font-size:10px}

  /* Charts */
  .chart{padding:14px}
  .chart h3{font-size:13px;margin-bottom:10px}
  canvas{max-height:240px!important}
  .chart.tall canvas{max-height:300px!important}

  /* Allocation bar */
  .alloc{height:36px}
  .alloc div{font-size:10px}

  /* Tables → wrap each chart in a scrollable container. CRITICAL: min-width:0 lets grid
     children shrink below content size so overflow can take effect. */
  .grid > *{min-width:0;max-width:100%}
  .chart{min-width:0;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;position:relative}
  .chart canvas{max-width:100%}
  .chart table{min-width:max-content;white-space:nowrap}
  section > table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap;max-width:100%}
  section > table thead,section > table tbody{display:table;min-width:max-content}
  th,td{padding:8px 10px;font-size:11px}
  th{font-size:9px}

  /* Scroll hint shadow on right */
  .chart.has-scroll::after{content:"";position:sticky;top:0;right:0;float:right;width:24px;height:100%;background:linear-gradient(to right,transparent,rgba(13,17,23,.95));pointer-events:none;margin-top:-100%}

  /* Heatmap: smaller cells + horizontal scroll */
  #heat{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .heatmap{min-width:520px;font-size:9px}
  .heatmap .cell{font-size:9px}

  /* F&G gauge */
  #fngGauge > div > div:first-child{font-size:48px!important}

  /* Vol cone */
  .cone{min-height:160px;padding:12px 0;gap:3px}
  .cone .col{min-width:0}
  .cone .col .lbl{font-size:10px}
  .cone .col .v{font-size:9px}
  .cone .col .v[style*="font-size:9px"]{display:none} /* hide secondary label on small */

  /* Reasons */
  .reasons{font-size:11px;padding:12px}

  /* Tabs */
  .tabs button{padding:8px 12px;font-size:11px}
}

/* Small mobile (≤ 480px) */
@media(max-width:480px){
  .tick .item{flex:1 1 calc(50% - 6px)}
  .card .val{font-size:16px}
  canvas{max-height:220px!important}
  .chart.tall canvas{max-height:280px!important}
  section h2{font-size:14px}
}

/* iOS safe area (notch + Dynamic Island) */
@supports(padding:max(0px)){
  .tick{padding-top:max(60px,calc(env(safe-area-inset-top) + 56px))}
  .menu-btn{top:max(14px,calc(env(safe-area-inset-top) + 8px));left:max(14px,env(safe-area-inset-left))}
  .content{padding-left:max(14px,env(safe-area-inset-left));padding-right:max(14px,env(safe-area-inset-right))}
}

.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:18px}
.card .lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.card .val{font-size:28px;font-weight:700;margin-top:6px;font-feature-settings:"tnum";letter-spacing:-.5px;text-shadow:0 0 14px currentColor}
.card .val.green{color:var(--green)} .card .val.red{color:var(--red)} .card .val.accent{color:var(--accent)} .card .val.blue{color:var(--blue)} .card .val.yellow{color:var(--yellow)}
.card{background:linear-gradient(180deg,var(--panel) 0%,#0a0c14 100%);transition:border-color .2s}
.card:hover{border-color:var(--accent)}
.card .delta{font-size:11px;color:var(--muted);margin-top:4px}
.card .bar{height:4px;background:var(--border);border-radius:2px;margin-top:10px;overflow:hidden}
.card .bar i{display:block;height:100%;background:var(--accent);border-radius:2px}

/* Gauge */
.gauge{height:6px;background:var(--border);border-radius:3px;margin-top:8px;position:relative;overflow:hidden}
.gauge .fill{height:100%;border-radius:3px}
.gauge .fill.green{background:var(--green)} .gauge .fill.yellow{background:var(--yellow)} .gauge .fill.red{background:var(--red)}
.gauge .mark{position:absolute;top:-3px;width:2px;height:12px;background:var(--text)}

/* Alloc bar */
.alloc{display:flex;height:44px;border-radius:6px;overflow:hidden;background:var(--border)}
.alloc div{display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.4);transition:flex .3s}
.alloc .btc{background:var(--accent)} .alloc .fund{background:var(--blue)} .alloc .bas{background:var(--yellow)} .alloc .cash{background:#48505a}

/* Chart wraps */
.chart{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:18px;margin-bottom:14px;position:relative}
.chart h3{margin:0 0 14px;font-size:13px;font-weight:700;display:flex;align-items:center;gap:8px;text-transform:uppercase;letter-spacing:1px;color:var(--accent2)}
.chart h3 .right{margin-left:auto;font-size:11px;color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0}
.chart::before{content:"";position:absolute;top:0;left:18px;right:18px;height:1px;background:linear-gradient(90deg,transparent,var(--accent),transparent)}
canvas{width:100%!important;max-height:340px}
.chart.tall canvas{max-height:440px}

/* Tables */
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden;font-feature-settings:"tnum"}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);font-size:12px}
th{color:var(--muted);font-weight:500;background:#0a0d12;font-size:10px;text-transform:uppercase;letter-spacing:.6px}
td.num{text-align:right;font-feature-settings:"tnum"}
td.good{color:var(--green)} td.bad{color:var(--red)}
tr:last-child td{border-bottom:none}
tr.row-hi{background:rgba(255,61,138,.05)}

/* Badge */
.b{display:inline-block;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.3px}
.b.buy{background:rgba(57,255,20,.12);color:var(--green)}
.b.sell{background:rgba(255,51,102,.12);color:var(--red)}
.b.hold{background:rgba(125,133,144,.12);color:var(--muted)}

/* Monthly heatmap */
.heatmap{display:grid;grid-template-columns:60px repeat(12,1fr);gap:3px;font-size:11px}
.heatmap .head{color:var(--muted);text-align:center;padding:6px 0;font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.4px}
.heatmap .yr{color:var(--muted);font-weight:500;text-align:right;padding-right:10px;display:flex;align-items:center;justify-content:flex-end}
.heatmap .cell{aspect-ratio:1.6;display:flex;align-items:center;justify-content:center;border-radius:4px;font-weight:600;font-feature-settings:"tnum";color:#fff;font-size:10px;cursor:default}
.heatmap .cell.empty{background:transparent;color:transparent}

/* Vol cone */
.cone{display:flex;gap:6px;align-items:flex-end;padding:20px 0;min-height:200px}
.cone .col{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;position:relative}
.cone .col .bar{width:80%;background:rgba(0,217,255,.15);border:1px solid rgba(0,217,255,.4);border-radius:4px;position:relative;display:flex;flex-direction:column}
.cone .col .cur{width:90%;height:3px;background:var(--accent);position:absolute;border-radius:2px;left:5%}
.cone .col .lbl{font-size:11px;color:var(--muted);margin-top:6px;font-weight:500}
.cone .col .v{font-size:11px;color:var(--text);font-feature-settings:"tnum"}

/* Reasoning */
.reasons{background:#0a0d12;border-radius:6px;padding:16px;font-size:12px;color:var(--muted);font-family:"SF Mono",Menlo,monospace;line-height:1.8}

.dim{color:var(--muted)}

/* Tabs */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:18px}
.tabs button{background:transparent;border:none;color:var(--muted);font-size:12px;font-weight:500;padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;letter-spacing:.4px}
.tabs button.on{color:var(--text);border-bottom-color:var(--accent)}
.tabs button:hover{color:var(--text)}

</style>
</head>
<body>
<button class="menu-btn" id="menuBtn" aria-label="菜单">☰</button>
<div class="menu-overlay" id="menuOverlay"></div>
<div class="app">
  <aside class="sb" id="sidebar">
    <div class="sb-logo">
      <div class="ttl">⬢ NEON · QUANT</div>
      <div class="sub">BTC TERMINAL · v2.0</div>
    </div>
    <nav class="sb-nav">
      <div class="grp">实时数据</div>
      <a href="#dash" class="on">► 决策面板</a>
      <a href="#spreads">实时跨所价差</a>
      <a href="#thresholds">动态阈值</a>
      <a href="#hunter">🎯 套利猎手 24/7</a>
      <a href="#yield">💰 固收 · Pendle PT</a>
      <a href="#sentiment">情绪 · 恐惧贪婪</a>
      <a href="#macro">宏观资产</a>
      <a href="#correlation">相关性矩阵</a>
      <a href="#donchian">趋势 · 突破策略</a>
      <a href="#sgday">SG 白天区间</a>
      <a href="#rangecombo">Range 组合</a>
      <a href="#funding">套利 · 资金费率</a>
      <a href="#basis">套利 · 期现基差</a>
      <a href="#vol">波动率锥</a>
      <div class="grp">策略分析</div>
      <a href="#metrics">策略对比</a>
      <a href="#equity">净值曲线</a>
      <a href="#heatmap">月度热力</a>
      <a href="#drawdown">回撤分析</a>
      <a href="#rollsharpe">滚动夏普</a>
      <div class="grp">风险实验室</div>
      <a href="#montecarlo">蒙特卡洛</a>
      <a href="#riskcalc">凯利 &amp; VaR</a>
      <div class="grp">历史</div>
      <a href="#decisions">决策回放</a>
      <a href="#reasoning">推理日志</a>
      <a href="#execlog">EXEC LOG</a>
    </nav>
    <div class="sb-foot"><span class="dot"></span><span id="tickStatus">实时 · BTC 价格</span></div>
  </aside>

  <main class="main">
    <div class="tick">
      <div class="item">
        <span class="lbl">BTC/USDT · 币安</span>
        <span class="val" id="liveBtc">--</span>
        <span class="chg" id="liveBtcChg">--</span>
      </div>
      <div class="sep"></div>
      <div class="item">
        <span class="lbl">24 小时成交量</span>
        <span class="val" id="liveVol">--</span>
        <span class="chg" id="live24h">--</span>
      </div>
      <div class="sep"></div>
      <div class="item">
        <span class="lbl">Donchian 信号</span>
        <span class="val accent" id="liveSig">--</span>
        <span class="chg" id="liveSigDist">--</span>
      </div>
      <div class="sep"></div>
      <div class="item">
        <span class="lbl">最佳资金费率 7d APY</span>
        <span class="val" id="liveFund">--</span>
        <span class="chg" id="liveFundVenue">--</span>
      </div>
      <div class="sep"></div>
      <div class="item">
        <span class="lbl">今日资金分配</span>
        <span class="val" id="liveAlloc">--</span>
        <span class="chg" id="liveExp">--</span>
      </div>
      <div class="right">更新于 <span id="upd">--</span></div>
    </div>

    <div class="content">

    <section id="dash">
      <h2>SUPER · DECIDER <span class="pill" id="regimePill">--</span><span class="live-badge">LIVE</span></h2>
      <div class="sub" id="dashSub"></div>
      <div class="grid g-4" id="kpiCards"></div>
      <br>
      <div class="grid g-mix" style="margin-top:10px">
        <div class="chart"><h3>资金分配 <span class="right">% of $10K</span></h3><div class="alloc" id="allocBar"></div>
          <table style="margin-top:14px;border:none" id="allocTbl"></table>
        </div>
        <div class="chart"><h3>风险仪表</h3>
          <div id="gauges" style="display:flex;flex-direction:column;gap:18px"></div>
        </div>
      </div>
    </section>

    <section id="spreads">
      <h2>实时跨所价差<span class="pill">CROSS-EXCHANGE</span><span class="live-badge">48H</span></h2>
      <div class="sub">每 60 秒扫描一次 · gross_bps = 最高买价跨所卖给最低卖价跨所的毛差 · 入场门槛 ~15 bps</div>
      <div class="chart"><h3>6 主流币 48 小时跨所价差（毛 bps）</h3><canvas id="spreadChart"></canvas></div>
      <table id="spreadTbl"></table>
    </section>

    <section id="thresholds">
      <h2>动态阈值<span class="pill">ADAPTIVE</span><span class="live-badge">P75/P90</span></h2>
      <div class="sub">入场门槛随 30 日历史分位调整 · 还要跑赢 USDT 4.5% 现金底线</div>
      <div class="grid g-2" id="thrCards"></div>
    </section>

    <section id="hunter">
      <h2>HUNTER · 24/7<span class="pill">$10K SPOT</span><span class="live-badge">SCANNING</span></h2>
      <div class="sub">每 60 秒扫一次：跨所价差 + 三角套利 + 稳定币脱锚 · 只记录扣费后净盈利 > 0 的机会</div>
      <div class="grid g-4" id="hunterStats"></div>
      <br>
      <div class="chart"><h3>当前快照 · 最近一次扫描</h3>
        <table id="hunterCurrentTbl"></table>
      </div>
      <div class="chart"><h3>历史聚合（按天）</h3>
        <table id="hunterDailyTbl"></table>
      </div>
    </section>

    <section id="yield">
      <h2>固收 · Pendle PT<span class="pill">CHAIN YIELD</span><span class="live-badge">LIVE</span></h2>
      <div class="sub">链上零息票据 · 锁定到期收益率 · 用作 BTC 策略闲置资金的"现金底仓"，与 Donchian/套利正交</div>
      <div class="grid g-4" id="yieldStats"></div>
      <br>
      <div class="chart"><h3>Pendle 稳定币 PT · 实时市场（按 APY 降序）</h3>
        <table id="yieldTbl"></table>
      </div>
    </section>

    <section id="sentiment">
      <h2>情绪指数 · 恐惧贪婪<span class="pill" id="fngPill">--</span></h2>
      <div class="sub">加密恐惧贪婪指数 · 0 极度恐惧（历史底部信号） · 100 极度贪婪（历史顶部信号）</div>
      <div class="grid g-mix">
        <div class="chart"><h3>恐惧贪婪历史（730 天）</h3><canvas id="fngChart"></canvas></div>
        <div class="chart"><h3>今日值</h3><div id="fngGauge"></div></div>
      </div>
    </section>

    <section id="macro">
      <h2>宏观资产对比<span class="pill">市场状态</span></h2>
      <div class="sub">所有资产标准化到 100 起点 · 看 BTC 与传统资产的相对走势</div>
      <div class="chart tall"><h3>BTC / ETH / 标普 / 美元指数 / 黄金 / 恐慌指数 · 近 500 天</h3><canvas id="macroChart"></canvas></div>
      <div class="grid g-4" id="domCards"></div>
    </section>

    <section id="correlation">
      <h2>相关性矩阵<span class="pill">状态识别器</span></h2>
      <div class="sub">资产日收益率相关系数 · 全周期 + 30 日滚动 · 与标普相关 > 0.5 = 宏观风险驱动</div>
      <div class="grid g-mix">
        <div class="chart"><h3>全周期相关矩阵</h3><div id="corrMatrix"></div></div>
        <div class="chart"><h3>BTC 与各资产 30 天滚动相关性</h3><canvas id="corrRoll"></canvas></div>
      </div>
    </section>

    <section id="donchian">
      <h2>趋势层 · Donchian(20)<span class="pill">趋势跟随</span></h2>
      <div class="sub">现货收盘 · 20 日通道 · 历史信号入场/出场标记（最近 50 笔）</div>
      <div class="chart tall"><h3>BTC vs 20 日通道 · 含交易标记</h3><canvas id="btcChart"></canvas></div>
    </section>

    <section id="sgday">
      <h2>SG 白天区间<span class="pill">日内低买高卖</span></h2>
      <div class="sub">新加坡时间 08:00-18:00 内选时段 · 近 N 小时高低区间 · 完成 K 线发信号，下一根 K 线开盘成交</div>
      <div class="grid g-4" id="sgDayStats"></div>
      <br>
      <div class="grid g-mix">
        <div class="chart"><h3>白天小时波动稳定度 <span class="right">近 90 天</span></h3><canvas id="sgHourChart"></canvas></div>
        <div class="chart"><h3>最近交易</h3><table id="sgTradesTbl"></table></div>
      </div>
    </section>

    <section id="rangecombo">
      <h2>Range 组合<span class="pill">主策略 + SG 辅助</span></h2>
      <div class="sub">主策略 70% 资金负责全天 4h 区间机会 · SG 辅助 30% 资金只在震荡/低波动/非单边下跌时开仓</div>
      <div class="grid g-4" id="rangeComboStats"></div>
    </section>

    <section id="funding">
      <h2>套利层 · 资金费率<span class="pill">Delta 中性</span></h2>
      <div class="sub">7 日滚动 APY（部署资金 = 2 倍名义本金），3 所并行 · 入场门槛 8%</div>
      <div class="chart"><h3>3 所 7 日 APY 历史</h3><canvas id="fundChart"></canvas></div>
      <table id="fundTbl"></table>
    </section>

    <section id="basis">
      <h2>套利层 · 季度基差<span class="pill">Carry 套利</span></h2>
      <div class="sub">季度合约 vs 现货年化基差 · 正值升水 · 入场门槛 10%</div>
      <div class="chart"><h3>BTC 年化基差历史 (%)</h3><canvas id="basisChart"></canvas></div>
    </section>

    <section id="vol">
      <h2>波动率锥<span class="pill">波动率状态</span></h2>
      <div class="sub">实际波动率分位数 vs 当前 · 蓝条 = P25-P75 区间，橙线 = 当前 · 越高市场越混乱</div>
      <div class="chart"><h3>14 天 / 30 天 / 60 天 / 90 天 实际波动率</h3>
        <div class="cone" id="volCone"></div>
      </div>
    </section>

    <section id="metrics">
      <h2>策略对比<span class="pill">8 个策略</span></h2>
      <div class="sub">7 年回测 · 部署资金 = $10K · 含手续费 + 滑点</div>
      <div class="chart" style="padding:0"><table id="metricsTbl"></table></div>
    </section>

    <section id="equity">
      <h2>净值曲线<span class="pill">基准化</span></h2>
      <div class="sub">起始基准化 1.0 · 对数坐标看复合增长更清晰</div>
      <div class="chart tall"><h3>策略净值对比</h3>
        <div class="tabs">
          <button class="on" onclick="setEqScale('linear')">线性</button>
          <button onclick="setEqScale('logarithmic')">对数</button>
        </div>
        <canvas id="eqChart"></canvas>
      </div>
    </section>

    <section id="heatmap">
      <h2>月度收益热力图<span class="pill">Donchian 策略</span></h2>
      <div class="sub">每个格子 = 该月策略收益 · 绿色 = 盈利，红色 = 亏损，深浅 = 幅度</div>
      <div class="chart"><div id="heat"></div></div>
    </section>

    <section id="drawdown">
      <h2>回撤分析<span class="pill">水下曲线</span></h2>
      <div class="sub">从峰值的最大下跌幅度 · 用来检验心理承受能力</div>
      <div class="chart"><h3>回撤水下曲线</h3><canvas id="ddChart"></canvas></div>
    </section>

    <section id="rollsharpe">
      <h2>滚动夏普 (90 天)<span class="pill">稳定性</span></h2>
      <div class="sub">如果夏普长期 &gt; 1 = 策略稳定 · 出现长时间负值 = alpha 已死</div>
      <div class="chart"><h3>Donchian 90 天滚动夏普</h3><canvas id="rsChart"></canvas></div>
    </section>

    <section id="montecarlo">
      <h2>MONTE · CARLO<span class="pill">1000 PATHS · 1Y</span></h2>
      <div class="sub">基于历史日收益分布，重采样 1000 条未来 365 天路径 · 5%-95% 分位区间</div>
      <div class="grid g-4" id="mcStats"></div>
      <div class="chart tall"><h3>Donchian 未来 1 年资金曲线（$10K 起跑）</h3><canvas id="mcChart"></canvas></div>
    </section>

    <section id="riskcalc">
      <h2>KELLY · VAR · LAB<span class="pill">RISK</span></h2>
      <div class="sub">凯利准则告诉你最佳仓位百分比 · VaR 告诉你 95%/99% 置信下单日最大损失</div>
      <div class="grid g-2">
        <div class="chart"><h3>Donchian 策略风险参数</h3><table id="riskTblDon"></table></div>
        <div class="chart"><h3>买入持有 风险参数（对照）</h3><table id="riskTblBH"></table></div>
      </div>
    </section>

    <section id="decisions">
      <h2>决策回放<span class="pill" id="decCount">--</span></h2>
      <div class="sub">超级智能体历史决策时间线</div>
      <div class="chart"><h3>资金分配演化</h3><canvas id="allocHist"></canvas></div>
      <div class="chart"><h3>预期日盈亏 vs $50 目标</h3><canvas id="pnlHist"></canvas></div>
      <table id="histTbl"></table>
    </section>

    <section id="reasoning">
      <h2>推理日志<span class="pill">TODAY</span></h2>
      <div class="sub">超级智能体今日决策推理链</div>
      <div class="reasons" id="reasonBox"></div>
    </section>

    <section id="execlog">
      <h2>EXECUTION · LOG<span class="pill">SYSTEM TAIL</span><span class="live-badge">STREAM</span></h2>
      <div class="sub">最近 60 次系统事件 · hunter 扫描 / agent 决策 / 信号检测</div>
      <div class="chart" style="padding:0;background:#040508">
        <div id="execLogBox" style="font-family:'SF Mono',Menlo,monospace;font-size:11px;line-height:1.7;padding:18px;max-height:340px;overflow-y:auto;color:var(--muted)"></div>
      </div>
    </section>

    </div>
  </main>
</div>

<script>
const D = __BUNDLE__;
const fmt = n => n.toLocaleString(undefined,{maximumFractionDigits:0});
const fmt2 = n => n.toLocaleString(undefined,{maximumFractionDigits:2});
const fullNum = n => (n>=0?"+":"") + Number(n||0).toLocaleString(undefined,{maximumFractionDigits:2});
const pct = n => (n*100).toFixed(2)+"%";
const pct1 = n => (n*100).toFixed(1)+"%";
const sc = D.scorecard;

// ============ HEADER TICKER (live) ============
document.getElementById("upd").textContent = sc.generated_at.slice(0,16).replace("T"," ");
let lastTick = null;
async function refreshTicker() {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT");
    const j = await r.json();
    const price = parseFloat(j.lastPrice);
    const chgPct = parseFloat(j.priceChangePercent);
    const vol = parseFloat(j.quoteVolume);
    document.getElementById("liveBtc").textContent = "$"+fmt(price);
    const chgEl = document.getElementById("liveBtcChg");
    chgEl.textContent = (chgPct>=0?"+":"")+chgPct.toFixed(2)+"% (24h)";
    chgEl.className = "chg "+(chgPct>=0?"green":"red");
    document.getElementById("liveVol").textContent = "$"+fmt(vol/1e9)+"B";
    document.getElementById("live24h").textContent = "现货成交额";

    // Distance to triggers
    const h = sc.donchian["20d_high"], l = sc.donchian["20d_low"];
    let sig = "HOLD", sigCls = "accent";
    if (price > h) {sig = "BUY"; sigCls = "green";}
    else if (price < l) {sig = "SELL"; sigCls = "red";}
    const sigEl = document.getElementById("liveSig");
    sigEl.textContent = sig;
    sigEl.className = "val "+sigCls;
    const toBuy = ((h - price) / price * 100);
    const toSell = ((price - l) / price * 100);
    document.getElementById("liveSigDist").textContent = `距 BUY +${toBuy.toFixed(1)}% · 距 SELL -${toSell.toFixed(1)}%`;
    if (lastTick && Math.abs(price - lastTick) > 50) {
      document.getElementById("tickStatus").textContent = "实时 · "+new Date().toLocaleTimeString();
    }
    lastTick = price;
    document.getElementById("upd").textContent = new Date().toLocaleTimeString();
  } catch(e) { document.getElementById("tickStatus").textContent = "实时 · 离线"; }
}
// Funding & alloc snapshot (not live, from scorecard)
document.getElementById("liveFund").textContent = pct(sc.decision.best_funding_apy);
document.getElementById("liveFundVenue").textContent = "@ "+(sc.decision.best_funding_venue||"-");
document.getElementById("liveFund").className = "val " + (sc.decision.best_funding_apy>0.08?"green":"red");
const a = sc.decision.allocation_pct;
document.getElementById("liveAlloc").textContent = `${a.btc_spot}/${a.funding_arb}/${a.basis_trade}/${a.cash}`;
document.getElementById("liveExp").textContent = `预期 $${sc.decision.expected_daily_pnl.toFixed(0)}/天 · 年化 ${pct(sc.decision.expected_apy)}`;
refreshTicker();
setInterval(refreshTicker, 15000);

// ============ DECISION PANEL ============
document.getElementById("regimePill").textContent = sc.decision.regime;
document.getElementById("dashSub").innerHTML =
  `<b>${sc.date}</b> · 基础资金 <b>$10,000</b> · 日目标 <b>$50</b> · 14 天实际波动率 <b>${(sc.vol_ann*100).toFixed(1)}%</b>`;

const kpis = [
  {lbl:"今日预期日均盈亏",val:"$"+sc.decision.expected_daily_pnl.toFixed(2),cls:sc.decision.expected_daily_pnl>=50?"green":"red",
    delta:"目标 $50 差距 "+(sc.decision.expected_daily_pnl-50).toFixed(2)},
  {lbl:"今日预期年化",val:pct(sc.decision.expected_apy),cls:"accent",
    delta:"美元货基约 4.5% 基准"},
  {lbl:"Donchian 信号",val:sc.donchian.signal,cls:sc.donchian.signal==="BUY"?"green":sc.donchian.signal==="SELL"?"red":"blue",
    delta:`20 日区间 [$${fmt(sc.donchian["20d_low"])} · $${fmt(sc.donchian["20d_high"])}]`},
  {lbl:"最佳资金费率 7 日 APY",val:pct(sc.decision.best_funding_apy),cls:sc.decision.best_funding_apy>0.08?"green":"red",
    delta:"@ "+(sc.decision.best_funding_venue||"-")+" · 门槛 8%"},
  {lbl:"季度基差年化",val:(sc.basis.ann_basis*100).toFixed(2)+"%",cls:sc.basis.ann_basis>0.10?"green":"red",
    delta:sc.basis.symbol+" · 剩余 "+sc.basis.days_to_expiry.toFixed(0)+" 天"},
  {lbl:"14 天波动率",val:(sc.vol_ann*100).toFixed(1)+"%",cls:sc.vol_ann>0.8?"red":sc.vol_ann<0.4?"green":"yellow",
    delta:sc.vol_ann>0.8?"高":sc.vol_ann<0.4?"低":"中"},
  {lbl:"在场资金",val:(100-a.cash)+"%",cls:"accent",delta:"$"+fmt((100-a.cash)*1000)+" 部署中"},
  {lbl:"现金缓冲",val:a.cash+"%",cls:"blue",delta:"$"+fmt(a.cash*1000)+" @ 约 5% APY"},
];
document.getElementById("kpiCards").innerHTML = kpis.map(c => `
  <div class="card">
    <div class="lbl">${c.lbl}</div>
    <div class="val ${c.cls||""}">${c.val}</div>
    <div class="delta">${c.delta||""}</div>
  </div>`).join("");

// Alloc bar
const bar = [
  {k:"btc",lbl:"BTC 现货",v:a.btc_spot},{k:"fund",lbl:"资金费率",v:a.funding_arb},
  {k:"bas",lbl:"基差套利",v:a.basis_trade},{k:"cash",lbl:"USDT 现金",v:a.cash},
].filter(x=>x.v>0);
document.getElementById("allocBar").innerHTML = bar.map(x =>
  `<div class="${x.k}" style="flex:${x.v}">${x.lbl} ${x.v}%</div>`).join("");
document.getElementById("allocTbl").innerHTML = `
  <thead><tr><th>层</th><th>%</th><th>$ on $10K</th><th>预期年化</th></tr></thead>
  <tbody>
    <tr><td>BTC 现货（Donchian）</td><td class="num">${a.btc_spot}%</td><td class="num">$${fmt(a.btc_spot*100)}</td><td class="num">40%</td></tr>
    <tr><td>资金费率套利</td><td class="num">${a.funding_arb}%</td><td class="num">$${fmt(a.funding_arb*100)}</td><td class="num">${pct(sc.decision.best_funding_apy)}</td></tr>
    <tr><td>季度基差套利</td><td class="num">${a.basis_trade}%</td><td class="num">$${fmt(a.basis_trade*100)}</td><td class="num">${(sc.basis.ann_basis*100).toFixed(2)}%</td></tr>
    <tr><td>USDT 现金</td><td class="num">${a.cash}%</td><td class="num">$${fmt(a.cash*100)}</td><td class="num">5%</td></tr>
  </tbody>`;

// Risk gauges
const gauges = [
  {lbl:"信号距离（BUY 触发）",v:1-Math.min(sc.donchian.pct_to_breakout/0.15,1),txt:`还差 ${(sc.donchian.pct_to_breakout*100).toFixed(1)}%`,cls:sc.donchian.pct_to_breakout<0.05?"green":sc.donchian.pct_to_breakout<0.1?"yellow":"red"},
  {lbl:"资金费率机会强度",v:Math.min(sc.decision.best_funding_apy/0.20,1),txt:pct(sc.decision.best_funding_apy)+" / 目标 20%",cls:sc.decision.best_funding_apy>0.10?"green":sc.decision.best_funding_apy>0.05?"yellow":"red"},
  {lbl:"基差机会强度",v:Math.max(0,Math.min(sc.basis.ann_basis/0.20,1)),txt:(sc.basis.ann_basis*100).toFixed(1)+"% / 目标 20%",cls:sc.basis.ann_basis>0.10?"green":sc.basis.ann_basis>0.05?"yellow":"red"},
  {lbl:"波动率分位",v:Math.min(sc.vol_ann/1.0,1),txt:(sc.vol_ann*100).toFixed(0)+"% 年化",cls:sc.vol_ann<0.4?"green":sc.vol_ann<0.7?"yellow":"red"},
];
document.getElementById("gauges").innerHTML = gauges.map(g=>`
  <div>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)"><span>${g.lbl}</span><span>${g.txt}</span></div>
    <div class="gauge"><div class="fill ${g.cls}" style="width:${(g.v*100).toFixed(0)}%"></div></div>
  </div>`).join("");

// ============ BTC PRICE CHART with trade markers ============
const btcCtx = document.getElementById("btcChart");
new Chart(btcCtx, {
  type:"line",
  data:{labels:D.btc.labels,datasets:[
    {label:"BTC 收盘价",data:D.btc.close,borderColor:"#ff3d8a",borderWidth:1.5,pointRadius:0,tension:.1,order:3},
    {label:"20 日高点",data:D.btc.d20_high,borderColor:"#39ff14",borderWidth:1,pointRadius:0,borderDash:[3,3],order:4},
    {label:"20 日低点",data:D.btc.d20_low,borderColor:"#ff3366",borderWidth:1,pointRadius:0,borderDash:[3,3],order:4},
    {label:"入场",type:"scatter",data:D.trade_markers.entries,backgroundColor:"#39ff14",borderColor:"#fff",borderWidth:1.5,pointRadius:5,pointStyle:"triangle",order:1},
    {label:"出场",type:"scatter",data:D.trade_markers.exits,backgroundColor:"#ff3366",borderColor:"#fff",borderWidth:1.5,pointRadius:5,pointStyle:"rectRot",order:2},
  ]},
  options:{
    scales:{x:{type:"category",grid:{color:"#161b25"},ticks:{color:"#7d8da0",maxTicksLimit:10}},
            y:{grid:{color:"#161b25"},ticks:{color:"#7d8da0",callback:v=>"$"+(v/1000).toFixed(0)+"k"}}},
    plugins:{legend:{labels:{color:"#e6edf3",font:{size:11}}}},
    maintainAspectRatio:false
  }
});

// ============ SG DAYTIME RANGE ============
if (D.sg_day?.summary) {
  const sg = D.sg_day.summary;
  const p = sg.selected_params;
  const trn = sg.train_60d || {};
  const val = sg.validation_30d || {};
  const full = sg.full_90d || {};
  const session = `${String(p.session_start).padStart(2,"0")}:00-${String(p.session_start + p.session_len).padStart(2,"0")}:00`;
  const statCards = [
    {lbl:"选中时段",val:session,cls:"blue",delta:`SGT · ${p.range_hours}h 区间 · 下一根开盘成交`},
    {lbl:"启用条件",val:p.market_filter?"ON":"OFF",cls:p.market_filter?"green":"red",
      delta:"震荡 + 低波动 + 非单边下跌"},
    {lbl:"训练 60 天",val:"$"+fullNum(trn.total),cls:trn.total>=0?"green":"red",
      delta:`${trn.trades||0} 笔 · 胜率 ${(trn.win||0).toFixed(1)}% · DD ${((trn.max_dd||0)*100).toFixed(2)}%`},
    {lbl:"验证 30 天",val:"$"+fullNum(val.total),cls:val.total>=0?"green":"red",
      delta:`${val.trades||0} 笔 · 胜率 ${(val.win||0).toFixed(1)}% · DD ${((val.max_dd||0)*100).toFixed(2)}%`},
    {lbl:"全 90 天",val:"$"+fullNum(full.total),cls:full.total>=0?"green":"red",
      delta:`日均 $${(full.avg_daily||0).toFixed(2)} · 达 $50 天 ${(full.hit50||0).toFixed(1)}%`},
  ];
  document.getElementById("sgDayStats").innerHTML = statCards.map(c => `
    <div class="card">
      <div class="lbl">${c.lbl}</div>
      <div class="val ${c.cls}">${c.val}</div>
      <div class="delta">${c.delta}</div>
    </div>`).join("");

  const daytime = (sg.hourly_profile || []).filter(x => x.hour_sgt >= 8 && x.hour_sgt < 18);
  new Chart(document.getElementById("sgHourChart"), {
    type:"bar",
    data:{labels:daytime.map(x=>String(x.hour_sgt).padStart(2,"0")+":00"),datasets:[
      {label:"平均 15m 波动 %",data:daytime.map(x=>x.avg_range*100),backgroundColor:"rgba(0,217,255,.55)",borderColor:"#00d9ff",borderWidth:1},
      {label:"稳定度 score",type:"line",data:daytime.map(x=>x.stability_score),borderColor:"#ff3d8a",borderWidth:1.8,pointRadius:2,yAxisID:"y1"},
    ]},
    options:{
      scales:{x:{ticks:{color:"#7d8da0"},grid:{color:"#161b25"}},
              y:{ticks:{color:"#7d8da0",callback:v=>v.toFixed(2)+"%"},grid:{color:"#161b25"}},
              y1:{position:"right",ticks:{color:"#ff3d8a"},grid:{display:false}}},
      plugins:{legend:{labels:{color:"#e6edf3"}}},
      maintainAspectRatio:false
    }
  });

  const sgTrades = D.sg_day.recent_trades || [];
  document.getElementById("sgTradesTbl").innerHTML = `<thead><tr><th>入场</th><th>出场</th><th>原因</th><th class="num">PnL</th></tr></thead><tbody>` +
    (sgTrades.length ? sgTrades.map(t => `<tr>
      <td>${String(t.entry_ts).slice(5,16).replace("T"," ")}</td>
      <td>${String(t.exit_ts).slice(5,16).replace("T"," ")}</td>
      <td><span class="b ${t.pnl>=0?'buy':'sell'}">${t.reason}</span></td>
      <td class="num ${t.pnl>=0?'good':'bad'}">$${Number(t.pnl).toFixed(2)}</td>
    </tr>`).join("") : `<tr><td colspan="4" class="dim">暂无交易</td></tr>`) + `</tbody>`;
}

// ============ RANGE COMBO ============
if (D.combined_range) {
  const c = D.combined_range;
  const s90 = c.last_90d || {};
  const s30 = c.last_30d || {};
  const po90 = c.profit_only_last_90d || {};
  const sleeve = c.sleeve_pnl_90d || {};
  const cards = [
    {lbl:"资金分配",val:"70/30",cls:"blue",delta:"主 4h 区间 / SG 过滤辅助"},
    {lbl:"近 90 天",val:"$"+fullNum(s90.final_vs_initial),cls:s90.final_vs_initial>=0?"green":"red",
      delta:`${s90.trades||0} 笔 · 胜率 ${(s90.win||0).toFixed(1)}% · DD ${((s90.max_dd||0)*100).toFixed(2)}%`},
    {lbl:"近 30 天",val:"$"+fullNum(s30.final_vs_initial),cls:s30.final_vs_initial>=0?"green":"red",
      delta:`日均 $${(s30.avg_daily||0).toFixed(2)} · 达 $50 天 ${(s30.hit50||0).toFixed(1)}%`},
    {lbl:"只盈利平仓实验",val:"$"+fullNum(po90.final_vs_initial),cls:po90.final_vs_initial>=0?"green":"red",
      delta:`90 天 DD ${((po90.max_dd||0)*100).toFixed(2)}% · 不作为默认`},
    {lbl:"子策略贡献",val:"$"+fullNum(sleeve.MAIN_4H_RANGE||0),cls:"accent",
      delta:`SG 辅助 $${fullNum(sleeve.SG_FILTERED_RANGE||0)}`},
  ];
  document.getElementById("rangeComboStats").innerHTML = cards.map(x => `
    <div class="card">
      <div class="lbl">${x.lbl}</div>
      <div class="val ${x.cls}">${x.val}</div>
      <div class="delta">${x.delta}</div>
    </div>`).join("");
}

// ============ FUNDING ============
new Chart(document.getElementById("fundChart"),{
  type:"line",
  data:{labels:D.funding.labels,datasets:[
    {label:"币安 Binance",data:D.funding.binance_apy,borderColor:"#f0b90b",borderWidth:1.5,pointRadius:0,tension:.2},
    {label:"Bybit",data:D.funding.bybit_apy,borderColor:"#f7931a",borderWidth:1.5,pointRadius:0,tension:.2},
    {label:"OKX",data:D.funding.okx_apy,borderColor:"#00d9ff",borderWidth:1.5,pointRadius:0,tension:.2},
  ]},
  options:{
    scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:10},grid:{color:"#161b25"}},
            y:{ticks:{color:"#7d8da0",callback:v=>v.toFixed(0)+"%"},grid:{color:"#161b25"}}},
    plugins:{legend:{labels:{color:"#e6edf3"}},
      annotation:{annotations:{thr:{type:"line",yMin:8,yMax:8,borderColor:"#ff3d8a",borderWidth:1,borderDash:[4,4]}}}
    },
    maintainAspectRatio:false
  }
});

const ft = sc.funding;
document.getElementById("fundTbl").innerHTML = `
  <thead><tr><th>交易所</th><th class="num">最新 8 小时</th><th class="num">3 日 APY</th><th class="num">7 日 APY</th><th class="num">30 日 APY</th><th>动作</th></tr></thead>
  <tbody>` +
  ["binance","bybit","okx"].map(v => {
    const f = ft[v]; if (!f) return "";
    const apy7 = f.apy_7d * 100;
    const action = apy7 > 8 ? '<span class="b buy">可入场</span>' : apy7 > 4 ? '<span class="b hold">偏弱</span>' : '<span class="b sell">跳过</span>';
    return `<tr ${v===(sc.decision.best_funding_venue||"")?'class="row-hi"':''}>
      <td><b>${v.toUpperCase()}</b></td>
      <td class="num ${f.latest>0?"good":"bad"}">${(f.latest*100).toFixed(4)}%</td>
      <td class="num ${f.apy_3d>0?"good":"bad"}">${(f.apy_3d*100).toFixed(2)}%</td>
      <td class="num ${apy7>0?"good":"bad"}">${apy7.toFixed(2)}%</td>
      <td class="num ${f.apy_30d>0?"good":"bad"}">${(f.apy_30d*100).toFixed(2)}%</td>
      <td>${action}</td></tr>`;
  }).join("") + `</tbody>`;

// ============ BASIS ============
new Chart(document.getElementById("basisChart"),{
  type:"line",
  data:{labels:D.basis.labels,datasets:[
    {label:"年化基差 %",data:D.basis.ann_pct,borderColor:"#ffd700",borderWidth:1.5,pointRadius:0,
      fill:true,backgroundColor:"rgba(255,215,0,.08)",tension:.2},
  ]},
  options:{
    scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:10},grid:{color:"#161b25"}},
            y:{ticks:{color:"#7d8da0",callback:v=>v.toFixed(0)+"%"},grid:{color:"#161b25"}}},
    plugins:{legend:{labels:{color:"#e6edf3"}}},
    maintainAspectRatio:false
  }
});

// ============ VOL CONE ============
const cone = D.vol_cone;
const maxVol = Math.max(...cone.flatMap(c=>[c.p90,c.current])) * 1.1;
document.getElementById("volCone").innerHTML = cone.map(c => {
  const h = (v) => (v/maxVol*180);
  return `<div class="col">
    <div class="bar" style="height:${h(c.p90)-h(c.p10)}px;margin-bottom:${h(c.p10)}px;position:relative">
      <div style="position:absolute;top:${(c.p90-c.p75)/(c.p90-c.p10)*100}%;left:0;right:0;height:1px;background:rgba(0,217,255,.6)"></div>
      <div style="position:absolute;top:${(c.p90-c.p50)/(c.p90-c.p10)*100}%;left:0;right:0;height:1px;background:rgba(0,217,255,.8)"></div>
      <div style="position:absolute;top:${(c.p90-c.p25)/(c.p90-c.p10)*100}%;left:0;right:0;height:1px;background:rgba(0,217,255,.6)"></div>
      <div class="cur" style="bottom:${(c.current-c.p10)/(c.p90-c.p10)*100}%"></div>
    </div>
    <div class="v">${c.current.toFixed(0)}%</div>
    <div class="lbl">${c.window}d</div>
    <div class="v" style="color:var(--muted);font-size:9px">P50: ${c.p50.toFixed(0)}%</div>
  </div>`;
}).join("");

// ============ STRATEGY METRICS TABLE ============
const M = D.metrics;
document.getElementById("metricsTbl").innerHTML = `
<thead><tr>
  <th>策略</th><th class="num">年化</th><th class="num">夏普</th><th class="num">索提诺</th>
  <th class="num">卡玛</th><th class="num">最大回撤</th><th class="num">回撤天数</th>
  <th class="num">胜率</th><th class="num">盈亏比</th><th class="num">溃疡指数</th>
  <th class="num">在场%</th><th class="num">交易次数</th>
</tr></thead><tbody>` +
M.map(m => {
  const cagrCls = m.cagr > 0 ? "good" : "bad";
  const ddCls = m.max_dd > -0.3 ? "good" : "bad";
  return `<tr>
    <td><b>${m.label}</b><br><span class="dim" style="font-size:10px">${m.years} 年</span></td>
    <td class="num ${cagrCls}">${(m.cagr*100).toFixed(2)}%</td>
    <td class="num ${m.sharpe>1?"good":""}">${m.sharpe.toFixed(2)}</td>
    <td class="num">${m.sortino.toFixed(2)}</td>
    <td class="num ${m.calmar>0.5?"good":""}">${m.calmar.toFixed(2)}</td>
    <td class="num ${ddCls}">${(m.max_dd*100).toFixed(2)}%</td>
    <td class="num">${m.max_dd_days}</td>
    <td class="num">${(m.win_rate*100).toFixed(1)}%</td>
    <td class="num ${m.profit_factor>1.5?"good":""}">${m.profit_factor.toFixed(2)}</td>
    <td class="num">${(m.ulcer*100).toFixed(2)}</td>
    <td class="num">${(m.pct_in_market*100).toFixed(1)}%</td>
    <td class="num">${m.n_trades}</td>
  </tr>`;
}).join("") + `</tbody>`;

// ============ EQUITY CURVES ============
const eqColors = {"Donchian 突破":"#ff3d8a","Donchian+震荡":"#ffd700","Range 组合":"#00d9ff","SG 白天区间":"#39ff14","买入持有":"#7d8da0","资金费率套利":"#39ff14","季度基差套利":"#ffd700"};
const eqDatasets = Object.entries(D.backtests).map(([k,v]) => ({
  label:k, data:v.labels.map((l,i)=>({x:l,y:v.equity[i]})),
  borderColor:eqColors[k]||"#00d9ff", borderWidth:1.6, pointRadius:0, tension:.1
}));
// Convert string dates to Date objects for proper time-axis sorting
const eqDatasetsTime = eqDatasets.map(ds => ({...ds, data: ds.data.map(p => ({x: new Date(p.x).getTime(), y: p.y}))}));
let eqChart = new Chart(document.getElementById("eqChart"),{
  type:"line",
  data:{datasets:eqDatasetsTime},
  options:{
    parsing:false,
    scales:{x:{type:"time",time:{unit:"year"},ticks:{color:"#7d8da0",maxTicksLimit:12},grid:{color:"#161b25"}},
            y:{type:"linear",ticks:{color:"#7d8da0"},grid:{color:"#161b25"}}},
    plugins:{legend:{labels:{color:"#e6edf3"}}},
    maintainAspectRatio:false
  }
});
window.setEqScale = (s) => {
  eqChart.options.scales.y.type = s;
  eqChart.update();
  document.querySelectorAll('#equity .tabs button').forEach(b => b.classList.toggle('on', b.textContent.toLowerCase().startsWith(s.slice(0,3))));
};

// ============ MONTHLY HEATMAP ============
const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const yearsSet = [...new Set(D.monthly_returns.map(r=>r.year))].sort();
const colorRet = r => {
  if (r === null || r === undefined) return "transparent";
  const cap = Math.max(-0.25, Math.min(0.25, r));
  const intensity = Math.abs(cap)/0.25;
  const base = cap >= 0 ? [57,255,20] : [255,51,102];
  const alpha = 0.15 + intensity*0.85;
  return `rgba(${base[0]},${base[1]},${base[2]},${alpha})`;
};
let heat = `<div class="heatmap"><div></div>` + months.map(m=>`<div class="head">${m}</div>`).join("");
for (const y of yearsSet) {
  heat += `<div class="yr">${y}</div>`;
  for (let m=1; m<=12; m++) {
    const r = D.monthly_returns.find(x=>x.year===y && x.month===m);
    if (!r) { heat += `<div class="cell empty">·</div>`; continue; }
    heat += `<div class="cell" style="background:${colorRet(r.ret)}" title="${y}-${m}: ${(r.ret*100).toFixed(2)}%">${(r.ret*100).toFixed(1)}</div>`;
  }
}
heat += `</div>`;
document.getElementById("heat").innerHTML = heat;

// ============ DRAWDOWN ============
new Chart(document.getElementById("ddChart"),{
  type:"line",
  data:{labels:D.drawdown.labels,datasets:[
    {label:"回撤 %",data:D.drawdown.dd,borderColor:"#ff3366",borderWidth:1.4,pointRadius:0,
      fill:true,backgroundColor:"rgba(255,51,102,.15)",tension:.05}
  ]},
  options:{
    scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:12},grid:{color:"#161b25"}},
            y:{ticks:{color:"#7d8da0",callback:v=>v.toFixed(0)+"%"},grid:{color:"#161b25"},max:0}},
    plugins:{legend:{labels:{color:"#e6edf3"}}},
    maintainAspectRatio:false
  }
});

// ============ DECISION HISTORY ============
const H = D.history;
document.getElementById("decCount").textContent = H.labels.length + " 天";
new Chart(document.getElementById("allocHist"),{
  type:"bar",
  data:{labels:H.labels,datasets:[
    {label:"BTC",data:H.alloc_btc,backgroundColor:"#ff3d8a",stack:"a"},
    {label:"资金费率",data:H.alloc_funding,backgroundColor:"#00d9ff",stack:"a"},
    {label:"基差",data:H.alloc_basis,backgroundColor:"#ffd700",stack:"a"},
    {label:"Yield PT",data:H.alloc_yield,backgroundColor:"#39ff14",stack:"a"},
    {label:"现金",data:H.alloc_cash,backgroundColor:"#48505a",stack:"a"},
  ]},
  options:{
    scales:{x:{stacked:true,ticks:{color:"#7d8da0"},grid:{color:"#161b25"}},
            y:{stacked:true,max:100,ticks:{color:"#7d8da0",callback:v=>v+"%"},grid:{color:"#161b25"}}},
    plugins:{legend:{labels:{color:"#e6edf3"}}},
    maintainAspectRatio:false
  }
});
new Chart(document.getElementById("pnlHist"),{
  type:"line",
  data:{labels:H.labels,datasets:[
    {label:"预期日盈亏 ($)",data:H.expected_daily,borderColor:"#00d9ff",borderWidth:2,
      fill:true,backgroundColor:"rgba(0,217,255,.12)",pointRadius:3,tension:.2},
    {label:"目标 $50",data:H.labels.map(()=>50),borderColor:"#ff3366",borderWidth:1,borderDash:[4,4],pointRadius:0},
  ]},
  options:{
    scales:{x:{ticks:{color:"#7d8da0"},grid:{color:"#161b25"}},
            y:{ticks:{color:"#7d8da0",callback:v=>"$"+v},grid:{color:"#161b25"}}},
    plugins:{legend:{labels:{color:"#e6edf3"}}},
    maintainAspectRatio:false
  }
});

document.getElementById("histTbl").innerHTML = `
<thead><tr><th>日期</th><th class="num">BTC</th><th class="num">14 日波动率</th><th class="num">最佳费率</th><th class="num">基差</th><th class="num">BTC%</th><th class="num">费率%</th><th class="num">基差%</th><th class="num">Yield%</th><th class="num">现金%</th><th class="num">预期日</th><th class="num">预期年化</th></tr></thead>
<tbody>` + H.labels.map((l,i)=>{
  return `<tr><td>${l}</td>
    <td class="num">$${fmt(H.btc_close[i])}</td>
    <td class="num">${(H.vol_14d[i]*100).toFixed(0)}%</td>
    <td class="num ${H.best_funding_apy[i]>0?"good":"bad"}">${(H.best_funding_apy[i]*100).toFixed(2)}%</td>
    <td class="num ${H.ann_basis[i]>0?"good":"bad"}">${(H.ann_basis[i]*100).toFixed(2)}%</td>
    <td class="num">${H.alloc_btc[i]}%</td>
    <td class="num">${H.alloc_funding[i]}%</td>
    <td class="num">${H.alloc_basis[i]}%</td>
    <td class="num">${H.alloc_yield[i]}%</td>
    <td class="num">${H.alloc_cash[i]}%</td>
    <td class="num ${H.expected_daily[i]>=50?"good":""}">$${H.expected_daily[i].toFixed(2)}</td>
    <td class="num">${(H.expected_apy[i]*100).toFixed(2)}%</td>
  </tr>`;
}).join("") + `</tbody>`;

// ============ REASONING ============
document.getElementById("reasonBox").innerHTML = sc.decision.reasons.map(r=>`<div>→ ${r}</div>`).join("");

// ============ CROSS-EXCHANGE SPREADS ============
if (D.hunter?.spreads && D.hunter.spreads.labels.length > 1) {
  const sp = D.hunter.spreads;
  const colors = {BTC:"#ff3d8a",ETH:"#b87fff",SOL:"#39ff14",BNB:"#ffd700",XRP:"#00d9ff",DOGE:"#ff3366"};
  const datasets = Object.entries(sp.series).map(([k,v]) => ({
    label:k, data:v, borderColor:colors[k]||"#7d8da0", borderWidth:1.4, pointRadius:0, tension:.2,
  }));
  new Chart(document.getElementById("spreadChart"),{
    type:"line",
    data:{labels:sp.labels, datasets:datasets},
    options:{
      scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:10},grid:{color:"#161b25"}},
              y:{ticks:{color:"#7d8da0",callback:v=>v.toFixed(1)+" bps"},grid:{color:"#161b25"}}},
      plugins:{legend:{labels:{color:"#e6edf3"}}},
      maintainAspectRatio:false
    }
  });
  let tbl = `<thead><tr><th>币种</th><th class="num">当前 bps</th><th class="num">均值</th><th class="num">峰值</th><th>主买</th><th>主卖</th><th class="num">样本</th><th>状态</th></tr></thead><tbody>`;
  Object.entries(sp.summary).forEach(([k,s])=>{
    const status = s.max > 15 ? '<span class="b buy">曾过门槛</span>' : s.max > 8 ? '<span class="b hold">接近</span>' : '<span class="b sell">远低</span>';
    tbl += `<tr><td><b style="color:${colors[k]}">${k}</b></td>
      <td class="num">${s.last.toFixed(2)}</td>
      <td class="num">${s.mean.toFixed(2)}</td>
      <td class="num ${s.max>15?"good":""}">${s.max.toFixed(2)}</td>
      <td>${s.best_buy_mode}</td>
      <td>${s.best_sell_mode}</td>
      <td class="num">${s.n_samples}</td>
      <td>${status}</td></tr>`;
  });
  tbl += "</tbody>";
  document.getElementById("spreadTbl").innerHTML = tbl;
}

// ============ THRESHOLDS ============
if (D.scorecard.decision.thresholds) {
  const t = D.scorecard.decision.thresholds;
  const cur_f = D.scorecard.decision.best_funding_apy;
  const cur_b = D.scorecard.basis.ann_basis;
  const fbar = (cur, p50, p75, p90) => {
    const max = Math.max(0.20, p90 * 1.2);
    const pct = v => Math.max(0, Math.min(100, v / max * 100));
    return `<div style="position:relative;height:36px;background:#0a0c14;border-radius:4px;margin-top:10px;overflow:hidden">
      <div style="position:absolute;left:0;top:0;bottom:0;width:${pct(0.045)}%;background:rgba(125,141,160,.2);border-right:1px dashed #7d8da0"></div>
      <div style="position:absolute;left:0;top:0;bottom:0;width:${pct(p50)}%;background:rgba(0,217,255,.08);border-right:1px solid #00d9ff"></div>
      <div style="position:absolute;left:0;top:0;bottom:0;width:${pct(p75)}%;background:rgba(57,255,20,.08);border-right:1px solid #39ff14"></div>
      <div style="position:absolute;left:0;top:0;bottom:0;width:${pct(p90)}%;background:rgba(255,61,138,.08);border-right:1px solid #ff3d8a"></div>
      <div style="position:absolute;left:calc(${pct(cur)}% - 2px);top:0;bottom:0;width:4px;background:#ffd700;box-shadow:0 0 8px #ffd700"></div>
      <div style="position:relative;font-size:10px;color:var(--muted);padding:11px 8px;display:flex;justify-content:space-between;font-feature-settings:'tnum'">
        <span style="color:#7d8da0">现金 4.5%</span><span style="color:#00d9ff">P50 ${(p50*100).toFixed(2)}%</span><span style="color:#39ff14">P75 ${(p75*100).toFixed(2)}%</span><span style="color:#ff3d8a">P90 ${(p90*100).toFixed(2)}%</span>
      </div>
    </div>`;
  };
  document.getElementById("thrCards").innerHTML = `
    <div class="chart">
      <h3>资金费率门槛 <span class="right">当前 ${(cur_f*100).toFixed(2)}% APY</span></h3>
      <div style="font-size:13px;color:var(--text)">${cur_f >= t.funding_p90 ? '<span style="color:#ff3d8a">🔥 进入 P90 顶部档</span>' : cur_f >= t.funding_p75 ? '<span style="color:#39ff14">✅ 进入 P75 上四分位</span>' : cur_f >= Math.max(t.funding_p50, 0.045) ? '<span style="color:#00d9ff">⚠️ 跑赢中位 + 现金</span>' : '<span style="color:#7d8da0">❌ 低于现金 4.5%，跳过</span>'}</div>
      ${fbar(cur_f, t.funding_p50, t.funding_p75, t.funding_p90)}
      <div style="font-size:11px;color:var(--muted);margin-top:10px">最佳交易所: <b style="color:var(--accent2)">${D.scorecard.decision.best_funding_venue||"-"}</b></div>
    </div>
    <div class="chart">
      <h3>季度基差门槛 <span class="right">当前 ${(cur_b*100).toFixed(2)}% 年化</span></h3>
      <div style="font-size:13px;color:var(--text)">${cur_b >= t.basis_p90 ? '<span style="color:#ff3d8a">🔥 进入 P90 顶部档</span>' : cur_b >= t.basis_p75 ? '<span style="color:#39ff14">✅ 进入 P75 上四分位</span>' : cur_b >= Math.max(t.basis_p50, 0.045) ? '<span style="color:#00d9ff">⚠️ 跑赢中位 + 现金</span>' : '<span style="color:#7d8da0">❌ 低于现金 4.5%，跳过</span>'}</div>
      ${fbar(cur_b, t.basis_p50, t.basis_p75, t.basis_p90)}
      <div style="font-size:11px;color:var(--muted);margin-top:10px">合约: <b style="color:var(--accent2)">${D.scorecard.basis.symbol||"-"}</b> · 剩余 ${D.scorecard.basis.days_to_expiry.toFixed(0)} 天</div>
    </div>
  `;
}

// ============ HUNTER ============
if (D.hunter && D.hunter.snapshot) {
  const hs = D.hunter.snapshot;
  const hd = D.hunter.daily || {days:{}};
  const today = Object.keys(hd.days || {}).sort().pop();
  const todayData = today ? hd.days[today] : {scans:0,opps_total:0,sum_potential_usd:0,best_single_usd:0};

  // Stats
  const stats = [
    {lbl:"今日扫描次数",val:fmt(todayData.scans),cls:"blue",delta:`${today || "-"}`},
    {lbl:"今日机会数",val:fmt(todayData.opps_total),cls:todayData.opps_total>0?"green":"red",
      delta:`命中率 ${todayData.scans>0?(todayData.opps_total/todayData.scans*100).toFixed(2):0}%`},
    {lbl:"今日潜在累计盈亏",val:"$"+todayData.sum_potential_usd.toFixed(2),cls:todayData.sum_potential_usd>0?"green":"red",
      delta:"假设每个机会都执行"},
    {lbl:"最佳单笔",val:"$"+todayData.best_single_usd.toFixed(2),cls:"accent",
      delta:"距 $50/天目标 "+(todayData.sum_potential_usd-50).toFixed(2)},
  ];
  document.getElementById("hunterStats").innerHTML = stats.map(c=>
    `<div class="card"><div class="lbl">${c.lbl}</div><div class="val ${c.cls}">${c.val}</div><div class="delta">${c.delta||""}</div></div>`
  ).join("");

  // Current snapshot table
  let cur = `<thead><tr><th>类型</th><th>详情</th><th class="num">净 bps</th><th class="num">净盈亏 $10K</th></tr></thead><tbody>`;
  const opps = hs.opportunities || [];
  if (opps.length === 0) {
    cur += `<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">
      当前无可盈利机会 · 行情数: 币安=${hs.n_binance_books||0} OKX=${hs.n_okx_books||0} Bybit=${hs.n_bybit_books||0}
    </td></tr>`;
  } else {
    for (const o of opps.sort((a,b)=>b.net_usd-a.net_usd).slice(0,20)) {
      let detail = "";
      if (o.type === "cross") detail = `${o.coin} ${o.buy_at}→${o.sell_at}`;
      else if (o.type === "tri") detail = `${o.path} (${o.direction})`;
      else if (o.type === "depeg") detail = `${o.stable} dev=${o.dev_bps.toFixed(1)}bps`;
      cur += `<tr><td><span class="b ${o.net_usd>5?'buy':'hold'}">${o.type.toUpperCase()}</span></td>
        <td>${detail}</td>
        <td class="num good">${(o.net_bps||0).toFixed(2)}</td>
        <td class="num good">$${o.net_usd.toFixed(2)}</td></tr>`;
    }
  }
  cur += `</tbody>`;
  document.getElementById("hunterCurrentTbl").innerHTML = cur;

  // Daily aggregate
  let dt = `<thead><tr><th>日期</th><th class="num">扫描数</th><th class="num">机会数</th><th class="num">跨所</th><th class="num">三角</th><th class="num">脱锚</th><th class="num">累计 $</th><th class="num">最佳 $</th><th class="num">vs $50 目标</th></tr></thead><tbody>`;
  const days = Object.entries(hd.days || {}).sort().reverse();
  for (const [d, x] of days) {
    const gap = x.sum_potential_usd - 50;
    dt += `<tr><td>${d}</td>
      <td class="num">${x.scans}</td>
      <td class="num">${x.opps_total}</td>
      <td class="num">${x.by_type?.cross || 0}</td>
      <td class="num">${x.by_type?.tri || 0}</td>
      <td class="num">${x.by_type?.depeg || 0}</td>
      <td class="num ${x.sum_potential_usd>0?'good':''}">$${x.sum_potential_usd.toFixed(2)}</td>
      <td class="num">$${x.best_single_usd.toFixed(2)}</td>
      <td class="num ${gap>=0?'good':'bad'}">${gap>=0?'+':''}$${gap.toFixed(2)}</td></tr>`;
  }
  dt += `</tbody>`;
  document.getElementById("hunterDailyTbl").innerHTML = dt;
}

// ============ YIELD · Pendle PT ============
if (D.yield && D.yield.markets) {
  const y = D.yield;
  const rec = y.recommendation || {};
  const mkts = y.markets || [];
  const top = mkts[0] || {};

  const stats = [
    {lbl:"实时 PT 市场数",val:mkts.length,cls:"blue",delta:y.source || ""},
    {lbl:"最高 APY",val:(top.implied_apy*100||0).toFixed(2)+"%",cls:"green",
      delta:top.name ? `${top.name} · ${top.days_to_expiry}d 到期` : "-"},
    {lbl:"推荐底仓",val:rec.available?rec.pick:"--",cls:rec.available?"accent":"red",
      delta:rec.available?`${rec.apy_pct}% · 流动性 $${(rec.liquidity_usd/1e6).toFixed(1)}M`:rec.reason||""},
    {lbl:"底仓日收益",val:rec.available?"$"+rec.daily_usd.toFixed(2):"$0",
      cls:rec.available&&rec.daily_usd>0?"green":"red",
      delta:rec.available?`50% × $${(rec.alloc_usd).toLocaleString()} · 年化 $${rec.annual_usd.toLocaleString()}`:""},
  ];
  document.getElementById("yieldStats").innerHTML = stats.map(c=>
    `<div class="card"><div class="lbl">${c.lbl}</div><div class="val ${c.cls}">${c.val}</div><div class="delta">${c.delta||""}</div></div>`
  ).join("");

  let tb = `<thead><tr><th>市场</th><th class="num">隐含 APY</th><th class="num">流动性</th><th class="num">到期天数</th><th>到期日</th><th>分类</th></tr></thead><tbody>`;
  for (const m of mkts) {
    const apyPct = (m.implied_apy*100).toFixed(2);
    const apyCls = m.implied_apy >= 0.10 ? "bad" : m.implied_apy >= 0.07 ? "good" : "";
    const liq = m.liquidity_usd >= 1e6 ? `$${(m.liquidity_usd/1e6).toFixed(1)}M` : `$${(m.liquidity_usd/1e3).toFixed(0)}K`;
    const cats = (m.categories||[]).filter(c=>c!=="stables").slice(0,3).join(" · ") || "-";
    const dExp = (m.expiry||"").slice(0,10);
    tb += `<tr><td><b>${m.name||"-"}</b></td>
      <td class="num ${apyCls}">${apyPct}%</td>
      <td class="num">${liq}</td>
      <td class="num">${m.days_to_expiry||"-"}</td>
      <td style="color:var(--muted)">${dExp}</td>
      <td style="color:var(--muted);font-size:11px">${cats}</td></tr>`;
  }
  tb += `</tbody>`;
  document.getElementById("yieldTbl").innerHTML = tb;
}

// ============ FEAR & GREED ============
if (D.macro && D.macro.fng) {
  const fng = D.macro.fng;
  const fngColor = v => v < 25 ? "#ff3366" : v < 45 ? "#ffd700" : v < 55 ? "#7d8da0" : v < 75 ? "#39ff14" : "#b87fff";
  document.getElementById("fngPill").textContent = fng.latest + " · " + fng.classification;
  new Chart(document.getElementById("fngChart"),{
    type:"line",
    data:{labels:fng.labels,datasets:[
      {label:"恐惧贪婪指数",data:fng.values,borderColor:"#ff3d8a",borderWidth:1.5,pointRadius:0,
        fill:true,backgroundColor:"rgba(255,61,138,.08)",tension:.2,
        segment:{borderColor:ctx=>{const v=ctx.p1.parsed.y; return fngColor(v);}}}
    ]},
    options:{
      scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:10},grid:{color:"#161b25"}},
              y:{min:0,max:100,ticks:{color:"#7d8da0"},grid:{color:"#161b25"}}},
      plugins:{legend:{labels:{color:"#e6edf3"}}},
      maintainAspectRatio:false
    }
  });
  // Gauge
  const v = fng.latest;
  // Translate classification
  const fngCnMap = {"Extreme Fear":"极度恐惧","Fear":"恐惧","Neutral":"中性","Greed":"贪婪","Extreme Greed":"极度贪婪"};
  const fngCn = fngCnMap[fng.classification] || fng.classification;
  document.getElementById("fngGauge").innerHTML = `
    <div style="text-align:center;padding:20px 0">
      <div style="font-size:64px;font-weight:700;color:${fngColor(v)};line-height:1">${v}</div>
      <div style="font-size:13px;color:var(--muted);margin-top:8px;letter-spacing:1px">${fngCn}</div>
      <div style="margin-top:24px;font-size:11px;color:var(--muted);text-align:left;line-height:2">
        <div>P10（历史低位）: <b style="color:#f85149">${fng.p10.toFixed(0)}</b></div>
        <div>P50（中位数）: <b style="color:var(--muted)">${fng.p50.toFixed(0)}</b></div>
        <div>P90（历史高位）: <b style="color:#a371f7">${fng.p90.toFixed(0)}</b></div>
      </div>
      <div style="margin-top:18px;padding:10px;background:rgba(255,51,102,.1);border-left:3px solid #f85149;text-align:left;font-size:12px;color:#f85149">
        ${v < 25 ? "⚠️ 极度恐惧 · 历史上是逆向买入机会" : v > 75 ? "⚠️ 极度贪婪 · 历史上是逆向卖出机会" : "中性区间"}
      </div>
    </div>`;
}

// ============ MACRO ASSETS ============
if (D.macro && D.macro.assets) {
  const a = D.macro.assets;
  const norm = arr => arr.length ? arr.map(v => v / arr[0] * 100) : [];
  // Find BTC daily aligned to macro dates (use btc.close subset)
  const datasets = [
    {label:"ETH",data:norm(a.eth),borderColor:"#b87fff"},
    {label:"SPX",data:norm(a.spx),borderColor:"#00d9ff"},
    {label:"DXY",data:norm(a.dxy),borderColor:"#ffd700"},
    {label:"Gold",data:norm(a.gold),borderColor:"#ffd700"},
    {label:"VIX",data:norm(a.vix),borderColor:"#ff3366"},
  ].filter(d => d.data.length > 0).map(d => ({...d,borderWidth:1.4,pointRadius:0,tension:.2}));
  // Add BTC normalized aligned to length
  if (D.btc && D.btc.close.length >= a.eth.length) {
    const btcN = norm(D.btc.close.slice(-a.eth.length));
    datasets.unshift({label:"BTC",data:btcN,borderColor:"#ff3d8a",borderWidth:2,pointRadius:0,tension:.2});
  }
  new Chart(document.getElementById("macroChart"),{
    type:"line",
    data:{labels:a.labels,datasets:datasets},
    options:{
      scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:10},grid:{color:"#161b25"}},
              y:{ticks:{color:"#7d8da0",callback:v=>v.toFixed(0)},grid:{color:"#161b25"}}},
      plugins:{legend:{labels:{color:"#e6edf3"}}},
      maintainAspectRatio:false
    }
  });

  // Dominance cards
  if (D.macro.dominance) {
    const dom = D.macro.dominance;
    document.getElementById("domCards").innerHTML = [
      {lbl:"BTC 市占率",val:dom.btc_dominance.toFixed(2)+"%",cls:"accent",delta:"加密总市值占比"},
      {lbl:"ETH 占比",val:dom.eth_dominance.toFixed(2)+"%",cls:"blue"},
      {lbl:"稳定币占比",val:dom.stablecoin_share.toFixed(2)+"%",cls:"yellow",delta:"USDT+USDC"},
      {lbl:"加密总市值",val:"$"+(dom.total_mcap_usd/1e12).toFixed(2)+"万亿",cls:"green",delta:"日成交 $"+(dom.total_volume_usd/1e9).toFixed(0)+" 亿"},
    ].map(c=>`<div class="card"><div class="lbl">${c.lbl}</div><div class="val ${c.cls}">${c.val}</div><div class="delta">${c.delta||""}</div></div>`).join("");
  }
}

// ============ CORRELATION MATRIX ============
if (D.correlation) {
  const C = D.correlation;
  // Build matrix HTML
  const corrColor = v => {
    const abs = Math.abs(v), alpha = 0.15 + abs * 0.85;
    return v >= 0 ? `rgba(57,255,20,${alpha})` : `rgba(255,51,102,${alpha})`;
  };
  let mhtml = `<div style="display:grid;grid-template-columns:60px repeat(${C.labels.length},1fr);gap:3px;font-size:11px"><div></div>`;
  mhtml += C.labels.map(l=>`<div style="text-align:center;padding:4px;color:var(--muted);font-weight:500">${l}</div>`).join("");
  for (let i = 0; i < C.matrix.length; i++) {
    mhtml += `<div style="color:var(--muted);font-weight:500;text-align:right;padding:6px 8px;display:flex;align-items:center;justify-content:flex-end">${C.labels[i]}</div>`;
    for (let j = 0; j < C.matrix[i].values.length; j++) {
      const v = C.matrix[i].values[j];
      mhtml += `<div style="aspect-ratio:1.4;display:flex;align-items:center;justify-content:center;border-radius:4px;background:${i===j?'transparent':corrColor(v)};color:${i===j?'var(--muted)':'#fff'};font-weight:600;font-feature-settings:'tnum';font-size:10px">${v.toFixed(2)}</div>`;
    }
  }
  mhtml += `</div>`;
  document.getElementById("corrMatrix").innerHTML = mhtml;

  // Rolling correlation chart
  const rollColors = {"eth_close":"#b87fff","spx_close":"#00d9ff","dxy_close":"#ffd700","gold_close":"#ffd700","vix_close":"#ff3366"};
  const rollSets = Object.entries(C.rolling_30d).map(([k,v]) => ({
    label:k.replace("_close","").toUpperCase(),
    data:v.history,
    borderColor:rollColors[k]||"#00d9ff",
    borderWidth:1.4,pointRadius:0,tension:.2
  }));
  new Chart(document.getElementById("corrRoll"),{
    type:"line",
    data:{labels:C.rolling_dates,datasets:rollSets},
    options:{
      scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:10},grid:{color:"#161b25"}},
              y:{min:-1,max:1,ticks:{color:"#7d8da0"},grid:{color:"#161b25"}}},
      plugins:{legend:{labels:{color:"#e6edf3"}}},
      maintainAspectRatio:false
    }
  });
}

// ============ ROLLING SHARPE ============
if (D.rolling_sharpe) {
  const RS = D.rolling_sharpe;
  new Chart(document.getElementById("rsChart"),{
    type:"line",
    data:{labels:RS.labels,datasets:[
      {label:"90 天滚动夏普",data:RS.values,borderColor:"#ff3d8a",borderWidth:1.6,pointRadius:0,
        fill:true,backgroundColor:"rgba(255,61,138,.06)",tension:.3,
        segment:{borderColor:ctx=>ctx.p1.parsed.y>1?"#39ff14":ctx.p1.parsed.y>0?"#ffd700":"#ff3366"}},
    ]},
    options:{
      scales:{x:{ticks:{color:"#7d8da0",maxTicksLimit:12},grid:{color:"#161b25"}},
              y:{ticks:{color:"#7d8da0"},grid:{color:"#161b25"}}},
      plugins:{legend:{labels:{color:"#e6edf3"}}},
      maintainAspectRatio:false
    }
  });
}

// ============ MONTE CARLO ============
if (D.monte_carlo && D.monte_carlo.donchian) {
  const mc = D.monte_carlo.donchian;
  document.getElementById("mcStats").innerHTML = [
    {lbl:"中位数终值（P50）",val:"$"+fmt(mc.final_p50),cls:"accent",delta:"中位年化 "+(mc.median_cagr*100).toFixed(1)+"%"},
    {lbl:"乐观情景（P95）",val:"$"+fmt(mc.final_p95),cls:"green"},
    {lbl:"悲观情景（P5）",val:"$"+fmt(mc.final_p5),cls:"red"},
    {lbl:"盈利概率",val:(mc.prob_profit*100).toFixed(1)+"%",cls:"blue",delta:`翻倍概率 ${(mc.prob_double*100).toFixed(1)}% · 腰斩概率 ${(mc.prob_lose_half*100).toFixed(1)}%`},
  ].map(c=>`<div class="card"><div class="lbl">${c.lbl}</div><div class="val ${c.cls}">${c.val}</div><div class="delta">${c.delta||""}</div></div>`).join("");

  new Chart(document.getElementById("mcChart"),{
    type:"line",
    data:{labels:mc.days,datasets:[
      {label:"P95（乐观）",data:mc.p95,borderColor:"rgba(57,255,20,.6)",borderWidth:1,pointRadius:0,fill:"+1",backgroundColor:"rgba(57,255,20,.06)"},
      {label:"P75",data:mc.p75,borderColor:"rgba(57,255,20,.3)",borderWidth:1,pointRadius:0,fill:"+1",backgroundColor:"rgba(57,255,20,.08)"},
      {label:"P50（中位）",data:mc.p50,borderColor:"#ff3d8a",borderWidth:2,pointRadius:0},
      {label:"P25",data:mc.p25,borderColor:"rgba(255,51,102,.3)",borderWidth:1,pointRadius:0,fill:"+1",backgroundColor:"rgba(255,51,102,.08)"},
      {label:"P5（悲观）",data:mc.p5,borderColor:"rgba(255,51,102,.6)",borderWidth:1,pointRadius:0},
    ]},
    options:{
      scales:{x:{title:{display:true,text:"未来天数",color:"#7d8da0"},ticks:{color:"#7d8da0",maxTicksLimit:12},grid:{color:"#161b25"}},
              y:{ticks:{color:"#7d8da0",callback:v=>"$"+fmt(v)},grid:{color:"#161b25"}}},
      plugins:{legend:{labels:{color:"#e6edf3",font:{size:11}}}},
      maintainAspectRatio:false
    }
  });
}

// ============ RISK CALCULATOR ============
function renderRiskTable(targetId, R, label) {
  if (!R) return;
  document.getElementById(targetId).innerHTML = `
    <tbody>
      <tr><td class="dim">凯利全仓建议</td><td class="num"><b>${(R.kelly_full*100).toFixed(1)}%</b></td></tr>
      <tr><td class="dim">半凯利（行业标准）</td><td class="num good"><b>${(R.kelly_half*100).toFixed(1)}%</b></td></tr>
      <tr><td class="dim">推荐仓位</td><td class="num accent"><b>${R.kelly_recommendation_pct.toFixed(0)}%</b></td></tr>
      <tr><td class="dim">日均收益</td><td class="num">${R.mean_daily_pct.toFixed(3)}%</td></tr>
      <tr><td class="dim">日波动率</td><td class="num">${R.std_daily_pct.toFixed(3)}%</td></tr>
      <tr><td class="dim">VaR 95%（日）</td><td class="num bad">${R.var_95_pct.toFixed(2)}% / $${fmt(Math.abs(R.var_95_usd))}</td></tr>
      <tr><td class="dim">VaR 99%（日）</td><td class="num bad">${R.var_99_pct.toFixed(2)}% / $${fmt(Math.abs(R.var_99_usd))}</td></tr>
      <tr><td class="dim">CVaR 95%（尾部均损）</td><td class="num bad">${R.cvar_95_pct.toFixed(2)}%</td></tr>
      <tr><td class="dim">CVaR 99%（尾部均损）</td><td class="num bad">${R.cvar_99_pct.toFixed(2)}%</td></tr>
    </tbody>`;
}
renderRiskTable("riskTblDon", D.risk?.donchian, "Donchian");
renderRiskTable("riskTblBH", D.risk?.buyhold, "买入持有");

// ============ EXECUTION LOG (synthesized from real data) ============
function renderExecLog() {
  const box = document.getElementById("execLogBox");
  if (!box) return;
  const lines = [];
  const H = D.history || {labels:[]};
  const hs = D.hunter?.snapshot;
  const hd = D.hunter?.daily?.days || {};

  // Recent agent decisions (last 12)
  for (let i = Math.max(0, H.labels.length - 12); i < H.labels.length; i++) {
    const d = H.labels[i], close = H.btc_close[i], cash = H.alloc_cash[i], yieldPt = H.alloc_yield?.[i] || 0, exp = H.expected_daily[i];
    lines.push({ts:d+" 08:10", type:"AGENT", color:"#00d9ff",
      msg:`SUPER_DECIDER · BTC=$${close.toFixed(0)} · yield=${yieldPt}% · cash=${cash}% · exp_daily=$${exp.toFixed(2)}`});
  }

  // Recent hunter days
  for (const [date, x] of Object.entries(hd).sort()) {
    lines.push({ts:date, type:"HUNTER", color:"#39ff14",
      msg:`SCAN_SUMMARY · scans=${x.scans} · opps=${x.opps_total} · sum_potential=$${x.sum_potential_usd.toFixed(2)}`});
  }

  // Live snapshot
  if (hs) {
    const tnow = hs.ts?.slice(11,19) || "--:--:--";
    lines.push({ts:hs.ts?.slice(0,10)+" "+tnow, type:"LIVE", color:"#ff3d8a",
      msg:`HUNTER_TICK · books bn=${hs.n_binance_books} okx=${hs.n_okx_books} bybit=${hs.n_bybit_books} · opps=${hs.total_opps}`});
  }

  // System boot
  lines.push({ts:D.scorecard.generated_at.slice(0,19).replace("T"," "), type:"BOOT", color:"#ffd700",
    msg:`SUPER_AGENT_BOOT · regime=${D.scorecard.decision.regime} · best_fund=${(D.scorecard.decision.best_funding_apy*100).toFixed(2)}% · basis=${(D.scorecard.basis.ann_basis*100).toFixed(2)}%`});

  lines.sort((a,b)=>a.ts.localeCompare(b.ts));
  box.innerHTML = lines.slice(-60).map(l =>
    `<div><span style="color:#484f58">[${l.ts}]</span> <span style="color:${l.color};font-weight:700">${l.type.padEnd(7)}</span> <span style="color:#e6edf3">${l.msg}</span></div>`
  ).join("");
  box.scrollTop = box.scrollHeight;
}
window.addEventListener("load", () => setTimeout(renderExecLog, 600));

// ============ MOBILE CHART LEGEND ADAPT + SCROLL HINTS ============
// Adjust Chart.js defaults for mobile (smaller legend, fonts)
if (window.innerWidth <= 768) {
  Chart.defaults.font.size = 10;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  Chart.defaults.plugins.legend.labels.padding = 6;
  Chart.defaults.plugins.legend.labels.font = {size:10};
}
// Detect scroll-overflow on chart containers and add hint shadow
function tagScroll() {
  document.querySelectorAll(".chart").forEach(el => {
    if (el.scrollWidth > el.clientWidth + 4) el.classList.add("has-scroll");
    else el.classList.remove("has-scroll");
  });
}
window.addEventListener("load", () => setTimeout(tagScroll, 400));
window.addEventListener("resize", tagScroll);

// ============ MOBILE DRAWER MENU ============
const sb = document.getElementById("sidebar");
const overlay = document.getElementById("menuOverlay");
const menuBtn = document.getElementById("menuBtn");
function toggleMenu(open) {
  if (open) { sb.classList.add("open"); overlay.classList.add("show"); }
  else      { sb.classList.remove("open"); overlay.classList.remove("show"); }
}
menuBtn.addEventListener("click", () => toggleMenu(!sb.classList.contains("open")));
overlay.addEventListener("click", () => toggleMenu(false));
// Auto-close drawer on nav click (mobile)
document.querySelectorAll(".sb-nav a").forEach(a =>
  a.addEventListener("click", () => { if (window.innerWidth <= 768) toggleMenu(false); })
);

// Add viewport meta tweak for iOS notch
if (!document.querySelector('meta[name="viewport"]')) {
  const m = document.createElement("meta");
  m.name = "viewport"; m.content = "width=device-width,initial-scale=1,viewport-fit=cover";
  document.head.appendChild(m);
}

// ============ SIDEBAR SCROLLSPY ============
const links = document.querySelectorAll(".sb-nav a");
const secs = [...document.querySelectorAll("section")];
window.addEventListener("scroll", () => {
  const y = window.scrollY + 120;
  let cur = secs[0].id;
  for (const s of secs) if (s.offsetTop <= y) cur = s.id;
  links.forEach(l => l.classList.toggle("on", l.getAttribute("href") === "#"+cur));
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
