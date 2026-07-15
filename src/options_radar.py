"""期权机会雷达（美股 Yahoo 期权链）。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from . import data as D


def _mid(bid, ask, last) -> float | None:
    try:
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2
        if last:
            return float(last)
        if ask:
            return float(ask)
        if bid:
            return float(bid)
    except Exception:
        return None
    return None


def _iv_percentile_proxy(chain_ivs: list[float], cur: float | None) -> float | None:
    """单日链上 IV 截面分位（弱代理，非历史 IV Rank）。"""
    if cur is None or not chain_ivs:
        return None
    return D.percentile_rank(chain_ivs, cur)


def _pick_put(puts: list[dict], spot: float, discount: float = 0.85) -> dict | None:
    target = spot * discount
    otm = [p for p in puts if p.get("strike") and p["strike"] <= target]
    if not otm:
        return None
    # 最接近目标行权价
    return min(otm, key=lambda p: abs(p["strike"] - target))


def _pick_call(calls: list[dict], spot: float, premium_pct: float = 1.05) -> dict | None:
    target = spot * premium_pct
    otm = [c for c in calls if c.get("strike") and c["strike"] >= target]
    if not otm:
        return None
    return min(otm, key=lambda c: abs(c["strike"] - target))


def _annualize(premium: float, spot: float, days: int) -> float | None:
    if not premium or not spot or days <= 0:
        return None
    return round((premium / spot) * (365 / days) * 100, 2)


def _approx_delta_prob(strike: float, spot: float, iv: float | None, days: int, is_put: bool) -> float | None:
    """极简近似：用距离/IV 估盈利概率（非 Black-Scholes 精确解）。"""
    if not spot or not days:
        return None
    sigma = iv if iv and iv > 0 else 0.3
    t = days / 365
    if t <= 0:
        return None
    moneyness = math.log(strike / spot) / (sigma * math.sqrt(t) + 1e-9)
    # 经验映射到 0-100
    if is_put:
        # 更深 OTM put → 卖方盈利概率更高
        score = 50 + moneyness * (-25)
    else:
        score = 50 + moneyness * 25
    return round(max(5, min(95, score)), 1)


def analyze_symbol(ticker: str, owned_shorts: set[str] | None = None) -> dict[str, Any]:
    owned_shorts = owned_shorts or set()
    meta_q = D.get_quote(ticker)
    meta, quote = meta_q["meta"], meta_q["quote"]
    if meta["market"] == "HK" or meta.get("secid_prefix") == 116:
        return {
            "symbol": ticker,
            "error": "港股期权不在 Yahoo 覆盖范围，跳过",
            "meta": meta,
        }

    yahoo = meta["yahoo"]
    try:
        chain = D.options_chain(yahoo)
    except Exception as e:
        return {"symbol": yahoo, "error": str(e), "meta": meta}

    spot = chain.get("underlying_price") or quote.get("price")
    exps = chain.get("expiration_dates") or []
    if not spot or not exps:
        return {"symbol": yahoo, "error": "无期权数据", "meta": meta, "quote": quote}

    now = datetime.now(timezone.utc).timestamp()
    # 短期 <45d，远期 >365d
    near = [e for e in exps if 7 * 86400 < (e - now) < 45 * 86400]
    far = [e for e in exps if (e - now) >= 365 * 86400]
    near_exp = near[0] if near else (exps[0] if exps else None)
    far_exp = far[0] if far else (exps[-1] if exps else None)

    def load_exp(exp):
        if exp is None:
            return None, [], []
        if exp == exps[0] or (chain.get("options") is not None and exp == exps[0]):
            # 第一次调用已带最近到期；若不是目标则重拉
            if exp == (chain.get("expiration_dates") or [None])[0]:
                return exp, chain.get("calls", []), chain.get("puts", [])
        c2 = D.options_chain(yahoo, exp)
        return exp, c2.get("calls", []), c2.get("puts", [])

    # 最近到期链用于 IV 截面
    all_ivs = [
        x["implied_volatility"]
        for x in (chain.get("calls", []) + chain.get("puts", []))
        if x.get("implied_volatility")
    ]
    atm_iv = None
    for c in chain.get("calls", []):
        if c.get("strike") and abs(c["strike"] - spot) / spot < 0.03:
            atm_iv = c.get("implied_volatility")
            break
    if atm_iv is None and all_ivs:
        atm_iv = sorted(all_ivs)[len(all_ivs) // 2]

    iv_pct = _iv_percentile_proxy(all_ivs, atm_iv)

    # 远期 sell put
    far_e, far_calls, far_puts = load_exp(far_exp)
    put = _pick_put(far_puts, spot, 0.85) if far_puts else None
    far_days = int((far_e - now) / 86400) if far_e else 0
    put_mid = _mid(put.get("bid"), put.get("ask"), put.get("last_price")) if put else None
    put_row = None
    if put and put_mid:
        put_row = {
            "expiry": datetime.fromtimestamp(far_e, tz=timezone.utc).strftime("%Y-%m-%d") if far_e else None,
            "days": far_days,
            "strike": put["strike"],
            "premium": round(put_mid, 2),
            "iv": put.get("implied_volatility"),
            "annualized_pct": _annualize(put_mid, spot, far_days),
            "approx_win_prob": _approx_delta_prob(put["strike"], spot, put.get("implied_volatility"), far_days, True),
            "contract": put.get("contract_symbol"),
        }

    # 短期 covered call
    near_e, near_calls, near_puts = load_exp(near_exp)
    call = _pick_call(near_calls, spot, 1.05) if near_calls else None
    near_days = int((near_e - now) / 86400) if near_e else 0
    call_mid = _mid(call.get("bid"), call.get("ask"), call.get("last_price")) if call else None
    call_row = None
    if call and call_mid:
        call_row = {
            "expiry": datetime.fromtimestamp(near_e, tz=timezone.utc).strftime("%Y-%m-%d") if near_e else None,
            "days": near_days,
            "strike": call["strike"],
            "premium": round(call_mid, 2),
            "iv": call.get("implied_volatility"),
            "annualized_pct": _annualize(call_mid, spot, near_days),
            "approx_win_prob": _approx_delta_prob(call["strike"], spot, call.get("implied_volatility"), near_days, False),
            "contract": call.get("contract_symbol"),
        }

    high_52 = quote.get("high_52w") or 0
    low_52 = quote.get("low_52w") or 0

    return {
        "symbol": yahoo,
        "name": meta.get("name"),
        "spot": spot,
        "pos_52w": D.pct_of_52w(spot, high_52, low_52),
        "iv": round(atm_iv * 100, 2) if atm_iv and atm_iv < 5 else (round(atm_iv, 2) if atm_iv else None),
        "iv_percentile_proxy": iv_pct,
        "iv_note": "IV 分位为当日链上截面弱代理，非历史 IV Rank",
        "highlight": (iv_pct or 0) >= 70,
        "sell_put_far": put_row,
        "covered_call_near": call_row,
        "already_short": yahoo.upper() in {x.upper() for x in owned_shorts},
        "quote": quote,
        "fetched_at": D.now_iso(),
    }


def build_radar(tickers: list[str], owned_shorts: list[str] | None = None) -> dict:
    owned = set(owned_shorts or [])
    rows = []
    for t in tickers:
        t = t.strip()
        if not t:
            continue
        rows.append(analyze_symbol(t, owned))
    # 按远期 put 年化排序
    def key(r):
        sp = r.get("sell_put_far") or {}
        return sp.get("annualized_pct") or -1

    rows_sorted = sorted([r for r in rows if not r.get("error")], key=key, reverse=True)
    errors = [r for r in rows if r.get("error")]
    return {
        "fetched_at": D.now_iso(),
        "rows": rows_sorted + errors,
        "rule": "IV 分位≥70% 高亮；远期 Sell Put 按 85 折行权价估权利金年化；短线备兑 Call 取约 105% 行权价。",
    }
