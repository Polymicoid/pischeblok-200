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
        self.assertIn("📐 Математика\n📍 Каб. 12", formatted)
        self.assertEqual(rooms, [("Математика", "Каб. 12")])
        legacy = "<b>1 пара (08:30 – 10:05)</b>\n📚 География\n📍 Каб. 3\n\n<b>2 пара</b>\n⏸ Окно / нет пары"
        storage.save_schedule("2026-11-16", "published", legacy)
        for view in (main.build_schedule_view, main.group_interface.render):
            text = view("today")[0]
            self.assertIn("🌍 География", text)
            self.assertIn("❌ Окно / нет пары", text)
        self.assertEqual(storage.get_schedule("2026-11-16")[1], legacy)

    def test_normalizes_aliases_and_shared_foreign_language_rooms(self):
        legacy = (
            "<b>1 пара (08:30 – 10:05)</b>\n📐 Матан\n📍 Каб. 8\n\n"
            "<b>2 пара (10:35 – 12:10)</b>\n🇩🇪 Немецкий язык\n📍 Каб. 13"
        )
        storage.save_schedule("2026-11-16", "published", legacy, learned_rooms=(("Матан", "Каб. 8"),))
        for name in ("Иностранный язык", "Английский 17", "#немецкий_язык 13"):
            text, rooms = schedule.format_schedule_blocks(f"матан\nфизра\n{name}", "2026-11-16", False)
            self.assertIn("📐 Математика\n📍 Каб. 8", text)
            self.assertIn("⚽ Физическая культура", text)
            self.assertIn("🌐 Иностранный язык\n📍 Немецкий — каб. 13 · Английский — каб. 17", text)
            self.assertEqual(rooms, [])
            self.assertEqual(schedule.refresh_subjects(text), text)
        view = main.group_interface.render("today")[0]
        self.assertEqual(view, main.build_schedule_view("today")[0])
        self.assertIn("<b>1 пара (08:30 – 10:05)</b>\n📐 Математика", view)
        self.assertIn("🌐 Иностранный язык\n📍 Немецкий — каб. 13 · Английский — каб. 17", view)
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
            storage.save_schedule("2026-11-16", "published", "История", topic_msg_id=5)
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
        if name in {"SendMessage", "SendPhoto", "SendRichMessage"}:
            params = method.ephemeral_message_parameters
            chat_id = method.chat_id
            user_id = params.receiver_user_id if params else None
            self.message_id += 1
            payload = {
                "message_id": self.message_id,
                "chat": {"id": chat_id, "type": "supergroup" if chat_id < 0 else "private"},
                "date": 1,
            }
            if params:
                payload.update(
                    message_id=0,
                    ephemeral_message_id=self.message_id,
                    receiver_user={"id": user_id, "is_bot": False, "first_name": "Тест"},
                )
            return Message.model_validate(payload, context={"bot": bot})
        return True


