import asyncio
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
        self.assertIn("❌ Пропуск", formatted)
        self.assertIn("Спортзал", formatted)
        self.assertIn("&lt;важно&gt; &amp; всем", formatted)
        self.assertEqual(learned, [("Биология", "Каб. 12А")])
        manual, _ = schedule.format_schedule_blocks("9.05–10.40 Биология", "2026-11-16", False)
        self.assertIn("1 пара (09:05 – 10:40)", manual)
        for raw in ("25:30–26:00 Биология", "10:40–09:05 Биология", "09:05–10:40"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                schedule.format_schedule_blocks(raw, "2026-11-16", False)

    def test_subject_emojis_for_names_tags_and_legacy_schedule(self):
        cases = (
            ("матан", "математика", "📐"), ("общество", "обществознание", "👥"),
            ("литература", "литература", "📖"), ("русский_язык", "русский язык", "✍️"),
            ("физика", "физика", "⚛️"), ("физкультура", "физическая культура", "⚽"),
            ("химия", "химия", "🧪"), ("английский_язык", "английский язык", "🇬🇧"),
            ("немецкий_язык", "немецкий язык", "🇩🇪"),
            ("ОБЗР", "Основы безопасности и защиты Родины", "🛡️"),
            ("ОИД", "Основы исследовательской деятельности", "🔎"),
            ("история", "история", "🏛️"), ("информатика", "информатика", "💻"),
            ("география", "география", "🌍"), ("микробио", "микробиология", "🔬"),
        )
        for tag, name, emoji in cases:
            with self.subTest(name=name):
                self.assertEqual(schedule.pick_subject_emoji(name), emoji)
                self.assertEqual(schedule.pick_subject_emoji("#" + tag), emoji)
        formatted, rooms = schedule.format_schedule_blocks(
            "Пропуск\nНет пары\n—\n09:05–10:40 #Матан 12", "2026-11-16", False,
        )
        self.assertEqual(formatted.count("❌ Пропуск"), 3)
        self.assertIn("📐 #Матан\n📍 Каб. 12", formatted)
        self.assertEqual(rooms, [("#Матан", "Каб. 12")])
        legacy = "<b>1 пара (08:30 – 10:05)</b>\n📚 География\n📍 Каб. 3\n\n<b>2 пара</b>\n⏸ Окно / нет пары"
        storage.save_schedule("2026-11-16", "published", legacy)
        for view in (main.build_schedule_view, main.group_interface.schedule_screen):
            text = view("today")[0]
            self.assertIn("🌍 География", text)
            self.assertIn("❌ Окно / нет пары", text)
        self.assertEqual(storage.get_schedule("2026-11-16")[1], legacy)

    def test_schedule_statuses(self):
        day = "2026-11-16"
        self.assertIn("не опубликовано", main.build_schedule_view("today")[0])
        storage.save_schedule(day, "holiday")
        self.assertIn("Выходной", main.build_schedule_view("today")[0])
        storage.save_schedule(day, "published", "<b>1 пара (08:30 – 10:05)</b>\n" + "🧪<&" * 300, "photo")
        text, photo, has_schedule = main.build_schedule_view("today")
        self.assertIn("1 пара", text)
        self.assertEqual(photo, "photo")
        self.assertTrue(has_schedule)
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
        self.failed_methods = {}
        self.message_id = 100

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Скачивание не ожидалось")
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name in self.failed_methods:
            raise TelegramBadRequest(method, self.failed_methods[name])
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="Тест", username="test_bot")
        if self.cannot_edit and name == "EditMessageText" and method.chat_id < 0:
            raise TelegramBadRequest(method, "there is no text in the message to edit")
        if name in {"SendMessage", "SendPhoto", "SendRichMessage"}:
            if self.fail_post and method.chat_id < 0:
                raise TelegramBadRequest(method, "test failure")
            self.message_id += 1
            payload = {
                "message_id": self.message_id,
                "chat": {"id": method.chat_id, "type": "supergroup" if method.chat_id < 0 else "private"},
                "date": 1,
            }
            if params := method.ephemeral_message_parameters:
                payload.update(
                    message_id=0,
                    ephemeral_message_id=self.message_id,
                    receiver_user={"id": params.receiver_user_id, "is_bot": False, "first_name": "Тест"},
                )
            return Message.model_validate(payload, context={"bot": bot})
        return True


