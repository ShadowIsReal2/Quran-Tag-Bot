"""
utils.py — Shared utility functions for Quran Tracker Bot.

Contains date helpers, reading-plan logic, backup utilities,
statistics engine, and other tools consumed by multiple modules.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import random
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

import messages as msg
from messages import (
    ARABIC_MONTHS,
    DAILY_DUAS,
    PLAN_READING_1_JUZ,
    PLAN_READING_CUSTOM,
    PLAN_READING_PAGES,
    QURAN_HADITHS,
    QURAN_VERSES,
    get_streak_badge,
    get_daily_motivation,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

def now_in_tz(tz: pytz.BaseTzInfo) -> datetime:
    """Return the current datetime in the given timezone."""
    return datetime.now(tz)


def today_in_tz(tz: pytz.BaseTzInfo) -> date:
    """Return today's date in the given timezone."""
    return now_in_tz(tz).date()


def format_date_arabic(d: date) -> str:
    """Format a date as Arabic string: e.g. '15 يونيو 2025'."""
    return f"{d.day} {ARABIC_MONTHS[d.month]} {d.year}"


def format_month_arabic(year: int, month: int) -> str:
    """Format year+month as Arabic string: e.g. 'يونيو 2025'."""
    return f"{ARABIC_MONTHS[month]} {year}"


def days_in_month(year: int, month: int) -> int:
    """Return the number of days in a given month."""
    return calendar.monthrange(year, month)[1]


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' and return (hour, minute)."""
    h, m = value.split(":")
    return int(h), int(m)


def week_bounds(target_date: date) -> tuple[date, date]:
    """
    Return the Monday and Sunday bounding the week that contains target_date.

    Returns:
        (monday, sunday) as date objects.
    """
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def iso_week_number(d: date) -> int:
    """Return the ISO week number of a date (1–53)."""
    return d.isocalendar()[1]


# ---------------------------------------------------------------------------
# Reading plan helpers
# ---------------------------------------------------------------------------

_TOTAL_JUZ   = 30
_TOTAL_PAGES = 604
_JUZ_PER_PAGE = _TOTAL_JUZ / _TOTAL_PAGES   # ≈ 0.0496


def get_reading_for_today(
    plan_key: str,
    custom_text: str = "",
    target_date: Optional[date] = None,
) -> str:
    """
    Return the reading text for today based on the active plan.

    For rotating plans (juz/pages) the assignment is based on
    day-of-year modulo the total cycle length, so it loops cleanly.
    """
    if target_date is None:
        target_date = date.today()

    day_of_year = target_date.timetuple().tm_yday  # 1–366

    if plan_key == "1_juz_day":
        juz = ((day_of_year - 1) % _TOTAL_JUZ) + 1
        return PLAN_READING_1_JUZ.format(juz=juz)

    if plan_key in ("2_pages_day", "5_pages_day", "10_pages_day"):
        ppd = int(plan_key.split("_")[0])
        from_page = ((day_of_year - 1) * ppd % _TOTAL_PAGES) + 1
        to_page = min(from_page + ppd - 1, _TOTAL_PAGES)
        return PLAN_READING_PAGES.format(
            pages=ppd, from_page=from_page, to_page=to_page
        )

    if plan_key == "custom":
        return PLAN_READING_CUSTOM.format(text=custom_text or "ورد مخصص")

    return "جزء من القرآن الكريم"


def estimate_juz_from_checkins(total_checkins: int, plan_key: str) -> int:
    """Estimate total juz completed given check-in count and plan."""
    if plan_key == "1_juz_day":
        return total_checkins
    ppd_map = {"2_pages_day": 2, "5_pages_day": 5, "10_pages_day": 10}
    ppd = ppd_map.get(plan_key, 0)
    if ppd:
        total_pages = total_checkins * ppd
        return int(total_pages / (_TOTAL_PAGES / _TOTAL_JUZ))
    return 0


def estimate_pages_from_checkins(total_checkins: int, plan_key: str) -> int:
    """Estimate total pages read given check-in count and plan."""
    if plan_key == "1_juz_day":
        return int(total_checkins * (_TOTAL_PAGES / _TOTAL_JUZ))
    ppd_map = {"2_pages_day": 2, "5_pages_day": 5, "10_pages_day": 10}
    ppd = ppd_map.get(plan_key, 0)
    if ppd:
        return total_checkins * ppd
    return 0


# ---------------------------------------------------------------------------
# Random content pickers
# ---------------------------------------------------------------------------

def pick_random_verse() -> str:
    """Return a random Quran verse from the messages pool."""
    return random.choice(QURAN_VERSES)


def pick_random_hadith() -> str:
    """Return a random hadith about the Quran."""
    return random.choice(QURAN_HADITHS)


