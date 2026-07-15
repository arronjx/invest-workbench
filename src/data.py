"""
数据层：基于 global-stock-data 技能的公开 API（零鉴权 / Yahoo crumb 自动获取）。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SEC_HEADERS = {"User-Agent": "invest-workbench/1.0 (local; contact@local)"}

# 统一对外时间显示：UTC+8（北京时间）
CST = timezone(timedelta(hours=8))

_yahoo_session: requests.Session | None = None
_cik_cache: dict | None = None


# ── Yahoo / 东财 helpers ──────────────────────────────────────────


def get_yahoo_session() -> requests.Session:
    global _yahoo_session
    if _yahoo_session and hasattr(_yahoo_session, "_crumb"):
        return _yahoo_session
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.get("https://fc.yahoo.com", timeout=10)
    r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    r.raise_for_status()
    s._crumb = r.text  # type: ignore[attr-defined]
    _yahoo_session = s
    return s


def yahoo_quote_summary(symbol: str, modules: list[str]) -> dict:
    s = get_yahoo_session()
    r = s.get(
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
        params={"modules": ",".join(modules), "crumb": s._crumb},  # type: ignore[attr-defined]
        timeout=20,
    )
    r.raise_for_status()
    results = r.json().get("quoteSummary", {}).get("result", [{}])
    return results[0] if results else {}


def eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ── 搜索 / 代码规范化 ─────────────────────────────────────────────


def stock_search(keyword: str, count: int = 10) -> list[dict]:
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword,
        "type": 14,
        "token": "D43BF722C8E33BDC906FB84D85E326E8",
        "count": count,
    }
    r = requests.get(url, params=params, timeout=10)
    suggestions = r.json().get("QuotationCodeTable", {}).get("Data", []) or []
    market_map = {"105": "NASDAQ", "106": "NYSE", "107": "US_OTHER", "116": "HK"}
    out = []
    for s in suggestions:
        mkt = str(s.get("MktNum", ""))
        if mkt not in market_map:
            continue
        out.append(
            {
                "code": s.get("Code"),
                "name": s.get("Name"),
                "mkt_num": int(mkt),
                "market_name": market_map[mkt],
                "security_type": s.get("SecurityTypeName"),
            }
        )
    return out


def resolve_symbol(raw: str) -> dict[str, Any]:
    """
    把用户输入规范化为统一结构。
    返回: {input, yahoo, eastmoney_secucode, secid_prefix, code, market, name}
    """
    raw = raw.strip().upper()
    # 港股纯数字
    if re.fullmatch(r"\d{1,5}", raw) or raw.endswith(".HK"):
        code = raw.replace(".HK", "").zfill(5)
        name = code
        try:
            hits = stock_search(code)
            hk = next((h for h in hits if h["mkt_num"] == 116), None)
            if hk:
                name = hk["name"]
        except Exception:
            pass
        return {
            "input": raw,
            "yahoo": f"{int(code):04d}.HK",
            "eastmoney_secucode": f"{code}.HK",
            "secid_prefix": 116,
            "code": code,
            "market": "HK",
            "name": name,
        }

    # 美股：搜索失败时直接按 ticker 兜底
    try:
        hits = stock_search(raw)
    except Exception:
        hits = []
    us = next((h for h in hits if h["mkt_num"] in (105, 106, 107) and h["code"].upper() == raw), None)
    if not us and hits:
        us = next((h for h in hits if h["mkt_num"] in (105, 106, 107)), None)
    if us:
        suffix = {105: ".O", 106: ".N", 107: ".O"}.get(us["mkt_num"], ".O")
        yahoo = us["code"].upper()
        return {
            "input": raw,
            "yahoo": yahoo,
            "eastmoney_secucode": f"{us['code']}{suffix}",
            "secid_prefix": us["mkt_num"],
            "code": us["code"].upper(),
            "market": us["market_name"],
            "name": us["name"],
        }

    return {
        "input": raw,
        "yahoo": raw,
        "eastmoney_secucode": f"{raw}.O",
        "secid_prefix": 105,
        "code": raw,
        "market": "US",
        "name": raw,
    }


# ── 行情 ──────────────────────────────────────────────────────────


def us_stock_quote_sina(ticker: str) -> dict:
    url = f"https://hq.sinajs.cn/list=gb_{ticker.lower()}"
    r = requests.get(
        url,
        headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": UA},
        timeout=10,
    )
    r.encoding = "gbk"
    m = re.search(r'"(.+)"', r.text)
    if not m:
        return {}
    fields = m.group(1).split(",")
    if len(fields) < 30:
        return {}

    def f(i, cast=float):
        try:
            return cast(fields[i]) if fields[i] not in ("", None) else 0
        except Exception:
            return 0

    return {
        "name": fields[0],
        "price": f(1),
        "change_pct": f(2),
        "timestamp": fields[3],
        "prev_close": f(26),
        "open": f(5),
        "high": f(6),
        "low": f(7),
        "volume": f(10),
        "high_52w": f(8),
        "low_52w": f(9),
        "market_cap": f(12),
        "eps": f(13),
        "pe": f(14),
        "source": "sina",
    }


def hk_stock_quote_tencent(code: str) -> dict:
    code = code.zfill(5)
    url = f"https://qt.gtimg.cn/q=r_hk{code}"
    r = requests.get(url, timeout=10)
    r.encoding = "gbk"
    m = re.search(r'"(.+)"', r.text)
    if not m:
        return {}
    fields = m.group(1).split("~")
    if len(fields) < 50:
        return {}

    def f(i, cast=float):
        try:
            return cast(fields[i]) if fields[i] not in ("", None) else 0
        except Exception:
            return 0

    return {
        "name": fields[1],
        "name_en": fields[2],
        "price": f(3),
        "prev_close": f(4),
        "open": f(5),
        "volume": f(6, int),
        "high": f(33),
        "low": f(34),
        "high_52w": f(35),
        "low_52w": f(36),
        "change_pct": f(32),
        "pe": f(39),
        "pb": f(56),
        "market_cap": f(44),
        "timestamp": fields[30],
        "source": "tencent",
    }


def get_quote(raw: str) -> dict:
    meta = resolve_symbol(raw)
    if meta["market"] == "HK" or str(meta.get("secid_prefix")) == "116":
        q = hk_stock_quote_tencent(meta["code"])
    else:
        q = us_stock_quote_sina(meta["code"])
        if not q:
            # Yahoo 兜底
            try:
                d = yahoo_quote_summary(meta["yahoo"], ["price", "summaryDetail"])
                p = d.get("price", {})
                sd = d.get("summaryDetail", {})

                def rv(x):
                    return x.get("raw") if isinstance(x, dict) else x

                q = {
                    "name": p.get("shortName") or meta["name"],
                    "price": rv(p.get("regularMarketPrice", {})),
                    "change_pct": (rv(p.get("regularMarketChangePercent", {})) or 0) * 100,
                    "prev_close": rv(p.get("regularMarketPreviousClose", {})),
                    "open": rv(p.get("regularMarketOpen", {})),
                    "high": rv(p.get("regularMarketDayHigh", {})),
                    "low": rv(p.get("regularMarketDayLow", {})),
                    "volume": rv(p.get("regularMarketVolume", {})),
                    "high_52w": rv(sd.get("fiftyTwoWeekHigh", {})),
                    "low_52w": rv(sd.get("fiftyTwoWeekLow", {})),
                    "pe": rv(sd.get("trailingPE", {})),
                    "source": "yahoo",
                }
            except Exception as e:
                q = {"error": str(e)}
    return {"meta": meta, "quote": q}


# ── K 线 ──────────────────────────────────────────────────────────


def stock_kline_yahoo(symbol: str, interval: str = "1d", range_: str = "5y") -> list[dict]:
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(
        url,
        params={"interval": interval, "range": range_},
        headers={"User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    chart = r.json().get("chart", {}).get("result", [{}])[0]
    timestamps = chart.get("timestamp", []) or []
    quote = chart.get("indicators", {}).get("quote", [{}])[0]
    out = []
    for i, ts in enumerate(timestamps):
        o, h, l, c, v = (
            quote["open"][i],
            quote["high"][i],
            quote["low"][i],
            quote["close"][i],
            quote["volume"][i],
        )
        if c is None:
            continue
        out.append(
            {
                "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                "open": round(o or 0, 4),
                "high": round(h or 0, 4),
                "low": round(l or 0, 4),
                "close": round(c or 0, 4),
                "volume": int(v or 0),
            }
        )
    return out


def us_stock_kline_sina(ticker: str, num: int = 250) -> list[dict]:
    url = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK"
    r = requests.get(
        url,
        params={"symbol": ticker.upper(), "num": num},
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=15,
    )
    m = re.search(r"\((\[.+\])\)", r.text)
    if not m:
        return []
    items = json.loads(m.group(1))
    return [
        {
            "date": item.get("d"),
            "open": float(item.get("o", 0)),
            "high": float(item.get("h", 0)),
            "low": float(item.get("l", 0)),
            "close": float(item.get("c", 0)),
            "volume": int(item.get("v", 0)),
        }
        for item in items
    ]


def get_klines(raw: str, range_: str = "5y") -> list[dict]:
    meta = resolve_symbol(raw)
    if meta["market"] == "HK" or meta.get("secid_prefix") == 116:
        return stock_kline_yahoo(meta["yahoo"], "1d", range_)
    kl = us_stock_kline_sina(meta["code"], 1200 if range_ in ("5y", "10y", "max") else 250)
    if not kl:
        return stock_kline_yahoo(meta["yahoo"], "1d", range_)
    return kl


# ── 基本面 / 分析师 / 新闻 ────────────────────────────────────────


def _raw(d: dict, key: str):
    v = d.get(key, {})
    return v.get("raw") if isinstance(v, dict) else v


def key_statistics(symbol: str) -> dict:
    data = yahoo_quote_summary(symbol, ["financialData", "defaultKeyStatistics", "summaryDetail"])
    fd, ks, sd = data.get("financialData", {}), data.get("defaultKeyStatistics", {}), data.get("summaryDetail", {})
    return {
        "current_price": _raw(fd, "currentPrice"),
        "target_high": _raw(fd, "targetHighPrice"),
        "target_low": _raw(fd, "targetLowPrice"),
        "target_mean": _raw(fd, "targetMeanPrice"),
        "recommendation": fd.get("recommendationKey"),
        "trailing_pe": _raw(sd, "trailingPE"),
        "forward_pe": _raw(ks, "forwardPE"),
        "peg_ratio": _raw(ks, "pegRatio"),
        "price_to_book": _raw(ks, "priceToBook"),
        "enterprise_value": _raw(ks, "enterpriseValue"),
        "ev_to_ebitda": _raw(ks, "enterpriseToEbitda"),
        "profit_margin": _raw(ks, "profitMargins"),
        "operating_margin": _raw(fd, "operatingMargins"),
        "gross_margin": _raw(fd, "grossMargins"),
        "return_on_equity": _raw(fd, "returnOnEquity"),
        "return_on_assets": _raw(fd, "returnOnAssets"),
        "earnings_growth": _raw(fd, "earningsGrowth"),
        "revenue_growth": _raw(fd, "revenueGrowth"),
        "beta": _raw(ks, "beta"),
        "dividend_yield": _raw(sd, "dividendYield"),
        "market_cap": _raw(sd, "marketCap"),
        "total_revenue": _raw(fd, "totalRevenue"),
        "total_cash": _raw(fd, "totalCash"),
        "total_debt": _raw(fd, "totalDebt"),
        "free_cashflow": _raw(fd, "freeCashflow"),
    }


def key_indicators_eastmoney(secucode: str, page_size: int = 4) -> list[dict]:
    market = "hk" if secucode.endswith(".HK") else "us"
    report_name = f"RPT_{'HK' if market == 'hk' else 'US'}F10_FN_GMAININDICATOR"
    return eastmoney_datacenter(
        report_name=report_name,
        filter_str=f'(SECUCODE="{secucode}")',
        page_size=page_size,
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )


def analyst_estimates(symbol: str) -> dict:
    data = yahoo_quote_summary(
        symbol,
        ["earningsTrend", "recommendationTrend", "upgradeDowngradeHistory"],
    )
    et = data.get("earningsTrend", {}).get("trend", [])
    eps_trend = []
    for t in et:
        eps_trend.append(
            {
                "period": t.get("period"),
                "end_date": t.get("endDate"),
                "eps_estimate": t.get("earningsEstimate", {}).get("avg", {}).get("raw"),
                "revenue_estimate": t.get("revenueEstimate", {}).get("avg", {}).get("raw"),
                "num_analysts": t.get("earningsEstimate", {}).get("numberOfAnalysts", {}).get("raw"),
            }
        )
    rt = data.get("recommendationTrend", {}).get("trend", [])
    rating_trend = [
        {
            "period": r_.get("period"),
            "strong_buy": r_.get("strongBuy"),
            "buy": r_.get("buy"),
            "hold": r_.get("hold"),
            "sell": r_.get("sell"),
            "strong_sell": r_.get("strongSell"),
        }
        for r_ in rt
    ]
    udh = data.get("upgradeDowngradeHistory", {}).get("history", [])[:15]
    upgrades = [
        {
            "date": u.get("epochGradeDate"),
            "firm": u.get("firm"),
            "to_grade": u.get("toGrade"),
            "from_grade": u.get("fromGrade"),
            "action": u.get("action"),
        }
        for u in udh
    ]
    return {"eps_trend": eps_trend, "rating_trend": rating_trend, "upgrade_downgrade": upgrades}


def stock_news(keyword: str, count: int = 8) -> list[dict]:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    try:
        s.get("https://fc.yahoo.com", timeout=10)
        r = s.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": keyword, "quotesCount": 0, "newsCount": count},
            timeout=10,
        )
        r.raise_for_status()
        news = r.json().get("news", [])
        return [
            {
                "title": n.get("title"),
                "publisher": n.get("publisher"),
                "link": n.get("link"),
                "publish_time": n.get("providerPublishTime"),
            }
            for n in news
        ]
    except Exception:
        return []


def ticker_to_cik(ticker: str) -> dict:
    global _cik_cache
    if not _cik_cache:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        _cik_cache = r.json()
    ticker_upper = ticker.upper()
    for _, v in _cik_cache.items():
        if v.get("ticker") == ticker_upper:
            return {
                "ticker": ticker_upper,
                "cik": str(v["cik_str"]).zfill(10),
                "company": v.get("title"),
            }
    return {}


# ── 期权 ──────────────────────────────────────────────────────────


def options_chain(symbol: str, expiration: int | None = None) -> dict:
    s = get_yahoo_session()
    params: dict[str, Any] = {"crumb": s._crumb}  # type: ignore[attr-defined]
    if expiration:
        params["date"] = expiration
    r = s.get(f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}", params=params, timeout=20)
    r.raise_for_status()
    oc = r.json().get("optionChain", {}).get("result", [{}])[0]
    exp_dates = oc.get("expirationDates", [])
    options = oc.get("options", [{}])[0] if oc.get("options") else {}

    def _parse(opts):
        rows = []
        for o in opts:
            def v(key):
                x = o.get(key, {})
                return x.get("raw") if isinstance(x, dict) else x

            rows.append(
                {
                    "strike": v("strike"),
                    "last_price": v("lastPrice"),
                    "bid": v("bid"),
                    "ask": v("ask"),
                    "volume": v("volume"),
                    "open_interest": v("openInterest"),
                    "implied_volatility": v("impliedVolatility"),
                    "in_the_money": o.get("inTheMoney"),
                    "contract_symbol": o.get("contractSymbol"),
                }
            )
        return rows

    return {
        "expiration_dates": exp_dates,
        "calls": _parse(options.get("calls", [])),
        "puts": _parse(options.get("puts", [])),
        "underlying_price": oc.get("quote", {}).get("regularMarketPrice"),
    }


# ── 工具函数 ──────────────────────────────────────────────────────


def percentile_rank(series: list[float], value: float) -> float | None:
    clean = [x for x in series if x is not None and not math.isnan(x)]
    if not clean:
        return None
    below = sum(1 for x in clean if x <= value)
    return round(below / len(clean) * 100, 2)


def pct_of_52w(price: float, high: float, low: float) -> float | None:
    if not high or not low or high == low:
        return None
    return round((price - low) / (high - low) * 100, 2)


def now_iso() -> str:
    """当前时间，固定为 UTC+8 墙钟（不带后缀）。"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def format_cst(value: Any) -> str | None:
    """把 datetime / ISO 字符串转成 UTC+8 墙钟显示；无法解析则原样字符串化。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            # 兼容 2026-07-15T10:28:46.000+09:00 / 带 Z / 空格分隔
            text_norm = text.replace("Z", "+00:00")
            if " " in text_norm and "T" not in text_norm:
                text_norm = text_norm.replace(" ", "T", 1)
            # 已是墙钟且无时区标记时，去掉可能残留的 UTC+8 文案再解析
            text_norm = text_norm.replace(" UTC+8", "").replace("UTC+8", "").strip()
            dt = datetime.fromisoformat(text_norm)
        except ValueError:
            return text.replace(" UTC+8", "").strip()
    if dt.tzinfo is None:
        # 无时区：假定已是 UTC+8 墙钟
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
