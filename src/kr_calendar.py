"""韩国交易日历：周末 + 已知法定假日（含常见替代休日）。

说明：未接入官方 API，每年需按 KRX 公告增补；未知假日仍可能误判为开盘。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

# KRX 休市日（现金市场），按年维护；含与周末重叠时的替代休日（已展开为具体日期）
KR_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2025
        "2025-01-01",
        "2025-01-27",
        "2025-01-28",
        "2025-01-29",
        "2025-01-30",
        "2025-03-01",
        "2025-03-03",  # 三一节替代
        "2025-05-05",
        "2025-05-06",  # 佛诞替代常见安排，以交易所为准
        "2025-06-06",
        "2025-08-15",
        "2025-10-03",
        "2025-10-06",
        "2025-10-07",
        "2025-10-08",
        "2025-10-09",
        "2025-12-25",
        # 2026
        "2026-01-01",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-03-01",
        "2026-03-02",  # 若周一补休视公告；保留宽松
        "2026-05-05",
        "2026-05-24",  # 佛诞（估）
        "2026-05-25",  # 常见补休预留
        "2026-06-06",
        "2026-08-15",
        "2026-08-17",  # 若周末则替代
        "2026-09-24",
        "2026-09-25",
        "2026-09-26",
        "2026-10-03",
        "2026-10-05",  # 开天补休预留
        "2026-10-09",
        "2026-12-25",
        # 2027（主要固定节日；农历假日请开年前核对）
        "2027-01-01",
        "2027-02-06",
        "2027-02-07",
        "2027-02-08",
        "2027-02-09",
        "2027-03-01",
        "2027-05-05",
        "2027-05-13",
        "2027-06-06",
        "2027-08-15",
        "2027-08-16",
        "2027-09-14",
        "2027-09-15",
        "2027-09-16",
        "2027-10-03",
        "2027-10-04",
        "2027-10-09",
        "2027-10-11",
        "2027-12-25",
    }
)


def _as_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def is_kr_holiday(day: date | datetime | str | None) -> bool:
    d = _as_date(day)
    if d is None:
        return False
    return d.isoformat() in KR_HOLIDAYS


def extend_holidays(extra: Iterable[str]) -> None:
    """运行时增补假日（测试 / 紧急补丁）。"""
    global KR_HOLIDAYS
    KR_HOLIDAYS = frozenset(set(KR_HOLIDAYS) | {str(x).strip()[:10] for x in extra})
