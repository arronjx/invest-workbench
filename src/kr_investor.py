"""KOR 投资者买卖动向（Toss WTS trading-trend）。"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import requests

from src import data as D
from src import kr_calendar

TOSS_TREND_URL = (
    "https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tossinvest.com",
    "Referer": "https://www.tossinvest.com/",
}

KST = timezone(timedelta(hours=9))
# 韩国交易所现金盘：09:00–15:30 KST（= UTC+8 的 08:00–14:30）
KR_OPEN = time(9, 0)
KR_CLOSE = time(15, 30)
BASEDATE_STALE_GRACE_MIN = 20
BASEDATE_STALE_CONFIRM_TICKS = 3
BASEDATE_STALE_MIN_SAMPLES = 2  # 至少两只有 baseDate 才可推断休市
PAUSED_PROBE_INTERVAL_SEC = 300  # 推断休市后低频探活，便于误判自愈

_inferred_holiday_date: str | None = None
_stale_basedate_date: str | None = None
_stale_basedate_ticks = 0

WATCHLIST = [
    {"code": "000660", "product_code": "A000660", "name": "SK海力士", "name_en": "SK Hynix"},
    {"code": "005930", "product_code": "A005930", "name": "三星电子", "name_en": "Samsung Electronics"},
]

# 列：与截图一致的主体 + 机构细分
COLUMNS = [
    ("foreigner", "外资", "foreigner"),
    ("individual", "个人", "individuals"),
    ("institution", "机构合计", "institution"),
    ("financial_investment", "金融投资", "financialInvestment"),
    ("insurance", "保险", "insurance"),
    ("trust", "投信", "trust"),
    ("pension", "年金等", "pensionFund"),
    ("private_equity", "私募基金", "privateEquityFund"),
]


def normalize_product_code(code: str) -> str:
    c = (code or "").strip().upper()
    if not c:
        raise ValueError("code required")
    if c.startswith("A") and len(c) == 7:
        return c
    digits = "".join(ch for ch in c if ch.isdigit())
    if len(digits) == 6:
        return f"A{digits}"
    return c if c.startswith("A") else f"A{c}"


def is_kr_cash_session(now: datetime | None = None) -> bool:
    """是否在 KOR 现金盘：工作日 + 09:00–15:30 KST，且非已知休市日。"""
    dt = now.astimezone(KST) if now else datetime.now(KST)
    if dt.weekday() >= 5:
        return False
    if kr_calendar.is_kr_holiday(dt):
        return False
    t = dt.time()
    return KR_OPEN <= t <= KR_CLOSE


def is_collection_paused_today(now: datetime | None = None) -> bool:
    """日历漏休市时，collector 只暂停当天，次日自动恢复。"""
    dt = now.astimezone(KST) if now else datetime.now(KST)
    return _inferred_holiday_date == dt.date().isoformat()


def _clear_inferred_holiday_state() -> None:
    global _inferred_holiday_date, _stale_basedate_date, _stale_basedate_ticks
    _inferred_holiday_date = None
    _stale_basedate_date = None
    _stale_basedate_ticks = 0


def restore_inferred_holiday_from_snapshot() -> bool:
    """进程重启后，从当日快照恢复推断休市暂停状态。"""
    global _inferred_holiday_date
    from src import kr_intraday_db as IDB

    snap = IDB.load_dashboard_snapshot()
    if not snap:
        return False
    today = datetime.now(KST).date().isoformat()
    if snap.get("kr_holiday_inferred") and snap.get("kr_holiday_inferred_date") == today:
        _inferred_holiday_date = today
        return True
    return False


def _opening_grace_elapsed(now: datetime) -> bool:
    dt = now.astimezone(KST)
    open_dt = dt.replace(hour=KR_OPEN.hour, minute=KR_OPEN.minute, second=0, microsecond=0)
    return dt >= open_dt + timedelta(minutes=BASEDATE_STALE_GRACE_MIN)


def _latest_base_date(stock: dict[str, Any]) -> str | None:
    latest = stock.get("latest") or {}
    today = (stock.get("day_scores") or [{}])[0]
    raw = latest.get("base_date") or today.get("base_date")
    return str(raw)[:10] if raw else None


def _update_inferred_holiday(
    stocks: list[dict[str, Any]],
    *,
    now: datetime,
    session_open: bool,
) -> dict[str, Any]:
    """开盘后一段时间仍全是旧 baseDate，则推断当天休市并暂停采集。

    仅用成功解析到 baseDate 的标的；若之后出现今日 baseDate 则清除暂停（自愈）。
    """
    global _inferred_holiday_date, _stale_basedate_date, _stale_basedate_ticks

    today = now.astimezone(KST).date().isoformat()
    base_dates = [d for d in (_latest_base_date(s) for s in stocks) if d]
    meta = {
        "base_dates": sorted(set(base_dates)),
        "stale_basedate_confirm_ticks": BASEDATE_STALE_CONFIRM_TICKS,
        "stale_basedate_grace_min": BASEDATE_STALE_GRACE_MIN,
        "stale_basedate_min_samples": BASEDATE_STALE_MIN_SAMPLES,
    }

    # 自愈：任一成功标的已切到今日 → 立即恢复采集
    if any(d == today for d in base_dates):
        was_paused = _inferred_holiday_date == today
        _clear_inferred_holiday_state()
        return {
            "kr_holiday_inferred": False,
            "kr_holiday_inferred_date": None,
            "stale_basedate_ticks": 0,
            "inferred_holiday_cleared": was_paused,
            **meta,
        }

    if _inferred_holiday_date == today:
        return {
            "kr_holiday_inferred": True,
            "kr_holiday_inferred_date": today,
            "market_closed_reason": "Toss baseDate 未切到今日，已暂停今日采集",
            "stale_basedate_ticks": _stale_basedate_ticks,
            **meta,
        }

    stale_today = (
        session_open
        and _opening_grace_elapsed(now)
        and len(base_dates) >= BASEDATE_STALE_MIN_SAMPLES
        and all(d != today for d in base_dates)
    )
    if not stale_today:
        if session_open and _opening_grace_elapsed(now):
            _stale_basedate_date = None
            _stale_basedate_ticks = 0
        return {
            "kr_holiday_inferred": False,
            "kr_holiday_inferred_date": None,
            "stale_basedate_ticks": _stale_basedate_ticks,
            **meta,
        }

    if _stale_basedate_date != today:
        _stale_basedate_date = today
        _stale_basedate_ticks = 0
    _stale_basedate_ticks += 1
    inferred = _stale_basedate_ticks >= BASEDATE_STALE_CONFIRM_TICKS
    if inferred:
        _inferred_holiday_date = today

    return {
        "kr_holiday_inferred": inferred,
        "kr_holiday_inferred_date": today if inferred else None,
        "market_closed_reason": (
            "Toss baseDate 未切到今日，已暂停今日采集"
            if inferred
            else "Toss baseDate 仍非今日，等待连续确认"
        ),
        "stale_basedate_ticks": _stale_basedate_ticks,
        **meta,
    }


def snapshot_freshness(
    sampled_at: str | None,
    *,
    session_open: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """快照新旧：盘中超过约 3 个采集周期未更新视为陈旧。"""
    now_cst = (now or datetime.now(KST)).astimezone(timezone(timedelta(hours=8)))
    age_sec: int | None = None
    if sampled_at:
        try:
            ts = datetime.strptime(str(sampled_at).strip()[:19], "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
            age_sec = max(0, int((now_cst - ts).total_seconds()))
        except ValueError:
            age_sec = None
    # 盘中 20s×3；非盘中不因「不采集」误报陈旧，仅超 48h 提示
    threshold = 60 if session_open else 48 * 3600
    stale = bool(age_sec is not None and age_sec > threshold)
    return {
        "data_age_sec": age_sec,
        "stale": stale,
        "stale_threshold_sec": threshold,
    }

def _num(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_NET_KEYS = {
    "individuals": "netIndividualsBuyVolume",
    "foreigner": "netForeignerBuyVolume",
    "institution": "netInstitutionBuyVolume",
    "financialInvestment": "netFinancialInvestmentBuyVolume",
    "insurance": "netInsuranceBuyVolume",
    "trust": "netTrustBuyVolume",
    "pensionFund": "netPensionFundBuyVolume",
    "privateEquityFund": "netPrivateEquityFundBuyVolume",
}


def _pair(row: dict[str, Any], prefix: str) -> dict[str, int | None]:
    """由 buy / net 推导 sell；优先用显式 sell 字段。"""
    buy = _num(row.get(f"{prefix}BuyVolume"))
    net = _num(row.get(_NET_KEYS.get(prefix, "")))
    sell = _num(row.get(f"{prefix}SellVolume"))
    if sell is None and buy is not None and net is not None:
        sell = buy - net
    return {"buy": buy, "sell": sell, "net": net}


def _trade_sum(cell: dict[str, int | None]) -> float:
    b, s = cell.get("buy"), cell.get("sell")
    if b is None and s is None:
        return 0.0
    return float((b or 0) + (s or 0))


def _format_table(row: dict[str, Any]) -> dict[str, Any]:
    cells: dict[str, dict[str, Any]] = {}
    for key, label, prefix in COLUMNS:
        cells[key] = {"label": label, **_pair(row, prefix), "estimated": False}

    has_individual = bool(row.get("hasIndividual"))
    individual_estimated = False
    # 盘中缺个人时：个人净买 ≈ −(外资净买 + 机构净买)，并打估算标记
    if not has_individual:
        f_net = cells["foreigner"].get("net")
        i_net = cells["institution"].get("net")
        if f_net is not None and i_net is not None:
            cells["individual"]["net"] = -(f_net + i_net)
            cells["individual"]["buy"] = None
            cells["individual"]["sell"] = None
            cells["individual"]["estimated"] = True
            individual_estimated = True

    # 成交占比分母：外资+个人+机构合计 双向成交量之和（估个人无买卖量，不进分母）
    denom = sum(_trade_sum(cells[k]) for k in ("foreigner", "individual", "institution"))
    for key, _, _ in COLUMNS:
        ts = _trade_sum(cells[key])
        cells[key]["volume_pct"] = round(ts / denom * 100, 2) if denom > 0 else None

    base = _num(row.get("base"))
    close = _num(row.get("close"))
    change = None
    change_pct = None
    if base is not None and close is not None and base:
        change = close - base
        change_pct = round(change / base * 100, 2)

    return {
        "base_date": row.get("baseDate"),
        "updated_at": D.format_cst(row.get("updatedAt")),
        "in_market_time": bool(row.get("inMarketTime")),
        "has_individual": has_individual,
        "individual_estimated": individual_estimated,
        "has_foreigner": bool(row.get("hasForeigner")),
        "has_institution": bool(row.get("hasInstitution")),
        "price": {
            "base": base,
            "close": close,
            "change": change,
            "change_pct": change_pct,
        },
        "columns": [
            {"key": k, "label": lab, **cells[k]}
            for k, lab, _ in COLUMNS
        ],
    }


def fetch_trading_trend(product_code: str, size: int = 1) -> dict[str, Any]:
    code = normalize_product_code(product_code)
    size = max(1, min(int(size), 30))
    r = requests.get(
        TOSS_TREND_URL,
        params={"productCode": code, "size": size},
        headers=HEADERS,
        timeout=12,
    )
    r.raise_for_status()
    payload = r.json()
    result = payload.get("result") or {}
    body = result.get("body") or []
    if not isinstance(body, list) or not body:
        raise RuntimeError(f"empty trading-trend body for {code}")
    tables = [_format_table(x) for x in body]
    return {
        "product_code": code,
        "latest": tables[0],
        "history": tables[1:],
        "days": tables,
    }


def _col_net(table: dict[str, Any], key: str) -> int | None:
    for c in table.get("columns") or []:
        if c.get("key") == key:
            return _num(c.get("net"))
    return None


def _col_cell(table: dict[str, Any], key: str) -> dict[str, Any]:
    for c in table.get("columns") or []:
        if c.get("key") == key:
            return c
    return {}


# ── 滤网常量（非 alpha；可手调）────────────────────────────────
INTENSITY_PASS = 0.02  # 聪明钱净买 / 双边成交 ≥ 2% → 可通过候选
INTENSITY_WATCH = 0.005  # ≥ 0.5% → 最多观察；更低视为噪声
CHASE_PCT = 5.0  # 当日涨幅 ≥ 5% → 最高观察
ORGAN_PULSE_RATIO = 0.70  # 金融投资占机构净买 ≥ 70% 且投信+年金净卖 → 短脉冲


def _retail_role(
    retail_net: int | None,
    resonance: bool,
    retail_estimated: bool,
) -> str:
    """个人仅作诊断，不计分。"""
    if retail_estimated:
        return "estimated"
    if retail_net is None:
        return "unknown"
    if resonance and retail_net < 0:
        return "confirming"
    if retail_net > 0:
        return "contradicting"
    return "neutral"


def _organ_quality(table: dict[str, Any], organ_net: int | None) -> dict[str, Any]:
    """机构质量：自营主导短脉冲 vs 年金/投信同向或中性。"""
    fin = _col_net(table, "financial_investment") or 0
    trust = _col_net(table, "trust") or 0
    pension = _col_net(table, "pension") or 0
    longish = trust + pension
    pulse_ratio = None
    label = "unknown"
    if organ_net is not None and organ_net > 0:
        pulse_ratio = round(fin / organ_net, 3) if organ_net else None
        if pulse_ratio is not None and pulse_ratio >= ORGAN_PULSE_RATIO and longish < 0:
            label = "short_horizon"
        else:
            label = "balanced"
    elif organ_net is not None and organ_net <= 0:
        label = "not_buying"

    return {
        "label": label,
        "financial_net": fin,
        "trust_pension_net": longish,
        "pulse_ratio": pulse_ratio,
    }


def _traded_denominator(table: dict[str, Any]) -> float:
    """双边成交分母：外+机；个人非估算时再加个人。"""
    f = _col_cell(table, "foreigner")
    i = _col_cell(table, "institution")
    r = _col_cell(table, "individual")
    traded = _trade_sum(f) + _trade_sum(i)
    if not r.get("estimated"):
        traded += _trade_sum(r)
    return float(traded)


def score_day(table: dict[str, Any]) -> dict[str, Any]:
    """单日供需诊断（个人不算分）。"""
    foreign_net = _col_net(table, "foreigner")
    organ_net = _col_net(table, "institution")
    retail_net = _col_net(table, "individual")
    has_individual = bool(table.get("has_individual"))
    retail_estimated = bool(table.get("individual_estimated"))

    foreign_ok = foreign_net is not None and foreign_net > 0
    organ_ok = organ_net is not None and organ_net > 0
    resonance = foreign_ok and organ_ok

    price = table.get("price") or {}
    close = price.get("close")
    base = price.get("base")
    change_pct = price.get("change_pct")
    price_ok = None
    if close is not None and base is not None:
        price_ok = close >= base
    chase_risk = isinstance(change_pct, (int, float)) and change_pct >= CHASE_PCT

    f = foreign_net or 0
    o = organ_net or 0
    smart_sum = f + o if (foreign_net is not None or organ_net is not None) else None
    traded = _traded_denominator(table)
    intensity = None
    if smart_sum is not None and traded > 0:
        intensity = smart_sum / traded

    intensity_tier = "noise"
    if intensity is not None:
        if intensity >= INTENSITY_PASS:
            intensity_tier = "pass"
        elif intensity >= INTENSITY_WATCH:
            intensity_tier = "watch"
        elif intensity > 0:
            intensity_tier = "noise"
        else:
            intensity_tier = "negative"

    organ_q = _organ_quality(table, organ_net)
    retail_role = _retail_role(retail_net, resonance, retail_estimated)

    # 历史摘要标签（面板用）
    if not resonance:
        if foreign_ok or organ_ok:
            diag_tag = "分裂"
        else:
            diag_tag = "双卖" if (f < 0 and o < 0) else "无共振"
    elif intensity_tier == "pass":
        diag_tag = "共振·强量"
    elif intensity_tier == "watch":
        diag_tag = "共振·弱量"
    elif intensity_tier == "noise":
        diag_tag = "共振·噪声"
    else:
        diag_tag = "共振·净卖"

    return {
        "base_date": table.get("base_date"),
        "foreign_net": foreign_net,
        "organ_net": organ_net,
        "retail_net": retail_net,
        "retail_estimated": retail_estimated,
        "retail_role": retail_role,
        "smart_sum": smart_sum,
        "traded": int(traded) if traded else 0,
        "intensity": round(intensity, 4) if intensity is not None else None,
        "intensity_tier": intensity_tier,
        "resonance": resonance,
        "organ_quality": organ_q["label"],
        "organ_quality_detail": organ_q,
        "diag_tag": diag_tag,
        "chase_risk": chase_risk,
        "checks": {
            "foreign_buy": foreign_ok,
            "organ_buy": organ_ok,
            "resonance": resonance,
            "price_above_prev": price_ok,
            "chase_risk": chase_risk,
        },
        "has_individual": has_individual,
        "price": price,
        "in_market_time": table.get("in_market_time"),
    }


def recommend_from_scores(day_scores: list[dict[str, Any]]) -> dict[str, Any]:
    """
    四层门控滤网（非交易系统）：
    共振 → 量级 → 价格 → 连续 → 机构质量
    level: pass / watch / fail / weaken / none
    """
    empty_filters = {
        "resonance": False,
        "intensity": None,
        "intensity_ok": False,
        "intensity_tier": None,
        "price_ok": None,
        "chase_risk": False,
        "continuity_ok": False,
        "organ_quality": "unknown",
        "retail_role": "unknown",
        "resonance_streak": 0,
        "weak_streak": 0,
    }
    if not day_scores:
        return {
            "action": "无数据",
            "level": "none",
            "reason": "无买卖动向数据",
            "filters": empty_filters,
        }

    today = day_scores[0]
    resonance = bool(today.get("resonance"))
    intensity = today.get("intensity")
    intensity_tier = today.get("intensity_tier") or "noise"
    price_ok = today["checks"].get("price_above_prev")
    chase_risk = bool(today.get("chase_risk"))
    organ_quality = today.get("organ_quality") or "unknown"
    retail_role = today.get("retail_role") or "unknown"
    retail_est = bool(today.get("retail_estimated"))

    resonance_streak = 0
    for d in day_scores:
        if d.get("resonance"):
            resonance_streak += 1
        else:
            break

    weak_streak = 0
    for d in day_scores:
        smart = d.get("smart_sum")
        no_res = not d.get("resonance")
        neg_smart = isinstance(smart, (int, float)) and smart < 0
        if no_res or neg_smart:
            weak_streak += 1
        else:
            break

    continuity_ok = resonance_streak >= 2
    intensity_ok = intensity_tier == "pass"
    intensity_watchable = intensity_tier in ("pass", "watch")

    filters = {
        "resonance": resonance,
        "intensity": intensity,
        "intensity_ok": intensity_ok,
        "intensity_tier": intensity_tier,
        "price_ok": price_ok,
        "chase_risk": chase_risk,
        "continuity_ok": continuity_ok,
        "organ_quality": organ_quality,
        "retail_role": retail_role,
        "resonance_streak": resonance_streak,
        "weak_streak": weak_streak,
        "smart_sum": today.get("smart_sum"),
        "traded": today.get("traded"),
    }

    def _note_retail(msg: str) -> str:
        if retail_est:
            return msg + "（个人为残差估算，仅诊断）"
        if retail_role == "contradicting":
            return msg + "（个人净买，与聪明钱逆向）"
        return msg

    inten_pct = f"{intensity * 100:.2f}%" if intensity is not None else "—"

    # 转弱：近 2 日均无共振，或聪明钱合计连续 2 日为负
    if weak_streak >= 2 and not resonance:
        return {
            "action": "转弱",
            "level": "weaken",
            "reason": _note_retail(
                f"近 {weak_streak} 日无外机共振或聪明钱净卖，供需转弱"
            ),
            "filters": filters,
        }

    # 共振门失败
    if not resonance:
        f_ok = today["checks"].get("foreign_buy")
        o_ok = today["checks"].get("organ_buy")
        if f_ok or o_ok:
            why = "外机分裂（一边买一边卖）"
        else:
            why = "外资与机构均未净买"
        return {
            "action": "不通过",
            "level": "fail",
            "reason": _note_retail(f"{why}，滤网不通过"),
            "filters": filters,
        }

    # 量级噪声
    if intensity_tier == "noise" or intensity_tier == "negative":
        return {
            "action": "不通过",
            "level": "fail",
            "reason": _note_retail(
                f"外机共振但强度 {inten_pct} 低于噪声阈值 "
                f"{INTENSITY_WATCH * 100:.1f}%，量级不足"
            ),
            "filters": filters,
        }

    # 价格未确认
    if price_ok is False:
        return {
            "action": "观望",
            "level": "watch",
            "reason": _note_retail(
                f"外机共振、强度 {inten_pct}，但现价未站上昨收，先等价格确认"
            ),
            "filters": filters,
        }
    if price_ok is None:
        return {
            "action": "观望",
            "level": "watch",
            "reason": _note_retail(f"外机共振、强度 {inten_pct}，缺价格确认数据"),
            "filters": filters,
        }

    # 弱量 / 仅 1 日共振 / 追高 / 短脉冲 → 观察
    blockers: list[str] = []
    if not intensity_ok:
        blockers.append(f"强度 {inten_pct} 未达通过阈值 {INTENSITY_PASS * 100:.0f}%")
    if not continuity_ok:
        blockers.append(f"外机共振仅连续 {resonance_streak} 日（要≥2）")
    if chase_risk:
        blockers.append(f"当日涨幅≥{CHASE_PCT:.0f}%，追高风险")
    if organ_quality == "short_horizon":
        blockers.append("机构为自营主导短脉冲（年金/投信未同向）")

    if blockers:
        return {
            "action": "观察",
            "level": "watch",
            "reason": _note_retail(
                "共振成立且价确认，但：" + "；".join(blockers) + " → 最高观察"
            ),
            "filters": filters,
        }

    # 全部通过
    return {
        "action": "通过",
        "level": "pass",
        "reason": _note_retail(
            f"外机共振、强度 {inten_pct}、价站上昨收、近 {resonance_streak} 日连续共振"
            + ("、机构非短脉冲" if organ_quality == "balanced" else "")
            + "；允许关注做多（滤网通过，非下单信号）"
        ),
        "filters": filters,
    }


RULE_META = {
    "gates": ["共振（外且机净买）", "量级（净买/双边成交）", "价格", "连续≥2日共振", "机构质量"],
    "thresholds": {
        "pass": f"共振 + 强度≥{INTENSITY_PASS * 100:.0f}% + 价≥昨收 + 连续≥2日 + 非短脉冲",
        "watch": f"共振但强度{INTENSITY_WATCH * 100:.1f}–{INTENSITY_PASS * 100:.0f}% / 仅1日 / 涨幅≥{CHASE_PCT:.0f}% / 自营短脉冲",
        "fail": "无共振、外机分裂、或强度低于噪声阈值",
        "weaken": "近2日无共振或聪明钱净卖",
    },
    "note": (
        "滤网只回答「是否允许关注做多」，不回答买多少/何时卖。"
        "个人净买不算分，盘中残差仅诊断并标「估」。非投资建议。"
    ),
}


def build_signal(product_code: str, size: int = 8) -> dict[str, Any]:
    pack = fetch_trading_trend(product_code, size=size)
    day_scores = [score_day(t) for t in pack["days"]]
    rec = recommend_from_scores(day_scores)
    return {
        **{k: pack[k] for k in ("product_code", "latest", "history", "days")},
        "day_scores": day_scores,
        "recommendation": rec,
        "rule": RULE_META,
    }


def _resolve_watch(codes: list[str] | None) -> list[dict[str, str]]:
    watch = list(WATCHLIST)
    if not codes:
        return watch
    wanted = {normalize_product_code(c) for c in codes}
    watch = [w for w in WATCHLIST if w["product_code"] in wanted]
    for c in wanted:
        if not any(w["product_code"] == c for w in watch):
            watch.append(
                {
                    "code": c[1:] if c.startswith("A") else c,
                    "product_code": c,
                    "name": c,
                    "name_en": c,
                }
            )
    return watch


def build(codes: list[str] | None = None) -> dict[str, Any]:
    """默认返回海力士 + 三星最新买卖动向。"""
    now = datetime.now(KST)
    session_open = is_kr_cash_session(now)
    stocks = []
    errors = []
    for w in _resolve_watch(codes):
        try:
            pack = fetch_trading_trend(w["product_code"], size=3)
            stocks.append({**w, **pack})
        except Exception as e:  # noqa: BLE001 — 面板需吞掉单标失败
            errors.append({"product_code": w["product_code"], "error": str(e)})

    return {
        "fetched_at": D.now_iso(),
        "timezone": "UTC+8",
        "kr_session_open": session_open,
        "poll_interval_ms": None,
        "unit": "股",
        "source": TOSS_TREND_URL,
        "stocks": stocks,
        "errors": errors,
    }


def _dashboard_summary(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    """组合提示：优先展示「通过」，其次「观察」；海力士优先（07709）。"""
    focusable = [
        s
        for s in stocks
        if (s.get("recommendation") or {}).get("level") in ("pass", "watch")
    ]
    summary: dict[str, Any] = {
        "headline": "今日滤网暂无通过/观察标的",
        "focus": None,
    }
    if not focusable:
        return summary
    preferred = next(
        (
            s
            for s in focusable
            if s.get("code") == "000660"
            and (s.get("recommendation") or {}).get("level") == "pass"
        ),
        None,
    )
    if preferred is None:
        preferred = next(
            (s for s in focusable if (s.get("recommendation") or {}).get("level") == "pass"),
            None,
        )
    if preferred is None:
        preferred = next((s for s in focusable if s.get("code") == "000660"), focusable[0])
    rec = preferred["recommendation"]
    return {
        "headline": f"{preferred['name']} · {rec['action']}",
        "focus": preferred["code"],
        "level": rec["level"],
        "reason": rec["reason"],
    }


# 服务端采集间隔；前端读库轮询建议值
COLLECT_INTERVAL_SEC = 20
UI_POLL_INTERVAL_MS = 10_000


def collect_dashboard(codes: list[str] | None = None) -> dict[str, Any]:
    """拉取 Toss → 写 SQLite 分时桶 + 仪表盘快照。供后台定时任务调用。"""
    from src import kr_intraday_db as IDB

    now = datetime.now(KST)
    calendar_holiday = kr_calendar.is_kr_holiday(now)
    session_open = is_kr_cash_session(now)
    today_kst = now.date().isoformat()
    stocks = []
    errors = []
    for w in _resolve_watch(codes):
        try:
            sig = build_signal(w["product_code"], size=8)
            stock = {**w, **sig}
            stocks.append(stock)
        except Exception as e:  # noqa: BLE001
            errors.append({"product_code": w["product_code"], "error": str(e)})

    inferred = _update_inferred_holiday(stocks, now=now, session_open=session_open)
    inferred_holiday = bool(inferred.get("kr_holiday_inferred"))
    effective_holiday = calendar_holiday or inferred_holiday
    effective_session_open = session_open and not effective_holiday

    for stock in stocks:
        points = IDB.record_from_signal(
            stock,
            expected_trade_date=today_kst if session_open else None,
            write=effective_session_open,
        )
        stock["intraday"] = {
            "bucket_minutes": IDB.BUCKET_MINUTES,
            "timezone": "UTC+8",
            "trade_date": today_kst if session_open else _latest_base_date(stock),
            "points": points,
            "db": IDB.db_display_path(),
            "source": "collector",
        }

    summary = _dashboard_summary(stocks)
    if inferred_holiday:
        summary = {
            "headline": "韩股今日疑似休市 · 已暂停采集",
            "focus": None,
            "reason": inferred.get("market_closed_reason"),
        }

    pack = {
        "fetched_at": D.now_iso(),
        "timezone": "UTC+8",
        "kr_today": today_kst,
        "kr_session_open": effective_session_open,
        "kr_holiday": effective_holiday,
        "kr_calendar_holiday": calendar_holiday,
        **inferred,
        "collect_interval_sec": COLLECT_INTERVAL_SEC,
        "poll_interval_ms": UI_POLL_INTERVAL_MS if effective_session_open else None,
        "data_source": "sqlite",
        "source": TOSS_TREND_URL,
        "summary": summary,
        "rule": RULE_META,
        "stocks": stocks,
        "errors": errors,
    }
    sampled_at = IDB.save_dashboard_snapshot(pack, session_open=effective_session_open)
    pack["sampled_at"] = sampled_at
    return pack


def build_dashboard(codes: list[str] | None = None) -> dict[str, Any]:
    """仪表盘只读：从 SQLite 快照 + 分时序列组装（不打 Toss）。"""
    from src import kr_intraday_db as IDB

    now = datetime.now(KST)
    calendar_holiday = kr_calendar.is_kr_holiday(now)
    session_open = is_kr_cash_session(now)
    snap = IDB.load_dashboard_snapshot()
    today_kst = now.date().isoformat()
    if snap is None:
        return {
            "fetched_at": D.now_iso(),
            "timezone": "UTC+8",
            "kr_today": today_kst,
            "kr_session_open": session_open,
            "kr_holiday": calendar_holiday,
            "kr_calendar_holiday": calendar_holiday,
            "kr_holiday_inferred": False,
            "collect_interval_sec": COLLECT_INTERVAL_SEC,
            "poll_interval_ms": UI_POLL_INTERVAL_MS if session_open else None,
            "data_source": "sqlite",
            "pending": True,
            "stale": True,
            "data_age_sec": None,
            "summary": {
                "headline": "等待服务端首次采集…",
                "focus": None,
                "reason": "后台每 20s 拉取 Toss 写入 SQLite",
            },
            "rule": RULE_META,
            "stocks": [],
            "errors": [],
        }

    stocks = list(snap.get("stocks") or [])
    # 若调用方筛 codes，只返回子集
    if codes:
        wanted = {normalize_product_code(c) for c in codes}
        stocks = [
            s
            for s in stocks
            if normalize_product_code(s.get("product_code") or s.get("code") or "") in wanted
        ]
    stocks = IDB.attach_intraday_from_db(stocks)
    sampled_at = snap.get("_snapshot_sampled_at")
    inferred_today = bool(snap.get("kr_holiday_inferred")) and (
        snap.get("kr_holiday_inferred_date") == today_kst
    )
    effective_holiday = calendar_holiday or inferred_today
    effective_session_open = session_open and not effective_holiday
    fresh = snapshot_freshness(sampled_at, session_open=effective_session_open, now=now)

    return {
        "fetched_at": snap.get("fetched_at") or D.now_iso(),
        "sampled_at": sampled_at,
        "timezone": "UTC+8",
        "kr_today": today_kst,
        "kr_session_open": effective_session_open,
        "kr_holiday": effective_holiday,
        "kr_calendar_holiday": calendar_holiday,
        "kr_holiday_inferred": inferred_today,
        "kr_holiday_inferred_date": snap.get("kr_holiday_inferred_date"),
        "market_closed_reason": snap.get("market_closed_reason") if inferred_today else None,
        "base_dates": snap.get("base_dates"),
        "stale_basedate_ticks": snap.get("stale_basedate_ticks"),
        "stale_basedate_confirm_ticks": snap.get("stale_basedate_confirm_ticks"),
        "stale_basedate_grace_min": snap.get("stale_basedate_grace_min"),
        "collect_interval_sec": COLLECT_INTERVAL_SEC,
        "poll_interval_ms": UI_POLL_INTERVAL_MS if effective_session_open else None,
        "data_source": "sqlite",
        "source": snap.get("source") or TOSS_TREND_URL,
        "summary": snap.get("summary") or _dashboard_summary(stocks),
        "rule": snap.get("rule") or RULE_META,
        "stocks": stocks,
        "errors": snap.get("errors") or [],
        **fresh,
    }
