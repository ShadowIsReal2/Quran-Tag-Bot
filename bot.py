"""
bot.py — Entry point for the Quran Tracker Bot.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import threading
import sys
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import settings
from database import Database
from handlers import (
    cmd_backup,
    cmd_checkin,
    cmd_daily,
    cmd_export,
    cmd_group,
    cmd_help,
    cmd_leaderboard,
    cmd_me,
    cmd_menu,
    cmd_missing,
    cmd_readingplan,
    cmd_report,
    cmd_reset_member,
    cmd_reset_month,
    cmd_restore,
    cmd_settings,
    cmd_start,
    cmd_stats,
    cmd_version,
    handle_callback,
    handle_left_member,
    handle_new_member,
    handle_restore_document,
    handle_text_message,
)
from scheduler import register_jobs


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    log_level = getattr(logging, settings.log_level, logging.INFO)
    max_bytes = settings.log_max_mb * 1024 * 1024

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
        )
    )
    root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Bot command menu
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("start",        "تشغيل البوت"),
    BotCommand("menu",         "القائمة الرئيسية"),
    BotCommand("help",         "المساعدة وقائمة الأوامر"),
    BotCommand("me",           "إحصائياتك الشخصية"),
    BotCommand("checkin",      "تسجيل ورد اليوم"),
    BotCommand("leaderboard",  "لوحة الشرف"),
    BotCommand("stats",        "إحصائيات المجموعة"),
    BotCommand("daily",        "إرسال ورد اليوم"),
    BotCommand("report",       "تقرير اليوم"),
    BotCommand("missing",      "من لم يُكمل الورد (مشرفون)"),
    BotCommand("settings",     "إعدادات المجموعة (مشرفون)"),
    BotCommand("group",        "اختيار المجموعة (مشرفون)"),
    BotCommand("readingplan",  "تغيير خطة القراءة (مشرفون)"),
    BotCommand("backup",       "نسخة احتياطية (مشرفون)"),
    BotCommand("restore",      "استعادة نسخة (مشرفون)"),
    BotCommand("export",       "تصدير البيانات (مشرفون)"),
    BotCommand("reset_member", "إعادة ضبط عضو (مشرفون)"),
    BotCommand("reset_month",  "إعادة ضبط الشهر (مشرفون)"),
    BotCommand("version",      "معلومات البوت"),
]


# ---------------------------------------------------------------------------
# Post-init hook
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    db = Database(settings.database_path)
    await db.init()
    app.bot_data["db"] = db
    await app.bot.set_my_commands(BOT_COMMANDS)
    logging.getLogger(__name__).info("Bot commands registered.")


async def post_shutdown(app: Application) -> None:
    db: Database = app.bot_data.get("db")
    if db:
        await db.close()
    logging.getLogger(__name__).info("Bot shut down cleanly.")


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("menu",         cmd_menu))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("me",           cmd_me))
    app.add_handler(CommandHandler("checkin",      cmd_checkin))
    app.add_handler(CommandHandler("leaderboard",  cmd_leaderboard))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("daily",        cmd_daily))
    app.add_handler(CommandHandler("report",       cmd_report))
    app.add_handler(CommandHandler("missing",      cmd_missing))
    app.add_handler(CommandHandler("settings",     cmd_settings))
    app.add_handler(CommandHandler("group",        cmd_group))
    app.add_handler(CommandHandler("readingplan",  cmd_readingplan))
    app.add_handler(CommandHandler("backup",       cmd_backup))
    app.add_handler(CommandHandler("restore",      cmd_restore))
    app.add_handler(CommandHandler("export",       cmd_export))
    app.add_handler(CommandHandler("reset_member", cmd_reset_member))
    app.add_handler(CommandHandler("reset_month",  cmd_reset_month))
    app.add_handler(CommandHandler("version",      cmd_version))

    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member)
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_left_member)
    )
    app.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_restore_document)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context) -> None:
    logger = logging.getLogger(__name__)
    logger.error("Unhandled exception for update %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ حدث خطأ غير متوقع.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Health server (for Hugging Face port 7860 requirement)
# Runs in a separate thread with its own event loop.
# Uses only Python stdlib.
# ---------------------------------------------------------------------------

async def handle_health(reader, writer):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok")
    await writer.drain()
    writer.close()


async def _run_health_server():
    server = await asyncio.start_server(handle_health, "0.0.0.0", 7860)
    logging.getLogger(__name__).info("Health server listening on port 7860")
    async with server:
        await server.serve_forever()


def start_health_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run_health_server())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting Quran Tracker Bot v%s …", settings.bot_version)

    if settings is None:
        logger.critical("Settings could not be loaded. Check your .env file.")
        sys.exit(1)

    # Start health server in a background thread
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    app = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    register_handlers(app)
    app.add_error_handler(error_handler)
    register_jobs(app)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
