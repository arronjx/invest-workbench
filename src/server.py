"""本地 HTTP 服务：面板 + JSON API。"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import kr_collector, kr_investor, market, options_radar, research  # noqa: E402
from src import data as D  # noqa: E402

PANELS = ROOT / "panels"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

# /api/health 供 Railway 探活，不做鉴权
PUBLIC_PATHS = frozenset({"/api/health"})


def auth_configured() -> bool:
    return bool(os.environ.get("BASIC_AUTH_USER") and os.environ.get("BASIC_AUTH_PASSWORD"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _unauthorized(self):
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="invest-workbench"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        user = os.environ.get("BASIC_AUTH_USER") or ""
        password = os.environ.get("BASIC_AUTH_PASSWORD") or ""
        if not user or not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        if ":" not in raw:
            return False
        u, p = raw.split(":", 1)
        return u == user and p == password

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization",
        )
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._authorized(path):
            return self._unauthorized()
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._file(PANELS / "index.html", "text/html; charset=utf-8")
        if path.startswith("/panels/"):
            name = path[len("/panels/") :]
            return self._file(PANELS / name, "text/html; charset=utf-8")
        if path == "/api/quote":
            sym = (qs.get("symbol") or [""])[0]
            return self._json(D.get_quote(sym) if sym else {"error": "symbol required"})
        if path == "/api/market":
            years = int((qs.get("years") or ["5"])[0])
            return self._json(market.build(years))
        if path == "/api/options":
            symbols = (qs.get("symbols") or ["AAPL,MSFT,NVDA,TSLA,META,AMD,GOOGL,AMZN"])[0]
            owned = (qs.get("owned") or [""])[0]
            tickers = [t.strip() for t in symbols.split(",") if t.strip()]
            owned_list = [t.strip() for t in owned.split(",") if t.strip()]
            return self._json(options_radar.build_radar(tickers, owned_list))
        if path == "/api/research":
            sym = (qs.get("symbol") or [""])[0]
            if not sym:
                return self._json({"error": "symbol required"}, 400)
            p = research.run(sym)
            return self._json({"path": str(p), "markdown": p.read_text(encoding="utf-8")})
        if path == "/api/kr-investor":
            # 与 dashboard 一致读库，避免无人页直打 Toss
            codes = (qs.get("codes") or [""])[0]
            code_list = [c.strip() for c in codes.split(",") if c.strip()] or None
            try:
                return self._json(kr_investor.build_dashboard(code_list))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 502)
        if path == "/api/kr-dashboard":
            codes = (qs.get("codes") or [""])[0]
            code_list = [c.strip() for c in codes.split(",") if c.strip()] or None
            try:
                return self._json(kr_investor.build_dashboard(code_list))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 502)
        if path == "/api/health":
            from src import kr_intraday_db as IDB

            return self._json(
                {
                    "ok": True,
                    "auth": auth_configured(),
                    "collect_interval_sec": kr_investor.COLLECT_INTERVAL_SEC,
                    "persistence": IDB.persistence_info(),
                }
            )

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized(parsed.path):
            return self._unauthorized()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid json"}, 400)

        if parsed.path == "/api/research":
            sym = payload.get("symbol", "")
            if not sym:
                return self._json({"error": "symbol required"}, 400)
            p = research.run(sym)
            return self._json({"path": str(p), "markdown": p.read_text(encoding="utf-8")})

        self.send_error(404)


def _lan_ips() -> list[str]:
    """探测本机局域网 IPv4，供启动提示。"""
    import socket

    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def resolve_port(cli_port: int | None = None) -> int:
    """Railway 等平台注入 PORT；CLI --port 优先。"""
    if cli_port is not None:
        return int(cli_port)
    raw = os.environ.get("PORT") or "8787"
    return int(raw)


def main(host: str = "0.0.0.0", port: int | None = None):
    listen_port = resolve_port(port)
    from src import kr_intraday_db as IDB

    info = IDB.persistence_info()
    print(f"SQLite: {info['db_path']} persistent={info['persistent']}")
    if info.get("on_railway") and not info["persistent"]:
        print(
            "警告: Railway 未检测到 Volume。重部署会清空 SQLite。"
            "请在 Volumes 将 Mount Path 设为 /app/data。"
        )
    kr_collector.start_collector(interval_sec=kr_investor.COLLECT_INTERVAL_SEC)
    server = ThreadingHTTPServer((host, listen_port), Handler)
    print(f"invest-workbench 监听: {host}:{listen_port}")
    print(f"  本机: http://127.0.0.1:{listen_port}/")
    if host in ("0.0.0.0", "::"):
        lan = _lan_ips()
        if lan:
            for ip in lan:
                print(f"  局域网: http://{ip}:{listen_port}/")
        else:
            print(f"  局域网: http://<本机IP>:{listen_port}/ （请在系统网络设置查看 IP）")
    else:
        print(f"  访问: http://{host}:{listen_port}/")
    if auth_configured():
        print("  鉴权: Basic Auth 已启用（/api/health 除外）")
    else:
        print("  鉴权: 未配置（设置 BASIC_AUTH_USER + BASIC_AUTH_PASSWORD 启用）")
    print(
        f"采集: 盘中每 {kr_investor.COLLECT_INTERVAL_SEC}s → SQLite；"
        "单实例 + Volume:/app/data；勿开 App Sleep"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        kr_collector.stop_collector()


if __name__ == "__main__":
    main()
