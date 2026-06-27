"""
keyboards.py — Inline keyboard factories for Quran Tracker Bot.

Every InlineKeyboardMarkup used by the bot is built here.
Callback data strings are defined as constants to avoid typos.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import messages as msg

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------

CB_CHECKIN = "checkin"
CB_MY_STATS = "my_stats"
CB_GROUP_STATS = "group_stats"

CB_SETTINGS_MENU = "settings_menu"
CB_SET_POST_TIME = "set_post_time"
CB_SET_REPORT_TIME = "set_report_time"
CB_SET_REMINDER_TIME = "set_reminder_time"
CB_SET_TIMEZONE = "set_timezone"
CB_SET_PLAN = "set_plan"
CB_TOGGLE_REPORT = "toggle_report"
CB_TOGGLE_MILESTONES = "toggle_milestones"
CB_TOGGLE_WEEKLY = "toggle_weekly"
CB_TOGGLE_VERSE = "toggle_verse"
CB_TOGGLE_HADITH = "toggle_hadith"
CB_TOGGLE_DUA = "toggle_dua"
CB_TOGGLE_REMINDER = "toggle_reminder"
CB_TOGGLE_ANNOUNCE = "toggle_announce"
CB_SET_JUZ = "set_juz"
CB_TOGGLE_HIJRI = "toggle_hijri"
CB_FORCE_DAILY = "force_daily"
CB_SKIP_DAY = "skip_day"
CB_RESET_MONTH = "reset_month"
CB_CONFIRM_RESET = "confirm_reset_month"
CB_CANCEL = "cancel"
CB_BACK = "back"

CB_PLAN_PREFIX = "plan:"   # e.g. "plan:1_juz_day"

# ---- Main menu ----
CB_MENU_HELP = "menu_help"
CB_MENU_LEADERBOARD = "menu_leaderboard"
CB_MENU_ACHIEVEMENTS = "menu_achievements"
CB_MENU_STATS = "menu_stats"
CB_MENU_MAIN = "menu_main"

# ---- Help categories ----
CB_HELP_GENERAL = "help_general"
CB_HELP_ADMIN = "help_admin"
CB_HELP_STATS = "help_stats"
CB_HELP_ACHIEVE = "help_achieve"

# ---- Leaderboard ----
CB_LB_CURRENT = "lb_current"
CB_LB_TOTAL = "lb_total"
CB_LB_MONTH = "lb_month"

# ---- Confirmations ----
CB_CONFIRM_SKIP_DAY = "confirm_skip_day"
CB_CONFIRM_RESET_MEMBER = "confirm_reset_member"


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def daily_post_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown under the daily Quran post."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(msg.BTN_CHECKIN, callback_data=CB_CHECKIN),
        ],
        [
            InlineKeyboardButton(msg.BTN_MY_STATS, callback_data=CB_MY_STATS),
            InlineKeyboardButton(msg.BTN_GROUP_STATS, callback_data=CB_GROUP_STATS),
        ],
    ])


def stats_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown under stats — quick check-in button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg.BTN_CHECKIN, callback_data=CB_CHECKIN)],
    ])


def settings_main_keyboard(
    is_supergroup: bool = True,
    report_enabled: bool = True,
    milestones_enabled: bool = True,
    weekly_report_enabled: bool = True,
    daily_verse_enabled: bool = True,
    daily_hadith_enabled: bool = False,
    daily_dua_enabled: bool = False,
    reminder_enabled: bool = False,
    announce_badges: bool = False,
    use_hijri_date: bool = False,
) -> InlineKeyboardMarkup:
    """Main settings menu keyboard for admins."""

    def _btn(label: str, cb: str, enabled: bool) -> InlineKeyboardButton:
        return InlineKeyboardButton(f"{label}: {'🟢' if enabled else '🔴'}", callback_data=cb)

    rows = [
        [InlineKeyboardButton(msg.BTN_SET_POST_TIME, callback_data=CB_SET_POST_TIME)],
        [InlineKeyboardButton(msg.BTN_SET_REPORT_TIME, callback_data=CB_SET_REPORT_TIME)],
        [InlineKeyboardButton(msg.BTN_SET_REMINDER_TIME, callback_data=CB_SET_REMINDER_TIME)],
        [InlineKeyboardButton(msg.BTN_SET_TIMEZONE, callback_data=CB_SET_TIMEZONE)],
        [InlineKeyboardButton(msg.BTN_READING_PLAN, callback_data=CB_SET_PLAN)],
        [InlineKeyboardButton(msg.BTN_SET_JUZ, callback_data=CB_SET_JUZ)],
        [_btn(msg.BTN_TOGGLE_HIJRI, CB_TOGGLE_HIJRI, use_hijri_date)],
        [_btn(msg.BTN_TOGGLE_REPORT, CB_TOGGLE_REPORT, report_enabled)],
        [_btn(msg.BTN_TOGGLE_MILESTONES, CB_TOGGLE_MILESTONES, milestones_enabled)],
        [_btn(msg.BTN_TOGGLE_WEEKLY, CB_TOGGLE_WEEKLY, weekly_report_enabled)],
        [_btn(msg.BTN_TOGGLE_VERSE, CB_TOGGLE_VERSE, daily_verse_enabled)],
        [_btn(msg.BTN_TOGGLE_HADITH, CB_TOGGLE_HADITH, daily_hadith_enabled)],
        [_btn(msg.BTN_TOGGLE_DUA, CB_TOGGLE_DUA, daily_dua_enabled)],
        [_btn(msg.BTN_TOGGLE_REMINDER, CB_TOGGLE_REMINDER, reminder_enabled)],
        [_btn(msg.BTN_TOGGLE_ANNOUNCE, CB_TOGGLE_ANNOUNCE, announce_badges)],
        [InlineKeyboardButton(msg.BTN_FORCE_DAILY, callback_data=CB_FORCE_DAILY)],
        [InlineKeyboardButton(msg.BTN_SKIP_DAY, callback_data=CB_SKIP_DAY)],
        [InlineKeyboardButton(msg.BTN_RESET_MONTH, callback_data=CB_RESET_MONTH)],
        [InlineKeyboardButton(msg.BTN_CANCEL, callback_data=CB_CANCEL)],
    ]
    return InlineKeyboardMarkup(rows)


def reading_plan_keyboard(plans: list) -> InlineKeyboardMarkup:
    """Keyboard listing all available reading plans."""
    rows = []
    for plan in plans:
        key = plan["plan_key"]
        label = msg.PLAN_LABELS.get(key, plan["label"])
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_PLAN_PREFIX}{key}")])
    rows.append([InlineKeyboardButton(msg.BTN_BACK, callback_data=CB_SETTINGS_MENU)])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(confirm_cb: str) -> InlineKeyboardMarkup:
    """Generic yes/no confirmation keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(msg.BTN_CONFIRM, callback_data=confirm_cb),
            InlineKeyboardButton(msg.BTN_CANCEL, callback_data=CB_CANCEL),
        ]
    ])


