"""Markdown report formatter for the backtest result."""

from __future__ import annotations

from app.backtest.replay import BacktestResult, BacktestTrade


def format_backtest_report(result: BacktestResult) -> str:
    L: list[str] = []
    L.append(f"# ORB Backtest — {result.start_ist} → {result.end_ist}")
    L.append("")
    L.append("_Replays the real `ORBStrategy` against historical 1-min bars from Angel One._")
    L.append("_LLM gates (Tier 1 regime + Tier 2 pretrade) are intentionally **disabled** —_")
    L.append("_they can't be replayed faithfully without contemporaneous prompt context._")
    L.append("")

    L.append(_section_aggregate(result))
    L.append("")
    L.append(_section_gates(result))
    L.append("")
    L.append(_section_daily(result))
    L.append("")
    L.append(_section_per_symbol(result))
    L.append("")
    L.append(_section_exit_breakdown(result))
    L.append("")
    L.append(_section_signal_attrition(result))
    L.append("")
    L.append(_section_trade_log(result))
    L.append("")
    L.append(_section_caveats())
    return "\n".join(L)


def _section_aggregate(r: BacktestResult) -> str:
    trades = r.trades
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    total = sum(t.net_pnl for t in trades)
    charges_total = sum(t.entry_charges + t.exit_charges for t in trades)
    avg_winner = (sum(t.net_pnl for t in wins) / len(wins)) if wins else None
    avg_loser = (sum(t.net_pnl for t in losses) / len(losses)) if losses else None
    expectancy = (total / n) if n else None
    win_rate = (100.0 * len(wins) / n) if n else None
    avg_r = (sum(t.r_multiple for t in trades) / n) if n else None
    max_dd, max_dd_pct = _max_drawdown(trades, r.capital_inr)

    out = ["## Aggregate stats", ""]
    out.append(f"- Sessions analyzed   : {len(r.sessions)}")
    out.append(f"- Trades taken        : {n}")
    out.append(f"- Wins                : {len(wins)}")
    out.append(f"- Losses              : {len(losses)}")
    out.append(
        f"- Win rate            : {win_rate:.1f}%"
        if win_rate is not None
        else "- Win rate            : n/a"
    )
    out.append(f"- Total net P&L (₹)   : {total:,.2f}")
    out.append(f"- Charges paid (₹)    : {charges_total:,.2f}")
    out.append(
        f"- Expectancy per trade: ₹{expectancy:,.2f}"
        if expectancy is not None
        else "- Expectancy per trade: n/a"
    )
    out.append(
        f"- Avg winner (₹)      : {avg_winner:,.2f}"
        if avg_winner is not None
        else "- Avg winner (₹)      : n/a"
    )
    out.append(
        f"- Avg loser (₹)       : {avg_loser:,.2f}"
        if avg_loser is not None
        else "- Avg loser (₹)       : n/a"
    )
    out.append(
        f"- Avg R-multiple      : {avg_r:+.2f} R"
        if avg_r is not None
        else "- Avg R-multiple      : n/a"
    )
    out.append(f"- Max drawdown        : ₹{max_dd:,.2f} ({max_dd_pct:.2f}% of capital)")
    return "\n".join(out)


def _section_gates(r: BacktestResult) -> str:
    n = len(r.trades)
    if n == 0:
        return "## Phase 7 gates\n\n(no trades — gates cannot be evaluated)"
    wins = sum(1 for t in r.trades if t.net_pnl > 0)
    win_rate = 100.0 * wins / n
    total = sum(t.net_pnl for t in r.trades)
    expectancy = total / n
    max_dd, max_dd_pct = _max_drawdown(r.trades, r.capital_inr)

    g1 = win_rate >= 40.0
    g2 = expectancy > 0.0
    g3 = max_dd_pct <= 8.0
    verdict = "GO" if (g1 and g2 and g3) else "NO-GO"
    return (
        "## Phase 7 gates (would the strategy graduate from paper to live?)\n\n"
        f"| Gate | Threshold | Actual | Pass? |\n"
        f"| --- | --- | --- | --- |\n"
        f"| Win rate | ≥ 40% | {win_rate:.1f}% | {'PASS' if g1 else 'FAIL'} |\n"
        f"| Expectancy / trade | > ₹0 | ₹{expectancy:+.2f} | {'PASS' if g2 else 'FAIL'} |\n"
        f"| Max drawdown | ≤ 8% capital | {max_dd_pct:.2f}% | {'PASS' if g3 else 'FAIL'} |\n\n"
        f"**Backtest verdict: {verdict}**"
    )


