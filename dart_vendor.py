"""Korean company statements as they were filed, from DART (opendart.fss.or.kr).

TradingAgents (commit 1394a3f) withholds Yahoo statements from every historical
run because Yahoo dates a statement by the period it covers, not by the day it
became public. SEC EDGAR is the only dated source and it covers US filers only,
so a KOSPI ticker gets no fundamentals at all.

DART dates every periodic report with its receipt date (``rcept_dt``). A run
dated ``as_of_date`` is served only reports received on or before that date, so
the same point-in-time rule holds for Korean filers.

This module is registered at run time (see ``register``) so the TradingAgents
checkout stays unmodified.

Needs a free API key from https://opendart.fss.or.kr in ``DART_API_KEY``.
A missing key raises ``VendorNotConfiguredError`` and the router moves on.

Known limits (also printed in every table):
- DART serves the latest corrected figures for a period. A period whose report
  was corrected (``[기재정정]``) AFTER ``as_of_date`` is withheld, so a later
  correction is never read early. A correction filed on or before the date is
  served, since it was public by then.
- Quarterly income and cash-flow rows are year-to-date, as Korean filers report
  them. They are labelled so and never subtracted into a standalone quarter.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.files import replace_file

logger = logging.getLogger(__name__)

_BASE = "https://opendart.fss.or.kr/api"
_CACHE_TTL_SECONDS = 24 * 60 * 60

# (statement kind) -> [(row label, DART account ids best first, Korean names as fallback)]
# Account ids are the XBRL ids DART returns in ``account_id``. A filer that does
# not use standard ids reports "-표준계정코드 미사용-", so names back them up.
_STATEMENTS: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "balance_sheet": [
        ("Total Assets", ("ifrs-full_Assets", "ifrs_Assets"), ("자산총계",)),
        ("Current Assets", ("ifrs-full_CurrentAssets", "ifrs_CurrentAssets"), ("유동자산",)),
        ("Cash and Equivalents", ("ifrs-full_CashAndCashEquivalents", "ifrs_CashAndCashEquivalents"),
         ("현금및현금성자산",)),
        ("Total Liabilities", ("ifrs-full_Liabilities", "ifrs_Liabilities"), ("부채총계",)),
        ("Current Liabilities", ("ifrs-full_CurrentLiabilities", "ifrs_CurrentLiabilities"), ("유동부채",)),
        ("Total Equity", ("ifrs-full_Equity", "ifrs_Equity"), ("자본총계",)),
    ],
    "income_statement": [
        ("Revenue", ("ifrs-full_Revenue", "ifrs_Revenue"), ("매출액", "수익(매출액)", "영업수익")),
        ("Cost of Revenue", ("ifrs-full_CostOfSales", "ifrs_CostOfSales"), ("매출원가",)),
        ("Gross Profit", ("ifrs-full_GrossProfit", "ifrs_GrossProfit"), ("매출총이익",)),
        ("Operating Income", ("dart_OperatingIncomeLoss",), ("영업이익", "영업이익(손실)")),
        ("Net Income", ("ifrs-full_ProfitLoss", "ifrs_ProfitLoss"),
         ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익")),
        ("Basic EPS (KRW per share)", ("ifrs-full_BasicEarningsLossPerShare",
                                       "ifrs_BasicEarningsLossPerShare"), ("기본주당이익", "기본주당이익(손실)")),
    ],
    "cashflow": [
        ("Operating Cash Flow", ("ifrs-full_CashFlowsFromUsedInOperatingActivities",
                                 "ifrs_CashFlowsFromUsedInOperatingActivities"), ("영업활동현금흐름", "영업활동 현금흐름")),
        ("Investing Cash Flow", ("ifrs-full_CashFlowsFromUsedInInvestingActivities",
                                 "ifrs_CashFlowsFromUsedInInvestingActivities"), ("투자활동현금흐름", "투자활동 현금흐름")),
        ("Financing Cash Flow", ("ifrs-full_CashFlowsFromUsedInFinancingActivities",
                                 "ifrs_CashFlowsFromUsedInFinancingActivities"), ("재무활동현금흐름", "재무활동 현금흐름")),
    ],
}

# Which statement sections of fnlttSinglAcntAll each kind reads. Income can be
# split across an income statement and a comprehensive income statement.
_SECTIONS = {"balance_sheet": ("BS",), "income_statement": ("IS", "CIS"), "cashflow": ("CF",)}
# Per-share rows are not money: they are not scaled to millions.
_PER_SHARE = "(KRW per share)"

_LIST_START = "20190101"

_REPORT_CODES = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}


def to_stock_code(ticker: str) -> str | None:
    """Six-digit KRX code for ``005930.KS`` / ``035720.KQ`` / ``005930``; None if not Korean."""
    match = re.fullmatch(r"(\d{6})(?:\.(?:KS|KQ))?", ticker.strip().upper())
    return match.group(1) if match else None


def _api_key() -> str:
    key = os.getenv("DART_API_KEY", "").strip()
    if not key:
        raise VendorNotConfiguredError("DART_API_KEY is not set (free key: opendart.fss.or.kr)")
    return key


def _get(path: str, **params) -> requests.Response:
    try:
        response = requests.get(f"{_BASE}/{path}", params={"crtfc_key": _api_key(), **params}, timeout=60)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise VendorUnavailableError(f"DART request failed ({status or type(exc).__name__})") from exc


def _get_json(path: str, **params) -> dict:
    """A DART JSON call. Status 000 is data, 013 is "no data", anything else is a refusal."""
    try:
        body = _get(path, **params).json()
    except ValueError as exc:
        raise VendorUnavailableError("DART returned an unreadable response") from exc
    status = str(body.get("status", ""))
    if status in ("000", "013"):
        return body
    # 010/011/012 key problems, 020 daily quota, 800 maintenance: none says
    # anything about the company, so the router should try the next vendor.
    raise VendorUnavailableError(f"DART refused the request (status {status}: {body.get('message', '')})")


def _cache_path(name: str) -> Path:
    return Path(get_config()["data_cache_dir"]) / "dart" / name


def _cached(name: str, fetch):
    path = _cache_path(name)
    if path.exists() and time.time() - path.stat().st_mtime < _CACHE_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass  # a truncated file is a miss, not a failure
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8"))
    return data


def _corp_codes() -> dict[str, str]:
    """{six-digit stock code: DART corp code} for every listed company."""

    def fetch() -> dict[str, str]:
        raw = _get("corpCode.xml").content
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                root = ElementTree.fromstring(archive.read(archive.namelist()[0]))
        except (zipfile.BadZipFile, ElementTree.ParseError, IndexError) as exc:
            # A bad key comes back as a small XML error body, not a zip.
            raise VendorUnavailableError("DART corp code list was not a readable archive") from exc
        table = {}
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            if stock:
                table[stock] = (item.findtext("corp_code") or "").strip()
        return table

    return _cached("corp_codes.json", fetch)


def _report_kind(report_nm: str) -> tuple[str, str] | None:
    """(report code, period end 'YYYY-MM') of a periodic report title, else None.

    '사업보고서 (2025.12)' -> annual, '분기보고서 (2025.03)' -> Q1, '(2025.09)' -> Q3.
    """
    match = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    if not match:
        return None
    period = f"{match.group(1)}-{match.group(2)}"
    if "사업보고서" in report_nm:
        return _REPORT_CODES["annual"], period
    if "반기보고서" in report_nm:
        return _REPORT_CODES["half"], period
    if "분기보고서" in report_nm:
        return (_REPORT_CODES["q1"] if match.group(2) in ("03", "04", "05") else _REPORT_CODES["q3"]), period
    return None


def _filings(corp_code: str, as_of_date: str) -> list[dict]:
    """Periodic reports with their receipt dates, as {period, code, original, amended}."""
    # One fixed window per company, so a backtest fetches each list once a day
    # instead of once per analysis date. It runs to today, not to the date:
    # corrections filed later are read only to withhold a period, never to serve it.
    bgn, stop = _LIST_START, datetime.now().strftime("%Y%m%d")

    def fetch() -> list[dict]:
        rows, page = [], 1
        while True:
            body = _get_json("list.json", corp_code=corp_code, bgn_de=bgn, end_de=stop,
                             pblntf_ty="A", page_no=page, page_count=100)
            rows += body.get("list") or []
            if page >= int(body.get("total_page") or 1):
                return rows
            page += 1

    rows = _cached(f"list_{corp_code}.json", fetch)
    periods: dict[tuple[str, str], dict] = {}
    for row in rows:
        kind = _report_kind(row.get("report_nm", ""))
        if kind is None:
            continue
        code, period = kind
        entry = periods.setdefault((code, period), {"code": code, "period": period, "original": None, "amended": []})
        received = f"{row['rcept_dt'][:4]}-{row['rcept_dt'][4:6]}-{row['rcept_dt'][6:8]}"
        if "정정" in row["report_nm"]:
            entry["amended"].append(received)
        else:
            entry["original"] = received
    return list(periods.values())


def _eligible(filings: list[dict], as_of_date: str, annual_only: bool) -> tuple[list[dict], list[str]]:
    """Reports public by ``as_of_date`` whose figures were not corrected afterwards.

    Returns (periods newest first, notes about periods withheld).
    """
    served, notes = [], []
    for entry in filings:
        if annual_only and entry["code"] != _REPORT_CODES["annual"]:
            continue
        # A report whose first filing is a correction is dated by that filing.
        first = entry["original"] or min(entry["amended"], default=None)
        if first is None or first > as_of_date:
            continue
        if any(day > as_of_date for day in entry["amended"]):
            notes.append(f"{entry['period']} withheld: corrected after {as_of_date}")
            continue
        served.append({**entry, "received": first})
    served.sort(key=lambda e: e["period"], reverse=True)
    return served, notes


def _amount(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = text.replace(",", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _accounts(corp_code: str, year: str, code: str) -> list[dict]:
    """Every account of one report, consolidated statements first, else separate."""

    def fetch() -> list[dict]:
        for fs_div in ("CFS", "OFS"):
            body = _get_json("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=year,
                             reprt_code=code, fs_div=fs_div)
            if body.get("list"):
                return [{**row, "fs_div": fs_div} for row in body["list"]]
        return []

    return _cached(f"acct_{corp_code}_{year}_{code}.json", fetch)


def _pick(accounts: list[dict], sections: tuple[str, ...], ids: tuple[str, ...],
          names: tuple[str, ...], cumulative: bool) -> float | None:
    rows = [a for a in accounts if a.get("sj_div") in sections]
    for wanted in ids:
        for row in rows:
            if row.get("account_id") == wanted:
                return _value(row, cumulative)
    for wanted in names:
        for row in rows:
            if (row.get("account_nm") or "").replace(" ", "") == wanted.replace(" ", ""):
                return _value(row, cumulative)
    return None


def _value(row: dict, cumulative: bool) -> float | None:
    # A quarterly income statement carries the quarter in thstrm_amount and the
    # year to date in thstrm_add_amount. The table shows the year to date.
    if cumulative:
        added = _amount(row.get("thstrm_add_amount"))
        if added is not None:
            return added
    return _amount(row.get("thstrm_amount"))


def _months(period: str) -> int:
    return int(period[5:7])


def _statement(kind: str, ticker: str, freq: str, as_of_date: str, title: str) -> str:
    as_of_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    stock = to_stock_code(ticker)
    if stock is None:
        raise NoMarketDataError(ticker, ticker, "not a Korean (KRX) ticker")
    corp_code = _corp_codes().get(stock)
    if corp_code is None:
        raise NoMarketDataError(ticker, ticker, "not a DART filer")

    annual = freq.lower() != "quarterly"
    served, notes = _eligible(_filings(corp_code, as_of_date), as_of_date, annual)
    served = served[: 3 if annual else 5]
    if not served:
        raise NoMarketDataError(ticker, ticker, f"no {freq} {title.lower()} filed by {as_of_date}")

    columns = []
    for entry in served:
        accounts = _accounts(corp_code, entry["period"][:4], entry["code"])
        if not accounts:
            continue
        cumulative = kind != "balance_sheet" and entry["code"] != _REPORT_CODES["annual"]
        label = entry["period"]
        if cumulative:
            label += f" (YTD {_months(entry['period'])} months)"
        values = {row_label: _pick(accounts, _SECTIONS[kind], ids, names, cumulative)
                  for row_label, ids, names in _STATEMENTS[kind]}
        columns.append((label, entry["received"], accounts[0].get("fs_div", ""), values))
    if not columns:
        raise NoMarketDataError(ticker, ticker, f"no {freq} {title.lower()} figures served by DART")

    header = (
        f"# {title} for {ticker.upper()} ({freq}), KRW in millions unless the row says otherwise\n"
        f"# DART periodic reports received on or before {as_of_date}; "
        f"{'consolidated' if columns[0][2] == 'CFS' else 'separate'} statements\n"
    )
    for note in notes:
        header += f"# {note}\n"
    header += "\n"
    rows = [",".join([""] + [label for label, _, _, _ in columns]),
            ",".join(["Report received"] + [received for _, received, _, _ in columns])]
    for row_label, _, _ in _STATEMENTS[kind]:
        cells = []
        for _, _, _, values in columns:
            value = values[row_label]
            if value is None:
                cells.append("unavailable (not tagged by this filer)")
            elif row_label.endswith(_PER_SHARE):
                cells.append(f"{value:.2f}")
            else:
                cells.append(f"{value / 1e6:.0f}")
        rows.append(",".join([row_label] + cells))
    return header + "\n".join(rows) + "\n"


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Balance sheet from reports DART had received by ``as_of_date``."""
    return _statement("balance_sheet", ticker, freq, as_of_date, "Balance Sheet")


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Income statement from reports DART had received by ``as_of_date`` (quarters are year to date)."""
    return _statement("income_statement", ticker, freq, as_of_date, "Income Statement")


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Cash flow statement from reports DART had received by ``as_of_date`` (quarters are year to date)."""
    return _statement("cashflow", ticker, freq, as_of_date, "Cash Flow Statement")


def register(config: dict | None = None) -> None:
    """Add DART to the router, ahead of EDGAR and Yahoo, without touching TradingAgents.

    ``config`` is the dict handed to the graph; its statement chain becomes
    ``dart,sec_edgar,yfinance`` so US tickers still reach EDGAR.
    """
    from tradingagents.dataflows import router

    router.VENDOR_METHODS["get_balance_sheet"]["dart"] = get_balance_sheet
    router.VENDOR_METHODS["get_income_statement"]["dart"] = get_income_statement
    router.VENDOR_METHODS["get_cashflow"]["dart"] = get_cashflow
    if config is not None:
        config.setdefault("data_vendors", {})["fundamental_data"] = "dart,sec_edgar,yfinance"
