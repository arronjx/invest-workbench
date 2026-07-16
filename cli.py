#!/usr/bin/env python3
"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import kr_investor, market, options_radar, research  # noqa: E402
from src import data as D  # noqa: E402


def cmd_quote(args):
    print(json.dumps(D.get_quote(args.symbol), ensure_ascii=False, indent=2))


def cmd_research(args):
    path = research.run(args.symbol)
    print(f"已生成: {path}")
    if args.print:
        print(path.read_text(encoding="utf-8"))


def cmd_market(args):
    pack = market.build(args.years)
    print(json.dumps(pack, ensure_ascii=False, indent=2))


def cmd_options(args):
    tickers = [t.strip() for t in args.symbols.split(",") if t.strip()]
    owned = [t.strip() for t in (args.owned or "").split(",") if t.strip()]
    pack = options_radar.build_radar(tickers, owned)
    print(json.dumps(pack, ensure_ascii=False, indent=2))


def cmd_kr_investor(args):
    codes = [t.strip() for t in (args.codes or "").split(",") if t.strip()] or None
    pack = kr_investor.build(codes)
    print(json.dumps(pack, ensure_ascii=False, indent=2))


def cmd_kr_dashboard(args):
    if getattr(args, "collect", False):
        pack = kr_investor.collect_dashboard(
            [t.strip() for t in (args.codes or "").split(",") if t.strip()] or None
        )
    else:
        pack = kr_investor.build_dashboard(
            [t.strip() for t in (args.codes or "").split(",") if t.strip()] or None
        )
    print(json.dumps(pack, ensure_ascii=False, indent=2))


def cmd_serve(args):
    from src.server import main as serve_main

    serve_main(args.host, args.port, role=getattr(args, "role", None))


def main():
    p = argparse.ArgumentParser(description="invest-workbench：美股/港股/韩股资讯与研报工作台")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("quote", help="实时行情")
    q.add_argument("symbol")
    q.set_defaults(func=cmd_quote)

    r = sub.add_parser("research", help="生成个股研报 Markdown")
    r.add_argument("symbol")
    r.add_argument("--print", action="store_true")
    r.set_defaults(func=cmd_research)

    m = sub.add_parser("market", help="大盘估值分位")
    m.add_argument("--years", type=int, default=5)
    m.set_defaults(func=cmd_market)

    o = sub.add_parser("options", help="期权机会雷达")
    o.add_argument("symbols", help="逗号分隔美股代码，如 AAPL,TSLA,NVDA")
    o.add_argument("--owned", default="", help="已有空头标的，逗号分隔")
    o.set_defaults(func=cmd_options)

    k = sub.add_parser("kr-investor", help="海力士/三星投资者买卖动向（Toss）")
    k.add_argument("--codes", default="", help="可选，如 A000660,A005930")
    k.set_defaults(func=cmd_kr_investor)

    kd = sub.add_parser("kr-dashboard", help="KOR 供需滤网（默认读 SQLite 快照）")
    kd.add_argument("--codes", default="", help="可选，如 A000660,A005930")
    kd.add_argument(
        "--collect",
        action="store_true",
        help="立即拉 Toss 并写入 SQLite（否则只读库）",
    )
    kd.set_defaults(func=cmd_kr_dashboard)

    s = sub.add_parser("serve", help="启动面板服务（默认 0.0.0.0，支持局域网）")
    s.add_argument("--host", default="0.0.0.0", help="绑定地址，默认 0.0.0.0 允许局域网访问")
    s.add_argument(
        "--port",
        type=int,
        default=None,
        help="端口；默认读环境变量 PORT，否则 8787（Railway 友好）",
    )
    s.add_argument(
        "--role",
        default=None,
        choices=["all", "web", "collector"],
        help="all=采集+全量API；collector=仅采集+KR读库；web=面板并转发KR到 KR_UPSTREAM（也可设环境变量 ROLE）",
    )
    s.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
