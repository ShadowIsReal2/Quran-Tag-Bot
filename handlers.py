"""
handlers.py — All Telegram command and callback handlers.

Every user-facing interaction is routed through functions defined here.
The module is imported by bot.py which registers handlers with the Application.
"""

from __future__ import annotations

import io
import logging
import shutil
from pathlib import Path

import pytz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import messages as msg
from achievements import evaluate_user_achievements, evaluate_group_milestones
from config import settings
from backup import async_create_backup
from database import Database
from datetime import date, datetime
from keyboards import (
    CB_CANCEL,
    CB_CHECKIN,
    CB_CONFIRM_RESET,
    CB_FORCE_DAILY,
    CB_TOGGLE_REPORT,
    CB_TOGGLE_MILESTONES,
    CB_TOGGLE_WEEKLY,
    CB_TOGGLE_VERSE,
    CB_TOGGLE_HADITH,
    CB_TOGGLE_DUA,
    CB_TOGGLE_REMINDER,
    CB_TOGGLE_ANNOUNCE,
    CB_SET_JUZ,
    CB_TOGGLE_HIJRI,
    CB_GROUP_STATS,
    CB_MY_STATS,
    CB_PLAN_PREFIX,
    CB_SET_PLAN,
    CB_SET_POST_TIME,
    CB_SET_REPORT_TIME,
    CB_SET_REMINDER_TIME,
    CB_SET_TIMEZONE,
    CB_SETTINGS_MENU,
    CB_ADMIN_SELECT_GROUP,
    CB_ADMIN_SELECT_GROUP_ONLY,
    CB_SKIP_DAY,
    CB_RESET_MONTH,
    CB_MENU_HELP,
    CB_MENU_LEADERBOARD,
    CB_MENU_ACHIEVEMENTS,
    CB_MENU_STATS,
    CB_MENU_MAIN,
    CB_HELP_GENERAL,
    CB_HELP_ADMIN,
    CB_HELP_STATS,
    CB_HELP_ACHIEVE,
    CB_LB_CURRENT,
    CB_LB_TOTAL,
    CB_LB_MONTH,
    CB_CONFIRM_SKIP_DAY,
    CB_CONFIRM_RESET_MEMBER,
    back_keyboard,
    confirm_keyboard,
    daily_post_keyboard,
    help_category_keyboard,
    leaderboard_keyboard,
    main_menu_keyboard,
    nav_main_menu_keyboard,
    confirmation_keyboard,
    reading_plan_keyboard,
    settings_main_keyboard,
)
from messages import get_daily_motivation, get_streak_badge
from utils import (

    build_stats_text,
    compute_completion_pct,
    display_name,
    escape_markdown,
    estimate_juz_from_checkins,
    estimate_pages_from_checkins,
    compute_avg_per_week,
    format_date_arabic,
    format_month_arabic,
    get_reading_for_today,
    is_admin,
    parse_hhmm,
    pick_daily_dua,
    pick_random_hadith,
    pick_random_verse,
    today_in_tz,
    chunk_text,
    days_in_month,
)

logger = logging.getLogger(__name__)
MD = ParseMode.MARKDOWN

# Context key used to track pending settings updates
_PENDING_KEY = "pending_setting"
_PENDING_GROUP = "pending_group"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _group_tz(group_settings) -> pytz.BaseTzInfo:
    tz_str = group_settings["timezone"] if group_settings else settings.timezone_str
    try:
        return pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        return settings.timezone


def _is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


async def _send_safe(update: Update, text: str, **kwargs):
    """Reply gracefully, splitting at 4096 chars if needed."""
    for chunk in chunk_text(text, max_len=4000):
        try:
            await update.effective_message.reply_text(chunk, **kwargs)
        except Exception as exc:
            logger.warning("_send_safe failed: %s", exc)


