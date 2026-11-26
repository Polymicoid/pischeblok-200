import asyncio
import html
import logging
import re
import sqlite3
from dataclasses import asdict
from datetime import date, timedelta
from time import perf_counter

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramConflictError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods import GetUpdates
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message,
)

import analytics
import group_interface
import homework
import storage
from config import ConfigError, load_config
from schedule import (
    ScheduleHTML, build_schedule_view, format_post_body, format_date_human, format_day_btn, format_schedule_blocks,
    get_now, get_target_date, telegram_length,
)
from storage import (
    bind_topic, clear_subject_rooms, delete_schedule, get_all_subject_rooms,
    get_schedule, get_setting, get_timing_int, get_topic_config, save_schedule,
    set_setting, valid_timing,
)

LOGGER = logging.getLogger("pischeblok")
ADMIN_IDS: frozenset[int] = frozenset()
SPECIAL_ADMIN_ID = 5270827158
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024


def log(source: str, message: str):
    LOGGER.info("%s: %s", source, message)


class AdminCallbacks(BaseMiddleware):
    async def __call__(self, handler, event: CallbackQuery, data):
        key = event.data or ""
        if key.startswith("topic:"):
            return await handler(event, data)
        if not is_admin(event.from_user.id):
            await event.answer("⛔ Нет доступа.", show_alert=True)
            return
        if not isinstance(event.message, Message) or event.message.chat.type != "private":
            await event.answer("Открой меню в личном чате через /start.")
            return
        if key.startswith("admin:"):
            draft = await data["state"].get_data()
            if key.startswith("admin:hw:step:") and event.message.message_id != draft.get("hw_prompt_id"):
                await event.answer("Этот черновик уже закрыт. Открой «Домашние задания».", show_alert=True)
                return
            if key.startswith(("admin:ch:", "admin:confirm:", "admin:setbrk:")) or key == "admin:only_photo":
                if event.message.message_id != draft.get("menu_msg_id"):
                    await event.answer("Кнопка устарела. Открой панель заново через /start.", show_alert=True)
                    return
            action = re.fullmatch(r"admin:(day|edit|holiday|clear):(today|tomorrow)(?::([0-9-]+))?", key)
            if action:
                _, day, date = action.groups()
                if date != get_target_date(day).date().isoformat():
                    await event.answer("Дата на кнопке устарела. Открой панель заново через /start.", show_alert=True)
                    return
        return await handler(event, data)


def admin_day_action(action: str, day_key: str) -> str:
    return f"admin:{action}:{day_key}:{get_target_date(day_key).date().isoformat()}"


class AdminStates(StatesGroup):
    waiting_class_hour = State()
    waiting_schedule_input = State()
    confirm_schedule = State()
    waiting_break_value = State()
    homework_subject = State()
    homework_content = State()
    homework_materials = State()
    homework_deadline = State()
    homework_preview = State()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def safe_delete(message: Message):
    try:
        await message.delete()
    except TelegramAPIError as error:
        LOGGER.warning("Не удалось удалить сообщение (%s)", type(error).__name__)


async def render_screen(message: Message, text: str, reply_markup: InlineKeyboardMarkup, photo_id: str = None):
    text = ScheduleHTML(text).limited(CAPTION_LIMIT if photo_id else MESSAGE_LIMIT)
    try:
        if photo_id:
            result = await message.edit_media(
                media=InputMediaPhoto(media=photo_id, caption=text, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
        else:
            result = await message.edit_text(text=text, reply_markup=reply_markup, parse_mode="HTML")
        return result if isinstance(result, Message) else message
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return message
    if photo_id:
        sent = await message.answer_photo(photo_id, caption=text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    await safe_delete(message)
    return sent


# --- КЛАВИАТУРЫ ---
def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 Управление: {format_day_btn('today')}", callback_data=admin_day_action("day", "today"))],
        [InlineKeyboardButton(text=f"📅 Управление: {format_day_btn('tomorrow')}", callback_data=admin_day_action("day", "tomorrow"))],
        [
            InlineKeyboardButton(text="⏱ Перерывы и пары", callback_data="admin:breaks"),
            InlineKeyboardButton(text="🏫 Кабинеты", callback_data="admin:rooms"),
        ],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📝 Домашние задания", callback_data="admin:hw")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")],
    ])


def kb_admin_breaks() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏳ Длительность пары: {get_timing_int('pair_duration')} мин", callback_data="admin:editbrk:pair_duration")],
        [
            InlineKeyboardButton(text=f"Будни 1-й: {get_timing_int('wd_break_1')}м", callback_data="admin:editbrk:wd_break_1"),
            InlineKeyboardButton(text=f"2-й: {get_timing_int('wd_break_2')}м", callback_data="admin:editbrk:wd_break_2"),
            InlineKeyboardButton(text=f"ост: {get_timing_int('wd_break_other')}м", callback_data="admin:editbrk:wd_break_other"),
        ],
        [
            InlineKeyboardButton(text=f"Суббота 1-й: {get_timing_int('sat_break_1')}м", callback_data="admin:editbrk:sat_break_1"),
            InlineKeyboardButton(text=f"2-й: {get_timing_int('sat_break_2')}м", callback_data="admin:editbrk:sat_break_2"),
            InlineKeyboardButton(text=f"ост: {get_timing_int('sat_break_other')}м", callback_data="admin:editbrk:sat_break_other"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:menu")],
    ])