def back_keyboard(back_cb: str = CB_SETTINGS_MENU) -> InlineKeyboardMarkup:
    """Single back button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg.BTN_BACK, callback_data=back_cb)]
    ])

CB_BACKUP_MENU = "admin_backup_menu"
CB_BACKUP_CREATE = "admin_backup_create"
CB_BACKUP_DOWNLOAD = "admin_backup_download"
CB_BACKUP_RESTORE = "admin_backup_restore"

def admin_backup_menu_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for Admin DM Backup settings."""
    keyboard = [
        [InlineKeyboardButton("📥 إنشاء نسخة احتياطية الآن", callback_data=CB_BACKUP_CREATE)],
        [InlineKeyboardButton("📤 تحميل أحدث نسخة", callback_data=CB_BACKUP_DOWNLOAD)],
        [InlineKeyboardButton("🔄 استعادة من نسخة سابقة", callback_data=CB_BACKUP_RESTORE)],
        [InlineKeyboardButton("❌ إغلاق", callback_data=CB_CANCEL)],
    ]
    return InlineKeyboardMarkup(keyboard)

CB_ADMIN_SELECT_GROUP = "admin_sg:"
CB_ADMIN_SELECT_GROUP_ONLY = "admin_sgo:"

def admin_groups_keyboard(groups_data: list[tuple[int, str]], go_to_settings: bool = True) -> InlineKeyboardMarkup:
    """Keyboard listing groups an admin manages.

    If *go_to_settings* is True (default), selecting a group opens its settings.
    Otherwise it just stores the selection for later use.
    """
    prefix = CB_ADMIN_SELECT_GROUP if go_to_settings else CB_ADMIN_SELECT_GROUP_ONLY
    keyboard = []
    for group_id, title in groups_data:
        keyboard.append([InlineKeyboardButton(title, callback_data=f"{prefix}{group_id}")])
    keyboard.append([InlineKeyboardButton("❌ إغلاق", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Main Menu
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Rich main menu shown from /start in DM or /menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg.BTN_CHECKIN_TODAY, callback_data=CB_CHECKIN)],
        [
            InlineKeyboardButton(msg.BTN_MY_STATS, callback_data=CB_MENU_STATS),
            InlineKeyboardButton(msg.MAIN_MENU_LEADERBOARD, callback_data=CB_MENU_LEADERBOARD),
        ],
        [
            InlineKeyboardButton(msg.MAIN_MENU_HELP, callback_data=CB_MENU_HELP),
            InlineKeyboardButton(msg.MAIN_MENU_SETTINGS, callback_data=CB_SETTINGS_MENU),
        ],
    ])


