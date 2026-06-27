"""
achievements.py — Reusable achievement & milestone engine for Quran Tracker Bot.

Responsibilities:
  • Check which achievements a user has newly earned after a check-in.
  • Check which group milestones have been newly reached.
  • Notify users privately and optionally announce in the group.
  • All award logic is centralised here so adding new badges requires
    only adding an entry to ACHIEVEMENT_DEFINITIONS in messages.py.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime

import pytz
from telegram import Bot
from telegram.constants import ParseMode

import messages as msg
from config import settings
from database import Database
from utils import display_name, today_in_tz

logger = logging.getLogger(__name__)

MD = ParseMode.MARKDOWN

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _send_safe(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Send a message gracefully, swallowing errors."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as exc:
        logger.warning("achievements: could not send to %d: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Achievement evaluation
# ---------------------------------------------------------------------------


async def evaluate_user_achievements(
    bot: Bot,
    db: Database,
    user_id: int,
    group_id: int,
    announce_badges: bool = True,
) -> list[str]:
    """
    Check all achievement conditions for a user and grant newly earned ones.

    Should be called after every successful check-in.

    Args:
        bot:            Telegram Bot instance for notifications.
        db:             Database instance.
        user_id:        Telegram user ID.
        group_id:       Telegram group ID.
        announce_badges: Whether to post earned badges in the group.

    Returns:
        List of achievement keys that were newly granted during this call.
    """
    if not settings.enable_badges:
        return []

    newly_earned: list[str] = []

    # Gather raw data needed for checks
    streak_row = await db.get_streak(user_id, group_id)
    user_row   = await db.get_user(user_id, group_id)
    total      = await db.count_checkins_total(user_id, group_id)

    if not streak_row or not user_row:
        return []

    current_streak = streak_row["current_streak"]
    today = date.today()

    # ── Joined-date based ──────────────────────────────────────────────────
    joined_str = user_row["joined_at"]
    joined_dt  = datetime.fromisoformat(joined_str)
    days_since_joined = (today - joined_dt.date()).days

    # ── First check-in ─────────────────────────────────────────────────────
    conditions: list[tuple[str, bool]] = [
        ("first_day",        total >= 1),
        ("streak_7",         current_streak >= 7),
        ("streak_30",        current_streak >= 30),
        ("streak_100",       current_streak >= 100),
        ("streak_365",       current_streak >= 365),
        ("checkins_50",      total >= 50),
        ("checkins_100",     total >= 100),
        ("checkins_200",     total >= 200),
        ("checkins_365",     total >= 365),
        ("one_year_member",  days_since_joined >= 365),
    ]

    # Check for first complete month
    year, month = today.year, today.month
    days_in_month = calendar.monthrange(year, month)[1]
    month_count = await db.count_checkins_this_month(user_id, group_id, year, month)
    conditions.append(("first_full_month", month_count >= days_in_month))

    for key, earned in conditions:
        if not earned:
            continue
        if await db.has_achievement(user_id, group_id, key):
            continue
        granted = await db.grant_achievement(user_id, group_id, key)
        if not granted:
            continue

        newly_earned.append(key)
        logger.info(
            "Achievement granted: user=%d group=%d key=%s", user_id, group_id, key
        )

        emoji, name, desc = msg.ACHIEVEMENT_MAP.get(key, ("🏅", key, ""))

        # Notify the user privately — disabled with NOTIFY_ACHIEVEMENTS=false,
        # so badges are silently recorded and only surface via /me.
        if settings.notify_achievements:
            await _send_safe(
                bot,
                user_id,
                msg.ACHIEVEMENT_EARNED_PRIVATE.format(emoji=emoji, name=name, desc=desc),
                parse_mode=MD,
            )

        # Optionally announce in the group
        if announce_badges and settings.notify_achievements:
            full_name = display_name(
                user_row["first_name"],
                user_row["last_name"],
                user_row["username"],
            )
            await _send_safe(
                bot,
                group_id,
                msg.ACHIEVEMENT_EARNED_GROUP.format(
                    user=full_name, emoji=emoji, name=name, desc=desc
                ),
                parse_mode=MD,
            )

    return newly_earned


# ---------------------------------------------------------------------------
# Group milestone evaluation
# ---------------------------------------------------------------------------

# Separate lists by milestone type so we can query different metrics
_CHECKIN_MILESTONES: list[tuple[str, int]] = [
    (key, threshold)
    for key, threshold, _, _ in msg.GROUP_MILESTONE_DEFINITIONS
    if key.startswith("checkins_")
]

_KHATMAH_MILESTONES: list[tuple[str, int]] = [
    (key, threshold)
    for key, threshold, _, _ in msg.GROUP_MILESTONE_DEFINITIONS
    if key.startswith("khatmahs_")
]

_MILESTONE_MSG: dict[str, tuple[str, str]] = {
    key: (emoji, message)
    for key, _, emoji, message in msg.GROUP_MILESTONE_DEFINITIONS
}

# Pages per full Quran khatmah and juz based on 1_juz_day plan
_PAGES_PER_KHATMAH = 604


def _estimate_khatmahs(total_checkins: int, plan_key: str) -> int:
    """Rough estimate of completed khatmahs based on plan and total check-ins."""
    if plan_key == "1_juz_day":
        return total_checkins // 30
    ppd_map = {"2_pages_day": 2, "5_pages_day": 5, "10_pages_day": 10}
    ppd = ppd_map.get(plan_key, 0)
    if ppd:
        return (total_checkins * ppd) // _PAGES_PER_KHATMAH
    return 0


async def evaluate_group_milestones(
    bot: Bot,
    db: Database,
    group_id: int,
    plan_key: str = "1_juz_day",
) -> list[str]:
    """
    Check whether any group milestone thresholds have been newly crossed.

    Should be called after each successful check-in in the group.

    Args:
        bot:      Telegram Bot instance.
        db:       Database instance.
        group_id: Telegram group ID.
        plan_key: Current reading plan key (used to estimate khatmahs).

    Returns:
        List of milestone keys newly reached.
    """
    if not settings.enable_milestones:
        return []

    group_settings = await db.get_settings(group_id)
    if group_settings is not None and not bool(int(group_settings.get("milestones_enabled", 1))):
        return []

    newly_reached: list[str] = []

    total_checkins = await db.count_group_checkins_total(group_id)
    estimated_khatmahs = _estimate_khatmahs(total_checkins, plan_key)

    checks: list[tuple[str, int, int]] = [
        (key, threshold, total_checkins)
        for key, threshold in _CHECKIN_MILESTONES
    ] + [
        (key, threshold, estimated_khatmahs)
        for key, threshold in _KHATMAH_MILESTONES
    ]

    for key, threshold, current in checks:
        if current < threshold:
            continue
        if await db.has_milestone(group_id, key):
            continue
        granted = await db.grant_milestone(group_id, key)
        if not granted:
            continue

        newly_reached.append(key)
        logger.info("Group milestone reached: group=%d key=%s", group_id, key)

        emoji, message = _MILESTONE_MSG.get(key, ("🎉", key))
        text = msg.GROUP_MILESTONE_ANNOUNCEMENT.format(emoji=emoji, message=message)
        await _send_safe(bot, group_id, text, parse_mode=MD)

    return newly_reached
