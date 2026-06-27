"""
scheduler.py — Scheduled jobs for the Quran Tracker Bot.

Jobs registered here:
  1. daily_post_job        — sends the daily Quran reminder to all active groups
  2. daily_report_job      — sends the evening completion report
  3. weekly_report_job     — sends a Friday weekly summary
  4. monthly_report_job    — sends a monthly summary on the 1st of each month
  5. reminder_job          — privately reminds members who haven't checked in
  6. auto_backup_job       — creates an automatic database backup

All times are scheduled in the per-group timezone stored in settings.
Global fallback uses the value from .env.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytz
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

import messages as msg
from config import settings
from backup import async_create_backup
from monitoring import monitoring_job
from database import Database
from utils import (

    compute_completion_pct,
    compute_trend,
    compute_weekly_stats,
    display_name,
    format_date_arabic,
    format_month_arabic,
    get_reading_for_today,
    pick_daily_dua,
    pick_random_hadith,
    pick_random_verse,
    today_in_tz,
    week_bounds,
    iso_week_number,
    parse_hhmm,
)
from keyboards import daily_post_keyboard
from messages import get_daily_motivation, get_weekly_encouragement

logger = logging.getLogger(__name__)
MD = ParseMode.MARKDOWN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db(app: Application) -> Database:
    return app.bot_data["db"]


async def _send_safe(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Send a message, logging and swallowing errors gracefully."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as exc:
        logger.warning("Failed to send message to %d: %s", chat_id, exc)
        return False


async def _get_group_tz(group_settings) -> pytz.BaseTzInfo:
    """Return the timezone for a group, falling back to global config."""
    tz_str = group_settings["timezone"] if group_settings else settings.timezone_str
    try:
        return pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        return settings.timezone


# ---------------------------------------------------------------------------
# Job 1: Daily Quran post  (dynamic motivation each day)
# ---------------------------------------------------------------------------

async def daily_post_job(context) -> None:
    """
    Scheduled job: send today's reading reminder to every active group.

    Features:
      • Rotating daily motivation text (never repeats two days in a row).
      • Optional verse / dua / hadith appended based on config.
      • Skips groups that have had today marked as skipped.
    """
    app: Application = context.application
    db = _get_db(app)
    bot: Bot = app.bot

    active_groups = await db.get_all_active_groups()
    if not active_groups:
        logger.info("daily_post_job: no active groups.")
        return

    for group_row in active_groups:
        group_id: int  = group_row["group_id"]
        group_title: str = group_row["title"]

        group_settings = await db.get_settings(group_id)
        tz = await _get_group_tz(group_settings)
        today = today_in_tz(tz)

        if await db.is_day_skipped(group_id, today):
            logger.info("daily_post_job: group %d skipped for %s", group_id, today)
            continue

        plan_key     = group_settings["plan_key"]     if group_settings else "1_juz_day"
        custom_text  = group_settings["custom_reading"] if group_settings else ""
        raw_start    = (group_settings["reading_start"] or "") if group_settings else ""
        start_date   = date.fromisoformat(raw_start) if raw_start else None
        reading      = get_reading_for_today(plan_key, custom_text, today, start_date)
        date_str     = format_date_arabic(today)

        # ── Dynamic motivation (no-repeat) ────────────────────────────────
        day_seed = today.timetuple().tm_yday
        last_index = await db.get_last_motivation_index(group_id)
        motivation = get_daily_motivation(day_seed, last_index)
        # Persist the index used today
        new_index = msg.DAILY_MOTIVATION_TEMPLATES.index(motivation)
        await db.set_last_motivation_index(group_id, new_index, today)

        text = (
            msg.DAILY_POST_HEADER + "\n\n"
            + msg.DAILY_POST_BODY.format(date=date_str, reading=reading, motivation=motivation)
        )

        def _gs(key: str, default: int) -> bool:
            return bool(int(group_settings.get(key, default))) if group_settings else bool(default)

        if _gs("daily_verse_enabled", 1 if (settings.enable_random_verses or settings.include_daily_verse) else 0):
            text += msg.DAILY_POST_WITH_VERSE.format(verse=pick_random_verse())

        if _gs("daily_dua_enabled", 1 if (settings.enable_random_dua or settings.include_daily_dua) else 0):
            text += msg.DAILY_POST_WITH_DUA.format(dua=pick_daily_dua())

        if _gs("daily_hadith_enabled", 1 if (settings.enable_random_hadith or settings.include_daily_hadith) else 0):
            text += msg.DAILY_POST_WITH_HADITH.format(hadith=pick_random_hadith())

        ok = await _send_safe(
            bot, group_id, text,
            parse_mode=MD,
            reply_markup=daily_post_keyboard(),
        )
        if ok:
            logger.info(
                "daily_post_job: sent to group %d (%s) for %s (motivation #%d)",
                group_id, group_title, today, new_index,
            )


# ---------------------------------------------------------------------------
# Job 2: Daily completion report
# ---------------------------------------------------------------------------

async def daily_report_job(context) -> None:
    """
    Scheduled job: send the evening participation report to each active group.
    Never publicly lists who is missing — only counts.
    """
    app: Application = context.application
    db = _get_db(app)
    bot: Bot = app.bot

    for group_row in await db.get_all_active_groups():
        group_id: int = group_row["group_id"]
        group_settings = await db.get_settings(group_id)
        tz = await _get_group_tz(group_settings)
        today = today_in_tz(tz)

        if await db.is_day_skipped(group_id, today):
            continue

        if group_settings and not bool(group_settings.get("report_enabled", 1)):
            continue

        active_users = await db.get_active_users(group_id)
        checked_ids  = set(await db.get_who_checked_in(group_id, today))
        total     = len(active_users)
        confirmed = len(checked_ids)
        pending   = total - confirmed
        pct       = compute_completion_pct(confirmed, total)

        await db.upsert_daily_report(group_id, today, confirmed, pending, total)

        if total == 0:
            continue

        text = msg.DAILY_REPORT_HEADER + "\n" + msg.DAILY_REPORT_BODY.format(
            date=format_date_arabic(today),
            confirmed=confirmed,
            pending=pending,
            pct=pct,
        )
        if confirmed == total and total > 0:
            text += msg.DAILY_REPORT_PERFECT

        await _send_safe(bot, group_id, text, parse_mode=MD)
        logger.info(
            "daily_report_job: group %d — %d/%d confirmed", group_id, confirmed, total
        )


# ---------------------------------------------------------------------------
# Job 3: Weekly report (runs on the configured day, default Friday)
# ---------------------------------------------------------------------------

async def weekly_report_job(context) -> None:
    """
    Scheduled job: send a weekly participation summary.

    Runs daily but only fires on the configured WEEKLY_REPORT_DAY
    (default 4 = Friday). Compares with the previous week's data.

    Never mentions members who missed; focuses on positive metrics.
    """
    if not settings.enable_weekly_report:
        return

    today_utc = datetime.utcnow().date()
    if today_utc.weekday() != settings.weekly_report_day:
        return  # Not the right day

    app: Application = context.application
    db = _get_db(app)
    bot: Bot = app.bot

    # The week that just ended: Mon–today (inclusive)
    monday, _ = week_bounds(today_utc)
    from_date = monday
    to_date   = today_utc

    # Previous week: Mon-7 to Sun-1
    prev_monday = monday - timedelta(days=7)
    prev_sunday = monday - timedelta(days=1)

    for group_row in await db.get_all_active_groups():
        group_id: int = group_row["group_id"]
        group_settings = await db.get_settings(group_id)
        if group_settings and not bool(int(group_settings.get("weekly_report_enabled", 1))):
            continue

        # Current week data
        curr_rows = await db.get_weekly_report_data(group_id, from_date, to_date)
        curr_stats = compute_weekly_stats(curr_rows, from_date, to_date)

        if curr_stats["total_checkins"] == 0:
            continue  # No activity this week — skip silently

        # Previous week data (for trend comparison)
        prev_rows = await db.get_weekly_report_data(group_id, prev_monday, prev_sunday)
        prev_stats = compute_weekly_stats(prev_rows, prev_monday, prev_sunday)

        direction, diff = compute_trend(
            curr_stats["avg_pct"],
            prev_stats["avg_pct"] if prev_rows else -1,
        )

        # Build trend string
        if direction == "new":
            trend_str = msg.WEEKLY_REPORT_TREND_NEW
        elif direction == "up":
            trend_str = msg.WEEKLY_REPORT_TREND_UP.format(diff=diff)
        elif direction == "down":
            trend_str = msg.WEEKLY_REPORT_TREND_DOWN.format(diff=diff)
        else:
            trend_str = msg.WEEKLY_REPORT_TREND_SAME

        # Encouragement (deterministic per week number)
        encouragement = get_weekly_encouragement(iso_week_number(today_utc))

        text = msg.WEEKLY_REPORT_HEADER + "\n" + msg.WEEKLY_REPORT_BODY.format(
            from_date=format_date_arabic(from_date),
            to_date=format_date_arabic(to_date),
            active_members=curr_stats["active_members"],
            total_checkins=curr_stats["total_checkins"],
            avg_pct=curr_stats["avg_pct"],
            days_tracked=curr_stats["days_tracked"],
            trend=trend_str,
            encouragement=encouragement,
        )

        await _send_safe(bot, group_id, text, parse_mode=MD)
        logger.info(
            "weekly_report_job: sent to group %d — week of %s (%d%% avg)",
            group_id, from_date, curr_stats["avg_pct"],
        )


# ---------------------------------------------------------------------------
# Job 4: Monthly report (runs on the 1st of each month)
# ---------------------------------------------------------------------------

async def monthly_report_job(context) -> None:
    """
    Scheduled job: sends a full monthly summary on the first day of each month
    for the previous month.
    """
    today_utc = datetime.utcnow().date()
    if today_utc.day != 1:
        return

    if today_utc.month == 1:
        prev_year, prev_month = today_utc.year - 1, 12
    else:
        prev_year, prev_month = today_utc.year, today_utc.month - 1

    app: Application = context.application
    db = _get_db(app)
    bot: Bot = app.bot

    for group_row in await db.get_all_active_groups():
        group_id: int = group_row["group_id"]

        report_rows  = await db.get_monthly_report_data(group_id, prev_year, prev_month)
        leaderboard  = await db.get_monthly_leaderboard(group_id, prev_year, prev_month)

        if not report_rows:
            continue

        total_checkins   = sum(r["confirmed"] for r in report_rows)
        days_with_data   = len(report_rows)
        active_members   = max((r["active_members"] for r in report_rows), default=0)

        pct_values = [
            compute_completion_pct(r["confirmed"], r["active_members"])
            for r in report_rows if r["active_members"] > 0
        ]
        avg_pct = round(sum(pct_values) / len(pct_values)) if pct_values else 0

        month_name = format_month_arabic(prev_year, prev_month)

        # Build top readers block
        top_lines = ""
        for i, row in enumerate(leaderboard[:5], start=1):
            name = display_name(
                row["full_name"].split()[0] if row["full_name"].strip() else "",
                " ".join(row["full_name"].split()[1:]),
                row["username"],
            )
            top_lines += msg.MONTHLY_REPORT_TOP_ENTRY.format(
                rank=i, name=name, days=row["days"]
            )

        if not top_lines:
            top_lines = "  لا توجد بيانات"

        text = msg.MONTHLY_REPORT_HEADER.format(
            month=month_name, year=""
        ).rstrip() + "\n" + msg.MONTHLY_REPORT_BODY.format(
            active_members=active_members,
            total_checkins=total_checkins,
            avg_participation=avg_pct,
            top_readers=top_lines,
        )

        await _send_safe(bot, group_id, text, parse_mode=MD)
        logger.info(
            "monthly_report_job: sent to group %d for %d/%d",
            group_id, prev_month, prev_year,
        )


# ---------------------------------------------------------------------------
# Job 5: Reminder (privately ping members who haven't checked in yet)
# ---------------------------------------------------------------------------

async def reminder_job(context) -> None:
    """
    Scheduled job: send private reminders to members who have not yet
    checked in today.

    Only runs if ENABLE_REMINDERS=true.
    Per-group settings can override the global toggle.
    Does NOT send messages publicly — only DMs.
    """
    if not settings.enable_reminders:
        return

    app: Application = context.application
    db = _get_db(app)
    bot: Bot = app.bot

    # Which reminder index is this (0-based, derived from job name set at registration)
    reminder_idx: int = (context.job.data or {}).get("reminder_idx", 0)

    for group_row in await db.get_all_active_groups():
        group_id: int = group_row["group_id"]
        group_settings = await db.get_settings(group_id)

        # Per-group reminder toggle (falls back to global setting)
        group_reminders = bool(
            group_settings["reminder_enabled"]
            if group_settings else settings.enable_reminders
        )
        if not group_reminders:
            continue

        tz = await _get_group_tz(group_settings)
        today = today_in_tz(tz)

        if await db.is_day_skipped(group_id, today):
            continue

        pending_users = await db.get_users_not_checked_in(group_id, today)
        if not pending_users:
            continue

        reminder_text = (
            msg.REMINDER_HEADER
            + "\n"
            + msg.get_reminder_text(reminder_idx)
        )

        sent_count = 0
        for user_row in pending_users:
            user_id: int = user_row["user_id"]
            ok = await _send_safe(bot, user_id, reminder_text, parse_mode=MD)
            if ok:
                sent_count += 1

        logger.info(
            "reminder_job[%d]: group %d — sent to %d/%d pending users",
            reminder_idx, group_id, sent_count, len(pending_users),
        )


# ---------------------------------------------------------------------------
# Job 6: Automatic backup
# ---------------------------------------------------------------------------

async def auto_backup_job(context) -> None:
    """Scheduled job: create an automatic database backup."""
    try:
        from config import settings
        from datetime import datetime
        
        # Check if today is the designated weekly backup day (e.g. Friday = 4)
        is_weekly = datetime.utcnow().weekday() == settings.weekly_report_day
        
        backup_path, checksum = await async_create_backup(
            settings.database_path,
            settings.backup_dir,
            settings.backup_retain,
            4, # retain 4 weekly backups
            is_weekly=is_weekly
        )
        logger.info("auto_backup_job: backup created at %s (Weekly: %s)", backup_path, is_weekly)
    except Exception as exc:
        logger.error("auto_backup_job failed: %s", exc)


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------

def register_jobs(app: Application) -> None:
    """
    Register all scheduled jobs with the Application's JobQueue.

    Times are read from settings and interpreted in the global timezone.
    Per-group timezone overrides are applied at runtime within each job.
    """
    jq = app.job_queue
    # Run health check every 4 hours
    jq.run_repeating(monitoring_job, interval=timedelta(hours=4), first=30, name="monitoring_job")
    tz = settings.timezone

    # ── Daily post ─────────────────────────────────────────────────────────
    post_h, post_m = parse_hhmm(settings.default_post_time)
    jq.run_daily(
        daily_post_job,
        time=_local_time(post_h, post_m, tz),
        name="daily_post",
    )
    logger.info("Scheduled daily_post at %s %s", settings.default_post_time, settings.timezone_str)

    # ── Daily report ───────────────────────────────────────────────────────
    report_h, report_m = parse_hhmm(settings.report_time)
    jq.run_daily(
        daily_report_job,
        time=_local_time(report_h, report_m, tz),
        name="daily_report",
    )
    logger.info("Scheduled daily_report at %s %s", settings.report_time, settings.timezone_str)

    # ── Weekly report (check daily; only acts on the right weekday) ────────
    if settings.enable_weekly_report:
        jq.run_daily(
            weekly_report_job,
            time=_local_time(23, 45, tz),
            name="weekly_report",
        )
        logger.info("Scheduled weekly_report (fires on weekday %d)", settings.weekly_report_day)

    # ── Monthly report (check daily; only acts on the 1st) ─────────────────
    jq.run_daily(
        monthly_report_job,
        time=_local_time(0, 30, tz),
        name="monthly_report",
    )

    # ── Reminders ──────────────────────────────────────────────────────────
    if settings.enable_reminders:
        for idx, time_str in enumerate(settings.reminder_times):
            try:
                r_h, r_m = parse_hhmm(time_str)
            except (ValueError, AttributeError):
                logger.warning("Invalid reminder time: %r — skipping", time_str)
                continue

            job_name = f"reminder_{idx}"
            job = jq.run_daily(
                reminder_job,
                time=_local_time(r_h, r_m, tz),
                name=job_name,
                data={"reminder_idx": idx},
            )
            logger.info(
                "Scheduled reminder[%d] at %s %s", idx, time_str, settings.timezone_str
            )

    # ── Auto backup ────────────────────────────────────────────────────────
    backup_h, backup_m = parse_hhmm(settings.backup_time)
    jq.run_daily(
        auto_backup_job,
        time=_local_time(backup_h, backup_m, tz),
        name="auto_backup",
    )
    logger.info("Scheduled auto_backup at %s %s", settings.backup_time, settings.timezone_str)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _local_time(hour: int, minute: int, tz: pytz.BaseTzInfo):
    """Return a timezone-aware time object for APScheduler."""
    import datetime as _dt
    return _dt.time(hour, minute, tzinfo=tz)
