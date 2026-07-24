"""KOR 服务端定时采集：现金盘 20s；盘后至 16:30 CST 降频写入 SQLite。"""

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
_last_collect_mono = 0.0


def collect_tick(codes: list[str] | None = None) -> dict:
    """单次采集（带锁，避免与 API 启动引导冲突）。"""
    with _lock:
        return kr_investor.collect_dashboard(codes)


def _loop(interval_sec: float, codes: list[str] | None) -> None:
    global _last_paused_probe_mono, _last_collect_mono

    if kr_investor.restore_inferred_holiday_from_snapshot():
        log.info("kr collector: restored inferred holiday pause from snapshot")

    # 启动立即采一次，保证 API 有快照可读（也可用于误判自愈）
    try:
        collect_tick(codes)
        _last_collect_mono = time.monotonic()
        log.info("kr collector: initial snapshot ok")
    except Exception:  # noqa: BLE001
        log.exception("kr collector: initial collect failed")

    # 主循环按最短间隔醒来，再按当前窗口决定是否真正采集
    wake = min(float(interval_sec), float(kr_investor.COLLECT_INTERVAL_SEC))
    while not _stop.wait(wake):
        try:
            in_window = kr_investor.is_kr_collect_window()
            if not in_window:
                # 采集窗外：仅在没有快照时补一次
                from src import kr_intraday_db as IDB

                if IDB.load_dashboard_snapshot() is None:
                    collect_tick(codes)
                    _last_collect_mono = time.monotonic()
                continue

            if kr_investor.is_collection_paused_today():
                now_m = time.monotonic()
                probe_every = float(kr_investor.PAUSED_PROBE_INTERVAL_SEC)
                if now_m - _last_paused_probe_mono < probe_every:
                    continue
                _last_paused_probe_mono = now_m
                collect_tick(codes)
                _last_collect_mono = time.monotonic()
                if kr_investor.is_collection_paused_today():
                    log.info("kr collector: paused probe still holiday")
                else:
                    log.info("kr collector: inferred holiday cleared, resume full collect")
                continue

            need = float(kr_investor.collect_interval_sec())
            now_m = time.monotonic()
            if now_m - _last_collect_mono < need:
                continue
            collect_tick(codes)
            _last_collect_mono = time.monotonic()
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
    log.info(
        "kr collector started cash=%ss after=%ss until=16:30 CST",
        kr_investor.COLLECT_INTERVAL_SEC,
        kr_investor.COLLECT_INTERVAL_AFTER_SEC,
    )


def stop_collector() -> None:
    _stop.set()