def _section_daily(r: BacktestResult) -> str:
    if not r.sessions:
        return "## Daily P&L\n\n(no sessions)"
    out = [
        "## Daily P&L",
        "",
        "| Date | Signals | Taken | Net P&L | Cum P&L |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    cum = 0.0
    for s in r.sessions:
        cum += s.net_pnl_day
        out.append(
            f"| {s.date_ist} | {s.signals_fired} | {s.trades_taken} "
            f"| ₹{s.net_pnl_day:+,.2f} | ₹{cum:+,.2f} |"
        )
    return "\n".join(out)


def _section_per_symbol(r: BacktestResult) -> str:
    if not r.trades:
        return "## Per-symbol breakdown\n\n(no trades)"
    out = [
        "## Per-symbol breakdown",
        "",
        "| Symbol | Trades | Wins | Losses | Net P&L | Avg R |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    by_sym: dict[str, list[BacktestTrade]] = {}
    for t in r.trades:
        by_sym.setdefault(t.symbol, []).append(t)
    for sym in sorted(by_sym):
        ts = by_sym[sym]
        wins = sum(1 for t in ts if t.net_pnl > 0)
        losses = sum(1 for t in ts if t.net_pnl < 0)
        net = sum(t.net_pnl for t in ts)
        avg_r = sum(t.r_multiple for t in ts) / len(ts)
        out.append(f"| {sym} | {len(ts)} | {wins} | {losses} | ₹{net:+,.2f} | {avg_r:+.2f}R |")
    return "\n".join(out)


def _section_exit_breakdown(r: BacktestResult) -> str:
    if not r.trades:
        return "## Exit-reason breakdown\n\n(no trades)"
    out = [
        "## Exit-reason breakdown",
        "",
        "| Reason | Count | Avg net P&L | Total net P&L |",
        "| --- | ---: | ---: | ---: |",
    ]
    by_reason: dict[str, list[BacktestTrade]] = {}
    for t in r.trades:
        by_reason.setdefault(t.exit_reason, []).append(t)
    for reason in ("target_hit", "stop_hit", "time_stop"):
        ts = by_reason.get(reason, [])
        if not ts:
            out.append(f"| {reason} | 0 | — | — |")
            continue
        avg = sum(t.net_pnl for t in ts) / len(ts)
        tot = sum(t.net_pnl for t in ts)
        out.append(f"| {reason} | {len(ts)} | ₹{avg:+,.2f} | ₹{tot:+,.2f} |")
    return "\n".join(out)


def _section_signal_attrition(r: BacktestResult) -> str:
    total_signals = sum(s.signals_fired for s in r.sessions)
    by_max = sum(s.signals_blocked_by_max_trades for s in r.sessions)
    by_re = sum(s.signals_blocked_by_reentry for s in r.sessions)
    by_open = sum(s.signals_blocked_by_already_open for s in r.sessions)
    by_size = sum(s.signals_blocked_by_sizing for s in r.sessions)
    taken = sum(s.trades_taken for s in r.sessions)
    return (
        "## Signal attrition — where the funnel narrows\n\n"
        f"- Raw ORB signals fired         : {total_signals}\n"
        f"- Blocked by max-2-trades       : {by_max}\n"
        f"- Blocked by re-entry-after-stop: {by_re}\n"
        f"- Blocked by already-in-symbol  : {by_open}\n"
        f"- Blocked by sizing (notional)  : {by_size}\n"
        f"- **Trades actually taken**     : **{taken}**"
    )


def _section_trade_log(r: BacktestResult) -> str:
    if not r.trades:
        return "## Trade log\n\n(no trades)"
    header = (
        "| Date | Symbol | Dir | Qty | Entry (IST) | Entry px "
        "| Exit (IST) | Exit px | Reason | Gross | Charges | **Net** | R |"
    )
    sep = (
        "| --- | --- | :---: | ---: | :---: | ---: | :---: "
        "| ---: | :---: | ---: | ---: | ---: | ---: |"
    )
    out = ["## Full trade log", "", header, sep]
    for t in r.trades:
        ch = t.entry_charges + t.exit_charges
        out.append(
            f"| {t.date_ist} | {t.symbol} | {t.direction} | {t.qty} "
            f"| {t.entry_time_ist} | {t.entry_price:.2f} "
            f"| {t.exit_time_ist} | {t.exit_price:.2f} | {t.exit_reason} "
            f"| ₹{t.gross_pnl:+,.2f} | ₹{ch:,.2f} | **₹{t.net_pnl:+,.2f}** | {t.r_multiple:+.2f} |"
        )
    return "\n".join(out)


def _section_caveats() -> str:
    lines = [
        "## Caveats — read before drawing conclusions",
        "",
        "1. **LLM gates are disabled.** Tier 1 regime (Gemini Pro) and Tier 2",
        "   pretrade (Gemini Flash Lite) require contemporaneous prompt context",
        "   (live breadth, news headlines, regime state at decision time) that we",
        "   don't have historically. **Live trading will see fewer trades** than",
        "   this backtest because some signals will be blocked by",
        "   `regime_risk_off` or `pretrade_skip`. Net effect on stats is",
        "   ambiguous — could improve win rate (filter bad days) or hurt",
        "   expectancy (skip legitimate trades on choppy regime calls).",
        "2. **Slippage on stops is likely understated.** We assume fill at the",
        "   stop price ± 5 bps. Real stops often slip further on fast moves and",
        "   gaps. **Realised stop-out P&L will be worse** in live.",
        "3. **No news / event days.** Earnings, RBI announcements, geopolitical",
        "   shocks aren't modeled. Strategy may behave differently on those.",
        "4. **Same-bar tie-break.** When a 1-min bar's range covers both stop and",
        "   target, we pessimistically assume stop hit first.",
        "5. **No pre-market / after-market.** Backtest covers 09:15–15:30 IST.",
        "",
    ]
    return "\n".join(lines)


def _max_drawdown(trades: list[BacktestTrade], capital_inr: float) -> tuple[float, float]:
    if not trades:
        return 0.0, 0.0
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for t in trades:
        equity += t.net_pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    pct = 100.0 * worst / capital_inr if capital_inr > 0 else 0.0
    return worst, pct