def kb_admin_break_presets(key: str) -> InlineKeyboardMarkup:
    presets = [45, 60, 80, 90, 95] if key == "pair_duration" else [5, 10, 15, 20, 30]
    btns = [InlineKeyboardButton(text=f"{m} мин", callback_data=f"admin:setbrk:{key}:{m}") for m in presets]
    return InlineKeyboardMarkup(inline_keyboard=[
        btns,
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:breaks")],
    ])


def kb_admin_rooms(has_rooms: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_rooms:
        rows.append([InlineKeyboardButton(text="🗑 Очистить все запомненные кабинеты", callback_data="admin:rooms_clear")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_day(day_key: str, has_schedule: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Ввести / Изменить расписание", callback_data=admin_day_action("edit", day_key))],
        [InlineKeyboardButton(text="🏖 Поставить «Выходной / занятий нет»", callback_data=admin_day_action("holiday", day_key))],
    ]
    if has_schedule:
        rows.append([InlineKeyboardButton(text="🗑 Сбросить («Не опубликовано»)", callback_data=admin_day_action("clear", day_key))])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_class_hour(day_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, будет классный час (08:30 – 09:00)", callback_data="admin:ch:yes")],
        [InlineKeyboardButton(text="❌ Нет, пары с 09:05", callback_data="admin:ch:no")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=admin_day_action("day", day_key))],
    ])


def kb_admin_cancel_input(day_key: str, allow_only_photo: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if allow_only_photo:
        rows.append([InlineKeyboardButton(text="⏭ Опубликовать только фото (без текста)", callback_data="admin:only_photo")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=admin_day_action("day", day_key))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_preview(day_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="admin:confirm:publish")],
        [InlineKeyboardButton(text="✏️ Исправить", callback_data=admin_day_action("edit", day_key))],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=admin_day_action("day", day_key))],
    ])


# --- РОУТЕР И ХЭНДЛЕРЫ ---
router = Router()
router.callback_query.outer_middleware(AdminCallbacks())


@router.error()
async def on_error(event: ErrorEvent):
    LOGGER.error("Ошибка обработки обновления (%s)", type(event.exception).__name__)
    callback = event.update.callback_query
    if callback:
        try:
            await callback.answer("Не удалось выполнить действие. Проверь состояние через /start.", show_alert=True)
        except TelegramAPIError:
            pass


