"""个股深度研报：采集公开数据 → Markdown，带来源与时间戳。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import data as D


def _fmt(v: Any, pct: bool = False, money: bool = False) -> str:
    if v is None:
        return "未获取到"
    try:
        if pct:
            return f"{float(v) * 100:.2f}%" if abs(float(v)) < 5 else f"{float(v):.2f}%"
        if money:
            n = float(v)
            if abs(n) >= 1e12:
                return f"{n / 1e12:.2f}T"
            if abs(n) >= 1e9:
                return f"{n / 1e9:.2f}B"
            if abs(n) >= 1e6:
                return f"{n / 1e6:.2f}M"
            return f"{n:,.2f}"
        return f"{float(v):.4g}" if isinstance(v, float) else str(v)
    except Exception:
        return str(v)


def collect(ticker: str) -> dict[str, Any]:
    meta_quote = D.get_quote(ticker)
    meta = meta_quote["meta"]
    quote = meta_quote["quote"]
    yahoo = meta["yahoo"]
    ts = D.now_iso()

    stats, analyst, news, indicators, klines = {}, {}, [], [], []
    errors: list[str] = []

    try:
        stats = D.key_statistics(yahoo)
    except Exception as e:
        errors.append(f"key_statistics: {e}")
    try:
        analyst = D.analyst_estimates(yahoo)
    except Exception as e:
        errors.append(f"analyst: {e}")
    try:
        news = D.stock_news(yahoo, 8)
    except Exception as e:
        errors.append(f"news: {e}")
    try:
        indicators = D.key_indicators_eastmoney(meta["eastmoney_secucode"], 4)
    except Exception as e:
        errors.append(f"eastmoney_indicators: {e}")
    try:
        klines = D.get_klines(ticker, "1y")
    except Exception as e:
        errors.append(f"klines: {e}")

    price = quote.get("price") or stats.get("current_price")
    high_52 = quote.get("high_52w")
    low_52 = quote.get("low_52w")
    pos_52 = D.pct_of_52w(price, high_52, low_52) if price else None

    ytd = None
    if klines and price:
        yr = [k for k in klines if k["date"][:4] == klines[-1]["date"][:4]]
        if yr:
            ytd = round((price / yr[0]["close"] - 1) * 100, 2)

    # 估值三法（简化、可解释）
    valuations = _build_valuations(price, stats, analyst)

    return {
        "fetched_at": ts,
        "meta": meta,
        "quote": quote,
        "stats": stats,
        "analyst": analyst,
        "news": news,
        "indicators": indicators,
        "pos_52w": pos_52,
        "ytd_pct": ytd,
        "valuations": valuations,
        "errors": errors,
    }


def _build_valuations(price, stats, analyst) -> dict:
    pe = stats.get("trailing_pe") or stats.get("forward_pe")
    pb = stats.get("price_to_book")
    target = stats.get("target_mean")
    fcf = stats.get("free_cashflow")
    mcap = stats.get("market_cap")

    relative = {
        "method": "相对估值（PE/PB/卖方目标价）",
        "trailing_pe": pe,
        "price_to_book": pb,
        "target_mean": target,
        "upside_to_target_pct": round((target / price - 1) * 100, 2) if target and price else None,
        "note": "我的判断：相对估值只给位置感，不单独构成买卖理由。",
    }

    # 粗糙 FCF yield 倍数
    fcf_yield = (fcf / mcap) if fcf and mcap else None
    dcf_proxy = {
        "method": "现金流粗估（非完整 DCF）",
        "fcf": fcf,
        "market_cap": mcap,
        "fcf_yield": round(fcf_yield * 100, 2) if fcf_yield else None,
        "assumptions": "用当前 FCF / 市值作现金流收益率；完整 DCF 需增长/WACC/永续假设。",
        "note": "我的判断：若 FCF yield 显著高于无风险利率溢价，才有向下保护。",
    }

    # 行业/成长：PEG + 营收增速
    peg = stats.get("peg_ratio")
    growth = {
        "method": "成长/行业指标（PEG + 增速）",
        "peg_ratio": peg,
        "revenue_growth": stats.get("revenue_growth"),
        "earnings_growth": stats.get("earnings_growth"),
        "note": "我的判断：PEG>2 通常偏贵；增速骤降时 PE 陷阱风险大。",
    }

    # 综合区间：目标价 ±20% 或 PE band 粗算
    band = None
    if target and price:
        band = {
            "low": round(target * 0.85, 2),
            "mid": round(target, 2),
            "high": round(target * 1.15, 2),
            "vs_price_pct": round((target / price - 1) * 100, 2),
        }
    elif pe and price and pe > 0:
        # 用 15/20/25 PE 带（通用科技/消费近似，需人工覆盖）
        eps = price / pe
        band = {
            "low": round(eps * 15, 2),
            "mid": round(eps * 20, 2),
            "high": round(eps * 25, 2),
            "vs_price_pct": None,
            "basis": f"隐含 EPS≈{eps:.2f}，15/20/25x 带（需按行业改）",
        }

    verdict = "观望"
    label = "合理"
    if band and price:
        if price < band["low"]:
            label, verdict = "低估", "买入偏多（仍须过纪律漏斗）"
        elif price > band["high"]:
            label, verdict = "高估", "规避/减仓偏多"
        else:
            label, verdict = "合理", "观望或试探"

    return {
        "relative": relative,
        "dcf_proxy": dcf_proxy,
        "growth": growth,
        "band": band,
        "label": label,
        "verdict": verdict,
        "cash_question": f"如果今天是一笔现金，我会买它吗？→ **{verdict}**（估值标签：{label}）",
    }


def to_markdown(pack: dict) -> str:
    m, q, s, v = pack["meta"], pack["quote"], pack["stats"], pack["valuations"]
    ts = pack["fetched_at"]
    lines = [
        f"# {m['name']}（{m['code']}）深度研报",
        "",
        f"> 生成时间：{ts}  ·  市场：{m['market']}  ·  Yahoo：`{m['yahoo']}`",
        f"> 数据纪律：事实与「我的判断」分开；拿不到写「未获取到」。",
        "",
        "## 1. 一句话结论",
        "",
        f"- 估值标签：**{v['label']}**",
        f"- 动作倾向：**{v['verdict']}**",
        f"- {v['cash_question']}",
        "",
        "## 2. 行情快照",
        "",
        f"| 项目 | 数值 | 来源 | 时间 |",
        f"|---|---|---|---|",
        f"| 最新价 | {_fmt(q.get('price'))} | {q.get('source', 'quote')} | {ts} |",
        f"| 涨跌幅 | {_fmt(q.get('change_pct'))}% | {q.get('source', 'quote')} | {ts} |",
        f"| 52周高/低 | {_fmt(q.get('high_52w'))} / {_fmt(q.get('low_52w'))} | quote | {ts} |",
        f"| 52周位置 | {_fmt(pack.get('pos_52w'))}% | 计算 | {ts} |",
        f"| 今年以来 | {_fmt(pack.get('ytd_pct'))}% | K线推算 | {ts} |",
        f"| PE / 前瞻PE | {_fmt(s.get('trailing_pe'))} / {_fmt(s.get('forward_pe'))} | Yahoo | {ts} |",
        f"| PB | {_fmt(s.get('price_to_book'))} | Yahoo | {ts} |",
        f"| 市值 | {_fmt(s.get('market_cap'), money=True)} | Yahoo | {ts} |",
        "",
        "## 3. 质量与护城河指标",
        "",
        f"| 指标 | 数值 | 来源 |",
        f"|---|---|---|",
        f"| 毛利率 | {_fmt(s.get('gross_margin'), pct=True)} | Yahoo financialData |",
        f"| 营业利润率 | {_fmt(s.get('operating_margin'), pct=True)} | Yahoo |",
        f"| 净利率 | {_fmt(s.get('profit_margin'), pct=True)} | Yahoo |",
        f"| ROE | {_fmt(s.get('return_on_equity'), pct=True)} | Yahoo |",
        f"| ROA | {_fmt(s.get('return_on_assets'), pct=True)} | Yahoo |",
        f"| 营收增速 | {_fmt(s.get('revenue_growth'), pct=True)} | Yahoo |",
        f"| 盈利增速 | {_fmt(s.get('earnings_growth'), pct=True)} | Yahoo |",
        f"| Beta | {_fmt(s.get('beta'))} | Yahoo |",
        f"| 现金 / 负债 | {_fmt(s.get('total_cash'), money=True)} / {_fmt(s.get('total_debt'), money=True)} | Yahoo |",
        f"| 自由现金流 | {_fmt(s.get('free_cashflow'), money=True)} | Yahoo |",
        "",
        "> 我的判断：护城河需结合商业模式，上述仅为可量化切片。",
        "",
        "## 4. 东财关键指标（近几期）",
        "",
    ]

    inds = pack.get("indicators") or []
    if not inds:
        lines.append("未获取到")
    else:
        lines.append("| 报告期 | 营收 | 归母净利 | ROE | 毛利率 |")
        lines.append("|---|---|---|---|---|")
        for row in inds[:4]:
            lines.append(
                f"| {row.get('REPORT_DATE', '')[:10]} | "
                f"{_fmt(row.get('OPERATE_INCOME'), money=True)} | "
                f"{_fmt(row.get('PARENT_HOLDER_NETPROFIT') or row.get('HOLDER_PROFIT'), money=True)} | "
                f"{_fmt(row.get('ROE_AVG'))} | "
                f"{_fmt(row.get('GROSS_PROFIT_RATIO'))} |"
            )

    lines += ["", "## 5. 分析师与目标价", ""]
    a = pack.get("analyst") or {}
    lines.append(f"- 卖方目标价：高 {_fmt(s.get('target_high'))} / 均 {_fmt(s.get('target_mean'))} / 低 {_fmt(s.get('target_low'))}")
    lines.append(f"- 综合建议：`{s.get('recommendation') or '未获取到'}`（Yahoo recommendationKey）")
    rt = a.get("rating_trend") or []
    if rt:
        cur = rt[0]
        lines.append(
            f"- 近月评级：强买 {cur.get('strong_buy')} / 买 {cur.get('buy')} / "
            f"持有 {cur.get('hold')} / 卖 {cur.get('sell')} / 强卖 {cur.get('strong_sell')}"
        )
    else:
        lines.append("- 评级趋势：未获取到")

    lines += ["", "## 6. 三法估值交叉验证", ""]
    for key in ("relative", "dcf_proxy", "growth"):
        block = v[key]
        lines.append(f"### {block['method']}")
        for k2, v2 in block.items():
            if k2 in ("method", "note"):
                continue
            lines.append(f"- {k2}: {_fmt(v2) if not isinstance(v2, str) else v2}")
        lines.append(f"- {block.get('note', '')}")
        lines.append("")

    band = v.get("band")
    lines.append("### 汇总区间")
    if band:
        lines.append(f"- 低估/中枢/高估带：{band.get('low')} / {band.get('mid')} / {band.get('high')}")
        if band.get("vs_price_pct") is not None:
            lines.append(f"- 相对目标中枢：{band['vs_price_pct']}%")
        if band.get("basis"):
            lines.append(f"- 依据：{band['basis']}")
    else:
        lines.append("- 未获取到足够数据构建区间")

    lines += ["", "## 7. 多空论据并陈", ""]
    lines.append("### 多头（事实倾向）")
    bulls = []
    if pack.get("pos_52w") is not None and pack["pos_52w"] < 40:
        bulls.append("价格处于 52 周偏低分位")
    if s.get("revenue_growth") and s["revenue_growth"] > 0.1:
        bulls.append("营收仍在双位数增长（Yahoo）")
    if s.get("return_on_equity") and s["return_on_equity"] > 0.15:
        bulls.append("ROE 较高")
    if s.get("target_mean") and q.get("price") and s["target_mean"] > q["price"] * 1.1:
        bulls.append("卖方目标价显著高于现价")
    lines.extend([f"- {b}" for b in bulls] or ["- 未获取到显著多头量化信号"])

    lines.append("")
    lines.append("### 空头（事实倾向）")
    bears = []
    if pack.get("pos_52w") is not None and pack["pos_52w"] > 80:
        bears.append("价格接近 52 周高位")
    if s.get("trailing_pe") and s["trailing_pe"] > 40:
        bears.append("trailing PE 偏高")
    if s.get("total_debt") and s.get("total_cash") and s["total_debt"] > (s["total_cash"] or 0) * 3:
        bears.append("负债相对现金更重")
    if s.get("peg_ratio") and s["peg_ratio"] > 2:
        bears.append("PEG > 2")
    lines.extend([f"- {b}" for b in bears] or ["- 未获取到显著空头量化信号"])

    lines += [
        "",
        "> 我的判断：多空清单来自规则触发，须人工核对商业叙事与一次性因素。",
        "",
        "## 8. 近期新闻（Yahoo）",
        "",
    ]
    news = pack.get("news") or []
    if not news:
        lines.append("未获取到")
    else:
        for n in news:
            lines.append(f"- [{n.get('title')}]({n.get('link')}) — {n.get('publisher')}")

    lines += [
        "",
        "## 9. 后续监控指标（3–5 个）",
        "",
        "1. 下一季营收/毛利率是否低于卖方一致预期",
        "2. 自由现金流与回购/分红是否可持续",
        "3. 估值带相对现价是否重新进入低估区",
        "4. 重大产品/监管/诉讼催化剂",
        "5. （可选）同业相对 PE/增速是否恶化",
        "",
        "## 10. 数据缺口与错误",
        "",
    ]
    errs = pack.get("errors") or []
    if errs:
        lines.extend([f"- {e}" for e in errs])
    else:
        lines.append("- 无接口级错误（仍可能有字段级「未获取到」）")

    lines += [
        "",
        "---",
        "",
        "*本报告由 invest-workbench 自动生成，不构成投资建议。最终买卖须过交易决策漏斗。*",
        "",
    ]
    return "\n".join(lines)


def run(ticker: str, out_dir: Path | None = None) -> Path:
    pack = collect(ticker)
    md = to_markdown(pack)
    out_dir = out_dir or Path(__file__).resolve().parents[1] / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    code = pack["meta"]["code"]
    stamp = pack["fetched_at"].replace(":", "").replace(" ", "-")
    path = out_dir / f"{code}-{stamp}.md"
    path.write_text(md, encoding="utf-8")
    # 同时写一份 latest 方便面板读取
    (out_dir / f"{code}-latest.md").write_text(md, encoding="utf-8")
    return path