class RouterCase(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(main, "ADMIN_IDS", frozenset({11, 22})))
        self.enterContext(patch.object(main, "BOT_USERNAME", "test_bot"))
        self.session = FakeSession()
        self.bot = Bot(token="1:offline-test-value", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(main.router)
        self.addAsyncCleanup(self.bot.session.close)
        self.addCleanup(setattr, main.router, "_parent_router", None)

    async def feed(
        self, *, action=None, text=None, user_id=11, message_id=10, group=False,
        photo=False, inline=False, thread_id=None, ephemeral_id=None, receiver_id=None,
        callback_id=None, chat_id=-1007,
    ):
        user = {"id": user_id, "is_bot": False, "first_name": "Тест"}
        chat = {"id": chat_id if group else user_id, "type": "supergroup" if group else "private"}
        message = {"message_id": message_id, "date": 1, "chat": chat}
        if thread_id is not None:
            message.update(message_thread_id=thread_id, is_topic_message=True)
        if ephemeral_id:
            message.update(
                message_id=0, ephemeral_message_id=ephemeral_id,
                receiver_user={**user, "id": receiver_id or user_id},
            )
        if photo:
            message["photo"] = [{"file_id": "photo", "file_unique_id": "unique", "width": 120, "height": 120}]
        update = {"update_id": 1}
        if action:
            update["callback_query"] = {
                "id": callback_id or str(user_id), "from": user, "chat_instance": "test",
                "data": action, "message": message,
            }
        elif inline:
            update["inline_query"] = {"id": "1", "from": user, "query": "", "offset": ""}
        elif photo:
            update["message"] = {
                **message, "from": user, "caption": text,
            }
        else:
            update["message"] = {**message, "from": user, "text": text}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(update, context={"bot": self.bot}))


class RouterTests(RouterCase):
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

    async def test_photo_only_preview_and_publication(self):
        await self.feed(action="admin:edit:tomorrow:2026-11-17", user_id=22)
        await self.feed(photo=True, user_id=22, message_id=11)
        await self.feed(action="admin:only_photo", user_id=22, message_id=self.session.message_id)
        await self.feed(action="admin:confirm:bot", user_id=22, message_id=self.session.message_id)
        row = storage.get_schedule("2026-11-17")
        self.assertEqual((row[0], row[1], row[3]), ("published", None, "photo"))

    async def test_inline_sends_schedule_text(self):
        storage.save_schedule("2026-11-16", "published", "<b>Биология</b>")
        await self.feed(inline=True, user_id=99)
        self.assertEqual(type(self.session.calls[-1]).__name__, "AnswerInlineQuery")
        article = self.session.calls[-1].results[0]
        self.assertIn("<b>Биология</b>", article.input_message_content.message_text)