def pick_daily_dua() -> str:
    """Return a daily dua (cycles by day-of-year)."""
    idx = (date.today().timetuple().tm_yday - 1) % len(DAILY_DUAS)
    return DAILY_DUAS[idx]


# ---------------------------------------------------------------------------
# Statistics engine
# ---------------------------------------------------------------------------


def compute_completion_pct(completed: int, total: int) -> int:
    """Return integer completion percentage; 0 if total is 0."""
    if total == 0:
        return 0
    return round((completed / total) * 100)


def compute_avg_per_week(total_checkins: int, first_checkin: Optional[date]) -> float:
    """
    Compute average check-ins per week based on the user's history length.

    Args:
        total_checkins: Total number of check-ins.
        first_checkin:  Date of the first check-in; None if no data.

    Returns:
        Average check-ins per week as a float rounded to one decimal.
    """
    if not first_checkin or total_checkins == 0:
        return 0.0
    days_active = max((date.today() - first_checkin).days + 1, 1)
    weeks = days_active / 7
    return round(total_checkins / weeks, 1)


def compute_trend(current_pct: int, previous_pct: int) -> tuple[str, int]:
    """
    Compute participation trend between two periods.

    Returns:
        (direction, abs_diff) where direction ∈ {'up', 'down', 'same', 'new'}
    """
    if previous_pct < 0:
        return ("new", 0)
    diff = current_pct - previous_pct
    if diff > 2:
        return ("up", diff)
    if diff < -2:
        return ("down", abs(diff))
    return ("same", 0)


def build_stats_text(
    name: str,
    joined: str,
    current_streak: int,
    longest_streak: int,
    this_month: int,
    month_days: int,
    this_year: int,
    total: int,
    total_juz: int,
    total_pages: int,
    avg_per_week: float,
    best_month_str: str,
) -> str:
    """
    Build a formatted personal statistics block.

    All parameters are pre-computed; this function only formats the message.
    """
    text = msg.STATS_HEADER + "\n" + msg.STATS_BODY.format(
        name=name,
        joined=joined,
        current_streak=current_streak,
        longest_streak=longest_streak,
        this_month=this_month,
        days_in_month=month_days,
        this_year=this_year,
        total=total,
        total_juz=total_juz,
        total_pages=total_pages,
        avg_per_week=avg_per_week,
        best_month=best_month_str,
    )
    badge = get_streak_badge(current_streak)
    if badge:
        text += f"\n\n{badge}"
    return text


def compute_weekly_stats(
    report_rows: list,
    from_date: date,
    to_date: date,
) -> dict:
    """
    Compute aggregate statistics from a list of daily_reports rows
    covering a single week.

    Args:
        report_rows: aiosqlite Row objects from get_weekly_report_data().
        from_date:   First day of the week.
        to_date:     Last day of the week.

    Returns:
        Dictionary with keys:
            total_checkins, days_tracked, avg_pct,
            active_members, avg_active_members
    """
    if not report_rows:
        return {
            "total_checkins": 0,
            "days_tracked": 0,
            "avg_pct": 0,
            "active_members": 0,
            "avg_active_members": 0,
        }

    total_checkins = sum(r["confirmed"] for r in report_rows)
    days_tracked   = len(report_rows)
    max_members    = max((r["active_members"] for r in report_rows), default=0)

    pct_values = [
        compute_completion_pct(r["confirmed"], r["active_members"])
        for r in report_rows
        if r["active_members"] > 0
    ]
    avg_pct = round(sum(pct_values) / len(pct_values)) if pct_values else 0

    avg_members = (
        round(sum(r["active_members"] for r in report_rows) / days_tracked)
        if days_tracked > 0 else 0
    )

    return {
        "total_checkins":   total_checkins,
        "days_tracked":     days_tracked,
        "avg_pct":          avg_pct,
        "active_members":   max_members,
        "avg_active_members": avg_members,
    }


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Name formatting helpers
# ---------------------------------------------------------------------------

def display_name(first_name: str, last_name: str = "", username: str = "") -> str:
    """Return the best display name for a user."""
    full = f"{first_name} {last_name}".strip()
    return full or username or "مستخدم مجهول"


def escape_markdown(text: str) -> str:
    """
    Escape special MarkdownV2 characters so that user-supplied text
    doesn't break Telegram message formatting.
    """
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)


# ---------------------------------------------------------------------------
# Admin check helper
# ---------------------------------------------------------------------------

async def is_admin(update, context) -> bool:
    """
    Return True if the message author is a chat administrator or creator.
    Works for both group and supergroup chats.
    """
    try:
        chat_member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        return chat_member.status in ("administrator", "creator")
    except Exception as exc:
        logger.warning("is_admin check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Chunk helper for long messages
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_len: int = 4000) -> list[str]:
    """Split a long string into chunks ≤ max_len characters."""
    if len(text) <= max_len:
        return [text]
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
