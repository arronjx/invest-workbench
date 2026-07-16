"""KOR 盘中分时抽样 + 仪表盘快照（SQLite）。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def resolve_data_dir() -> Path:
    """数据目录：优先 DATA_DIR，其次 Railway Volume，否则 ./data。

    Railway 重部署会清空容器层；必须把 Volume 挂到该路径（推荐 /app/data）。
    """
    explicit = (os.environ.get("DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    vol = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if vol:
        return Path(vol)
    return ROOT / "data"


DATA_DIR = resolve_data_dir()
DB_PATH = DATA_DIR / "kr_intraday.sqlite"

CST = timezone(timedelta(hours=8))
BUCKET_MINUTES = 5


def db_display_path() -> str:
    try:
        return str(DB_PATH.relative_to(ROOT))
    except ValueError:
        return str(DB_PATH)


def persistence_info() -> dict[str, Any]:
    vol = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    on_railway = bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_SERVICE_ID")
    )
    # Railway 上只有挂了 Volume 才算持久；本地/compose 绑定盘视为持久
    return {
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "railway_volume_mount": vol or None,
        "on_railway": on_railway,
        "persistent": bool(vol) if on_railway else True,
    }


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kr_intraday (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            bucket_ts TEXT NOT NULL,
            foreign_net INTEGER,
            organ_net INTEGER,
            change_pct REAL,
            close_price INTEGER,
            sampled_at TEXT NOT NULL,
            PRIMARY KEY (code, trade_date, bucket_ts)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kr_intraday_day ON kr_intraday(code, trade_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kr_dashboard_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            sampled_at TEXT NOT NULL,
            session_open INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def floor_bucket(dt: datetime, minutes: int = BUCKET_MINUTES) -> datetime:
    """将时间向下取整到 N 分钟桶（用 UTC+8 墙钟）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    else:
        dt = dt.astimezone(CST)
    m = (dt.minute // minutes) * minutes
    return dt.replace(minute=m, second=0, microsecond=0)


def upsert_sample(
    code: str,
    *,
    foreign_net: int | None,
    organ_net: int | None,
    change_pct: float | None,
    close_price: int | None,
    trade_date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """写入/更新当前 5 分钟桶。"""
    now_cst = (now or datetime.now(CST)).astimezone(CST)
    bucket = floor_bucket(now_cst)
    day = trade_date or bucket.strftime("%Y-%m-%d")
    bucket_ts = bucket.strftime("%Y-%m-%d %H:%M:%S")
    sampled_at = now_cst.strftime("%Y-%m-%d %H:%M:%S")
    code = (code or "").strip()
    if code.startswith("A") and len(code) == 7:
        code = code[1:]

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO kr_intraday (
                code, trade_date, bucket_ts,
                foreign_net, organ_net, change_pct, close_price, sampled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date, bucket_ts) DO UPDATE SET
                foreign_net = excluded.foreign_net,
                organ_net = excluded.organ_net,
                change_pct = excluded.change_pct,
                close_price = excluded.close_price,
                sampled_at = excluded.sampled_at
            """,
            (
                code,
                day,
                bucket_ts,
                foreign_net,
                organ_net,
                change_pct,
                close_price,
                sampled_at,
            ),
        )
        conn.commit()

    return {
        "code": code,
        "trade_date": day,
        "bucket_ts": bucket_ts,
        "foreign_net": foreign_net,
        "organ_net": organ_net,
        "change_pct": change_pct,
        "close": close_price,
        "sampled_at": sampled_at,
    }


def load_day_series(code: str, trade_date: str | None = None) -> list[dict[str, Any]]:
    """读取某日 5 分钟序列（UTC+8 桶）。"""
    code = (code or "").strip()
    if code.startswith("A") and len(code) == 7:
        code = code[1:]
    day = trade_date or datetime.now(CST).strftime("%Y-%m-%d")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT bucket_ts, foreign_net, organ_net, change_pct, close_price, sampled_at
            FROM kr_intraday
            WHERE code = ? AND trade_date = ?
            ORDER BY bucket_ts ASC
            """,
            (code, day),
        ).fetchall()
    return [
        {
            "t": r["bucket_ts"],
            "foreign_net": r["foreign_net"],
            "organ_net": r["organ_net"],
            "change_pct": r["change_pct"],
            "close": r["close_price"],
            "sampled_at": r["sampled_at"],
        }
        for r in rows
    ]


def record_from_signal(stock: dict[str, Any]) -> list[dict[str, Any]]:
    """从单标 signal 落库并返回当日序列。"""
    code = stock.get("code") or ""
    latest = stock.get("latest") or {}
    today = (stock.get("day_scores") or [{}])[0]
    price = latest.get("price") or {}
    trade_date = latest.get("base_date") or today.get("base_date")
    upsert_sample(
        code,
        foreign_net=today.get("foreign_net"),
        organ_net=today.get("organ_net"),
        change_pct=price.get("change_pct"),
        close_price=price.get("close"),
        trade_date=trade_date,
    )
    return load_day_series(code, trade_date)


def save_dashboard_snapshot(payload: dict[str, Any], *, session_open: bool) -> str:
    """覆盖写入全局仪表盘快照（供 API 只读）。"""
    sampled_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    body = json.dumps(payload, ensure_ascii=False, default=str)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO kr_dashboard_snapshot (id, payload, sampled_at, session_open)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload = excluded.payload,
                sampled_at = excluded.sampled_at,
                session_open = excluded.session_open
            """,
            (body, sampled_at, 1 if session_open else 0),
        )
        conn.commit()
    return sampled_at


def load_dashboard_snapshot() -> dict[str, Any] | None:
    """读取最新仪表盘快照。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, sampled_at, session_open FROM kr_dashboard_snapshot WHERE id = 1"
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["_snapshot_sampled_at"] = row["sampled_at"]
    payload["_snapshot_session_open"] = bool(row["session_open"])
    return payload


def attach_intraday_from_db(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用 SQLite 分时序列覆盖/补齐 stocks[].intraday。"""
    out = []
    for s in stocks:
        stock = dict(s)
        code = stock.get("code") or ""
        latest = stock.get("latest") or {}
        today = (stock.get("day_scores") or [{}])[0]
        trade_date = latest.get("base_date") or today.get("base_date")
        stock["intraday"] = {
            "bucket_minutes": BUCKET_MINUTES,
            "timezone": "UTC+8",
            "points": load_day_series(code, trade_date),
            "db": db_display_path(),
            "source": "sqlite",
        }
        out.append(stock)
    return out
