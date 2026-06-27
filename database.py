"""
database.py — Async SQLite database layer for Quran Tracker Bot.

All database I/O is performed here. No SQL lives anywhere else.
Uses aiosqlite for fully non-blocking access.

Usage:
    db = Database("data/quran_tracker.db")
    await db.init()
    ...
    await db.close()
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL — table schemas
# ---------------------------------------------------------------------------

_CREATE_GROUPS = """
CREATE TABLE IF NOT EXISTS groups (
    group_id        INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL,
    added_at        TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1
);"""

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER NOT NULL,
    group_id        INTEGER NOT NULL,
    first_name      TEXT    NOT NULL DEFAULT '',
    last_name       TEXT    NOT NULL DEFAULT '',
    username        TEXT    NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    joined_at       TEXT    NOT NULL,
    PRIMARY KEY (user_id, group_id),
    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
);"""

_CREATE_DAILY_CHECKINS = """
CREATE TABLE IF NOT EXISTS daily_checkins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    group_id        INTEGER NOT NULL,
    checkin_date    TEXT    NOT NULL,
    checked_in_at   TEXT    NOT NULL,
    UNIQUE (user_id, group_id, checkin_date),
    FOREIGN KEY (user_id, group_id) REFERENCES users(user_id, group_id)
);"""