class TopicInterfaceTests(RouterCase):
    def methods(self):
        return [type(call).__name__ for call in self.session.calls]

    async def open(self, action, **kwargs):
        await self.feed(action=f"topic:{action}", group=True, thread_id=2, **kwargs)

    async def test_pin_and_refresh_same_message(self):
        await self.feed(text="/set_topic", group=True, thread_id=2)
        self.assertEqual(self.methods(), ["DeleteMessage", "SendMessage", "PinChatMessage", "SendMessage"])
        menu = self.session.calls[1]
        self.assertIn("ПИЩЕБЛОК-200", menu.text)
        self.assertNotIn("ЛЧПП", menu.text)
        self.assertEqual(menu.reply_markup.inline_keyboard[0][0].callback_data, "topic:schedule")
        self.assertTrue(all(
            button.callback_data is None or button.callback_data.startswith("topic:")
            for row in menu.reply_markup.inline_keyboard for button in row
        ))
        saved_id = int(storage.get_setting("topic_menu_msg_id"))
        storage.save_schedule("2026-11-16", "published", topic_msg_id=22)
        self.session.calls.clear()
        self.session.failed_methods["EditMessageText"] = "message is not modified"
        await self.feed(text="/set_topic", group=True, thread_id=2)
        self.assertEqual(self.methods(), ["DeleteMessage", "EditMessageText", "PinChatMessage", "SendMessage"])
        self.assertEqual(self.session.calls[2].message_id, saved_id)
        self.assertEqual(storage.get_schedule("2026-11-16")[4], 22)

    async def test_rebind_preserves_old_menu_on_pin_failure(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "published", topic_msg_id=22)
        self.session.failed_methods["PinChatMessage"] = "bot has no rights"
        await self.feed(text="/set_topic", group=True, thread_id=3)
        self.assertEqual(storage.get_topic_config(), (-1007, 2))
        self.assertEqual(storage.get_setting("topic_menu_msg_id"), "42")
        self.assertEqual(storage.get_schedule("2026-11-16")[4], 22)
        self.assertNotIn(42, [call.message_id for call in self.session.calls if type(call).__name__ == "DeleteMessage"])

    async def test_missing_pin_is_replaced_and_rebind_commits_after_pin(self):
        storage.bind_topic(-1007, 2, 42)
        self.session.failed_methods["EditMessageText"] = "message to edit not found"
        await self.feed(text="/set_topic", group=True, thread_id=2)
        self.assertEqual(self.methods(), [
            "DeleteMessage", "EditMessageText", "SendMessage", "PinChatMessage", "DeleteMessage", "SendMessage",
        ])
        self.assertEqual(self.session.calls[4].message_id, 42)
        self.assertEqual(storage.get_setting("topic_menu_msg_id"), "101")

    async def test_student_navigation_is_private_for_two_concurrent_users(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule(
            "2026-11-16", "published",
            "<b>1 пара (08:30 – 10:05)</b>\n🧪 Химия\n📍 Каб. 6\n\n"
            "<b>2 пара (10:35 – 12:10)</b>\n📚 История",
        )
        await asyncio.gather(*(self.open("schedule", user_id=user, message_id=42) for user in (98, 99)))
        sends = [call for call in self.session.calls if type(call).__name__ == "SendMessage"]
        self.assertEqual({call.ephemeral_message_parameters.receiver_user_id for call in sends}, {98, 99})
        self.assertEqual({call.ephemeral_message_parameters.callback_query_id for call in sends}, {"98", "99"})
        self.assertTrue(all(call.message_thread_id == 2 for call in sends))
        self.session.calls.clear()
        await asyncio.gather(
            self.open("today", user_id=98, ephemeral_id=101),
            self.open("tomorrow", user_id=99, ephemeral_id=102),
        )
        edits = [call for call in self.session.calls if type(call).__name__ == "EditEphemeralMessageText"]
        self.assertEqual({(call.receiver_user_id, call.ephemeral_message_id) for call in edits}, {(98, 101), (99, 102)})
        today = next(call for call in edits if call.receiver_user_id == 98)
        self.assertEqual(today.parse_mode, "HTML")
        self.assertIn("1️⃣ 08:30–10:05", today.text)
        self.assertIn("Кабинет 6\n\n<b>2️⃣", today.text)
        self.assertIn("пока не опубликовано", next(call for call in edits if call.receiver_user_id == 99).text)
        await self.open("main", user_id=98, ephemeral_id=101)
        self.assertIn("ПИЩЕБЛОК-200", self.session.calls[-2].text)
        self.assertNotIn("EditMessageText", self.methods())
        self.assertEqual(storage.get_setting("topic_menu_msg_id"), "42")

    async def test_expired_screen_is_replaced_for_same_student(self):
        storage.bind_topic(-1007, 2, 42)
        self.session.failed_methods["EditEphemeralMessageText"] = "message to edit not found"
        await self.open("hw", user_id=99, ephemeral_id=87)
        self.assertEqual(self.methods(), ["EditEphemeralMessageText", "SendMessage", "DeleteEphemeralMessage", "AnswerCallbackQuery"])
        self.assertEqual(self.session.calls[1].ephemeral_message_parameters.receiver_user_id, 99)
        self.assertEqual(self.session.calls[2].ephemeral_message_id, 87)
        self.assertIn("Раздел в разработке", self.session.calls[1].text)

    async def test_stale_or_foreign_buttons_only_get_private_alerts(self):
        storage.bind_topic(-1007, 2, 42)
        await self.open("schedule", user_id=99, message_id=41)
        await self.open("main", user_id=99, ephemeral_id=101, receiver_id=98)
        self.assertEqual(self.methods(), ["AnswerCallbackQuery", "AnswerCallbackQuery"])
        self.assertTrue(all(call.show_alert for call in self.session.calls))

    async def test_temporary_send_failure_uses_private_alert(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "holiday")
        await self.open("today", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["SendMessage", "AnswerCallbackQuery"])
        self.assertIn("Выходной", self.session.calls[0].text)
        self.assertEqual(self.session.calls[0].ephemeral_message_parameters.receiver_user_id, 99)
        self.session.calls.clear()
        self.session.failed_methods["SendMessage"] = "unsupported"
        await self.open("today", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["SendMessage", "AnswerCallbackQuery"])
        self.assertTrue(self.session.calls[-1].show_alert)
        self.assertIn("Открыть бота в ЛС", self.session.calls[-1].text)
        self.assertEqual(storage.get_topic_screen(-1007, 2, 99), (101, False))
        self.assertLessEqual(schedule.telegram_length(self.session.calls[-1].text), 200)

    async def test_repeated_pin_click_opens_screen_in_current_client_and_retires_old_screen(self):
        storage.bind_topic(-1007, 2, 42)
        await self.open("schedule", user_id=99, message_id=42)
        storage.init_db()
        self.session.calls.clear()
        await self.open("schedule", user_id=99, message_id=42, callback_id="current-client")
        self.assertEqual(self.methods(), ["SendMessage", "DeleteEphemeralMessage", "AnswerCallbackQuery"])
        self.assertEqual(self.session.calls[0].ephemeral_message_parameters.callback_query_id, "current-client")
        self.assertEqual(self.session.calls[1].ephemeral_message_id, 101)
        self.assertEqual(storage.get_topic_screen(-1007, 2, 99), (102, False))
        self.session.calls.clear()
        await self.open("main", user_id=99, ephemeral_id=88)
        self.assertEqual(self.methods(), ["EditEphemeralMessageText", "DeleteEphemeralMessage", "AnswerCallbackQuery"])
        self.assertEqual(self.session.calls[0].ephemeral_message_id, 102)
        self.assertEqual(self.session.calls[1].ephemeral_message_id, 88)

    async def test_photo_and_back_to_main_after_reopening_db(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "published", "🧪" * 700, "photo")
        storage.init_db()
        await self.open("today", user_id=99, message_id=42)
        sent = self.session.calls[0]
        self.assertEqual(type(sent).__name__, "SendPhoto")
        self.assertEqual(sent.photo, "photo")
        self.assertEqual(sent.ephemeral_message_parameters.receiver_user_id, 99)
        self.assertLessEqual(schedule.telegram_length(schedule.ScheduleHTML(sent.caption).plain_text()), 1024)
        self.assertEqual(sent.reply_markup.inline_keyboard[-1][0].url, "https://t.me/test_bot?start=today")
        await self.open("main", user_id=99, ephemeral_id=101, photo=True)
        self.assertEqual(self.methods()[-3:], ["SendMessage", "DeleteEphemeralMessage", "AnswerCallbackQuery"])
        self.assertNotIn("DeleteMessage", self.methods())

    async def test_photo_failure_preserves_text_and_private_link(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "published", "Химия", "photo")
        self.session.failed_methods["SendPhoto"] = "unsupported photo"
        await self.open("today", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["SendPhoto", "SendMessage", "AnswerCallbackQuery"])
        fallback = self.session.calls[1]
        self.assertIn("Химия", fallback.text)
        self.assertEqual(fallback.ephemeral_message_parameters.receiver_user_id, 99)

    async def test_brand_and_callback_sizes(self):
        await self.feed(text="/start", user_id=99)
        self.assertIn("ПИЩЕБЛОК-200", self.session.calls[0].text)
        self.assertNotIn("ЛЧПП", self.session.calls[0].text)
        for section in main.group_interface.SCREENS:
            _, _, markup = main.group_interface.render(section, "test_bot")
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64)


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