# ---------------------------------------------------------------------------
# Help Categories
# ---------------------------------------------------------------------------

def help_category_keyboard() -> InlineKeyboardMarkup:
    """Keyboard showing help categories."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(msg.BTN_HELP_GENERAL, callback_data=CB_HELP_GENERAL),
            InlineKeyboardButton(msg.BTN_HELP_ADMIN, callback_data=CB_HELP_ADMIN),
        ],
        [
            InlineKeyboardButton(msg.BTN_HELP_STATS, callback_data=CB_HELP_STATS),
            InlineKeyboardButton(msg.BTN_HELP_ACHIEVE, callback_data=CB_HELP_ACHIEVE),
        ],
        [InlineKeyboardButton(msg.NAV_MAIN_MENU, callback_data=CB_MENU_MAIN)],
    ])


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

def leaderboard_keyboard() -> InlineKeyboardMarkup:
    """Keyboard choosing leaderboard mode."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg.BTN_LEADERBOARD_CURRENT, callback_data=CB_LB_CURRENT)],
        [InlineKeyboardButton(msg.BTN_LEADERBOARD_TOTAL, callback_data=CB_LB_TOTAL)],
        [InlineKeyboardButton(msg.BTN_LEADERBOARD_MONTH, callback_data=CB_LB_MONTH)],
        [InlineKeyboardButton(msg.NAV_MAIN_MENU, callback_data=CB_MENU_MAIN)],
    ])


# ---------------------------------------------------------------------------
# Generic Navigation
# ---------------------------------------------------------------------------

def nav_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Single button to return to main menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg.NAV_MAIN_MENU, callback_data=CB_MENU_MAIN)]
    ])


def confirmation_keyboard(confirm_cb: str, cancel_cb: str = CB_CANCEL) -> InlineKeyboardMarkup:
    """Destructive action confirmation: yes / no."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(msg.CONFIRM_YES, callback_data=confirm_cb),
            InlineKeyboardButton(msg.CONFIRM_NO, callback_data=cancel_cb),
        ]
    ])
