"""Stage 0/1 runner: TradingAgents (unmodified) on KOSPI tickers, price + fundamentals only.

Run from the repo root of the TradingAgents checkout (commit 1394a3f) so that
`import tradingagents` resolves, e.g.:

    cd TradingAgents && pip install -e . yfinance
    python ../run_stage1.py --smoke            # Stage 0: 3 tickers x 3 weeks
    python ../run_stage1.py --run-id kr_s1_seed1   # Stage 1 full grid

LLM provider/model come from the usual TRADINGAGENTS_* env vars or .env.
Nothing in the agent graph or prompts is changed.

Different seeds = different --run-id (each run keeps its own memory log; a ticker/date cell
that is already in a run's log is skipped, so an interrupted run resumes when re-run).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent


def weekly_trading_dates(start: str, end: str, index_ticker: str = "^KS11") -> list[str]:
    """First KOSPI trading day of each calendar week in [start, end]."""
    import yfinance as yf

    px = yf.download(index_ticker, start=start, end=end, progress=False, auto_adjust=False)
    days = pd.Series(px.index.normalize()).drop_duplicates()
    wk = days.groupby(days.dt.to_period("W")).first()
    return [d.strftime("%Y-%m-%d") for d in wk]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(HERE / "universe_kospi10.json"))
    ap.add_argument("--control", action="store_true", help="also run the US control tickers")
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--every-n-weeks", type=int, default=1)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--analysts", default="market,fundamentals",
                    help="Stage 1 = market,fundamentals (no news/social: no point-in-time Korean feed)")
    ap.add_argument("--smoke", action="store_true", help="Stage 0: 3 tickers, 3 weeks")
    ap.add_argument("--dates-file", help="optional JSON list of YYYY-MM-DD analysis dates")
    ap.add_argument("--results-dir", default=str(HERE / "runs"))
    a = ap.parse_args()

    from tradingagents.backtest import run_backtest, summarize
    from tradingagents.default_config import build_default_config

    uni = json.loads(Path(a.universe).read_text())
    tickers = [t["ticker"] for t in uni["kospi"]]
    if a.control:
        tickers += uni["us_control"]

    if a.dates_file:
        dates = json.loads(Path(a.dates_file).read_text())
    else:
        dates = weekly_trading_dates(a.start, a.end)[:: a.every_n_weeks]
    if a.smoke:
        tickers, dates = tickers[:3], dates[:3]

    config = build_default_config()
    config["results_dir"] = a.results_dir
    # TradingAgents has no dated source of Korean statements (EDGAR is US-only, Yahoo is
    # withheld from historical runs), so KR fundamentals would be empty. DART dates every
    # report; it is registered here so the TradingAgents checkout stays unmodified.
    if os.getenv("DART_API_KEY"):
        import dart_vendor
        dart_vendor.register(config)
        print("fundamentals: DART point-in-time statements for KR tickers (EDGAR for US)")
    else:
        print("WARNING: DART_API_KEY not set -> KR fundamentals will be empty (price-only runs)")
    analysts = tuple(x.strip() for x in a.analysts.split(",") if x.strip())
    run_id = a.run_id or time.strftime("kr_s1_%Y%m%d_%H%M%S")

    print(f"run_id={run_id} tickers={len(tickers)} dates={len(dates)} cells={len(tickers)*len(dates)} "
          f"analysts={analysts}")
    print(f"llm: provider={config['llm_provider']} deep={config['deep_think_llm']} "
          f"quick={config['quick_think_llm']} debate_rounds={config['max_debate_rounds']} "
          f"risk_rounds={config['max_risk_discuss_rounds']}")

    t0 = time.time()
    result = run_backtest(
        tickers, dates, config, selected_analysts=analysts, run_id=run_id,
        progress=lambda i, n, tk, d: print(f"[{i}/{n}] {tk} {d}  ({time.time()-t0:.0f}s)", flush=True),
    )
    print(f"\ncells run={result.cells_run} skipped={result.skipped} failures={len(result.failures)}")
    for tk, d, err in result.failures[:20]:
        print(f"  FAIL {tk} {d}: {err[:160]}")
    print(f"memory log: {result.log_path}")
    try:
        print(summarize(result).render())
    except Exception as exc:  # summary needs settled cells; short smoke runs may have none
        print(f"(summary unavailable: {exc})")
    print("\nNext: python portfolio_sim.py --logs", result.log_path, "--download --start", a.start,
          "--end", a.end, "--prices-dir prices")


if __name__ == "__main__":
    main()
