import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, Update, User

import main
import schedule
import storage
from config import parse_admin_ids


class DatabaseCase:
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "test.db"
        self.enterContext(patch.object(storage, "DB_PATH", path))
        self.enterContext(patch.object(schedule, "get_now", return_value=datetime(2026, 11, 16, 9)))
        storage.init_db()


class ScheduleTests(DatabaseCase, unittest.TestCase):
    def test_admin_ids(self):
        self.assertEqual(parse_admin_ids("11, 22"), {11, 22})
        for invalid in ("", "0", "-1", "11,11", "1,", "a", "9999999999999999"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_admin_ids(invalid)

    def test_weekday_saturday_and_class_hour(self):
        regular = schedule.calculate_pair_times("2026-11-16", False)
        saturday = schedule.calculate_pair_times("2026-11-21", False)
        special = schedule.calculate_pair_times("2026-11-19", True)
        self.assertEqual((regular[1], regular[2]), ("08:30 – 10:05", "10:35 – 12:10"))
        self.assertEqual(saturday[2], "10:15 – 11:50")
        self.assertEqual(special[1], "09:05 – 10:40")
        storage.set_setting("pair_duration", "60")
        self.assertEqual(schedule.calculate_pair_times("2026-11-16", False)[1], "08:30 – 09:30")

    def test_local_midnight(self):
        real_get_now = self.get_now_function
        for utc_hour, utc_minute, today, tomorrow in (
            (18, 59, "2026-12-31", "2027-01-01"),
            (19, 0, "2027-01-01", "2027-01-02"),
        ):
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime(2026, 12, 31, utc_hour, utc_minute, tzinfo=timezone.utc).astimezone(tz)
            with patch.object(schedule, "get_now", real_get_now), patch.object(schedule, "datetime", Clock):
                self.assertEqual(schedule.get_target_date("today").date().isoformat(), today)
                self.assertEqual(schedule.get_target_date("tomorrow").date().isoformat(), tomorrow)

    def test_formatting_manual_time_rooms_and_escaping(self):
        storage.save_schedule("2026-11-15", "published", learned_rooms=(("Физра", "Спортзал"),))
        raw = "1.9.05-10.40 биология каб. 12А\n2. физра\n3. —\n4. социально-экономическая география\n- <важно> & всем"
        formatted, learned = schedule.format_schedule_blocks(raw, "2026-11-16", True)
        self.assertIn("Классный час (08:30 – 09:00)", formatted)
        self.assertIn("1 пара (09:05 – 10:40)", formatted)
        self.assertIn("Биология", formatted)
        self.assertIn("Социально-экономическая география", formatted)
        self.assertIn("Окно / нет пары", formatted)
        self.assertIn("Спортзал", formatted)
        self.assertIn("&lt;важно&gt; &amp; всем", formatted)
        self.assertEqual(learned, [("Биология", "Каб. 12А")])
        manual, _ = schedule.format_schedule_blocks("9.05–10.40 Биология", "2026-11-16", False)
        self.assertIn("1 пара (09:05 – 10:40)", manual)
        for raw in ("25:30–26:00 Биология", "10:40–09:05 Биология", "09:05–10:40"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                schedule.format_schedule_blocks(raw, "2026-11-16", False)

    def test_statuses_and_alert_limit(self):
        day = "2026-11-16"
        self.assertIn("не опубликовано", main.build_schedule_view("today")[0])
        storage.save_schedule(day, "holiday")
        self.assertIn("Выходной", main.build_schedule_view("today")[0])
        storage.save_schedule(day, "published", "<b>1 пара (08:30 – 10:05)</b>\n" + "🧪<&" * 300, "photo")
        text, photo, has_schedule = main.build_schedule_view("today")
        self.assertIn("1 пара", text)
        self.assertEqual(photo, "photo")
        self.assertTrue(has_schedule)
        self.assertLessEqual(schedule.telegram_length(main.build_compact_alert_text("today")), 200)
        safe = schedule.ScheduleHTML("<b>Математика</b><a href='https://example.org'> &lt;3</a>")
        self.assertEqual(safe.limited(100), "<b>Математика</b> &lt;3")
        storage.delete_schedule(day)
        self.assertFalse(main.build_schedule_view("today")[2])

    def test_old_sqlite_schema_and_topic_rebind(self):
        path = storage.DB_PATH.with_name("legacy.db")
        with patch.object(storage, "DB_PATH", path):
            with storage.connection() as conn:
                conn.executescript("""
                    CREATE TABLE schedules (date_str TEXT PRIMARY KEY, status TEXT NOT NULL,
                        content TEXT, updated_at TEXT NOT NULL);
                    INSERT INTO schedules VALUES ('2026-11-16','published','История','2026-11-15 12:30');
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO settings VALUES ('pair_duration','80');
                """)
            storage.init_db()
            storage.init_db()
            self.assertEqual(storage.get_schedule("2026-11-16"), ("published", "История", "2026-11-15 12:30", None, None, None, 0))
            self.assertEqual(storage.get_timing_int("pair_duration"), 80)
            storage.bind_topic(-1001, 2, 100)
            storage.set_topic_message("2026-11-16", 5)
            storage.bind_topic(-1001, 2, 101)
            self.assertEqual(storage.get_schedule("2026-11-16")[4], 5)
            storage.bind_topic(-1001, 3, 102)
            self.assertIsNone(storage.get_schedule("2026-11-16")[4])

    def test_poll_lock(self):
        with storage.polling_lock(storage.DB_PATH):
            with self.assertRaises(RuntimeError), storage.polling_lock(storage.DB_PATH):
                self.fail("Открылась вторая блокировка")
        with storage.polling_lock(storage.DB_PATH):
            pass

    get_now_function = staticmethod(schedule.get_now)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_post = False
        self.cannot_edit = False
        self.message_id = 100

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Скачивание не ожидалось")
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="Тест", username="test_bot")
        if self.cannot_edit and name == "EditMessageText" and method.chat_id < 0:
            raise TelegramBadRequest(method, "there is no text in the message to edit")
        if name in {"SendMessage", "SendPhoto"}:
            if self.fail_post and method.chat_id < 0:
                raise TelegramBadRequest(method, "test failure")
            self.message_id += 1
            return Message.model_validate({
                "message_id": self.message_id,
                "chat": {"id": method.chat_id, "type": "private"},
                "date": 1,
            }, context={"bot": bot})
        return True


class RouterTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(main, "ADMIN_IDS", frozenset({11, 22})))
        self.session = FakeSession()
        self.bot = Bot(token="1:offline-test-value", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(main.router)
        self.addAsyncCleanup(self.bot.session.close)
        self.addCleanup(setattr, main.router, "_parent_router", None)

    async def feed(self, *, action=None, text=None, user_id=11, message_id=10, group=False, photo=False, inline=False):
        user = {"id": user_id, "is_bot": False, "first_name": "Тест"}
        chat = {"id": -1007 if group else user_id, "type": "supergroup" if group else "private"}
        message = {"message_id": message_id, "date": 1, "chat": chat}
        update = {"update_id": 1}
        if action:
            update["callback_query"] = {"id": "1", "from": user, "chat_instance": "test", "data": action, "message": message}
        elif inline:
            update["inline_query"] = {"id": "1", "from": user, "query": "", "offset": ""}
        elif photo:
            update["message"] = {
                **message, "from": user, "caption": text,
                "photo": [{"file_id": "photo", "file_unique_id": "unique", "width": 120, "height": 120}],
            }
        else:
            update["message"] = {**message, "from": user, "text": text}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(update, context={"bot": self.bot}))

    async def test_non_admin_cannot_use_admin_callbacks_or_bind_topic(self):
        storage.save_schedule("2026-11-15", "published", learned_rooms=(("Биология", "Каб. 12"),))
        for action in ("admin:menu", "admin:only_photo", "admin:rooms_clear", "admin:confirm:topic",
                       "admin:clear:today:2026-11-16", "admin:setbrk:pair_duration:1"):
            await self.feed(action=action, user_id=99)
            self.assertEqual(self.session.calls[-1].text, "⛔ Нет доступа.")
        before = len(self.session.calls)
        await self.feed(text="/set_topic", user_id=99, group=True)
        self.assertEqual(len(self.session.calls), before)
        self.assertEqual(storage.get_saved_room("Биология"), "Каб. 12")
        self.assertEqual(storage.get_topic_config(), (None, None))

    async def test_admin_rejects_old_dates_and_wrong_preview_buttons(self):
        storage.save_schedule("2026-11-16", "holiday")
        await self.feed(action="admin:clear:today:2026-11-15")
        self.assertIn("устарела", self.session.calls[-1].text)
        self.assertEqual(storage.get_schedule("2026-11-16")[0], "holiday")
        await self.feed(action="admin:edit:today:2026-11-16")
        await self.feed(action="admin:ch:yes", message_id=20)
        self.assertIn("устарела", self.session.calls[-1].text)

    async def test_publish_saves_before_failed_topic_and_can_retry(self):
        storage.bind_topic(-1007, 2, 100)
        self.session.fail_post = True
        await self.feed(action="admin:edit:today:2026-11-16")
        await self.feed(action="admin:ch:yes")
        await self.feed(text="1. Биология 12", message_id=11)
        await self.feed(action="admin:confirm:topic", message_id=self.session.message_id)
        row = storage.get_schedule("2026-11-16")
        self.assertEqual(row[0], "published")
        self.assertEqual(row[6], 1)
        self.assertIsNone(row[4])
        self.assertEqual(storage.get_saved_room("Биология"), "Каб. 12")
        self.session.fail_post = False
        await self.feed(action="admin:push:today:2026-11-16")
        self.assertGreater(storage.get_schedule("2026-11-16")[4], 0)

    async def test_photo_to_text_replacement_preserves_old_on_failure(self):
        storage.bind_topic(-1007, 2, 100)
        storage.save_schedule("2026-11-16", "published", "Расписание", "old-photo", 42)
        self.session.cannot_edit = True
        self.session.fail_post = True
        await self.feed(action="admin:holiday:today:2026-11-16")
        self.assertEqual(storage.get_schedule("2026-11-16")[4], 42)
        self.assertFalse(any(type(call).__name__ == "DeleteMessage" for call in self.session.calls))
        self.session.calls.clear()
        self.session.fail_post = False
        await self.feed(action="admin:holiday:today:2026-11-16")
        methods = [type(call).__name__ for call in self.session.calls]
        self.assertLess(methods.index("SendMessage"), methods.index("DeleteMessage"))
        self.assertEqual(storage.get_schedule("2026-11-16")[4], self.session.message_id)

    async def test_photo_only_preview_and_quick_view(self):
        await self.feed(action="admin:edit:tomorrow:2026-11-17", user_id=22)
        await self.feed(photo=True, user_id=22, message_id=11)
        await self.feed(action="admin:only_photo", user_id=22, message_id=self.session.message_id)
        await self.feed(action="admin:confirm:bot", user_id=22, message_id=self.session.message_id)
        row = storage.get_schedule("2026-11-17")
        self.assertEqual((row[0], row[1], row[3]), ("published", None, "photo"))
        await self.feed(action="quick:tomorrow", group=True, user_id=99)
        self.assertIn("только на фото", self.session.calls[-1].text)

    async def test_inline_sends_schedule_text(self):
        storage.save_schedule("2026-11-16", "published", "<b>Биология</b>")
        await self.feed(inline=True, user_id=99)
        self.assertEqual(type(self.session.calls[-1]).__name__, "AnswerInlineQuery")
        article = self.session.calls[-1].results[0]
        self.assertIn("<b>Биология</b>", article.input_message_content.message_text)


class StartupTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def test_entrypoint_keeps_pending_updates_and_closes_session(self):
        session = FakeSession()
        with (
            patch.object(session, "close", new_callable=AsyncMock) as close,
            patch.object(main, "Bot", return_value=Bot(token="1:offline-test-value", session=session)),
            patch.object(main, "load_config", return_value=("unused", frozenset({11, 22}), storage.DB_PATH)),
            patch.object(main, "ADMIN_IDS", frozenset()),
            patch.object(main, "BOT_USERNAME", ""),
            patch.object(Dispatcher, "start_polling", new_callable=AsyncMock) as run,
        ):
            self.addCleanup(setattr, main.router, "_parent_router", None)
            await main.main()
        self.assertFalse(session.calls[-1].drop_pending_updates)
        self.assertTrue(run.await_args.kwargs["handle_signals"])
        self.assertFalse(run.await_args.kwargs["handle_as_tasks"])
        close.assert_awaited_once()