class RouterCase(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(main, "ADMIN_IDS", frozenset({11, 22})))
        self.session = FakeSession()
        self.bot = Bot(token="1:offline-test-value", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(main.router)
        self.addAsyncCleanup(self.bot.session.close)
        self.addCleanup(setattr, main.router, "_parent_router", None)

    async def feed(
        self, *, action=None, text=None, user_id=11, message_id=10, group=False,
        photo=False, thread_id=None, ephemeral_id=None, receiver_id=None,
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
            callback_id = callback_id or str(user_id)
            update["callback_query"] = {
                "id": callback_id, "from": user, "chat_instance": "test",
                "data": action, "message": message,
            }
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
        for action in ("admin:menu", "admin:only_photo", "admin:rooms_clear", "admin:confirm:publish",
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

    async def test_publish_shows_one_button_and_updates_only_the_personal_schedule(self):
        storage.bind_topic(-1007, 2, 100)
        await self.feed(action="admin:edit:today:2026-11-16")
        await self.feed(action="admin:ch:yes")
        await self.feed(text="1. Биология 12", message_id=11)
        preview = next(call for call in self.session.calls if "Предпросмотр" in (getattr(call, "text", "") or ""))
        self.assertEqual([row[0].text for row in preview.reply_markup.inline_keyboard], [
            "✅ Опубликовать", "✏️ Исправить", "❌ Отмена",
        ])
        await self.feed(action="admin:confirm:publish", message_id=self.session.message_id)
        row = storage.get_schedule("2026-11-16")
        self.assertEqual(row[0], "published")
        self.assertEqual(row[6], 1)
        self.assertIsNone(row[4])
        self.assertEqual(storage.get_saved_room("Биология"), "Каб. 12")
        self.assertFalse(any(getattr(call, "chat_id", None) == -1007 for call in self.session.calls))
        self.session.calls.clear()
        await self.feed(action="topic:today", user_id=99, group=True, thread_id=2, message_id=100)
        sent = self.session.calls[0]
        self.assertIn("Биология", sent.text)
        self.assertEqual(sent.ephemeral_message_parameters.callback_query_id, "99")

    async def test_holiday_is_private_and_preserves_legacy_post_reference(self):
        storage.bind_topic(-1007, 2, 100)
        storage.save_schedule("2026-11-16", "published", "Расписание", "old-photo", 42)
        await self.feed(action="admin:holiday:today:2026-11-16")
        row = storage.get_schedule("2026-11-16")
        self.assertEqual((row[0], row[3], row[4]), ("holiday", None, 42))
        self.assertFalse(any(getattr(call, "chat_id", None) == -1007 for call in self.session.calls))

    async def test_photo_only_preview_and_publication(self):
        await self.feed(action="admin:edit:tomorrow:2026-11-17", user_id=22)
        await self.feed(photo=True, user_id=22, message_id=11)
        await self.feed(action="admin:only_photo", user_id=22, message_id=self.session.message_id)
        await self.feed(action="admin:confirm:publish", user_id=22, message_id=self.session.message_id)
        row = storage.get_schedule("2026-11-17")
        self.assertEqual((row[0], row[1], row[3]), ("published", None, "photo"))

    async def test_admin_day_menu_omits_removed_actions(self):
        storage.save_schedule("2026-11-16", "published", "<b>Биология</b>")
        await self.feed(action="admin:day:today:2026-11-16")
        buttons = self.session.calls[0].reply_markup.inline_keyboard
        self.assertEqual(len(buttons), 4)
        self.assertTrue(all(button[0].callback_data for button in buttons))
        self.session.calls.clear()
        await self.feed(action="admin:push:today:2026-11-16")
        self.assertEqual([type(call).__name__ for call in self.session.calls], ["AnswerCallbackQuery"])


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
        self.assertIn("<i>Учебный штаб группы ПКД-1</i>", menu.text)
        self.assertIn("<b>Наша задача: дожить до диплома и ничего не забыть.</b>", menu.text)
        self.assertNotIn("ЛЧПП", menu.text)
        self.assertEqual(menu.reply_markup.inline_keyboard[0][0].callback_data, "topic:schedule")
        self.assertTrue(all(
            button.callback_data and button.callback_data.startswith("topic:") and not button.url
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
        self.assertTrue(all(call.chat_id == -1007 and call.message_thread_id == 2 for call in sends))
        self.assertTrue(all(call.ephemeral_message_parameters.replace_callback_query_message is False for call in sends))
        self.session.calls.clear()
        await asyncio.gather(
            self.open("today", user_id=98, ephemeral_id=101),
            self.open("tomorrow", user_id=99, ephemeral_id=102),
        )
        edits = [call for call in self.session.calls if type(call).__name__ == "EditEphemeralMessageText"]
        self.assertEqual({(call.receiver_user_id, call.ephemeral_message_id) for call in edits}, {(98, 101), (99, 102)})
        today = next(call for call in edits if call.receiver_user_id == 98)
        self.assertEqual(today.parse_mode, "HTML")
        self.assertIn("<b>1 пара (08:30 – 10:05)</b>", today.text)
        self.assertIn("Каб. 6\n\n<b>2 пара", today.text)
        self.assertIn("пока не опубликовано", next(call for call in edits if call.receiver_user_id == 99).text)
        await self.open("main", user_id=98, ephemeral_id=101)
        self.assertEqual(self.methods()[-2:], ["DeleteEphemeralMessage", "AnswerCallbackQuery"])
        self.assertIsNone(storage.get_topic_screen(-1007, 2, 98))
        self.assertEqual(storage.get_topic_screen(-1007, 2, 99), (102, False))
        self.assertNotIn("EditMessageText", self.methods())
        self.assertEqual(storage.get_setting("topic_menu_msg_id"), "42")

    async def test_expired_screen_asks_to_reopen_from_pin_without_sending_duplicate(self):
        storage.bind_topic(-1007, 2, 42)
        self.session.failed_methods["EditEphemeralMessageText"] = "message to edit not found"
        await self.open("hw", user_id=99, ephemeral_id=87)
        self.assertEqual(self.methods(), ["EditEphemeralMessageText", "AnswerCallbackQuery"])
        self.assertIn("закреплённого меню", self.session.calls[-1].text)

    async def test_stale_or_foreign_buttons_only_get_private_alerts(self):
        storage.bind_topic(-1007, 2, 42)
        await self.open("schedule", user_id=99, message_id=41)
        await self.open("main", user_id=99, ephemeral_id=101, receiver_id=98)
        await self.open("main", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["AnswerCallbackQuery"] * 3)
        self.assertTrue(all(call.show_alert for call in self.session.calls))

    async def test_temporary_send_failure_uses_private_alert(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "holiday")
        await self.open("today", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["SendMessage", "AnswerCallbackQuery"])
        self.assertIn("Выходной", self.session.calls[0].text)
        self.assertEqual(self.session.calls[0].ephemeral_message_parameters.callback_query_id, "99")
        self.session.calls.clear()
        self.session.failed_methods["SendMessage"] = "unsupported"
        await self.open("today", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["SendMessage", "AnswerCallbackQuery"])
        self.assertTrue(self.session.calls[-1].show_alert)
        self.assertIn("закреплённого меню темы", self.session.calls[-1].text)
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
        self.assertEqual(self.methods(), ["DeleteEphemeralMessage", "DeleteEphemeralMessage", "AnswerCallbackQuery"])
        self.assertEqual(self.session.calls[0].ephemeral_message_id, 88)
        self.assertEqual(self.session.calls[1].ephemeral_message_id, 102)
        self.assertIsNone(storage.get_topic_screen(-1007, 2, 99))

    async def test_photo_and_back_to_main_after_reopening_db(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "published", "🧪" * 700, "photo")
        storage.init_db()
        await self.open("schedule", user_id=99, message_id=42)
        self.session.calls.clear()
        await self.open("today", user_id=99, ephemeral_id=101)
        edit = self.session.calls[0]
        self.assertEqual(type(edit).__name__, "EditEphemeralMessageText")
        self.assertEqual(edit.rich_message.media[0].media.media, "photo")
        self.assertTrue(edit.rich_message.html.startswith('<img src="tg://photo?id=schedule-photo"/>'))
        self.assertIn("</b><br>&#160;<br>", edit.rich_message.html)
        self.assertEqual(edit.ephemeral_message_id, 101)
        self.assertEqual(edit.reply_markup.inline_keyboard[-2][0].callback_data, "topic:text:today")
        await self.open("text:today", user_id=99, ephemeral_id=101)
        self.assertEqual(self.methods()[-2:], ["EditEphemeralMessageText", "AnswerCallbackQuery"])
        self.assertIn("🧪" * 700, self.session.calls[-2].text)
        self.assertEqual(self.session.calls[-2].reply_markup.inline_keyboard[-2][0].callback_data, "topic:today")
        self.assertNotIn("DeleteMessage", self.methods())

    async def test_photo_failure_preserves_text_in_topic(self):
        storage.bind_topic(-1007, 2, 42)
        storage.save_schedule("2026-11-16", "published", "Химия", "photo")
        self.session.failed_methods["SendRichMessage"] = "unsupported photo"
        await self.open("today", user_id=99, message_id=42)
        self.assertEqual(self.methods(), ["SendRichMessage", "SendMessage", "AnswerCallbackQuery"])
        fallback = self.session.calls[1]
        self.assertIn("Химия", fallback.text)
        self.assertFalse(fallback.ephemeral_message_parameters.replace_callback_query_message)
        self.assertEqual(fallback.ephemeral_message_parameters.callback_query_id, "99")

    async def test_brand_and_callback_sizes(self):
        await self.feed(text="/start", user_id=11)
        self.assertIn("ПИЩЕБЛОК-200", self.session.calls[0].text)
        self.assertNotIn("ЛЧПП", self.session.calls[0].text)
        self.assertIn("Панель старосты", self.session.calls[0].text)
        for section in main.group_interface.SCREENS:
            _, _, markup = main.group_interface.render(section)
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_student_private_start_and_old_buttons_do_not_open_menu(self):
        for command in ("/start", "/start schedule", "/start today"):
            await self.feed(text=command, user_id=99)
            self.assertIn("доступен старостам", self.session.calls[-1].text)
            self.assertIsNone(self.session.calls[-1].reply_markup)
        self.session.calls.clear()
        for action in ("menu:main", "menu:schedule", "sched:today", "stub:hw"):
            await self.feed(action=action, user_id=99)
            self.assertTrue(self.session.calls[-1].show_alert)
        self.assertEqual(self.methods(), ["AnswerCallbackQuery"] * 4)


class StartupTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def test_entrypoint_keeps_pending_updates_and_closes_session(self):
        session = FakeSession()
        with (
            patch.object(session, "close", new_callable=AsyncMock) as close,
            patch.object(main, "Bot", return_value=Bot(token="1:offline-test-value", session=session)),
            patch.object(main, "load_config", return_value=("unused", frozenset({11, 22}), storage.DB_PATH)),
            patch.object(main, "ADMIN_IDS", frozenset()),
            patch.object(Dispatcher, "start_polling", new_callable=AsyncMock) as run,
        ):
            self.addCleanup(setattr, main.router, "_parent_router", None)
            await main.main()
        self.assertFalse(session.calls[-1].drop_pending_updates)
        self.assertTrue(run.await_args.kwargs["handle_signals"])
        self.assertFalse(run.await_args.kwargs["handle_as_tasks"])
        close.assert_awaited_once()