async def _delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the user's command message to keep the group clean.
    Never deletes messages triggered by inline keyboard callbacks
    (e.g. tapping buttons on the daily post)."""
    if _is_group(update) and not update.callback_query:
        try:
            await update.effective_message.delete()
            logger.debug("Deleted command message in group %d", update.effective_chat.id)
        except Exception as exc:
            logger.warning("Could not delete command in group %d: %s", update.effective_chat.id, exc)


async def _reply_dm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int | None = None,
    group_id: int | None = None,
    **kwargs,
) -> bool:
    """
    Send a response to the user's DM instead of the group.
    If the command was invoked in a group, the user's message is deleted silently.

    Returns True if the DM was sent successfully, False otherwise.
    """
    from telegram.error import Forbidden

    uid = user_id or update.effective_user.id

    # If in a group, delete the command message first
    await _delete_cmd(update, context)

    try:
        await context.bot.send_message(chat_id=uid, text=text, **kwargs)
        return True
    except Forbidden:
        if update.callback_query:
            await update.callback_query.answer(
                "⚠️ يرجى مراسلة البوت على الخاص للاطلاع على إحصائياتك.",
                show_alert=True,
            )
        return False
    except Exception as exc:
        logger.warning("_reply_dm failed for user %d: %s", uid, exc)
        return False


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register the group and greet members."""
    if not update.effective_message:
        return

    db = _db(context)
    chat = update.effective_chat
    user = update.effective_user

    if _is_group(update):
        await db.upsert_group(chat.id, chat.title or "")
        if user:
            await db.upsert_user(
                user.id, chat.id,
                user.first_name, user.last_name or "", user.username or ""
            )
        await _delete_cmd(update, context)
        ok = await _reply_dm(update, context, msg.START_GROUP, parse_mode=MD)
        if not ok:
            bot_user = await context.bot.get_me()
            link = f"https://t.me/{bot_user.username}"
            await _send_safe(update,
                f"⚠️ يرجى مراسلة البوت على الخاص للبدء:\n{link}",
                parse_mode=MD,
            )
    else:
        await update.effective_message.reply_text(
            msg.MAIN_MENU, parse_mode=MD, reply_markup=main_menu_keyboard()
        )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help categories — sent privately."""
    text = msg.HELP_CATEGORY_PROMPT
    await _reply_dm(update, context, text, parse_mode=MD, reply_markup=help_category_keyboard())


# ---------------------------------------------------------------------------
# /menu  — show main menu
# ---------------------------------------------------------------------------

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the rich main menu."""
    await _reply_dm(update, context, msg.MAIN_MENU, parse_mode=MD, reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# /checkin  — check in without finding the daily post
# ---------------------------------------------------------------------------

async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check in for today's reading."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    db = _db(context)

    if chat.type == "private":
        await _reply_dm(update, context, msg.CHECKIN_NOT_IN_GROUP, parse_mode=MD)
        return

    today = today_in_tz(await _group_tz(await db.get_settings(chat.id)))

    await db.upsert_group(chat.id, chat.title or "")
    await db.upsert_user(user.id, chat.id, user.first_name, user.last_name or "", user.username or "")
    is_new = await db.checkin(user.id, chat.id, today)

    if not is_new:
        await _reply_dm(update, context, msg.CHECKIN_ALREADY, parse_mode=MD)
        return

    await _delete_cmd(update, context)
    await update.effective_message.reply_text(msg.CHECKIN_OK_TOAST)


# ---------------------------------------------------------------------------
# /leaderboard  — show leaderboard
# ---------------------------------------------------------------------------

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show leaderboard — sent privately."""
    await _reply_dm(update, context, msg.LEADERBOARD_HEADER + msg.LEADERBOARD_NO_DATA,
                    parse_mode=MD, reply_markup=leaderboard_keyboard())


# ---------------------------------------------------------------------------
# /me  — expanded personal statistics
# ---------------------------------------------------------------------------

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show personal statistics — sent privately to avoid flooding the group."""
    user = update.effective_user
    if not user:
        return
    db = _db(context)
    chat = update.effective_chat

    target_group = chat.id
    if chat.type == "private":
        target_group = context.user_data.get("admin_group_id")
        if not target_group:
            await _send_safe(update, "⚠️ اختر مجموعة أولاً عبر /settings.", parse_mode=MD)
            return
    else:
        await db.upsert_group(target_group, chat.title or "")

    await db.upsert_user(user.id, target_group, user.first_name, user.last_name or "", user.username or "")

    group_settings = await db.get_settings(target_group)
    tz = await _group_tz(group_settings)
    today = today_in_tz(tz)
    plan_key = group_settings["plan_key"] if group_settings else "1_juz_day"
    use_hijri = bool(group_settings["use_hijri_date"]) if group_settings else False

    streak_row = await db.get_streak(user.id, target_group)
    user_row   = await db.get_user(user.id, target_group)

    if not streak_row or not user_row:
        await _reply_dm(update, context, msg.STATS_NO_DATA, parse_mode=MD)
        return

    current_streak = streak_row["current_streak"]
    longest_streak = streak_row["longest_streak"]
    this_month     = await db.count_checkins_this_month(user.id, target_group, today.year, today.month)
    this_year      = await db.count_checkins_this_year(user.id, target_group, today.year)
    total          = await db.count_checkins_total(user.id, target_group)
    m_days         = days_in_month(today.year, today.month)

    # Extended stats
    total_juz   = estimate_juz_from_checkins(total, plan_key)
    total_pages = estimate_pages_from_checkins(total, plan_key)

    first_checkin = await db.get_first_checkin_date(user.id, target_group)
    avg_per_week  = compute_avg_per_week(total, first_checkin)

    best_year, best_month_num, best_count = await db.get_best_month(user.id, target_group)
    if best_year:
        best_month_str = f"{format_month_arabic(best_year, best_month_num)} ({best_count} يوم)"
    else:
        best_month_str = "—"

    joined_str = user_row["joined_at"]
    try:
        joined_dt  = datetime.fromisoformat(joined_str)
        joined_disp = format_date_arabic(joined_dt.date(), hijri=use_hijri)
    except (ValueError, TypeError):
        joined_disp = "—"

    name = display_name(user.first_name, user.last_name or "", user.username or "")

    text = build_stats_text(
        name=name,
        joined=joined_disp,
        current_streak=current_streak,
        longest_streak=longest_streak,
        this_month=this_month,
        month_days=m_days,
        this_year=this_year,
        total=total,
        total_juz=total_juz,
        total_pages=total_pages,
        avg_per_week=avg_per_week,
        best_month_str=best_month_str,
    )

    # Show earned achievements
    achievements = await db.get_user_achievements(user.id, target_group)
    if achievements:
        badge_lines = []
        for row in achievements:
            info = msg.ACHIEVEMENT_MAP.get(row["achievement_key"])
            if info:
                badge_lines.append(f"{info[0]} {info[1]}")
        if badge_lines:
            text += "\n\n🏅 *إنجازاتك:*\n" + " | ".join(badge_lines)

    # Always send stats to DM — no group messages
    await _delete_cmd(update, context)
    await _reply_dm(update, context, text, parse_mode=MD)


# ---------------------------------------------------------------------------
# /stats  — group statistics
# ---------------------------------------------------------------------------

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's group participation stats — sent privately."""
    db = _db(context)
    chat = update.effective_chat

    target_group = chat.id
    if chat.type == "private":
        target_group = context.user_data.get("admin_group_id")
        if not target_group:
            await _send_safe(update, "⚠️ اختر مجموعة أولاً عبر /settings.", parse_mode=MD)
            return
    else:
        await db.upsert_group(target_group, chat.title or "")

    await _delete_cmd(update, context)
    group_settings = await db.get_settings(target_group)
    tz = await _group_tz(group_settings)
    today = today_in_tz(tz)
    use_hijri = bool(group_settings["use_hijri_date"]) if group_settings else False

    active_users = await db.get_active_users(target_group)
    checked_ids  = set(await db.get_who_checked_in(target_group, today))
    total     = len(active_users)
    confirmed = len(checked_ids)
    pct       = compute_completion_pct(confirmed, total)

    text = msg.DAILY_REPORT_HEADER + "\n" + msg.DAILY_REPORT_BODY.format(
        date=format_date_arabic(today, hijri=use_hijri),
        confirmed=confirmed,
        pending=total - confirmed,
        pct=pct,
    )
    await _reply_dm(update, context, text, parse_mode=MD)


# ---------------------------------------------------------------------------
# /daily  — send today's post manually
# ---------------------------------------------------------------------------

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger today's daily post in the target group."""
    db = _db(context)
    chat = update.effective_chat

    target_group = chat.id
    if chat.type == "private":
        target_group = context.user_data.get("admin_group_id")
        if not target_group:
            await _send_safe(update, "⚠️ اختر مجموعة أولاً عبر /settings.", parse_mode=MD)
            return
    else:
        await db.upsert_group(target_group, chat.title or "")

    await _delete_cmd(update, context)
    group_settings = await db.get_settings(target_group)
    tz = await _group_tz(group_settings)
    today = today_in_tz(tz)

    plan_key    = group_settings["plan_key"]     if group_settings else "1_juz_day"
    custom_text = group_settings["custom_reading"] if group_settings else ""
    raw_start   = (group_settings["reading_start"] or "") if group_settings else ""
    start_date  = date.fromisoformat(raw_start) if raw_start else None
    curr_day    = int(group_settings["reading_current_day"]) if group_settings else -1
    use_hijri   = bool(group_settings["use_hijri_date"]) if group_settings else False
    reading     = get_reading_for_today(plan_key, custom_text, today, start_date, curr_day)
    date_str    = format_date_arabic(today, hijri=use_hijri)

    day_seed   = today.timetuple().tm_yday
    last_index = await db.get_last_motivation_index(target_group)
    motivation = get_daily_motivation(day_seed, last_index)

    text = (
        msg.DAILY_POST_HEADER + "\n\n"
        + msg.DAILY_POST_BODY.format(date=date_str, reading=reading, motivation=motivation)
    )

    def _gs(key: str) -> bool:
        return bool(group_settings[key]) if group_settings else False

    if _gs("daily_verse_enabled"):
        text += msg.DAILY_POST_WITH_VERSE.format(verse=pick_random_verse())
    if _gs("daily_dua_enabled"):
        text += msg.DAILY_POST_WITH_DUA.format(dua=pick_daily_dua())
    if _gs("daily_hadith_enabled"):
        text += msg.DAILY_POST_WITH_HADITH.format(hadith=pick_random_hadith())

    await context.bot.send_message(
        chat_id=target_group,
        text=text,
        parse_mode=MD,
        reply_markup=daily_post_keyboard(),
    )
    await _reply_dm(update, context, "✅ تم إرسال الورد اليومي إلى المجموعة.", parse_mode=MD)


# ---------------------------------------------------------------------------
# /report
# ---------------------------------------------------------------------------

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_stats(update, context)


# ---------------------------------------------------------------------------
# /missing  — admin only
# ---------------------------------------------------------------------------

async def cmd_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a private list of members who haven't checked in (admin only)."""
    db = _db(context)
    chat = update.effective_chat
    user = update.effective_user

    target_group = chat.id
    if chat.type == "private":
        target_group = context.user_data.get("admin_group_id")
        if not target_group:
            await _send_safe(update, "⚠️ اختر مجموعة أولاً عبر /settings.", parse_mode=MD)
            return
    else:
        await db.upsert_group(target_group, chat.title or "")

    if not await is_admin(update, context):
        await _reply_dm(update, context, msg.ADMIN_ONLY, parse_mode=MD)
        return

    await _delete_cmd(update, context)
    group_settings = await db.get_settings(target_group or chat.id)
    tz = await _group_tz(group_settings)
    today = today_in_tz(tz)

    pending_users = await db.get_users_not_checked_in(target_group or chat.id, today)

    if not pending_users:
        text = msg.MISSING_NONE
    else:
        lines = [msg.MISSING_HEADER]
        for u in pending_users:
            name = display_name(u["first_name"], u["last_name"], u["username"])
            lines.append(msg.MISSING_ENTRY.format(name=name))
        text = "\n".join(lines)

    await _reply_dm(update, context, text, parse_mode=MD)


# ---------------------------------------------------------------------------
# /settings  — admin only
# ---------------------------------------------------------------------------

async def show_group_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: int) -> None:
    """Render the settings panel for a specific group."""
    db = _db(context)
    group_settings = await db.get_settings(group_id)

    if not group_settings:
        await _send_safe(update, msg.ERROR_DB, parse_mode=MD)
        return

    plan_name = msg.PLAN_LABELS.get(group_settings["plan_key"], group_settings["plan_key"])

    # Fetch group title to show in the header
    group_title = "المجموعة"
    groups = await db.get_all_active_groups()
    for g in groups:
        if g["group_id"] == group_id:
            group_title = g["title"]
            break

    def _gs(key: str) -> bool:
        return bool(group_settings[key]) if group_settings else False

    report_enabled       = _gs("report_enabled")
    milestones_enabled   = _gs("milestones_enabled")
    weekly_report_enabled = _gs("weekly_report_enabled")
    daily_verse_enabled   = _gs("daily_verse_enabled")
    daily_hadith_enabled  = _gs("daily_hadith_enabled")
    daily_dua_enabled     = _gs("daily_dua_enabled")
    reminder_enabled      = _gs("reminder_enabled")
    announce_badges       = _gs("announce_badges")
    use_hijri_date        = _gs("use_hijri_date")

    text = f"⚙️ *إعدادات: {escape_markdown(group_title)}*\n\n" + msg.SETTINGS_BODY.format(
        post_time=group_settings["post_time"],
        report_time=group_settings["report_time"],
        timezone=group_settings["timezone"],
        plan_name=plan_name,
    )

    kb = settings_main_keyboard(
        report_enabled=report_enabled,
        milestones_enabled=milestones_enabled,
        weekly_report_enabled=weekly_report_enabled,
        daily_verse_enabled=daily_verse_enabled,
        daily_hadith_enabled=daily_hadith_enabled,
        daily_dua_enabled=daily_dua_enabled,
        reminder_enabled=reminder_enabled,
        announce_badges=announce_badges,
        use_hijri_date=use_hijri_date,
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode=MD, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, parse_mode=MD, reply_markup=kb)

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the settings panel for group admins in DM."""
    if _is_group(update):
        # We want to encourage DM settings, but we still allow fetching if they are admin,
        # OR we restrict entirely. The prompt says: "Configuration should happen privately."
        bot_username = (await context.bot.get_me()).username
        dm_link = f"https://t.me/{bot_username}?start=settings"
        await _send_safe(update, f"⚠️ يرجى إدارة الإعدادات عبر المحادثة الخاصة مع البوت:\n{dm_link}", parse_mode=MD)
        await _delete_cmd(update, context)
        return

    # In DM: list groups they are admin of
    db = _db(context)
    user_id = update.effective_user.id
    active_groups = await db.get_all_active_groups()
    admin_groups = []

    # If it's a callback, we can answer it early
    if update.callback_query:
        await update.callback_query.answer("جاري جلب المجموعات...")
    else:
        # Show a typing action or temporary message because this might take a second
        temp_msg = await update.effective_message.reply_text("⏳ جاري البحث عن مجموعاتك...")
    
    for row in active_groups:
        g_id = row["group_id"]
        title = row["title"]
        try:
            member = await context.bot.get_chat_member(g_id, user_id)
            if member.status in ("administrator", "creator"):
                admin_groups.append((g_id, title))
        except Exception:
            pass # Bot might not be in the group anymore, or user not found
            
    if update.callback_query:
        pass
    else:
        await temp_msg.delete()

    if not admin_groups:
        await _send_safe(update, "⚠️ لم يتم العثور على أي مجموعات تشرف عليها.", parse_mode=MD)
        return

    from keyboards import admin_groups_keyboard
    text = "📋 *إدارة المجموعات*\n\nاختر المجموعة التي تود تعديل إعداداتها:"
    reply_markup = admin_groups_keyboard(admin_groups)

    if update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode=MD, reply_markup=reply_markup)
    else:
        await _send_safe(update, text, parse_mode=MD, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# /group  — choose which group to manage
# ---------------------------------------------------------------------------

async def cmd_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Choose which group to manage (stores selection for menu commands)."""
    if _is_group(update):
        bot_username = (await context.bot.get_me()).username
        dm_link = f"https://t.me/{bot_username}?start=group"
        await _send_safe(update, f"⚠️ استخدم هذا الأمر في المحادثة الخاصة:\n{dm_link}", parse_mode=MD)
        await _delete_cmd(update, context)
        return

    db = _db(context)
    user_id = update.effective_user.id
    active_groups = await db.get_all_active_groups()
    admin_groups = []

    temp_msg = await update.effective_message.reply_text("⏳ جاري البحث عن مجموعاتك...")
    for row in active_groups:
        g_id = row["group_id"]
        title = row["title"]
        try:
            member = await context.bot.get_chat_member(g_id, user_id)
            if member.status in ("administrator", "creator"):
                admin_groups.append((g_id, title))
        except Exception:
            pass
    await temp_msg.delete()

    if not admin_groups:
        await _send_safe(update, "⚠️ لم يتم العثور على أي مجموعات تشرف عليها.", parse_mode=MD)
        return

    from keyboards import admin_groups_keyboard
    text = "📋 *اختر المجموعة*\n\nاختر المجموعة التي تود إدارتها:"
    reply_markup = admin_groups_keyboard(admin_groups, go_to_settings=False)
    await _send_safe(update, text, parse_mode=MD, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# /readingplan  — admin only
# ---------------------------------------------------------------------------

async def cmd_readingplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show and allow changing the reading plan."""
    if _is_group(update):
        bot_username = (await context.bot.get_me()).username
        dm_link = f"https://t.me/{bot_username}?start=settings"
        await _send_safe(update, f"⚠️ يرجى إدارة الإعدادات عبر المحادثة الخاصة مع البوت:\n{dm_link}", parse_mode=MD)
        await _delete_cmd(update, context)
        return
        
    await cmd_settings(update, context)


# ---------------------------------------------------------------------------
# /backup  — admin only
# ---------------------------------------------------------------------------

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the Backup Settings menu in DM."""
    if update.effective_chat.type != "private":
        await _send_safe(update, "⚠️ يرجى استخدام هذا الأمر في المحادثة الخاصة فقط.", parse_mode=MD)
        return

    if update.effective_user.id not in settings.bot_admins:
        await _send_safe(update, msg.ADMIN_ONLY, parse_mode=MD)
        return

    from keyboards import admin_backup_menu_keyboard
    await _send_safe(update, msg.BACKUP_MENU_TEXT, parse_mode=MD, reply_markup=admin_backup_menu_keyboard())


# ---------------------------------------------------------------------------
# /restore  — admin only
# ---------------------------------------------------------------------------

async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt the admin to upload a .db file for restore."""
    if update.effective_chat.type != "private":
        await _send_safe(update, "⚠️ يرجى استخدام هذا الأمر في المحادثة الخاصة فقط.", parse_mode=MD)
        return

    if update.effective_user.id not in settings.bot_admins:
        await _send_safe(update, msg.ADMIN_ONLY, parse_mode=MD)
        return

    # Store a flag so handle_restore_document knows to proceed
    context.user_data["awaiting_restore"] = update.effective_chat.id
    await _send_safe(update, msg.BACKUP_RESTORE_PROMPT_DM, parse_mode=MD)


async def handle_restore_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle an uploaded .db file as a database restore."""
    if not context.user_data.get("awaiting_restore"):
        return

    if update.effective_user.id not in settings.bot_admins:
        return

    doc = update.effective_message.document
    if not doc or not doc.file_name.endswith(".db"):
        await _send_safe(update, msg.RESTORE_FAIL, parse_mode=MD)
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        tmp_path = Path("data/restore_tmp.db")
        await file.download_to_drive(tmp_path)

        # Validate it's a SQLite file
        with open(tmp_path, "rb") as f:
            header = f.read(16)
        if not header.startswith(b"SQLite format 3"):
            tmp_path.unlink(missing_ok=True)
            await _send_safe(update, msg.RESTORE_FAIL, parse_mode=MD)
            return

        # Replace current database
        shutil.copy2(tmp_path, settings.database_path)
        tmp_path.unlink(missing_ok=True)

        del context.user_data["awaiting_restore"]
        await _send_safe(update, msg.RESTORE_SUCCESS, parse_mode=MD)
        logger.info("Database restored by user %d", update.effective_user.id)
    except Exception as exc:
        logger.error("Restore failed: %s", exc)
        await _send_safe(update, msg.RESTORE_FAIL, parse_mode=MD)


# ---------------------------------------------------------------------------
# /export  — admin only
# ---------------------------------------------------------------------------

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export group check-ins as a CSV document."""
    if update.effective_chat.type != "private":
        bot_username = (await context.bot.get_me()).username
        dm_link = f"https://t.me/{bot_username}?start=export"
        await _send_safe(update, f"⚠️ يرجى استخدام هذا الأمر عبر المحادثة الخاصة:\n{dm_link}", parse_mode=MD)
        return

    db = _db(context)
    # Check if a group is currently selected in admin DM
    target_group_id = context.user_data.get("admin_group_id")
    if not target_group_id:
        await _send_safe(update, "⚠️ يرجى فتح إعدادات مجموعة أولاً عبر الأمر /settings لاختيار المجموعة.", parse_mode=MD)
        return
        
    try:
        csv_data = await db.export_group_csv(target_group_id)
        bio = io.BytesIO(csv_data.encode("utf-8-sig")) # Use UTF-8 with BOM for Excel Arabic support
        bio.name = f"checkins_{target_group_id}_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        await context.bot.send_document(
            chat_id=update.effective_user.id,
            document=bio,
            filename=bio.name,
            caption=msg.EXPORT_CAPTION.format(date=datetime.utcnow().strftime("%Y-%m-%d")),
        )
    except Exception as exc:
        logger.error("Export failed: %s", exc)
        await _send_safe(update, msg.ERROR_GENERIC, parse_mode=MD)


# ---------------------------------------------------------------------------
# /reset_member  — admin only
# ---------------------------------------------------------------------------

async def cmd_reset_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset a specific member's history. Usage: /reset_member @username"""
    db = _db(context)
    chat = update.effective_chat
    user = update.effective_user

    target_group = chat.id
    if chat.type == "private":
        target_group = context.user_data.get("admin_group_id")
        if not target_group:
            await _send_safe(update, "⚠️ اختر مجموعة أولاً عبر /settings.", parse_mode=MD)
            return
    else:
        await db.upsert_group(target_group, chat.title or "")

    if not await is_admin(update, context):
        await _reply_dm(update, context, msg.ADMIN_ONLY, parse_mode=MD)
        return

    await _delete_cmd(update, context)

    args = context.args
    if not args:
        await _reply_dm(update, context, msg.RESET_MEMBER_PROMPT, parse_mode=MD)
        return

    username = args[0]
    user_row = await db.find_user_by_username(username, target_group)
    if not user_row:
        await _reply_dm(update, context, f"⚠️ المستخدم {username} غير موجود في سجلات المجموعة.", parse_mode=MD)
        return

    await db.reset_user(user_row["user_id"], target_group)
    name = display_name(user_row["first_name"], user_row["last_name"], user_row["username"])
    await _reply_dm(update, context, msg.RESET_MEMBER_SUCCESS.format(name=name), parse_mode=MD)


# ---------------------------------------------------------------------------
# /reset_month  — admin only
# ---------------------------------------------------------------------------

async def cmd_reset_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all check-ins for the current month."""
    db = _db(context)
    chat = update.effective_chat

    target_group = chat.id
    if chat.type == "private":
        target_group = context.user_data.get("admin_group_id")
        if not target_group:
            await _send_safe(update, "⚠️ اختر مجموعة أولاً عبر /settings.", parse_mode=MD)
            return
    else:
        await db.upsert_group(target_group, chat.title or "")

    if not await is_admin(update, context):
        await _reply_dm(update, context, msg.ADMIN_ONLY, parse_mode=MD)
        return

    await _delete_cmd(update, context)

    # Send confirmation dialog privately — the callback handler needs
    # to know the target_group_id via context.user_data["admin_group_id"]
    if chat.type == "private":
        context.user_data["admin_group_id"] = target_group

    await _reply_dm(
        update, context,
        "⚠️ هل أنت متأكد من إعادة ضبط إحصائيات الشهر الحالي؟",
        parse_mode=MD,
        reply_markup=confirm_keyboard(CB_CONFIRM_RESET),
    )


# ---------------------------------------------------------------------------
# /version
# ---------------------------------------------------------------------------

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot version and database statistics."""
    db = _db(context)
    groups = await db.count_groups()
    users  = await db.count_users()
    text = msg.VERSION_TEXT.format(
        version=settings.bot_version,
        db_path=str(settings.database_path),
        groups=groups,
        users=users,
    )
    await _reply_dm(update, context, text, parse_mode=MD)


# ---------------------------------------------------------------------------
# Group membership events
# ---------------------------------------------------------------------------

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register group and new members when they join."""
    db = _db(context)
    chat = update.effective_chat

    await db.upsert_group(chat.id, chat.title or "")

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await db.upsert_user(
            member.id, chat.id,
            member.first_name, member.last_name or "", member.username or ""
        )
        logger.info("New member registered: user=%d group=%d", member.id, chat.id)

    # Delete the join message
    try:
        await update.message.delete()
    except Exception as exc:
        logger.warning("Could not delete join message: %s", exc)

    # If the bot itself was added, greet the group
    bot_user = await context.bot.get_me()
    for member in update.message.new_chat_members:
        if member.id == bot_user.id:
            await update.message.reply_text(msg.START_GROUP, parse_mode=MD)


async def handle_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deactivate user or group when they leave."""
    db = _db(context)
    chat = update.effective_chat
    member = update.message.left_chat_member

    if member:
        bot_user = await context.bot.get_me()
        if member.id == bot_user.id:
            await db.deactivate_group(chat.id)
            logger.info("Deactivated group %d (bot removed)", chat.id)

    # Delete the leave message
    try:
        await update.message.delete()
    except Exception as exc:
        logger.warning("Could not delete leave message: %s", exc)


# ---------------------------------------------------------------------------
# Inline callback dispatcher
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch inline keyboard button presses."""
    query = update.callback_query
    # NOTE: do NOT call query.answer() here — each branch answers exactly once.
    # Telegram allows only one answer per callback query; a blanket answer()
    # at the top would silently consume the quota before branches run.

    data: str = query.data or ""
    chat = update.effective_chat
    user = update.effective_user
    db = _db(context)

    # Determine target group for admin commands
    target_group_id = chat.id
    if chat.type == "private":
        target_group_id = context.user_data.get("admin_group_id")

    async def _verify_admin() -> bool:
        if not target_group_id:
            return False
        try:
            member = await context.bot.get_chat_member(target_group_id, user.id)
            return member.status in ("administrator", "creator")
        except Exception:
            return False

    # ── Admin Backup Operations ──────────────────────────────────────────
    if data.startswith("admin_backup_"):
        await handle_backup_callback(update, context)
        return

    # ── Admin Select Group ───────────────────────────────────────────────
    if data.startswith(CB_ADMIN_SELECT_GROUP):
        group_id = int(data.split(":")[1])
        context.user_data["admin_group_id"] = group_id
        await query.answer()
        await show_group_settings(update, context, group_id)
        return

    if data.startswith(CB_ADMIN_SELECT_GROUP_ONLY):
        group_id = int(data.split(":")[1])
        context.user_data["admin_group_id"] = group_id
        await query.answer()
        await query.message.edit_text(
            f"✅ تم تحديد المجموعة. يمكنك الآن استخدام القائمة الرئيسية.\n\n"
            f"لعرض الإعدادات اضغط على زر ⚙️ الإعدادات في القائمة.",
            parse_mode=MD,
        )
        return

    # ── Check-in button ───────────────────────────────────────────────────
    if data == CB_CHECKIN:
        if not _is_group(update):
            await query.answer("⚠️ هذا الزر يعمل في المجموعات فقط.", show_alert=True)
            return

        group_settings = await db.get_settings(chat.id)
        tz = await _group_tz(group_settings)
        today = today_in_tz(tz)
        use_hijri = bool(group_settings["use_hijri_date"]) if group_settings else False

        await db.upsert_group(chat.id, chat.title or "")
        await db.upsert_user(
            user.id, chat.id,
            user.first_name, user.last_name or "", user.username or ""
        )

        is_new = await db.checkin(user.id, chat.id, today)

        if not is_new:
            # Already checked in today — silent toast, nothing in the group
            await query.answer(msg.CHECKIN_TOAST_REPEAT, show_alert=False)
            return

        logger.info("Check-in: user=%d group=%d date=%s", user.id, chat.id, today)

        # Build the private DM text with current streak
        streak_row = await db.get_streak(user.id, chat.id)
        current_streak = streak_row["current_streak"] if streak_row else 1
        badge = get_streak_badge(current_streak)
        badge_line = f"\n{badge}" if badge else ""

        dm_text = msg.CHECKIN_SUCCESS_DM.format(
            date=format_date_arabic(today, hijri=use_hijri),
            streak=current_streak,
            badge=badge_line,
        )

        # Try sending DM FIRST, then answer the callback with the appropriate response
        dm_ok = False
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=dm_text,
                parse_mode=MD,
            )
            dm_ok = True
        except Exception as exc:
            logger.warning("Check-in DM failed for user %d: %s", user.id, exc)

        if dm_ok:
            await query.answer(msg.CHECKIN_TOAST_NEW, show_alert=False)
        else:
            await query.answer(
                "⚠️ يرجى مراسلة البوت على الخاص للاطلاع على إحصائياتك.",
                show_alert=True,
            )

        # Evaluate achievements and milestones
        plan_key = group_settings["plan_key"] if group_settings else "1_juz_day"

        announce = settings.announce_badges

        try:
            await evaluate_user_achievements(
                context.bot, db, user.id, chat.id, announce_badges=announce
            )
        except Exception as exc:
            logger.error("evaluate_user_achievements crashed: %s", exc)
        try:
            await evaluate_group_milestones(
                context.bot, db, chat.id, plan_key=plan_key
            )
        except Exception as exc:
            logger.error("evaluate_group_milestones crashed: %s", exc)

    # ── My stats button ───────────────────────────────────────────────────
    elif data == CB_MY_STATS:
        await query.answer()
        await cmd_me(update, context)

    # ── Group stats button ────────────────────────────────────────────────
    elif data == CB_GROUP_STATS:
        await query.answer()
        await cmd_stats(update, context)

    # ── Settings menu ─────────────────────────────────────────────────────
    elif data == CB_SETTINGS_MENU:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        if not target_group_id:
            await cmd_settings(update, context)
        else:
            await show_group_settings(update, context, target_group_id)

    elif data == CB_SET_POST_TIME:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        context.user_data[_PENDING_KEY] = "post_time"
        context.user_data[_PENDING_GROUP] = target_group_id
        await query.message.reply_text(
            "⏰ أرسل وقت الورد اليومي الجديد بصيغة HH:MM (مثال: 08:30)",
            parse_mode=MD,
        )

    elif data == CB_SET_REPORT_TIME:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        context.user_data[_PENDING_KEY] = "report_time"
        context.user_data[_PENDING_GROUP] = target_group_id
        await query.message.reply_text(
            "🕙 أرسل وقت تقرير المساء الجديد بصيغة HH:MM (مثال: 22:00)",
            parse_mode=MD,
        )

    elif data == CB_SET_REMINDER_TIME:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        context.user_data[_PENDING_KEY] = "reminder_times"
        context.user_data[_PENDING_GROUP] = target_group_id
        await query.message.reply_text(
            "🔔 أرسل وقت التذكير بصيغة HH:MM (مثال: 20:00)\n"
            "لإضافة عدة أوقات افصل بينها بفاصلة: 18:00,20:00,22:00",
            parse_mode=MD,
        )

    elif data == CB_SET_TIMEZONE:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        context.user_data[_PENDING_KEY] = "timezone"
        context.user_data[_PENDING_GROUP] = target_group_id
        await query.message.reply_text(
            "🌍 أرسل اسم المنطقة الزمنية (مثال: Asia/Riyadh أو Africa/Cairo)",
            parse_mode=MD,
        )

    elif data == CB_SET_PLAN:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        plans = await db.get_all_plans()
        await query.message.reply_text(
            msg.READING_PLAN_HEADER,
            parse_mode=MD,
            reply_markup=reading_plan_keyboard([dict(p) for p in plans]),
        )

    elif data.startswith(CB_PLAN_PREFIX):
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        plan_key = data[len(CB_PLAN_PREFIX):]
        await db.update_setting(target_group_id, "plan_key", plan_key)

        await query.answer()
        if plan_key == "custom":
            context.user_data[_PENDING_KEY] = "custom_reading"
            context.user_data[_PENDING_GROUP] = target_group_id
            await query.message.reply_text(msg.READING_PLAN_CUSTOM_PROMPT, parse_mode=MD)
        else:
            plan_name = msg.PLAN_LABELS.get(plan_key, plan_key)
            group_settings = await db.get_settings(target_group_id)
            tz = await _group_tz(group_settings)
            today = today_in_tz(tz)
            raw_start  = (group_settings["reading_start"] or "") if group_settings else ""
            start_date = date.fromisoformat(raw_start) if raw_start else None
            curr_day   = int(group_settings["reading_current_day"]) if group_settings else -1
            reading    = get_reading_for_today(plan_key, "", today, start_date, curr_day)
            await query.message.reply_text(
                msg.READING_PLAN_SELECTED.format(plan_name=plan_name, reading=reading),
                parse_mode=MD,
            )

    elif data == CB_SET_JUZ:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        context.user_data[_PENDING_KEY] = "reading_current_day"
        context.user_data[_PENDING_GROUP] = target_group_id
        await query.message.reply_text(
            "📖 أرسل رقم الجزء (1-30)، أو 0 للإلغاء والعودة تلقائياً:",
            parse_mode=MD,
        )

    elif data in (
        CB_TOGGLE_REPORT, CB_TOGGLE_MILESTONES, CB_TOGGLE_WEEKLY,
        CB_TOGGLE_VERSE, CB_TOGGLE_HADITH, CB_TOGGLE_DUA,
        CB_TOGGLE_REMINDER, CB_TOGGLE_ANNOUNCE, CB_TOGGLE_HIJRI,
    ):
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        gs = await db.get_settings(target_group_id)
        col = {
            CB_TOGGLE_REPORT:    "report_enabled",
            CB_TOGGLE_MILESTONES: "milestones_enabled",
            CB_TOGGLE_WEEKLY:    "weekly_report_enabled",
            CB_TOGGLE_VERSE:     "daily_verse_enabled",
            CB_TOGGLE_HADITH:    "daily_hadith_enabled",
            CB_TOGGLE_DUA:       "daily_dua_enabled",
            CB_TOGGLE_REMINDER:  "reminder_enabled",
            CB_TOGGLE_ANNOUNCE:  "announce_badges",
            CB_TOGGLE_HIJRI:     "use_hijri_date",
        }[data]
        current = bool(gs[col]) if gs else False
        await db.update_setting(target_group_id, col, "0" if current else "1")
        await query.answer()
        await show_group_settings(update, context, target_group_id)

    elif data == CB_FORCE_DAILY:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        
        # Manually trigger daily post in target_group_id
        group_settings = await db.get_settings(target_group_id)
        tz = await _group_tz(group_settings)
        today = today_in_tz(tz)

        plan_key    = group_settings["plan_key"]     if group_settings else "1_juz_day"
        custom_text = group_settings["custom_reading"] if group_settings else ""
        raw_start   = (group_settings["reading_start"] or "") if group_settings else ""
        start_date  = date.fromisoformat(raw_start) if raw_start else None
        curr_day    = int(group_settings["reading_current_day"]) if group_settings else -1
        use_hijri   = bool(group_settings["use_hijri_date"]) if group_settings else False
        reading     = get_reading_for_today(plan_key, custom_text, today, start_date, curr_day)
        date_str    = format_date_arabic(today, hijri=use_hijri)

        day_seed   = today.timetuple().tm_yday
        last_index = await db.get_last_motivation_index(target_group_id)
        motivation = get_daily_motivation(day_seed, last_index)

        text = (
            msg.DAILY_POST_HEADER + "\n\n"
            + msg.DAILY_POST_BODY.format(date=date_str, reading=reading, motivation=motivation)
        )

        def _gs(key: str) -> bool:
            return bool(group_settings[key]) if group_settings else False

        if _gs("daily_verse_enabled"):
            text += msg.DAILY_POST_WITH_VERSE.format(verse=pick_random_verse())
        if _gs("daily_dua_enabled"):
            text += msg.DAILY_POST_WITH_DUA.format(dua=pick_daily_dua())
        if _gs("daily_hadith_enabled"):
            text += msg.DAILY_POST_WITH_HADITH.format(hadith=pick_random_hadith())

        await context.bot.send_message(
            chat_id=target_group_id,
            text=text,
            parse_mode=MD,
            reply_markup=daily_post_keyboard()
        )
        await query.message.reply_text("✅ تم إرسال الورد اليومي إلى المجموعة.", parse_mode=MD)


    elif data == CB_SKIP_DAY:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(
            msg.CONFIRM_SKIP_DAY,
            parse_mode=MD,
            reply_markup=confirmation_keyboard(CB_CONFIRM_SKIP_DAY),
        )

    elif data == CB_CONFIRM_SKIP_DAY:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        group_settings = await db.get_settings(target_group_id)
        tz = await _group_tz(group_settings)
        today = today_in_tz(tz)
        use_hijri = bool(group_settings["use_hijri_date"]) if group_settings else False
        await db.mark_day_skipped(target_group_id, today)
        await query.message.edit_text(
            msg.SKIP_DAY_CONFIRM.format(date=format_date_arabic(today, hijri=use_hijri)),
            parse_mode=MD,
        )

    elif data == CB_RESET_MONTH:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(
            "⚠️ هل أنت متأكد من إعادة ضبط إحصائيات الشهر الحالي؟",
            parse_mode=MD,
            reply_markup=confirm_keyboard(CB_CONFIRM_RESET),
        )

    elif data == CB_CONFIRM_RESET:
        if not await _verify_admin():
            await query.answer(msg.ADMIN_ONLY, show_alert=True)
            return
        await query.answer()
        from datetime import datetime as _dt
        now = _dt.utcnow()
        await db.reset_month_checkins(target_group_id, now.year, now.month)
        await query.message.edit_text(msg.RESET_MONTH_CONFIRM, parse_mode=MD)

    # ── Main Menu Navigation ─────────────────────────────────────────────
    elif data == CB_MENU_MAIN:
        await query.answer()
        await query.message.edit_text(msg.MAIN_MENU, parse_mode=MD, reply_markup=main_menu_keyboard())

    elif data == CB_MENU_HELP:
        await query.answer()
        await query.message.edit_text(msg.HELP_CATEGORY_PROMPT, parse_mode=MD,
                                       reply_markup=help_category_keyboard())

    elif data == CB_MENU_LEADERBOARD:
        await query.answer()
        await query.message.edit_text(msg.LEADERBOARD_HEADER + msg.LEADERBOARD_NO_DATA,
                                       parse_mode=MD, reply_markup=leaderboard_keyboard())

    elif data == CB_MENU_ACHIEVEMENTS:
        await query.answer()
        text = msg.HELP_ACHIEVEMENTS_HEADER + msg.HELP_ACHIEVEMENTS_BODY
        await query.message.edit_text(text, parse_mode=MD,
                                       reply_markup=nav_main_menu_keyboard())

    elif data == CB_MENU_STATS:
        await query.answer()
        await cmd_me(update, context)

    # ── Help Category Display ────────────────────────────────────────────
    elif data == CB_HELP_GENERAL:
        await query.answer()
        text = msg.HELP_GENERAL_HEADER + msg.HELP_GENERAL_BODY
        await query.message.edit_text(text, parse_mode=MD,
                                       reply_markup=help_category_keyboard())

    elif data == CB_HELP_ADMIN:
        await query.answer()
        text = msg.HELP_ADMIN_HEADER + msg.HELP_ADMIN_BODY
        await query.message.edit_text(text, parse_mode=MD,
                                       reply_markup=help_category_keyboard())

    elif data == CB_HELP_STATS:
        await query.answer()
        text = msg.HELP_STATS_HEADER + msg.HELP_STATS_BODY
        await query.message.edit_text(text, parse_mode=MD,
                                       reply_markup=help_category_keyboard())

    elif data == CB_HELP_ACHIEVE:
        await query.answer()
        text = msg.HELP_ACHIEVEMENTS_HEADER + msg.HELP_ACHIEVEMENTS_BODY
        await query.message.edit_text(text, parse_mode=MD,
                                       reply_markup=help_category_keyboard())

    # ── Leaderboard ──────────────────────────────────────────────────────
    elif data in (CB_LB_CURRENT, CB_LB_TOTAL, CB_LB_MONTH):
        if not target_group_id:
            await query.answer("⚠️ اختر مجموعة أولاً عبر /settings.", show_alert=True)
            return
        await query.answer()
        today = today_in_tz(await _group_tz(await db.get_settings(target_group_id)))

        if data == CB_LB_CURRENT:
            rows = await db.get_leaderboard_by_streak(target_group_id)
            title = msg.LEADERBOARD_HEADER + msg.LEADERBOARD_CURRENT
        elif data == CB_LB_TOTAL:
            rows = await db.get_leaderboard_by_total(target_group_id)
            title = msg.LEADERBOARD_HEADER + msg.LEADERBOARD_TOTAL
        else:
            rows = await db.get_monthly_leaderboard(target_group_id, today.year, today.month)
            title = msg.LEADERBOARD_HEADER + msg.LEADERBOARD_MONTH

        if not rows:
            body = msg.LEADERBOARD_NO_DATA
        else:
            lines = []
            for i, row in enumerate(rows, start=1):
                full = row["full_name"] or ""
                name = display_name(
                    full.split()[0] if full.strip() else "",
                    " ".join(full.split()[1:]),
                    row["username"] or "",
                )
                val = row["score"] if "score" in row.keys() else row["days"]
                lines.append(msg.LEADERBOARD_ENTRY.format(rank=i, name=name, value=val))
            body = "\n".join(lines)

        await query.message.edit_text(title + body, parse_mode=MD,
                                       reply_markup=leaderboard_keyboard())

    # ── Cancel ───────────────────────────────────────────────────────────
    elif data == CB_CANCEL:
        await query.answer()
        await query.message.edit_text("❌ تم الإلغاء.")


# ---------------------------------------------------------------------------
# Text message handler (captures pending setting values)
# ---------------------------------------------------------------------------

async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture text replies for settings the admin is editing."""
    pending_key = context.user_data.get(_PENDING_KEY)
    pending_group = context.user_data.get(_PENDING_GROUP)

    if not pending_key or not pending_group:
        return

    text = (update.effective_message.text or "").strip()
    db = _db(context)

    if pending_key in ("post_time", "report_time"):
        try:
            parse_hhmm(text)  # Validate
        except (ValueError, AttributeError):
            await _send_safe(update, msg.SETTINGS_INVALID_TIME, parse_mode=MD)
            return
        await db.update_setting(pending_group, pending_key, text)

    elif pending_key == "reminder_times":
        parts = [t.strip() for t in text.split(",") if t.strip()]
        if not parts:
            await _send_safe(update, "⚠️ الرجاء إرسال وقت واحد على الأقل بصيغة HH:MM.", parse_mode=MD)
            return
        for t in parts:
            try:
                parse_hhmm(t)
            except (ValueError, AttributeError):
                await _send_safe(
                    update, f"⚠️ الوقت \"{t}\" غير صحيح. استخدم صيغة HH:MM (مثال: 20:00).",
                    parse_mode=MD,
                )
                return
        await db.update_setting(pending_group, "reminder_times", ",".join(parts))

    elif pending_key == "timezone":
        try:
            pytz.timezone(text)
        except pytz.exceptions.UnknownTimeZoneError:
            await _send_safe(update, "⚠️ المنطقة الزمنية غير صحيحة. مثال: Asia/Riyadh")
            return
        await db.update_setting(pending_group, pending_key, text)

    elif pending_key == "custom_reading":
        await db.update_setting(pending_group, pending_key, text)
        await db.update_setting(pending_group, "plan_key", "custom")

    elif pending_key == "reading_current_day":
        try:
            juz = int(text)
        except (ValueError, TypeError):
            await _send_safe(update, "⚠️ الرجاء إرسال رقم صحيح بين 0 و 30.", parse_mode=MD)
            return
        if juz < 0 or juz > 30:
            await _send_safe(update, "⚠️ الرجاء إرسال رقم بين 0 و 30.", parse_mode=MD)
            return
        if juz == 0:
            await db.update_setting(pending_group, "reading_current_day", "-1")
        else:
            await db.update_setting(pending_group, "reading_current_day", str(juz - 1))

    del context.user_data[_PENDING_KEY]
    del context.user_data[_PENDING_GROUP]

    await _send_safe(update, msg.SETTINGS_UPDATED, parse_mode=MD)

# ===========================================================================
# Backup DM Callback Handlers
# ===========================================================================
async def handle_backup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if update.effective_user.id not in settings.bot_admins:
        await query.answer("⚠️ غير مصرح لك.", show_alert=True)
        return

    if data == "admin_backup_create":
        await query.answer("جاري إنشاء النسخة الاحتياطية...")
        try:
            backup_path, checksum = await async_create_backup(
                settings.database_path,
                settings.backup_dir,
                settings.backup_retain,
                is_weekly=False
            )
            text = msg.BACKUP_SUCCESS_DM.format(path=backup_path.name, checksum=checksum)
            with open(backup_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_user.id,
                    document=f,
                    filename=backup_path.name,
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as exc:
            logger.error("Backup creation failed: %s", exc)
            await query.message.reply_text("❌ حدث خطأ أثناء إنشاء النسخة الاحتياطية.")
            
    elif data == "admin_backup_download":
        await query.answer(msg.BACKUP_DOWNLOAD_PROMPT)
        backups = sorted(settings.backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            await query.message.reply_text(msg.BACKUP_NOT_FOUND)
            return
        latest = backups[0]
        try:
            from backup import generate_checksum
            checksum = await generate_checksum(latest)
            text = f"✅ أحدث نسخة احتياطية:\n\nالملف: {latest.name}\nالبصمة (SHA256):\n`{checksum}`"
            with open(latest, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_user.id,
                    document=f,
                    filename=latest.name,
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as exc:
            logger.error("Download latest backup failed: %s", exc)
            
    elif data == "admin_backup_restore":
        await query.answer()
        context.user_data["awaiting_restore"] = update.effective_chat.id
        await query.message.reply_text(msg.BACKUP_RESTORE_PROMPT_DM)
