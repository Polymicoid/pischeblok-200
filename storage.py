import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from schedule import get_now

DB_PATH = Path("/data/pkd1_bot.db")
DEFAULT_TIMINGS = {
    "pair_duration": "95",
    "wd_break_1": "30",
    "wd_break_2": "30",
    "wd_break_other": "10",
    "sat_break_1": "10",
    "sat_break_2": "30",
    "sat_break_other": "10",
}


@contextmanager
def connection():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        yield conn


@contextmanager
def polling_lock(path: Path):
    # Файл остаётся на диске, а блокировку ОС освобождает даже при падении процесса.
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            acquire()
        except OSError:
            raise RuntimeError("Эта база уже используется другим экземпляром бота.") from None
        yield


def init_db():
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                date_str TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                content TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()]
        if "photo_id" not in cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN photo_id TEXT")
        if "topic_msg_id" not in cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN topic_msg_id INTEGER")
        if "raw_content" not in cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN raw_content TEXT")
        if "has_class_hour" not in cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN has_class_hour INTEGER DEFAULT 0")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subject_rooms (
                subject_key TEXT PRIMARY KEY,
                subject_name TEXT NOT NULL,
                room TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_screens (
                chat_id INTEGER NOT NULL,
                thread_key INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ephemeral_id INTEGER NOT NULL,
                has_photo INTEGER NOT NULL,
                PRIMARY KEY (chat_id, thread_key, user_id)
            )
        """)
        for k, v in DEFAULT_TIMINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()


def get_setting(key: str, default: str = None):
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    with connection() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        conn.commit()


def get_timing_int(key: str) -> int:
    default = int(DEFAULT_TIMINGS[key])
    try:
        value = int(get_setting(key, str(default)))
    except (ValueError, TypeError):
        return default
    return value if valid_timing(key, value) else default


def valid_timing(key: str, value: int) -> bool:
    return key in DEFAULT_TIMINGS and (1 if key == "pair_duration" else 0) <= value <= 180


def get_saved_room(subject_name: str) -> str | None:
    key = subject_name.strip().lower()
    with connection() as conn:
        row = conn.execute("SELECT room FROM subject_rooms WHERE subject_key = ?", (key,)).fetchone()
        return row[0] if row else None


def get_all_subject_rooms():
    with connection() as conn:
        return conn.execute("SELECT subject_name, room FROM subject_rooms ORDER BY subject_name").fetchall()


def clear_subject_rooms():
    with connection() as conn:
        conn.execute("DELETE FROM subject_rooms")
        conn.commit()


def get_topic_config():
    chat_id = get_setting("topic_chat_id")
    thread_id = get_setting("topic_thread_id")
    if not chat_id:
        return None, None
    try:
        return int(chat_id), (int(thread_id) if thread_id and thread_id != "None" else None)
    except (TypeError, ValueError):
        return None, None


def bind_topic(chat_id: int, thread_id: int | None, menu_id: int):
    previous = get_topic_config()
    with connection() as conn:
        if previous != (chat_id, thread_id):
            conn.execute("UPDATE schedules SET topic_msg_id = NULL")
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, str(value)) for key, value in (
                ("topic_chat_id", chat_id), ("topic_thread_id", thread_id),
                ("topic_menu_msg_id", menu_id),
            )],
        )


def get_topic_screen(chat_id: int, thread_key: int, user_id: int) -> tuple[int, bool] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT ephemeral_id, has_photo FROM topic_screens WHERE chat_id=? AND thread_key=? AND user_id=?",
            (chat_id, thread_key, user_id),
        ).fetchone()
    return (row[0], bool(row[1])) if row else None


def save_topic_screen(chat_id: int, thread_key: int, user_id: int, ephemeral_id: int, has_photo: bool):
    with connection() as conn:
        conn.execute("""
            INSERT INTO topic_screens (chat_id, thread_key, user_id, ephemeral_id, has_photo)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_key, user_id) DO UPDATE SET
                ephemeral_id=excluded.ephemeral_id,
                has_photo=excluded.has_photo
        """, (chat_id, thread_key, user_id, ephemeral_id, has_photo))


def clear_topic_screen(chat_id: int, thread_key: int, user_id: int):
    with connection() as conn:
        conn.execute(
            "DELETE FROM topic_screens WHERE chat_id=? AND thread_key=? AND user_id=?",
            (chat_id, thread_key, user_id),
        )


def set_topic_message(date_str: str, message_id: int):
    with connection() as conn:
        conn.execute(
            "UPDATE schedules SET topic_msg_id = ? WHERE date_str = ?",
            (message_id, date_str),
        )


def get_schedule(date_str: str):
    with connection() as conn:
        return conn.execute(
            "SELECT status, content, updated_at, photo_id, topic_msg_id, raw_content, has_class_hour FROM schedules WHERE date_str = ?",
            (date_str,),
        ).fetchone()


def save_schedule(
    date_str: str,
    status: str,
    content: str = None,
    photo_id: str = None,
    topic_msg_id: int = None,
    raw_content: str = None,
    has_class_hour: int = 0,
    learned_rooms: tuple[tuple[str, str], ...] = (),
):
    updated_at = get_now().strftime("%Y-%m-%d %H:%M")
    with connection() as conn:
        conn.execute("""
            INSERT INTO schedules (date_str, status, content, updated_at, photo_id, topic_msg_id, raw_content, has_class_hour)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_str) DO UPDATE SET
                status = excluded.status,
                content = excluded.content,
                updated_at = excluded.updated_at,
                photo_id = excluded.photo_id,
                topic_msg_id = COALESCE(excluded.topic_msg_id, schedules.topic_msg_id),
                raw_content = excluded.raw_content,
                has_class_hour = excluded.has_class_hour
        """, (date_str, status, content, updated_at, photo_id, topic_msg_id, raw_content, has_class_hour))
        conn.executemany("""
            INSERT INTO subject_rooms (subject_key, subject_name, room) VALUES (?, ?, ?)
            ON CONFLICT(subject_key) DO UPDATE SET
                subject_name = excluded.subject_name,
                room = excluded.room
        """, ((subject.strip().lower(), subject.strip(), room.strip()) for subject, room in learned_rooms))
        conn.commit()


def delete_schedule(date_str: str):
    with connection() as conn:
        conn.execute("DELETE FROM schedules WHERE date_str = ?", (date_str,))
        conn.commit()
