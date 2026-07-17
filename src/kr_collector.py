"""KOR 服务端定时采集：每 20s 拉 Toss，写入 SQLite。"""

from __future__ import annotations

import logging
import threading
import time

from src import kr_investor

log = logging.getLogger("kr_collector")

_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()
_last_paused_probe_mono = 0.0


def collect_tick(codes: list[str] | None = None) -> dict:
    """单次采集（带锁，避免与 API 启动引导冲突）。"""
    with _lock:
        return kr_investor.collect_dashboard(codes)


def _loop(interval_sec: float, codes: list[str] | None) -> None:
    global _last_paused_probe_mono

    if kr_investor.restore_inferred_holiday_from_snapshot():
        log.info("kr collector: restored inferred holiday pause from snapshot")

    # 启动立即采一次，保证 API 有快照可读（也可用于误判自愈）
    try:
        collect_tick(codes)
        log.info("kr collector: initial snapshot ok")
    except Exception:  # noqa: BLE001
        log.exception("kr collector: initial collect failed")

    while not _stop.wait(interval_sec):
        try:
            # 盘中采满频率；收盘后也按同间隔温更新（失败可忽略），便于隔日开盘前仍有数据
            if kr_investor.is_kr_cash_session():
                if kr_investor.is_collection_paused_today():
                    now_m = time.monotonic()
                    probe_every = float(kr_investor.PAUSED_PROBE_INTERVAL_SEC)
                    if now_m - _last_paused_probe_mono < probe_every:
                        continue
                    _last_paused_probe_mono = now_m
                    collect_tick(codes)
                    if kr_investor.is_collection_paused_today():
                        log.info("kr collector: paused probe still holiday")
                    else:
                        log.info("kr collector: inferred holiday cleared, resume full collect")
                    continue
                collect_tick(codes)
            else:
                # 非盘中：每轮只在没有快照时补一次，其余跳过省出站
                from src import kr_intraday_db as IDB

                if IDB.load_dashboard_snapshot() is None:
                    collect_tick(codes)
        except Exception:  # noqa: BLE001
            log.exception("kr collector: tick failed")


def start_collector(
    interval_sec: float | None = None,
    codes: list[str] | None = None,
) -> None:
    """幂等启动后台采集线程。"""
    global _thread
    sec = float(interval_sec if interval_sec is not None else kr_investor.COLLECT_INTERVAL_SEC)
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        name="kr-collector",
        args=(sec, codes),
        daemon=True,
    )
    _thread.start()
    log.info("kr collector started interval=%ss", sec)


def stop_collector() -> None:
    _stop.set()