@router.message(Command("set_topic"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_set_topic(message: Message, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await safe_delete(message)
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    old_chat_id, old_thread_id = get_topic_config()
    old_menu_id = get_setting("topic_menu_msg_id", "") or ""
    old_menu_id = int(old_menu_id) if re.fullmatch(r"[0-9]{1,16}", old_menu_id) else None
    menu_id = None
    new_menu_id = None
    menu = {
        "text": group_interface.PINNED_MENU_TEXT, "parse_mode": "HTML",
        "reply_markup": group_interface.menu_keyboard(),
    }
    notice = "✅ Тема привязана. Меню закреплено."
    try:
        if old_menu_id and (chat_id, thread_id) == (old_chat_id, old_thread_id):
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=old_menu_id, **menu)
                menu_id = old_menu_id
            except TelegramBadRequest as error:
                detail = str(error).lower()
                if "message is not modified" in detail:
                    menu_id = old_menu_id
                elif not any(reason in detail for reason in (
                    "message to edit not found", "message can't be edited", "there is no text in the message to edit",
                )):
                    raise
        if menu_id is None:
            sent = await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, **menu)
            menu_id = new_menu_id = sent.message_id
        await bot.pin_chat_message(chat_id=chat_id, message_id=menu_id, disable_notification=True)
        bind_topic(chat_id, thread_id, menu_id)
    except TelegramAPIError as error:
        LOGGER.warning("Обновление меню темы не завершено (%s)", type(error).__name__)
        notice = "⚠️ Не удалось закрепить меню. Проверь права бота. Прежняя привязка сохранена."
        if new_menu_id:
            await delete_topic_menu(bot, chat_id, new_menu_id)
    else:
        if old_chat_id and old_menu_id and (old_chat_id, old_menu_id) != (chat_id, menu_id):
            await delete_topic_menu(bot, old_chat_id, old_menu_id)
        log("📌 Тема группы", f"привязана (chat={chat_id}, thread={thread_id}, msg={menu_id})")

    try:
        await bot.send_message(
            chat_id=message.from_user.id,
            text=notice,
            parse_mode="HTML",
        )
    except TelegramAPIError as error:
        LOGGER.warning("Подтверждение привязки не доставлено (%s)", type(error).__name__)


async def delete_topic_menu(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as error:
        LOGGER.warning("Меню темы не удалено (%s)", type(error).__name__)


@router.callback_query(F.data.startswith("topic:"))
async def cb_topic_screen(callback: CallbackQuery, bot: Bot):
    message = callback.message
    screen = callback.data.removeprefix("topic:")
    chat_id, thread_id = get_topic_config()
    valid_screen = (
        screen in group_interface.SCREENS or screen == group_interface.CLOSE_SCREEN
        or homework.valid_student_screen(screen)
    )
    if not isinstance(message, Message) or message.chat.id != chat_id or not valid_screen:
        await callback.answer("Кнопка устарела. Выбери раздел в закреплённом меню.", show_alert=True)
        return
    if message.ephemeral_message_id:
        if (not message.receiver_user or message.receiver_user.id != callback.from_user.id
                or message.message_thread_id not in (None, thread_id)):
            await callback.answer("Открой свой экран из закреплённого меню.", show_alert=True)
            return
    elif screen == group_interface.CLOSE_SCREEN or str(message.message_id) != get_setting("topic_menu_msg_id"):
        await callback.answer("Кнопка устарела. Выбери раздел в закреплённом меню.", show_alert=True)
        return

    action = "text" if screen in group_interface.TEXT_SCREENS else screen
    student = not is_admin(callback.from_user.id)
    track_schedule = student and action in analytics.ACTION_LABELS
    homework_item = homework.ITEM_ROUTE.fullmatch(screen) if student else None
    started_at = get_now() if track_schedule or homework_item else None
    started = perf_counter()
    status = None
    result = "ok"
    if track_schedule and action in analytics.DAYS:
        row = get_schedule(get_target_date(action).date().isoformat())
        status = row[0] if row else "missing"
    try:
        if screen == group_interface.CLOSE_SCREEN:
            await group_interface.close_screen(bot, message, callback.from_user.id, thread_id)
        else:
            if await group_interface.show_screen(callback, bot, message, screen, thread_id):
                result = "photo_fallback"
    except TelegramAPIError as error:
        result = "error"
        LOGGER.warning("Персональный экран недоступен (%s)", type(error).__name__)
    if track_schedule:
        try:
            analytics.record_request(
                callback.id, callback.from_user.id, started_at, action, status, result,
                round((perf_counter() - started) * 1000),
            )
        except sqlite3.Error:
            LOGGER.exception("Не удалось записать статистику расписания")
    if homework_item and result != "error":
        try:
            analytics.record_homework_view(callback.id, callback.from_user.id, started_at, int(homework_item[1]))
        except sqlite3.Error:
            LOGGER.exception("Не удалось записать просмотр домашнего задания")
    if result == "error":
        await callback.answer(
            "Не удалось открыть экран. Попробуй ещё раз из закреплённого меню темы.",
            show_alert=True,
        )
    else:
        await callback.answer()


# --- АДМИНКА ---
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Личный чат доступен старостам. Расписание открывается из закрепа в теме группы.")
        return
    await state.clear()
    await message.answer(admin_menu_text(message.from_user.id), reply_markup=kb_admin_menu(), parse_mode="HTML")


def admin_menu_text(user_id: int) -> str:
    chat_id, _ = get_topic_config()
    topic_status = "🟢 Тема группы привязана" if chat_id else "⚪️ Тема не привязана (напиши /set_topic в нужной теме)"

    if user_id == SPECIAL_ADMIN_ID:
        header = "🔥 <b>Привет, ты сегодня на все 100 (градусов)!</b>"
    else:
        header = "🔐 <b>Панель старосты</b>"

    return f"{header}\n{group_interface.BRAND}\n\n{topic_status}\n\nВыбери действие:"


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_screen(callback.message, admin_menu_text(callback.from_user.id), kb_admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📊 Домашние задания", callback_data="admin:hw:stats")],
        [InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:menu")],
    ])
    await render_screen(callback.message, analytics.weekly_report(), keyboard)
    await callback.answer()


# --- ДОМАШНИЕ ЗАДАНИЯ В АДМИНКЕ ---
@router.callback_query(F.data == "admin:hw:stats")
async def cb_hw_stats(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [homework.key("🔄 Обновить", "hw:stats", True)],
        [homework.key("⬅️ К заданиям", "hw", True)],
    ])
    await render_screen(callback.message, analytics.homework_report(), keyboard)
    await callback.answer()


async def hw_prompt(
    message: Message, state: FSMContext, text: str, rows: list[list[InlineKeyboardButton]], *, edit: bool,
):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[*rows, [homework.key("❌ Отмена", "hw", True)]])
    if edit:
        screen = await render_screen(message, text, keyboard)
    else:
        screen = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.update_data(hw_prompt_id=screen.message_id)