_CREATE_STREAKS = """
CREATE TABLE IF NOT EXISTS streaks (
    user_id         INTEGER NOT NULL,
    group_id        INTEGER NOT NULL,
    current_streak  INTEGER NOT NULL DEFAULT 0,
    longest_streak  INTEGER NOT NULL DEFAULT 0,
    last_checkin    TEXT,
    PRIMARY KEY (user_id, group_id),
    FOREIGN KEY (user_id, group_id) REFERENCES users(user_id, group_id)
);"""

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    group_id            INTEGER PRIMARY KEY,
    post_time           TEXT    NOT NULL DEFAULT '08:00',
    report_time         TEXT    NOT NULL DEFAULT '22:00',
    timezone            TEXT    NOT NULL DEFAULT 'Africa/Cairo',
    plan_key            TEXT    NOT NULL DEFAULT '1_juz_day',
    custom_reading      TEXT    NOT NULL DEFAULT '',
    reminder_enabled    INTEGER NOT NULL DEFAULT 0,
    reminder_times      TEXT    NOT NULL DEFAULT '20:00',
    report_enabled      INTEGER NOT NULL DEFAULT 1,
    announce_badges     INTEGER NOT NULL DEFAULT 0,
    milestones_enabled  INTEGER NOT NULL DEFAULT 1,
    weekly_report_enabled INTEGER NOT NULL DEFAULT 1,
    daily_verse_enabled   INTEGER NOT NULL DEFAULT 1,
    daily_hadith_enabled  INTEGER NOT NULL DEFAULT 0,
    daily_dua_enabled     INTEGER NOT NULL DEFAULT 0,
    reading_start       TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
);"""

_CREATE_READING_PLANS = """
CREATE TABLE IF NOT EXISTS reading_plans (
    plan_key        TEXT    PRIMARY KEY,
    label           TEXT    NOT NULL,
    pages_per_day   INTEGER,
    juz_per_day     REAL
);"""

_CREATE_DAILY_REPORTS = """
CREATE TABLE IF NOT EXISTS daily_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL,
    report_date     TEXT    NOT NULL,
    confirmed       INTEGER NOT NULL DEFAULT 0,
    pending         INTEGER NOT NULL DEFAULT 0,
    active_members  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    UNIQUE (group_id, report_date),
    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
);"""

_CREATE_SKIPPED_DAYS = """
CREATE TABLE IF NOT EXISTS skipped_days (
    group_id        INTEGER NOT NULL,
    skip_date       TEXT    NOT NULL,
    PRIMARY KEY (group_id, skip_date),
    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
);"""

_CREATE_ACHIEVEMENTS = """
CREATE TABLE IF NOT EXISTS achievements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    group_id        INTEGER NOT NULL,
    achievement_key TEXT    NOT NULL,
    earned_at       TEXT    NOT NULL,
    UNIQUE (user_id, group_id, achievement_key),
    FOREIGN KEY (user_id, group_id) REFERENCES users(user_id, group_id)
);"""

_CREATE_GROUP_MILESTONES = """
CREATE TABLE IF NOT EXISTS group_milestones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL,
    milestone_key   TEXT    NOT NULL,
    reached_at      TEXT    NOT NULL,
    UNIQUE (group_id, milestone_key),
    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
);"""

_CREATE_LAST_MOTIVATION = """
CREATE TABLE IF NOT EXISTS last_motivation (
    group_id        INTEGER PRIMARY KEY,
    last_index      INTEGER NOT NULL DEFAULT -1,
    last_date       TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
);"""

_ALL_DDL = [
    _CREATE_GROUPS,
    _CREATE_USERS,
    _CREATE_DAILY_CHECKINS,
    _CREATE_STREAKS,
    _CREATE_SETTINGS,
    _CREATE_READING_PLANS,
    _CREATE_DAILY_REPORTS,
    _CREATE_SKIPPED_DAYS,
    _CREATE_ACHIEVEMENTS,
    _CREATE_GROUP_MILESTONES,
    _CREATE_LAST_MOTIVATION,
]

# Migrations: safe ALTER TABLE statements (ignored if column already exists)
_MIGRATIONS = [
    "ALTER TABLE settings ADD COLUMN reminder_enabled INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE settings ADD COLUMN reminder_times TEXT NOT NULL DEFAULT '20:00';",
    "ALTER TABLE settings ADD COLUMN announce_badges INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE settings ADD COLUMN reading_start TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE settings ADD COLUMN report_enabled INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE settings ADD COLUMN milestones_enabled INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE settings ADD COLUMN weekly_report_enabled INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE settings ADD COLUMN daily_verse_enabled INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE settings ADD COLUMN daily_hadith_enabled INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE settings ADD COLUMN daily_dua_enabled INTEGER NOT NULL DEFAULT 0;",
]

# Default reading plans seed data
_DEFAULT_PLANS = [
    ("1_juz_day",    "جزء يومياً",       None, 1.0),
    ("2_pages_day",  "صفحتان يومياً",    2,    None),
    ("5_pages_day",  "5 صفحات يومياً",   5,    None),
    ("10_pages_day", "10 صفحات يومياً",  10,   None),
    ("custom",       "مخصص",             None, None),
]


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """Async wrapper around aiosqlite for all bot persistence."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open the connection, enable WAL mode, create tables, seed plans."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        for ddl in _ALL_DDL:
            await self._conn.execute(ddl)

        # Safe schema migrations
        for migration in _MIGRATIONS:
            try:
                await self._conn.execute(migration)
            except Exception:
                pass  # Column already exists — safe to ignore

        # Seed default reading plans (idempotent)
        await self._conn.executemany(
            "INSERT OR IGNORE INTO reading_plans "
            "(plan_key, label, pages_per_day, juz_per_day) VALUES (?,?,?,?);",
            _DEFAULT_PLANS,
        )
        await self._conn.commit()
        logger.info("Database initialised at %s", self.path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() was not called.")
        return self._conn

    async def _execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        commit: bool = False,
    ) -> aiosqlite.Cursor:
        cursor = await self._db.execute(sql, params)
        if commit:
            await self._db.commit()
        return cursor

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Optional[aiosqlite.Row]:
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchall()

    # ==================================================================
    # GROUPS
    # ==================================================================

    async def upsert_group(self, group_id: int, title: str) -> None:
        """Insert or update a group record."""
        now = datetime.utcnow().isoformat()
        await self._execute(
            """
            INSERT INTO groups (group_id, title, added_at, is_active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(group_id) DO UPDATE SET
                title     = excluded.title,
                is_active = 1;
            """,
            (group_id, title, now),
            commit=True,
        )
        # Ensure settings row exists (set reading_start for new groups)
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        await self._execute(
            "INSERT OR IGNORE INTO settings (group_id, reading_start) VALUES (?, ?);",
            (group_id, today_str),
            commit=True,
        )
        logger.debug("Upserted group %d (%s)", group_id, title)

    async def get_all_active_groups(self) -> list[aiosqlite.Row]:
        """Return all active groups."""
        return await self._fetchall("SELECT * FROM groups WHERE is_active = 1;")

    async def deactivate_group(self, group_id: int) -> None:
        """Mark a group as inactive (bot was removed)."""
        await self._execute(
            "UPDATE groups SET is_active = 0 WHERE group_id = ?;",
            (group_id,),
            commit=True,
        )

    async def count_groups(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS cnt FROM groups WHERE is_active = 1;")
        return row["cnt"] if row else 0

    # ==================================================================
    # USERS
    # ==================================================================

    async def upsert_user(
        self,
        user_id: int,
        group_id: int,
        first_name: str,
        last_name: str = "",
        username: str = "",
    ) -> None:
        """Insert or update a user record."""
        now = datetime.utcnow().isoformat()
        await self._execute(
            """
            INSERT INTO users (user_id, group_id, first_name, last_name, username,
                               is_active, joined_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id, group_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                username   = excluded.username,
                is_active  = 1;
            """,
            (user_id, group_id, first_name, last_name, username, now),
            commit=True,
        )
        # Ensure streak row exists
        await self._execute(
            "INSERT OR IGNORE INTO streaks (user_id, group_id) VALUES (?, ?);",
            (user_id, group_id),
            commit=True,
        )

    async def get_active_users(self, group_id: int) -> list[aiosqlite.Row]:
        """Return all active users in a group."""
        return await self._fetchall(
            "SELECT * FROM users WHERE group_id = ? AND is_active = 1;",
            (group_id,),
        )

    async def count_users(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(DISTINCT user_id) AS cnt FROM users WHERE is_active = 1;"
        )
        return row["cnt"] if row else 0

    async def get_user(self, user_id: int, group_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM users WHERE user_id = ? AND group_id = ?;",
            (user_id, group_id),
        )

    async def reset_user(self, user_id: int, group_id: int) -> None:
        """Delete all check-ins and reset streak for a user in a group."""
        await self._execute(
            "DELETE FROM daily_checkins WHERE user_id = ? AND group_id = ?;",
            (user_id, group_id),
            commit=False,
        )
        await self._execute(
            """
            UPDATE streaks SET current_streak = 0, longest_streak = 0,
                               last_checkin = NULL
            WHERE user_id = ? AND group_id = ?;
            """,
            (user_id, group_id),
            commit=True,
        )

    async def find_user_by_username(
        self, username: str, group_id: int
    ) -> Optional[aiosqlite.Row]:
        clean = username.lstrip("@")
        return await self._fetchone(
            "SELECT * FROM users WHERE username = ? AND group_id = ?;",
            (clean, group_id),
        )

    async def get_users_not_checked_in(
        self, group_id: int, checkin_date: date
    ) -> list[aiosqlite.Row]:
        """Return active users who have NOT checked in on the given date."""
        return await self._fetchall(
            """
            SELECT u.* FROM users u
            WHERE u.group_id = ? AND u.is_active = 1
              AND u.user_id NOT IN (
                  SELECT dc.user_id FROM daily_checkins dc
                  WHERE dc.group_id = ? AND dc.checkin_date = ?
              );
            """,
            (group_id, group_id, checkin_date.isoformat()),
        )

    # ==================================================================
    # DAILY CHECK-INS
    # ==================================================================

    async def checkin(
        self, user_id: int, group_id: int, checkin_date: date
    ) -> bool:
        """
        Record a check-in. Returns True if new, False if already exists.
        Also updates the user's streak.
        """
        date_str = checkin_date.isoformat()
        now = datetime.utcnow().isoformat()
        try:
            await self._execute(
                """
                INSERT INTO daily_checkins (user_id, group_id, checkin_date, checked_in_at)
                VALUES (?, ?, ?, ?);
                """,
                (user_id, group_id, date_str, now),
                commit=True,
            )
        except aiosqlite.IntegrityError:
            return False  # duplicate

        await self._update_streak(user_id, group_id, checkin_date)
        return True

    async def has_checked_in(
        self, user_id: int, group_id: int, checkin_date: date
    ) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM daily_checkins WHERE user_id=? AND group_id=? AND checkin_date=?;",
            (user_id, group_id, checkin_date.isoformat()),
        )
        return row is not None

    async def get_checkins_for_date(
        self, group_id: int, checkin_date: date
    ) -> list[aiosqlite.Row]:
        """Return all check-in rows for a group on a date."""
        return await self._fetchall(
            "SELECT * FROM daily_checkins WHERE group_id=? AND checkin_date=?;",
            (group_id, checkin_date.isoformat()),
        )

    async def count_checkins_this_month(
        self, user_id: int, group_id: int, year: int, month: int
    ) -> int:
        prefix = f"{year}-{month:02d}-"
        row = await self._fetchone(
            """
            SELECT COUNT(*) AS cnt FROM daily_checkins
            WHERE user_id=? AND group_id=? AND checkin_date LIKE ?;
            """,
            (user_id, group_id, prefix + "%"),
        )
        return row["cnt"] if row else 0

    async def count_checkins_this_year(
        self, user_id: int, group_id: int, year: int
    ) -> int:
        prefix = f"{year}-"
        row = await self._fetchone(
            """
            SELECT COUNT(*) AS cnt FROM daily_checkins
            WHERE user_id=? AND group_id=? AND checkin_date LIKE ?;
            """,
            (user_id, group_id, prefix + "%"),
        )
        return row["cnt"] if row else 0

    async def count_checkins_total(self, user_id: int, group_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS cnt FROM daily_checkins WHERE user_id=? AND group_id=?;",
            (user_id, group_id),
        )
        return row["cnt"] if row else 0

    async def count_group_checkins_total(self, group_id: int) -> int:
        """Return total check-ins ever recorded in a group."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS cnt FROM daily_checkins WHERE group_id=?;",
            (group_id,),
        )
        return row["cnt"] if row else 0

    async def get_who_checked_in(
        self, group_id: int, checkin_date: date
    ) -> list[int]:
        """Return list of user_ids that checked in on given date."""
        rows = await self._fetchall(
            "SELECT user_id FROM daily_checkins WHERE group_id=? AND checkin_date=?;",
            (group_id, checkin_date.isoformat()),
        )
        return [r["user_id"] for r in rows]

    async def get_monthly_leaderboard(
        self, group_id: int, year: int, month: int
    ) -> list[aiosqlite.Row]:
        """Return top readers for a month ordered by check-in count."""
        prefix = f"{year}-{month:02d}-"
        return await self._fetchall(
            """
            SELECT dc.user_id,
                   u.first_name || ' ' || u.last_name AS full_name,
                   u.username,
                   COUNT(*) AS days
            FROM daily_checkins dc
            JOIN users u ON u.user_id = dc.user_id AND u.group_id = dc.group_id
            WHERE dc.group_id = ? AND dc.checkin_date LIKE ?
            GROUP BY dc.user_id
            ORDER BY days DESC
            LIMIT 10;
            """,
            (group_id, prefix + "%"),
        )

    async def get_leaderboard_by_streak(
        self, group_id: int, limit: int = 10
    ) -> list[aiosqlite.Row]:
        """Return top readers ordered by current streak."""
        return await self._fetchall(
            """
            SELECT u.user_id,
                   u.first_name || ' ' || u.last_name AS full_name,
                   u.username,
                   s.current_streak AS score
            FROM streaks s
            JOIN users u ON u.user_id = s.user_id AND u.group_id = s.group_id
            WHERE s.group_id = ? AND u.is_active = 1
            ORDER BY s.current_streak DESC
            LIMIT ?;
            """,
            (group_id, limit),
        )

    async def get_leaderboard_by_total(
        self, group_id: int, limit: int = 10
    ) -> list[aiosqlite.Row]:
        """Return top readers ordered by total check-ins."""
        return await self._fetchall(
            """
            SELECT dc.user_id,
                   u.first_name || ' ' || u.last_name AS full_name,
                   u.username,
                   COUNT(*) AS score
            FROM daily_checkins dc
            JOIN users u ON u.user_id = dc.user_id AND u.group_id = dc.group_id
            WHERE dc.group_id = ? AND u.is_active = 1
            GROUP BY dc.user_id
            ORDER BY score DESC
            LIMIT ?;
            """,
            (group_id, limit),
        )

    async def get_weekly_checkin_counts(
        self, group_id: int, from_date: date, to_date: date
    ) -> list[aiosqlite.Row]:
        """Return daily confirmed/member counts for a date range (for weekly report)."""
        return await self._fetchall(
            """
            SELECT report_date, confirmed, active_members
            FROM daily_reports
            WHERE group_id = ?
              AND report_date >= ? AND report_date <= ?
            ORDER BY report_date;
            """,
            (group_id, from_date.isoformat(), to_date.isoformat()),
        )

    async def get_best_month(
        self, user_id: int, group_id: int
    ) -> tuple[int, int, int]:
        """
        Return (year, month, count) of the month with the most check-ins.
        Returns (0, 0, 0) if no data.
        """
        row = await self._fetchone(
            """
            SELECT SUBSTR(checkin_date, 1, 7) AS ym, COUNT(*) AS cnt
            FROM daily_checkins
            WHERE user_id = ? AND group_id = ?
            GROUP BY ym
            ORDER BY cnt DESC
            LIMIT 1;
            """,
            (user_id, group_id),
        )
        if not row:
            return (0, 0, 0)
        ym = row["ym"]
        year, month = int(ym[:4]), int(ym[5:7])
        return (year, month, row["cnt"])

    async def get_first_checkin_date(
        self, user_id: int, group_id: int
    ) -> Optional[date]:
        """Return the date of the user's very first check-in, or None."""
        row = await self._fetchone(
            """
            SELECT MIN(checkin_date) AS first_date FROM daily_checkins
            WHERE user_id = ? AND group_id = ?;
            """,
            (user_id, group_id),
        )
        if row and row["first_date"]:
            return date.fromisoformat(row["first_date"])
        return None

    async def reset_month_checkins(self, group_id: int, year: int, month: int) -> None:
        """Delete all check-ins for a group in a given year-month."""
        prefix = f"{year}-{month:02d}-"
        await self._execute(
            "DELETE FROM daily_checkins WHERE group_id=? AND checkin_date LIKE ?;",
            (group_id, prefix + "%"),
            commit=True,
        )

    # ------------------------------------------------------------------
    # Internal: streak update
    # ------------------------------------------------------------------

    async def _update_streak(
        self, user_id: int, group_id: int, checkin_date: date
    ) -> None:
        """Recalculate and persist the user's current and longest streak."""
        row = await self._fetchone(
            "SELECT current_streak, longest_streak, last_checkin FROM streaks "
            "WHERE user_id=? AND group_id=?;",
            (user_id, group_id),
        )
        if not row:
            return

        last_str = row["last_checkin"]
        current = row["current_streak"]
        longest = row["longest_streak"]

        if last_str:
            last = date.fromisoformat(last_str)
            if checkin_date == last + timedelta(days=1):
                current += 1
            elif checkin_date == last:
                return  # same day, no change
            else:
                current = 1
        else:
            current = 1

        longest = max(longest, current)

        await self._execute(
            """
            UPDATE streaks SET current_streak=?, longest_streak=?, last_checkin=?
            WHERE user_id=? AND group_id=?;
            """,
            (current, longest, checkin_date.isoformat(), user_id, group_id),
            commit=True,
        )

    # ==================================================================
    # STREAKS
    # ==================================================================

    async def get_streak(
        self, user_id: int, group_id: int
    ) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM streaks WHERE user_id=? AND group_id=?;",
            (user_id, group_id),
        )

    # ==================================================================
    # SETTINGS
    # ==================================================================

    async def get_settings(self, group_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM settings WHERE group_id=?;", (group_id,)
        )

    async def update_setting(self, group_id: int, key: str, value: str) -> None:
        """Update a single setting column for a group."""
        allowed = {
            "post_time", "report_time", "timezone", "plan_key",
            "custom_reading", "reminder_enabled", "reminder_times",
            "announce_badges", "reading_start", "report_enabled",
            "milestones_enabled", "weekly_report_enabled",
            "daily_verse_enabled", "daily_hadith_enabled", "daily_dua_enabled",
        }
        if key not in allowed:
            raise ValueError(f"Unknown setting key: {key!r}")
        await self._execute(
            f"UPDATE settings SET {key} = ? WHERE group_id = ?;",  # noqa: S608
            (value, group_id),
            commit=True,
        )

    async def get_all_group_settings(self) -> list[aiosqlite.Row]:
        """Return settings joined with group info for all active groups."""
        return await self._fetchall(
            """
            SELECT s.*, g.title, g.group_id AS gid
            FROM settings s
            JOIN groups g ON g.group_id = s.group_id
            WHERE g.is_active = 1;
            """
        )

    # ==================================================================
    # READING PLANS
    # ==================================================================

    async def get_plan(self, plan_key: str) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM reading_plans WHERE plan_key=?;", (plan_key,)
        )

    async def get_all_plans(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM reading_plans ORDER BY plan_key;")

    # ==================================================================
    # DAILY REPORTS
    # ==================================================================

    async def upsert_daily_report(
        self,
        group_id: int,
        report_date: date,
        confirmed: int,
        pending: int,
        active_members: int,
    ) -> None:
        now = datetime.utcnow().isoformat()
        await self._execute(
            """
            INSERT INTO daily_reports
                (group_id, report_date, confirmed, pending, active_members, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, report_date) DO UPDATE SET
                confirmed      = excluded.confirmed,
                pending        = excluded.pending,
                active_members = excluded.active_members,
                created_at     = excluded.created_at;
            """,
            (group_id, report_date.isoformat(), confirmed, pending, active_members, now),
            commit=True,
        )

    async def get_daily_report(
        self, group_id: int, report_date: date
    ) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM daily_reports WHERE group_id=? AND report_date=?;",
            (group_id, report_date.isoformat()),
        )

    async def get_monthly_report_data(
        self, group_id: int, year: int, month: int
    ) -> list[aiosqlite.Row]:
        prefix = f"{year}-{month:02d}-"
        return await self._fetchall(
            "SELECT * FROM daily_reports WHERE group_id=? AND report_date LIKE ?;",
            (group_id, prefix + "%"),
        )

    async def get_weekly_report_data(
        self, group_id: int, from_date: date, to_date: date
    ) -> list[aiosqlite.Row]:
        """Return daily report rows for a week date range."""
        return await self._fetchall(
            """
            SELECT * FROM daily_reports
            WHERE group_id = ?
              AND report_date >= ? AND report_date <= ?
            ORDER BY report_date;
            """,
            (group_id, from_date.isoformat(), to_date.isoformat()),
        )

    # ==================================================================
    # SKIPPED DAYS
    # ==================================================================

    async def mark_day_skipped(self, group_id: int, skip_date: date) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO skipped_days (group_id, skip_date) VALUES (?, ?);",
            (group_id, skip_date.isoformat()),
            commit=True,
        )

    async def is_day_skipped(self, group_id: int, skip_date: date) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM skipped_days WHERE group_id=? AND skip_date=?;",
            (group_id, skip_date.isoformat()),
        )
        return row is not None

    # ==================================================================
    # ACHIEVEMENTS
    # ==================================================================

    async def has_achievement(
        self, user_id: int, group_id: int, achievement_key: str
    ) -> bool:
        """Return True if the user already has this achievement."""
        row = await self._fetchone(
            """
            SELECT 1 FROM achievements
            WHERE user_id=? AND group_id=? AND achievement_key=?;
            """,
            (user_id, group_id, achievement_key),
        )
        return row is not None

    async def grant_achievement(
        self, user_id: int, group_id: int, achievement_key: str
    ) -> bool:
        """
        Grant an achievement. Returns True if newly granted, False if already exists.
        """
        now = datetime.utcnow().isoformat()
        try:
            await self._execute(
                """
                INSERT INTO achievements (user_id, group_id, achievement_key, earned_at)
                VALUES (?, ?, ?, ?);
                """,
                (user_id, group_id, achievement_key, now),
                commit=True,
            )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_user_achievements(
        self, user_id: int, group_id: int
    ) -> list[aiosqlite.Row]:
        """Return all achievements earned by a user in a group."""
        return await self._fetchall(
            """
            SELECT achievement_key, earned_at FROM achievements
            WHERE user_id=? AND group_id=?
            ORDER BY earned_at;
            """,
            (user_id, group_id),
        )

    # ==================================================================
    # GROUP MILESTONES
    # ==================================================================

    async def has_milestone(self, group_id: int, milestone_key: str) -> bool:
        """Return True if the group has already reached this milestone."""
        row = await self._fetchone(
            "SELECT 1 FROM group_milestones WHERE group_id=? AND milestone_key=?;",
            (group_id, milestone_key),
        )
        return row is not None

    async def grant_milestone(self, group_id: int, milestone_key: str) -> bool:
        """
        Record a group milestone. Returns True if newly granted.
        """
        now = datetime.utcnow().isoformat()
        try:
            await self._execute(
                """
                INSERT INTO group_milestones (group_id, milestone_key, reached_at)
                VALUES (?, ?, ?);
                """,
                (group_id, milestone_key, now),
                commit=True,
            )
            return True
        except aiosqlite.IntegrityError:
            return False

    # ==================================================================
    # LAST MOTIVATION TRACKING
    # ==================================================================

    async def get_last_motivation_index(self, group_id: int) -> int:
        """Return the index of the last motivation template used, or -1."""
        row = await self._fetchone(
            "SELECT last_index FROM last_motivation WHERE group_id=?;",
            (group_id,),
        )
        return row["last_index"] if row else -1

    async def set_last_motivation_index(
        self, group_id: int, index: int, for_date: date
    ) -> None:
        """Persist the motivation index used today."""
        await self._execute(
            """
            INSERT INTO last_motivation (group_id, last_index, last_date)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                last_index = excluded.last_index,
                last_date  = excluded.last_date;
            """,
            (group_id, index, for_date.isoformat()),
            commit=True,
        )

    # ==================================================================
    # EXPORT
    # ==================================================================

    async def export_group_csv(self, group_id: int) -> str:
        """Return a CSV string of all check-ins for a group."""
        rows = await self._fetchall(
            """
            SELECT u.user_id, u.first_name, u.last_name, u.username,
                   dc.checkin_date, dc.checked_in_at
            FROM daily_checkins dc
            JOIN users u ON u.user_id = dc.user_id AND u.group_id = dc.group_id
            WHERE dc.group_id = ?
            ORDER BY dc.checkin_date, u.user_id;
            """,
            (group_id,),
        )
        lines = ["user_id,first_name,last_name,username,checkin_date,checked_in_at"]
        for r in rows:
            lines.append(
                f"{r['user_id']},{r['first_name']},{r['last_name']},"
                f"{r['username']},{r['checkin_date']},{r['checked_in_at']}"
            )
        return "\n".join(lines)
