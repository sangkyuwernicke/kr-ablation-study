"""Offline tests for dart_vendor: point-in-time filtering against canned DART responses."""
import pytest

import dart_vendor as dv
from tradingagents.dataflows.errors import NoMarketDataError, VendorNotConfiguredError

LIST_ROWS = [
    {"report_nm": "사업보고서 (2024.12)", "rcept_dt": "20250311"},
    {"report_nm": "분기보고서 (2025.03)", "rcept_dt": "20250515"},
    {"report_nm": "반기보고서 (2025.06)", "rcept_dt": "20250814"},
    {"report_nm": "[기재정정]반기보고서 (2025.06)", "rcept_dt": "20251020"},
    {"report_nm": "분기보고서 (2025.09)", "rcept_dt": "20251114"},
    {"report_nm": "주요사항보고서(자기주식취득결정)", "rcept_dt": "20250601"},
]


def _accounts(rev, assets, annual=False):
    # DART: an annual report has the full year in thstrm_amount and no thstrm_add_amount;
    # a quarter has the quarter in thstrm_amount and the year to date in thstrm_add_amount.
    return [
        {"sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계", "thstrm_amount": f"{assets}"},
        {"sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "매출액",
         "thstrm_amount": f"{rev}" if annual else "1", **({} if annual else {"thstrm_add_amount": f"{rev}"})},
        {"sj_div": "IS", "account_id": "-표준계정코드 미사용-", "account_nm": "영업이익", "thstrm_amount": "5000000000000"},
        {"sj_div": "CF", "account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities",
         "account_nm": "영업활동현금흐름", "thstrm_amount": "2000000000000"},
    ]


@pytest.fixture(autouse=True)
def fake_dart(monkeypatch, tmp_path):
    monkeypatch.setenv("DART_API_KEY", "test")
    monkeypatch.setattr(dv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(dv, "_corp_codes", lambda: {"005930": "00126380"})
    rows = [{**r, "rcept_no": "x"} for r in LIST_ROWS]

    def fake_get_json(path, **params):
        if path == "list.json":
            return {"status": "000", "total_page": 1, "list": rows}
        year, code = params["bsns_year"], params["reprt_code"]
        rev = 300_000_000_000_000 if code == "11011" else 80_000_000_000_000
        return {"status": "000", "list": _accounts(rev, 500_000_000_000_000, annual=code == "11011")}

    monkeypatch.setattr(dv, "_get_json", fake_get_json)


def _columns(out):
    return next(l for l in out.splitlines() if l.startswith(",")).split(",")[1:]


def test_only_reports_received_by_the_date_are_served():
    out = dv.get_income_statement("005930.KS", "quarterly", "2025-09-01")
    assert _columns(out) == ["2025-03 (YTD 3 months)", "2024-12"]   # 2025-09 not yet received, 2025-06 corrected later
    assert any(l.startswith("# 2025-06 withheld") for l in out.splitlines())


def test_later_correction_is_served_once_public():
    out = dv.get_income_statement("005930.KS", "quarterly", "2025-12-01")
    assert "2025-09 (YTD 9 months)" in out and "2025-06 (YTD 6 months)" in out
    assert "withheld" not in out


def test_quarterly_income_uses_year_to_date_and_scales_to_millions():
    out = dv.get_income_statement("005930.KS", "quarterly", "2025-09-01")
    revenue = next(l for l in out.splitlines() if l.startswith("Revenue"))
    assert "80000000" in revenue and "300000000" in revenue   # 80조 / 300조 in KRW millions


def test_name_fallback_when_filer_uses_no_standard_id():
    out = dv.get_income_statement("005930.KS", "annual", "2025-09-01")
    op = next(l for l in out.splitlines() if l.startswith("Operating Income"))
    assert "5000000" in op


def test_annual_only_serves_annual_reports():
    out = dv.get_balance_sheet("005930.KS", "annual", "2025-12-01")
    assert _columns(out) == ["2024-12"]


def test_untagged_row_is_marked_unavailable_not_zero():
    out = dv.get_balance_sheet("005930.KS", "annual", "2025-09-01")
    assert "unavailable (not tagged by this filer)" in next(l for l in out.splitlines() if l.startswith("Current Assets"))


def test_non_korean_and_unknown_tickers_fall_through():
    with pytest.raises(NoMarketDataError):
        dv.get_balance_sheet("AAPL", "quarterly", "2025-09-01")
    with pytest.raises(NoMarketDataError):
        dv.get_balance_sheet("999999.KS", "quarterly", "2025-09-01")


def test_nothing_filed_yet_is_no_data():
    with pytest.raises(NoMarketDataError):
        dv.get_balance_sheet("005930.KS", "quarterly", "2024-01-02")


def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("DART_API_KEY")
    with pytest.raises(VendorNotConfiguredError):
        dv._api_key()


def test_register_routes_korean_tickers_to_dart_and_us_tickers_past_it(monkeypatch):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.config import set_config

    config = {"data_vendors": {"fundamental_data": "sec_edgar,yfinance"}}
    dv.register(config)
    assert config["data_vendors"]["fundamental_data"] == "dart,sec_edgar,yfinance"
    set_config({**config, "data_cache_dir": "/tmp/unused"})
    seen = []
    monkeypatch.setitem(router.VENDOR_METHODS["get_balance_sheet"], "sec_edgar",
                        lambda *a, **k: seen.append("edgar") or "EDGAR TABLE")
    assert "DART periodic reports" in router.route_to_vendor("get_balance_sheet", "005930.KS", "quarterly", "2025-09-01")
    assert router.route_to_vendor("get_balance_sheet", "AAPL", "quarterly", "2025-09-01") == "EDGAR TABLE"
    assert seen == ["edgar"]


@pytest.mark.parametrize("ticker,code", [("005930.KS", "005930"), ("035720.kq", "035720"), ("005930", "005930"),
                                          ("AAPL", None), ("7203.T", None)])
def test_stock_code(ticker, code):
    assert dv.to_stock_code(ticker) == code