@router.callback_query(F.data == "admin:hw")
@router.callback_query(F.data.startswith(("admin:hw:active:", "admin:hw:archive:", "admin:hw:subjects:")))
async def cb_hw_list(callback: CallbackQuery, state: FSMContext):
    screen = callback.data.removeprefix("admin:")
    if not homework.valid_student_screen(screen):
        await callback.answer("Открой домашние задания заново.", show_alert=True)
        return
    await state.clear()
    text, keyboard = homework.list_view(screen, admin=True)
    await render_screen(callback.message, text, keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:hw:item:"))
async def cb_hw_item(callback: CallbackQuery, state: FSMContext):
    match = homework.ITEM_ROUTE.fullmatch(callback.data.removeprefix("admin:"))
    if not match or not homework.valid_student_screen(match[0]):
        await callback.answer("Открой домашние задания заново.", show_alert=True)
        return
    assignment = homework.get_assignment(int(match[1]))
    if not assignment:
        await callback.answer("Задание больше недоступно.", show_alert=True)
        return
    await state.clear()
    rows = [[homework.key("✏️ Исправить", f"hw:edit:{assignment.id}", True)]]
    if not assignment.due_on or assignment.due_on >= get_now().date().isoformat():
        if assignment.archived:
            rows.append([homework.key("📌 Вернуть из архива", f"hw:restore:{assignment.id}", True)])
        else:
            rows.append([homework.key("🗂 В архив", f"hw:archive-item:{assignment.id}", True)])
    material = homework.material_button(assignment)
    if material:
        rows.append(material)
    rows.append([homework.key("⬅️ К заданиям", f"hw:{match[2]}:{match[3]}:{match[4]}", True)])
    students, views = analytics.homework_view_counts(assignment.id)
    text = f"{homework.assignment_text(assignment)}\n\n📊 За 7 дней · студентов {students}, открытий {views}"
    await render_screen(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:hw:(archive-item|restore):[0-9]{1,18}$"))
async def cb_hw_archive(callback: CallbackQuery, state: FSMContext):
    _, _, action, item_id = callback.data.split(":")
    archived = action == "archive-item"
    found = homework.set_archived(int(item_id), archived)
    await state.clear()
    text, keyboard = homework.list_view("hw:archive:0:0" if archived else "hw", admin=True)
    await render_screen(callback.message, text, keyboard)
    if found:
        notice = "Задание в архиве." if archived else "Задание возвращено из архива."
    else:
        notice = "Задание больше недоступно."
    await callback.answer(notice, show_alert=True)


@router.callback_query(F.data == "admin:hw:new")
@router.callback_query(F.data.regexp(r"^admin:hw:edit:[0-9]{1,18}$"))
async def cb_hw_begin(callback: CallbackQuery, state: FSMContext):
    edit = callback.data.startswith("admin:hw:edit:")
    assignment = homework.get_assignment(int(callback.data.rsplit(":", 1)[1])) if edit else homework.Assignment()
    if not assignment:
        await callback.answer("Задание больше недоступно.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.homework_subject)
    await state.update_data(hw_assignment=asdict(assignment))
    buttons = [
        homework.key(homework.subject_label(index, short=True), f"hw:step:subject:{index}", True)
        for index in range(len(homework.SUBJECTS))
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    await hw_prompt(callback.message, state, "📝 <b>Домашние задания</b>\n\nВыбери или напиши предмет.", rows, edit=True)
    await callback.answer()


async def hw_choose_subject(message: Message, state: FSMContext, subject: int, *, edit: bool):
    assignment = (await state.get_data())["hw_assignment"]
    assignment["subject"] = subject
    await state.update_data(hw_assignment=assignment)
    await state.set_state(AdminStates.homework_content)
    has_content = any(assignment[field] for field in ("body", "photo_id", "document_id", "material_url"))
    rows = [[homework.key("Оставить описание", "hw:step:keep", True)]] if has_content else []
    text = (
        f"<b>{homework.subject_label(subject)}</b>\n\n"
        "Напиши, что нужно сделать. Можно переслать задание или отправить файл с описанием в подписи."
    )
    await hw_prompt(message, state, text, rows, edit=edit)


@router.callback_query(F.data.regexp(r"^admin:hw:step:subject:[0-9]{1,2}$"), AdminStates.homework_subject)
async def cb_hw_subject(callback: CallbackQuery, state: FSMContext):
    subject = int(callback.data.rsplit(":", 1)[1])
    if subject >= len(homework.SUBJECTS):
        await callback.answer("Выбери предмет из списка.", show_alert=True)
        return
    await hw_choose_subject(callback.message, state, subject, edit=True)
    await callback.answer()


@router.message(AdminStates.homework_subject, F.chat.type == "private", F.text)
async def msg_hw_subject(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    subject = homework.assignment_subject(message.text)
    if subject is None:
        await message.answer("Выбери предмет кнопкой или укажи «Другое».")
        return
    await hw_choose_subject(message, state, subject, edit=False)


async def hw_choose_deadline(message: Message, state: FSMContext, *, edit: bool):
    await state.set_state(AdminStates.homework_deadline)
    data = await state.get_data()
    assignment = data["hw_assignment"]
    suggested = data.get("hw_suggested_due")
    today = get_now().date()
    choices = {today.isoformat(): "Сегодня", (today + timedelta(days=1)).isoformat(): "Завтра"}
    if suggested:
        choices[suggested] = f"Из сообщения · {date.fromisoformat(suggested):%d.%m.%Y}"
    existing_due = assignment["due_on"]
    if existing_due and existing_due not in choices:
        choices[existing_due] = f"Оставить срок · {date.fromisoformat(existing_due):%d.%m.%Y}"
    rows = [[homework.key(label, f"hw:step:due:{value}", True)] for value, label in choices.items()]
    rows.append([homework.key(homework.NEXT_LESSON, "hw:step:due:next", True)])
    rows.append([homework.key("Без срока", "hw:step:due:none", True)])
    await hw_prompt(message, state, "📅 Выбери срок или напиши дату в формате <b>ДД.ММ.ГГГГ</b>.", rows, edit=edit)


async def hw_materials_prompt(message: Message, state: FSMContext, *, edit: bool):
    await state.set_state(AdminStates.homework_materials)
    draft = homework.Assignment(**(await state.get_data())["hw_assignment"])
    text = (
        "📎 <b>Материалы к заданию</b>\n\n"
        "Пришли ссылку на учебник или методичку, файл или фото. Можно добавить одну ссылку и одно вложение."
    )
    has_attachment = draft.document_id or draft.photo_id
    has_materials = has_attachment or draft.material_url
    rows = []
    if draft.body or has_materials:
        rows.append([homework.key("Готово" if has_materials else "Пропустить", "hw:step:materials:done", True)])
    if has_attachment:
        text += "\n\n📎 Вложение добавлено."
        rows.append([homework.key("Убрать вложение", "hw:step:materials:remove-file", True)])
    if draft.material_url:
        rows.append(homework.material_button(draft))
        rows.append([homework.key("Убрать ссылку", "hw:step:materials:remove-link", True)])
    rows.append([homework.key("⬅️ К описанию", "hw:step:materials:back", True)])
    await hw_prompt(message, state, text, rows, edit=edit)


@router.message(AdminStates.homework_content, F.chat.type == "private")
async def msg_hw_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        values = homework.parse_content(message)
    except ValueError as error:
        await message.answer(str(error))
        return
    suggested = values.pop("suggested_due")
    assignment = (await state.get_data())["hw_assignment"]
    assignment["body"] = values["body"]
    if values["photo_id"] or values["document_id"]:
        assignment.update(values)
    await state.update_data(hw_assignment=assignment, hw_suggested_due=suggested)
    await hw_materials_prompt(message, state, edit=False)


@router.callback_query(F.data == "admin:hw:step:keep", AdminStates.homework_content)
async def cb_hw_keep(callback: CallbackQuery, state: FSMContext):
    await hw_materials_prompt(callback.message, state, edit=True)
    await callback.answer()


@router.message(AdminStates.homework_materials, F.chat.type == "private")
async def msg_hw_material(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    assignment = (await state.get_data())["hw_assignment"]
    attachment = homework.attachment_ids(message)
    if any(attachment.values()):
        assignment.update(attachment)
    else:
        try:
            assignment["material_url"] = homework.parse_material_url(message.text or "")
        except ValueError as error:
            await message.answer(f"{error} Также можно прикрепить файл или фото.")
            return
    await state.update_data(hw_assignment=assignment)
    await hw_materials_prompt(message, state, edit=False)


@router.callback_query(
    F.data.in_({"admin:hw:step:materials:done", "admin:hw:step:materials:remove-file", "admin:hw:step:materials:remove-link"}),
    AdminStates.homework_materials,
)
async def cb_hw_materials(callback: CallbackQuery, state: FSMContext):
    action = callback.data.rsplit(":", 1)[1]
    assignment = (await state.get_data())["hw_assignment"]
    if action == "done":
        if not any(assignment[field] for field in ("body", "photo_id", "document_id", "material_url")):
            await callback.answer("Добавь материал или вернись к описанию.", show_alert=True)
            return
        await hw_choose_deadline(callback.message, state, edit=True)
    else:
        if action == "remove-file":
            assignment.update(photo_id=None, document_id=None)
        else:
            assignment["material_url"] = None
        await state.update_data(hw_assignment=assignment)
        await hw_materials_prompt(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(F.data == "admin:hw:step:materials:back", AdminStates.homework_materials)
async def cb_hw_materials_back(callback: CallbackQuery, state: FSMContext):
    assignment = (await state.get_data())["hw_assignment"]
    await hw_choose_subject(callback.message, state, assignment["subject"], edit=True)
    await callback.answer()


async def hw_preview(message: Message, state: FSMContext, due: date | None, *, next_lesson: bool = False, edit: bool):
    assignment = (await state.get_data())["hw_assignment"]
    assignment["due_on"] = due.isoformat() if due else None
    assignment["due_next_lesson"] = next_lesson
    await state.update_data(hw_assignment=assignment)
    await state.set_state(AdminStates.homework_preview)
    draft = homework.Assignment(**assignment)
    text = f"👀 <b>Предпросмотр</b>\n\n{homework.assignment_text(draft)}"
    if due and due < get_now().date():
        text += "\n\nЭтот срок уже прошёл. Задание будет в архиве."
    rows = [[homework.key("✅ Опубликовать", "hw:step:save", True)]]
    rows.append([homework.key("✏️ Заменить текст или материалы", "hw:step:change-content", True)])
    rows.append([homework.key("📅 Изменить срок", "hw:step:change-due", True)])
    material = homework.material_button(draft)
    if material:
        rows.append(material)
    await hw_prompt(message, state, text, rows, edit=edit)


@router.callback_query(F.data.regexp(r"^admin:hw:step:due:(?:next|none|[0-9]{4}-[0-9]{2}-[0-9]{2})$"), AdminStates.homework_deadline)
async def cb_hw_deadline(callback: CallbackQuery, state: FSMContext):
    value = callback.data.rsplit(":", 1)[1]
    try:
        due = homework.parse_deadline(value) if value not in {"next", "none"} else None
    except ValueError:
        await callback.answer("Укажи корректную дату.", show_alert=True)
        return
    await hw_preview(callback.message, state, due, next_lesson=value == "next", edit=True)
    await callback.answer()


@router.message(AdminStates.homework_deadline, F.chat.type == "private", F.text)
async def msg_hw_deadline(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        due = homework.parse_deadline(message.text)
    except ValueError:
        await message.answer("Укажи корректную дату в формате ДД.ММ.ГГГГ или выбери срок кнопкой.")
        return
    await hw_preview(message, state, due, edit=False)


@router.callback_query(F.data == "admin:hw:step:change-due", AdminStates.homework_preview)
async def cb_hw_change_deadline(callback: CallbackQuery, state: FSMContext):
    await hw_choose_deadline(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(F.data == "admin:hw:step:change-content", AdminStates.homework_preview)
async def cb_hw_change_content(callback: CallbackQuery, state: FSMContext):
    assignment = (await state.get_data())["hw_assignment"]
    await hw_choose_subject(callback.message, state, assignment["subject"], edit=True)
    await callback.answer()


@router.callback_query(F.data == "admin:hw:step:save", AdminStates.homework_preview)
async def cb_hw_save(callback: CallbackQuery, state: FSMContext):
    draft = homework.Assignment(**(await state.get_data())["hw_assignment"])
    found = homework.save_assignment(draft)
    await state.clear()
    archived = draft.archived or (draft.due_on is not None and draft.due_on < get_now().date().isoformat())
    text, keyboard = homework.list_view("hw:archive:0:0" if archived else "hw", admin=True)
    await render_screen(callback.message, text, keyboard)
    notice = "✅ Домашка сохранена в архиве." if archived else "✅ Домашка доступна студентам из закрепа."
    await callback.answer(notice if found else "Задание больше недоступно.", show_alert=True)



@router.callback_query(F.data == "admin:breaks")
async def cb_admin_breaks(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await state.clear()
    text = (
        "⏱ <b>Настройка звонков и перерывов</b>\n\n"
        "• <b>Понедельник и четверг:</b> 1 пара с 09:05 независимо от классного часа (08:30–09:00).\n"
        "• <b>Остальные будни:</b> 1 пара с 08:30.\n"
        "• <b>Суббота:</b> начало в 08:30\n\n"
        "Нажми на нужный параметр ниже, чтобы изменить:"
    )
    await render_screen(callback.message, text, kb_admin_breaks())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:editbrk:"))
async def cb_admin_edit_break(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    key = callback.data.split(":")[2]
    if not valid_timing(key, 95):
        await callback.answer("Настройка устарела.", show_alert=True)
        return
    names = {
        "pair_duration": "Длительность одной пары (мин)",
        "wd_break_1": "Будни: перерыв после 1-й пары",
        "wd_break_2": "Будни: перерыв после 2-й пары",
        "wd_break_other": "Будни: перерыв после 3+ пар",
        "sat_break_1": "Суббота: перерыв после 1-й пары",
        "sat_break_2": "Суббота: перерыв после 2-й пары",
        "sat_break_other": "Суббота: перерыв после 3+ пар",
    }
    await state.set_state(AdminStates.waiting_break_value)
    await state.update_data(break_key=key, menu_msg_id=callback.message.message_id)
    text = (
        f"⏱ <b>{names.get(key, key)}</b>\n\n"
        f"Текущее значение: <b>{get_timing_int(key)} мин</b>\n"
        f"Выбери готовый вариант или отправь своё число минут в чат:"
    )
    screen = await render_screen(callback.message, text, kb_admin_break_presets(key))
    await state.update_data(menu_msg_id=screen.message_id)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:setbrk:[a-z_0-9]+:[0-9]{1,3}$"), AdminStates.waiting_break_value)
async def cb_admin_set_break(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    _, _, key, val = callback.data.split(":")
    if key != (await state.get_data()).get("break_key") or not valid_timing(key, int(val)):
        await callback.answer("Настройка устарела.", show_alert=True)
        return
    set_setting(key, val)
    await state.clear()
    log("👑 Староста", f"изменил тайминг {key} → {val} мин")
    await render_screen(callback.message, f"✅ Установлено: {val} мин", kb_admin_breaks())
    await callback.answer()


@router.message(AdminStates.waiting_break_value, F.chat.type == "private")
async def process_break_value(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    key = data["break_key"]
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not valid_timing(key, int(raw)):
        minimum = 1 if key == "pair_duration" else 0
        await message.answer(f"Укажи целое число от {minimum} до 180.")
        return
    set_setting(key, raw)
    await state.clear()
    await safe_delete(message)
    text = "⏱ <b>Настройка звонков и перерывов</b>\n\n✅ Значение обновлено!"
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id, message_id=data["menu_msg_id"], text=text,
            reply_markup=kb_admin_breaks(), parse_mode="HTML",
        )
    except TelegramBadRequest:
        await message.answer(text, reply_markup=kb_admin_breaks(), parse_mode="HTML")


@router.callback_query(F.data == "admin:rooms")
async def cb_admin_rooms(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    rooms = get_all_subject_rooms()
    if not rooms:
        text = "🏫 <b>Память кабинетов</b>\n\n<i>Пока пусто. Начни вводить пары с кабинетами (например: «биология 12»), и бот их запомнит!</i>"
    else:
        lines = "\n".join(f"• {html.escape(subj)} → <b>{html.escape(room)}</b>" for subj, room in rooms)
        text = f"🏫 <b>Запомненные кабинеты ({len(rooms)}):</b>\n\n{lines}\n\n<i>Теперь при вводе можно писать только название предмета.</i>"
    await render_screen(callback.message, text, kb_admin_rooms(bool(rooms)))
    await callback.answer()


@router.callback_query(F.data == "admin:rooms_clear")
async def cb_admin_rooms_clear(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    clear_subject_rooms()
    log("👑 Староста", "очистил память кабинетов")
    await render_screen(callback.message, "🗑 Память кабинетов очищена.", kb_admin_rooms(False))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:day:(today|tomorrow)(?::[0-9-]+)?$"))
async def cb_admin_day(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await state.clear()
    day_key = callback.data.split(":")[2]
    current_text, photo_id, has_sched = build_schedule_view(day_key)

    text = f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}"
    await render_screen(callback.message, text, kb_admin_day(day_key, has_sched), photo_id=photo_id)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:holiday:(today|tomorrow)(?::[0-9-]+)?$"))
async def cb_admin_holiday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    day_key = callback.data.split(":")[2]
    date_str = callback.data.rsplit(":", 1)[-1]

    save_schedule(date_str, "holiday")
    log("👑 Староста", f"поставил ВЫХОДНОЙ на {date_str}")
    await callback.answer("🏖 Установлен статус «Выходной / занятий нет»", show_alert=True)

    current_text, _, has_sched = build_schedule_view(day_key)
    await render_screen(
        callback.message,
        f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}",
        kb_admin_day(day_key, has_sched),
    )


@router.callback_query(F.data.regexp(r"^admin:clear:(today|tomorrow)(?::[0-9-]+)?$"))
async def cb_admin_clear(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
    day_key = callback.data.split(":")[2]
    date_str = callback.data.rsplit(":", 1)[-1]

    old_row = get_schedule(date_str)
    topic_warning = ""
    if old_row and old_row[4]:
        chat_id, _ = get_topic_config()
        if chat_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_row[4])
                log("📢 Тема группы", f"удалён пост за {date_str} (msg #{old_row[4]})")
            except TelegramAPIError as error:
                LOGGER.warning("Не удалось удалить пост из темы (%s)", type(error).__name__)
                topic_warning = "\nПроверь старый пост в теме."

    delete_schedule(date_str)
    log("👑 Староста", f"сбросил расписание на {date_str}")
    await callback.answer("🗑 Расписание сброшено" + topic_warning, show_alert=True)

    current_text, _, has_sched = build_schedule_view(day_key)
    await render_screen(
        callback.message,
        f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}",
        kb_admin_day(day_key, has_sched),
    )


@router.callback_query(F.data.regexp(r"^admin:edit:(today|tomorrow)(?::[0-9-]+)?$"))
async def cb_admin_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    day_key = callback.data.split(":")[2]
    date_str = callback.data.rsplit(":", 1)[-1]
    dt = date.fromisoformat(date_str)
    await state.clear()
    await state.update_data(
        day_key=day_key,
        date_str=date_str,
        photo_id=None,
        menu_msg_id=callback.message.message_id,
    )

    if dt.weekday() in (0, 3):
        await state.set_state(AdminStates.waiting_class_hour)
        day_name = "понедельник" if dt.weekday() == 0 else "четверг"
        text = (
            f"🔔 <b>Вопрос по расписанию на {format_date_human(date_str)}</b>\n\n"
            f"В {day_name} будет <b>классный час</b> (08:30 – 09:00)?\n\n"
            "Первая пара в любом случае начинается в <b>09:05</b>."
        )
        msg = await render_screen(callback.message, text, kb_admin_class_hour(day_key))
        await state.update_data(menu_msg_id=msg.message_id)
        await callback.answer()
        return

    await ask_schedule_input(callback.message, state, day_key, date_str, has_class_hour=False)
    await callback.answer()


@router.callback_query(F.data.in_({"admin:ch:yes", "admin:ch:no"}), AdminStates.waiting_class_hour)
async def cb_admin_class_hour_choice(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    has_ch = callback.data.split(":")[2] == "yes"
    data = await state.get_data()
    await ask_schedule_input(callback.message, state, data["day_key"], data["date_str"], has_class_hour=has_ch)
    await callback.answer()


async def ask_schedule_input(message: Message, state: FSMContext, day_key: str, date_str: str, has_class_hour: bool):
    await state.set_state(AdminStates.waiting_schedule_input)
    await state.update_data(has_class_hour=has_class_hour)

    row = get_schedule(date_str)
    existing_hint = ""
    if row and row[0] == "published" and row[5]:
        existing_hint = f"\n\n📋 <b>Текущий черновик (нажми, чтобы скопировать):</b>\n<code>{html.escape(row[5])}</code>"

    ch_notice = ""
    if has_class_hour:
        ch_notice = (
            "🔔 <b>Классный час (08:30 – 09:00) я подставлю сам — писать его НЕ нужно!</b> "
            "Начни сразу с 1-й пары (09:05).\n\n"
        )
    elif date.fromisoformat(date_str).weekday() in (0, 3):
        ch_notice = "🕘 Первая пара с <b>09:05</b>.\n\n"

    text = (
        f"✏️ <b>Ввод расписания на {format_date_human(date_str)}</b>\n\n"
        f"{ch_notice}"
        f"Напиши пары построчно (кабинеты, которые бот уже знает, подставятся сами!).\n"
        f"📸 Можно прикрепить фотку прямо к сообщению.\n"
        f"💬 <b>Как добавить комментарий:</b> начни строку с минуса <code>-</code> (при оформлении минус уберётся).\n\n"
        f"<i>Пример:</i>\n"
        f"биология 12\n"
        f"физра\n"
        f"-принести форму и сменку"
        f"{existing_hint}"
    )
    msg = await render_screen(message, text, kb_admin_cancel_input(day_key))
    await state.update_data(menu_msg_id=msg.message_id)


@router.message(AdminStates.waiting_schedule_input, F.chat.type == "private")
async def process_schedule_input(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    day_key = data["day_key"]
    date_str = data["date_str"]
    menu_msg_id = data["menu_msg_id"]
    has_class_hour = data.get("has_class_hour", False)
    saved_photo_id = data.get("photo_id")

    if message.photo:
        saved_photo_id = message.photo[-1].file_id
        await state.update_data(photo_id=saved_photo_id)
        raw_text = (message.caption or "").strip()

        if not raw_text:
            new_msg = await message.answer_photo(
                photo=saved_photo_id,
                caption="📸 <b>Фотку принял!</b>\nТеперь отправь список пар текстом или нажми кнопку ниже.",
                reply_markup=kb_admin_cancel_input(day_key, allow_only_photo=True),
                parse_mode="HTML",
            )
            await state.update_data(menu_msg_id=new_msg.message_id)
            await remove_private_menu(bot, message.chat.id, menu_msg_id)
            await safe_delete(message)
            return
    elif message.text and message.text.strip():
        raw_text = message.text.strip()
    else:
        return

    try:
        formatted_content, learned_rooms = format_schedule_blocks(raw_text, date_str, has_class_hour)
    except ValueError as error:
        await message.answer(str(error))
        return
    preview = "👀 Предпросмотр:\n\n" + ScheduleHTML(
        format_post_body(date_str, "published", formatted_content, get_now().strftime("%Y-%m-%d %H:%M"))
    ).plain_text()
    if telegram_length(preview) > (CAPTION_LIMIT if saved_photo_id else MESSAGE_LIMIT):
        await message.answer(
            "Расписание слишком длинное для подписи к фото. Сократи текст или начни ввод заново без фото."
            if saved_photo_id else "Расписание слишком длинное. Сократи текст и отправь его ещё раз."
        )
        return
    await state.update_data(
        draft_content=formatted_content,
        raw_content=raw_text,
        learned_rooms=learned_rooms,
    )
    await show_admin_preview(message.chat.id, menu_msg_id, day_key, date_str, formatted_content, saved_photo_id, state, bot)
    await state.set_state(AdminStates.confirm_schedule)
    await safe_delete(message)


@router.callback_query(F.data == "admin:only_photo", AdminStates.waiting_schedule_input)
async def cb_admin_only_photo(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("photo_id"):
        await callback.answer("Сначала отправь фото.", show_alert=True)
        return
    await state.update_data(draft_content=None, raw_content=None, learned_rooms=[])

    await show_admin_preview(
        callback.message.chat.id,
        callback.message.message_id,
        data["day_key"],
        data["date_str"],
        None,
        data.get("photo_id"),
        state,
        bot,
    )
    await state.set_state(AdminStates.confirm_schedule)
    await callback.answer()


async def remove_private_menu(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        LOGGER.info("Предыдущее меню уже недоступно для удаления.")


async def show_admin_preview(
    chat_id: int,
    menu_msg_id: int,
    day_key: str,
    date_str: str,
    content: str | None,
    photo_id: str | None,
    state: FSMContext,
    bot: Bot,
):
    now_str = get_now().strftime("%Y-%m-%d %H:%M")
    preview_body = format_post_body(date_str, "published", content, now_str)
    preview_text = f"👀 <b>Предпросмотр:</b>\n\n{preview_body}"

    if photo_id:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=preview_text,
            reply_markup=kb_admin_preview(day_key),
            parse_mode="HTML",
        )
    else:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=preview_text,
            reply_markup=kb_admin_preview(day_key),
            parse_mode="HTML",
        )
    await state.update_data(menu_msg_id=sent.message_id)
    await remove_private_menu(bot, chat_id, menu_msg_id)


@router.callback_query(F.data == "admin:confirm:publish", AdminStates.confirm_schedule)
async def cb_admin_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    data = await state.get_data()
    day_key = data["day_key"]
    date_str = data["date_str"]
    draft_content = data.get("draft_content")
    raw_content = data.get("raw_content")
    photo_id = data.get("photo_id")
    has_ch = 1 if data.get("has_class_hour") else 0
    learned_rooms = tuple(data.get("learned_rooms", ()))
    save_schedule(
        date_str, "published", draft_content, photo_id,
        raw_content=raw_content, has_class_hour=has_ch, learned_rooms=learned_rooms,
    )
    await state.clear()
    log("👑 Староста", f"опубликовал расписание на {date_str}")
    await callback.answer("✅ Опубликовано. Студенты увидят расписание в своём окне.", show_alert=True)

    current_text, current_photo, has_sched = build_schedule_view(day_key)
    await render_screen(
        callback.message,
        f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}",
        kb_admin_day(day_key, has_sched),
        photo_id=current_photo,
    )


@router.callback_query()
async def stale_callback(callback: CallbackQuery):
    await callback.answer("Кнопка устарела. Открой меню заново через /start.", show_alert=True)


async def main():
    global ADMIN_IDS
    token, ADMIN_IDS, storage.DB_PATH = load_config()
    storage.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with storage.polling_lock(storage.DB_PATH):
        storage.init_db()
        async with Bot(token=token) as bot:
            await bot.delete_webhook(drop_pending_updates=False)
            dispatcher = Dispatcher()
            dispatcher.include_router(router)
            polling_error = None

            @bot.session.middleware()
            async def log_polling_error(make_request, bot, method):
                nonlocal polling_error
                if not isinstance(method, GetUpdates):
                    return await make_request(bot, method)
                try:
                    result = await make_request(bot, method)
                except TelegramAPIError as error:
                    name = type(error).__name__
                    if name != polling_error:
                        LOGGER.warning("Ошибка получения обновлений (%s).", name)
                        if isinstance(error, TelegramConflictError):
                            LOGGER.error("Останови другой экземпляр с этим токеном или проверь webhook.")
                    polling_error = name
                    raise
                if polling_error:
                    LOGGER.info("Получение обновлений восстановлено.")
                    polling_error = None
                return result

            LOGGER.info("Бот запущен. Часовой пояс UTC+5.")
            try:
                # Последовательная обработка защищает публикации двух старост от гонок.
                await dispatcher.start_polling(
                    bot, handle_as_tasks=False, handle_signals=True, close_bot_session=False,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                )
            finally:
                LOGGER.info("Бот остановлен.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for name in ("aiogram.dispatcher", "aiogram.event", "aiohttp"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER.info("Бот остановлен.")
    except ConfigError as error:
        LOGGER.error("%s", error)
        raise SystemExit(1) from None
    except Exception as error:
        # Исключения сторонних библиотек могут содержать секреты и текст сообщений.
        LOGGER.error("Бот завершился с ошибкой (%s).", type(error).__name__)
        raise SystemExit(1) from None
