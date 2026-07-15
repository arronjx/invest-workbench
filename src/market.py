"""大盘估值分位：VIX / SPX / NDX + ERP 粗算 + 恐惧贪婪（尽力获取）。"""

from __future__ import annotations

import math
from typing import Any

import requests

from . import data as D

UA = D.UA


def _klose(symbol: str, range_: str = "10y") -> list[float]:
    kl = D.stock_kline_yahoo(symbol, "1d", range_)
    return [k["close"] for k in kl]


def _ma(series: list[float], n: int) -> float | None:
    if len(series) < n:
        return None
    return sum(series[-n:]) / n


def _trend_deviation_percentile(closes: list[float], lookback_years: int = 5) -> dict:
    """当前价相对长期均线的偏离，在历史偏离中的分位。"""
    if len(closes) < 120:
        return {"error": "K线不足"}
    days = min(len(closes), lookback_years * 252)
    window = closes[-days:]
    ma200 = _ma(window, min(200, len(window)))
    if not ma200:
        return {"error": "均线不足"}
    deviations = []
    for i in range(200, len(window)):
        ma = sum(window[i - 200 : i]) / 200
        if ma:
            deviations.append((window[i] / ma - 1) * 100)
    cur_dev = (window[-1] / ma200 - 1) * 100
    pct = D.percentile_rank(deviations, cur_dev) if deviations else None
    return {
        "price": round(window[-1], 2),
        "ma200": round(ma200, 2),
        "deviation_pct": round(cur_dev, 2),
        "percentile": pct,
        "lookback_years": lookback_years,
    }


def _treasury_yield_10y() -> float | None:
    """Yahoo ^TNX 为百分点报价。"""
    try:
        kl = D.stock_kline_yahoo("^TNX", "1d", "5d")
        if kl:
            return kl[-1]["close"]
    except Exception:
        pass
    return None


def _spx_pe_approx() -> dict:
    """
    粗估：用 SPY 的 trailing PE 作大盘代理（非官方标普 PE）。
    标注来源，避免假装精确。
    """
    try:
        s = D.key_statistics("SPY")
        pe = s.get("trailing_pe") or s.get("forward_pe")
        return {"pe": pe, "source": "Yahoo SPY trailing/forward PE（标普代理）"}
    except Exception as e:
        return {"pe": None, "source": f"未获取到 ({e})"}


def _fear_greed() -> dict:
    """CNN Fear & Greed — 接口可能变动，失败则明确未获取到。"""
    urls = [
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        "https://api.alternative.me/fng/?limit=1",
    ]
    # alternative.me crypto FNG 不是股票，但可作情绪备选并标注
    try:
        r = requests.get(
            urls[0],
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            score = d.get("fear_and_greed", {}).get("score")
            rating = d.get("fear_and_greed", {}).get("rating")
            if score is not None:
                return {"score": round(float(score), 1), "rating": rating, "source": "CNN Fear & Greed"}
    except Exception:
        pass
    try:
        r = requests.get(urls[1], timeout=10)
        d = r.json()
        row = d.get("data", [{}])[0]
        return {
            "score": float(row.get("value")),
            "rating": row.get("value_classification"),
            "source": "alternative.me（加密恐惧贪婪，仅作情绪弱代理）",
        }
    except Exception:
        return {"score": None, "rating": None, "source": "未获取到"}


def _score_piece(percentile: float | None, invert: bool = False) -> float | None:
    """分位 → 0-100 买点分贡献。invert=True 表示越高越贵（如偏离高位）。"""
    if percentile is None:
        return None
    return 100 - percentile if invert else percentile


def build(lookback_years: int = 5) -> dict[str, Any]:
    ts = D.now_iso()
    out: dict[str, Any] = {"fetched_at": ts, "lookback_years": lookback_years}

    # 位置层
    layers = {}
    for key, symbol in (("vix", "^VIX"), ("spx", "^GSPC"), ("ndx", "^NDX")):
        try:
            closes = _klose(symbol, "max" if lookback_years >= 10 else "10y")
            layers[key] = _trend_deviation_percentile(closes, lookback_years)
            # VIX：用绝对值分位更直观
            if key == "vix" and closes:
                window = closes[-lookback_years * 252 :]
                layers[key]["abs_percentile"] = D.percentile_rank(window, window[-1])
                layers[key]["price"] = round(window[-1], 2)
        except Exception as e:
            layers[key] = {"error": str(e)}
    out["position"] = layers

    # 估值层
    pe_info = _spx_pe_approx()
    tnx = _treasury_yield_10y()
    pe = pe_info.get("pe")
    earnings_yield = (100 / pe) if pe else None
    erp = (earnings_yield - tnx) if earnings_yield is not None and tnx is not None else None
    out["valuation"] = {
        "spx_pe_proxy": pe,
        "pe_source": pe_info.get("source"),
        "earnings_yield_pct": round(earnings_yield, 2) if earnings_yield else None,
        "ust10y_pct": tnx,
        "erp_pct": round(erp, 2) if erp is not None else None,
        "note": "ERP = 盈利收益率 − 10Y 美债；PE 用 SPY 代理，非官方 Shiller/标普营运 PE。",
    }

    # 情绪层
    out["sentiment"] = _fear_greed()

    # 合成买点分（透明权重）
    weights = {
        "spx_cheap": 0.30,  # SPX 偏离分位越低越好买 → invert
        "ndx_cheap": 0.15,
        "vix_high": 0.20,  # VIX 绝对分位越高越好买（恐惧）
        "erp": 0.20,
        "fear_greed": 0.15,  # F&G 越低越好买
    }
    pieces = {}
    spx_p = layers.get("spx", {}).get("percentile")
    ndx_p = layers.get("ndx", {}).get("percentile")
    vix_p = layers.get("vix", {}).get("abs_percentile")
    pieces["spx_cheap"] = _score_piece(spx_p, invert=True)
    pieces["ndx_cheap"] = _score_piece(ndx_p, invert=True)
    pieces["vix_high"] = vix_p

    # ERP：<0 差，>4 较好，粗映射到 0-100
    if erp is None:
        pieces["erp"] = None
    else:
        pieces["erp"] = max(0, min(100, (erp + 1) / 6 * 100))

    fg = out["sentiment"].get("score")
    pieces["fear_greed"] = (100 - fg) if fg is not None else None

    avail = {k: v for k, v in pieces.items() if v is not None}
    if avail:
        wsum = sum(weights[k] for k in avail)
        score = sum(avail[k] * weights[k] for k in avail) / wsum
    else:
        score = None

    if score is None:
        stance = "数据不足"
    elif score >= 65:
        stance = "可以更贪婪（历史坐标偏便宜/偏恐惧）"
    elif score <= 35:
        stance = "应当更恐惧（历史坐标偏贵/偏贪婪）"
    else:
        stance = "中性：不预测涨跌，只提示位置居中"

    out["score"] = {
        "value": round(score, 1) if score is not None else None,
        "stance": stance,
        "pieces": {k: (round(v, 1) if v is not None else None) for k, v in pieces.items()},
        "weights": weights,
        "rule": (
            "买点分 = 加权平均："
            "SPX偏离分位越低越好买(30%)；NDX同理(15%)；"
            "VIX绝对分位越高越好买(20%)；ERP越高越好买(20%)；"
            "恐惧贪婪越低越好买(15%)。缺失项自动重新归一。"
        ),
    }
    return out
