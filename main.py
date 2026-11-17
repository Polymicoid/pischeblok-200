import asyncio
import html
import logging
import re
from datetime import date

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramConflictError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods import GetUpdates
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle, InputMediaPhoto, InputTextMessageContent, Message,
)

import storage
import group_interface
from config import ConfigError, load_config
from schedule import (
    ScheduleHTML, format_date_human, format_day_btn, format_schedule_blocks,
    format_updated_human, get_now, get_target_date, telegram_length, truncate_plain,
)
from storage import (
    bind_topic, clear_subject_rooms, delete_schedule, get_all_subject_rooms,
    get_schedule, get_setting, get_timing_int, get_topic_config, save_schedule,
    set_setting, set_topic_message, valid_timing,
)

LOGGER = logging.getLogger("pischeblok")
ADMIN_IDS: frozenset[int] = frozenset()
SPECIAL_ADMIN_ID = 5270827158
BOT_USERNAME = ""
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
ALERT_LIMIT = 200


def log(source: str, message: str):
    LOGGER.info("%s: %s", source, message)


class AdminCallbacks(BaseMiddleware):
    async def __call__(self, handler, event: CallbackQuery, data):
        key = event.data or ""
        if key.startswith(("quick:", "topic:")):
            return await handler(event, data)
        if key.startswith("admin:") and not is_admin(event.from_user.id):
            await event.answer("⛔ Нет доступа.", show_alert=True)
            return
        if not isinstance(event.message, Message) or event.message.chat.type != "private":
            await event.answer("Открой меню в личном чате через /start.")
            return
        if key.startswith("admin:"):
            draft = await data["state"].get_data()
            if key.startswith(("admin:ch:", "admin:confirm:", "admin:setbrk:")) or key == "admin:only_photo":
                if event.message.message_id != draft.get("menu_msg_id"):
                    await event.answer("Кнопка устарела. Открой панель заново через /start.", show_alert=True)
                    return
            action = re.fullmatch(r"admin:(day|edit|push|holiday|clear):(today|tomorrow)(?::([0-9-]+))?", key)
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
def kb_main_menu(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📅 Расписание", callback_data="menu:schedule")],
        [
            InlineKeyboardButton(text="📝 Домашние задания", callback_data="stub:hw"),
            InlineKeyboardButton(text="📚 Конспекты", callback_data="stub:notes"),
        ],
        [
            InlineKeyboardButton(text="📖 Учебники и методички", callback_data="stub:books"),
            InlineKeyboardButton(text="📢 Объявления", callback_data="stub:news"),
        ],
    ]
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton(text="🔐 Панель старосты (Админка)", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_schedule_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"📌 {format_day_btn('today')}", callback_data="sched:today"),
            InlineKeyboardButton(text=f"➡️ {format_day_btn('tomorrow')}", callback_data="sched:tomorrow"),
        ],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")],
    ])


def kb_schedule_view(day_key: str) -> InlineKeyboardMarkup:
    other_key = "tomorrow" if day_key == "today" else "today"
    other_text = "➡️ На завтра" if day_key == "today" else "⬅️ На сегодня"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=other_text, callback_data=f"sched:{other_key}"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"sched:{day_key}"),
        ],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")],
    ])


def kb_stub_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")]
    ])


def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 Управление: {format_day_btn('today')}", callback_data=admin_day_action("day", "today"))],
        [InlineKeyboardButton(text=f"📅 Управление: {format_day_btn('tomorrow')}", callback_data=admin_day_action("day", "tomorrow"))],
        [
            InlineKeyboardButton(text="⏱ Перерывы и пары", callback_data="admin:breaks"),
            InlineKeyboardButton(text="🏫 Кабинеты", callback_data="admin:rooms"),
        ],
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


def kb_admin_day(day_key: str, has_schedule: bool, has_topic: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Ввести / Изменить расписание", callback_data=admin_day_action("edit", day_key))],
        [InlineKeyboardButton(text="🏖 Поставить «Выходной / занятий нет»", callback_data=admin_day_action("holiday", day_key))],
    ]
    if has_schedule and has_topic:
        rows.append([InlineKeyboardButton(text="📤 Отправить в тему (новым сообщением)", callback_data=admin_day_action("push", day_key))])
    if has_schedule:
        rows.append([InlineKeyboardButton(text="📲 Переслать в другой чат (Inline)", switch_inline_query=day_key)])
        rows.append([InlineKeyboardButton(text="🗑 Сбросить («Не опубликовано»)", callback_data=admin_day_action("clear", day_key))])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_class_hour(day_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, будет классный час (08:30 – 09:00)", callback_data="admin:ch:yes")],
        [InlineKeyboardButton(text="❌ Нет, пары сразу с 08:30", callback_data="admin:ch:no")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=admin_day_action("day", day_key))],
    ])


def kb_admin_cancel_input(day_key: str, allow_only_photo: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if allow_only_photo:
        rows.append([InlineKeyboardButton(text="⏭ Опубликовать только фото (без текста)", callback_data="admin:only_photo")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=admin_day_action("day", day_key))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_preview(day_key: str, has_topic: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_topic:
        rows.append([InlineKeyboardButton(text="✅ Опубликовать (в боте + в теме)", callback_data="admin:confirm:topic")])
        rows.append([InlineKeyboardButton(text="🔒 Опубликовать только в боте", callback_data="admin:confirm:bot")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Опубликовать", callback_data="admin:confirm:bot")])
    rows.append([InlineKeyboardButton(text="✏️ Исправить", callback_data=admin_day_action("edit", day_key))])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=admin_day_action("day", day_key))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- СБОРКА ТЕКСТА РАСПИСАНИЯ ---
MAIN_MENU_TEXT = group_interface.MENU


def format_post_body(date_str: str, status: str, content: str | None, updated_at: str = None) -> str:
    date_human = format_date_human(date_str)
    header = f"📅 <b>Расписание на {date_human}</b>"

    if status == "holiday":
        body = f"{header}\n\n🏖 <b>Выходной / занятий нет.</b>"
    elif content:
        body = f"{header}\n\n{content}"
    else:
        body = header

    if updated_at:
        upd_human = format_updated_human(updated_at)
        body += f"\n\n🕒 Обновлено: {html.escape(upd_human)}"
    return body


def build_schedule_view(day_key: str) -> tuple[str, str | None, bool]:
    dt = get_target_date(day_key)
    date_str = dt.strftime("%Y-%m-%d")
    row = get_schedule(date_str)

    if not row:
        date_human = format_date_human(date_str)
        return (
            f"📅 <b>Расписание на {date_human}</b>\n\n"
            f"⏳ <i>Расписание пока не опубликовано.</i>",
            None,
            False,
        )

    status, content, updated_at, photo_id, _, _, _ = row
    return format_post_body(date_str, status, content, updated_at), photo_id, True


def build_compact_alert_text(day_key: str) -> str:
    dt = get_target_date(day_key)
    date_str = dt.strftime("%Y-%m-%d")
    row = get_schedule(date_str)
    title = f"📅 {format_date_human(date_str)}:"

    if not row:
        return f"{title}\n⏳ Расписание пока не опубликовано."
    status, content, _, _, _, _, _ = row
    if status == "holiday":
        return f"{title}\n🏖 Выходной / занятий нет."
    if not content:
        return f"{title}\n📸 Расписание только на фото — открой бота в ЛС."

    clean = ScheduleHTML(content).plain_text()
    blocks = [b.strip() for b in clean.split("\n\n") if b.strip()]
    lines = [title]
    for b in blocks:
        b_lines = [l.strip() for l in b.splitlines() if l.strip()]
        if b_lines[0].startswith("💬"):
            lines.append(b_lines[0])
            continue
        if b_lines[0].startswith("Классный час"):
            lines.append("🏫 08:30 Кл. час")
            continue
        m_p = re.match(r"(\d+)\s*пара\s*\((\d{2}:\d{2})", b_lines[0])
        num_t = f"{m_p.group(1)}) {m_p.group(2)}" if m_p else b_lines[0]
        subj = b_lines[1] if len(b_lines) > 1 else ""
        subj_short = re.sub(r"^[^\wА-Яа-яЁё]+", "", subj).strip()
        cab = ""
        if len(b_lines) > 2:
            cab = " (" + b_lines[2].replace("📍", "").replace("Каб.", "к.").strip() + ")"
        lines.append(f"{num_t} {subj_short}{cab}")

    return truncate_plain("\n".join(lines), ALERT_LIMIT, "\n…полное в ЛС бота")


# --- ПУБЛИКАЦИЯ В ТЕМУ СУПЕРГРУППЫ ---
async def sync_schedule_to_topic(
    bot: Bot,
    date_str: str,
    status: str,
    content: str | None,
    photo_id: str | None,
    old_topic_msg_id: int | None,
    updated_at: str = None,
    force_new: bool = False,
) -> tuple[int | None, str | None]:
    chat_id, thread_id = get_topic_config()
    if not chat_id:
        return None, "Тема группы не привязана (/set_topic)."

    upd = updated_at or get_now().strftime("%Y-%m-%d %H:%M")
    post_text = ScheduleHTML(format_post_body(date_str, status, content, upd)).limited(
        CAPTION_LIMIT if photo_id else MESSAGE_LIMIT,
    )

    try:
        if old_topic_msg_id and not force_new:
            try:
                if photo_id:
                    await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=old_topic_msg_id,
                        media=InputMediaPhoto(media=photo_id, caption=post_text, parse_mode="HTML"),
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=old_topic_msg_id,
                        text=post_text,
                        parse_mode="HTML",
                    )
                log("📢 Тема группы", f"обновлён пост за {date_str} (msg #{old_topic_msg_id})")
                return old_topic_msg_id, None
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return old_topic_msg_id, None

        if photo_id:
            sent = await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread_id,
                photo=photo_id,
                caption=post_text,
                parse_mode="HTML",
            )
            log("📢 Тема группы", f"отправлен НОВЫЙ ФОТО-пост за {date_str} (msg #{sent.message_id})")
        else:
            sent = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=post_text,
                parse_mode="HTML",
            )
            log("📢 Тема группы", f"отправлен НОВЫЙ пост за {date_str} (msg #{sent.message_id})")
        set_topic_message(date_str, sent.message_id)
        if old_topic_msg_id and not force_new:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_topic_msg_id)
            except TelegramAPIError as error:
                LOGGER.warning("Старый пост остался в теме (%s)", type(error).__name__)
        return sent.message_id, None

    except TelegramAPIError as error:
        LOGGER.warning("Ошибка публикации в тему (%s)", type(error).__name__)
        return old_topic_msg_id, "Проверь привязку темы и права бота."


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
        "text": group_interface.MENU, "parse_mode": "HTML",
        "reply_markup": group_interface.menu_keyboard(BOT_USERNAME, pinned=True),
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
    if not isinstance(message, Message) or message.chat.id != chat_id or screen not in group_interface.SCREENS:
        await callback.answer("Кнопка устарела. Выбери раздел в закреплённом меню.", show_alert=True)
        return
    if message.ephemeral_message_id:
        if (not message.receiver_user or message.receiver_user.id != callback.from_user.id
                or message.message_thread_id not in (None, thread_id)):
            await callback.answer("Открой свой экран из закреплённого меню.", show_alert=True)
            return
    elif str(message.message_id) != get_setting("topic_menu_msg_id"):
        await callback.answer("Кнопка устарела. Выбери раздел в закреплённом меню.", show_alert=True)
        return

    try:
        await group_interface.show_screen(callback, bot, message, screen, BOT_USERNAME, thread_id)
    except TelegramAPIError as error:
        LOGGER.warning("Персональный экран недоступен (%s)", type(error).__name__)
        fallback = (
            build_compact_alert_text(screen) if screen in group_interface.DAYS
            else "Персональный экран недоступен. В закреплённом меню есть «Быстро» и ссылка на бота."
        )
        await callback.answer(fallback, show_alert=True)
    else:
        await callback.answer()


@router.callback_query(F.data.startswith("quick:"))
async def cb_quick_view(callback: CallbackQuery):
    day_key = callback.data.split(":")[1]
    alert_text = build_compact_alert_text(day_key)
    await callback.answer(alert_text, show_alert=True)


@router.inline_query()
async def inline_schedule(query: InlineQuery):
    results = []
    for day_key, title in [("today", "📅 Расписание на сегодня"), ("tomorrow", "➡️ Расписание на завтра")]:
        text, _, has_sched = build_schedule_view(day_key)
        if not has_sched:
            continue
        results.append(
            InlineQueryResultArticle(
                id=day_key,
                title=title,
                description="Нажми, чтобы отправить расписание в этот чат",
                input_message_content=InputTextMessageContent(message_text=ScheduleHTML(text).limited(MESSAGE_LIMIT), parse_mode="HTML"),
            )
        )
    if not results:
        results.append(
            InlineQueryResultArticle(
                id="empty",
                title="⏳ Расписание ещё не опубликовано",
                description="Пока нет актуальных данных",
                input_message_content=InputTextMessageContent(
                    message_text="⏳ Расписание пока не опубликовано.", parse_mode="HTML"
                ),
            )
        )
    await query.answer(results, cache_time=5, is_personal=False)


# --- ЛИЧНЫЕ СООБЩЕНИЯ ---
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args

    if arg in group_interface.DAYS:
        text, photo, _ = build_schedule_view(arg)
        keyboard = kb_schedule_view(arg)
        if photo:
            caption = ScheduleHTML(text).limited(CAPTION_LIMIT)
            await message.answer_photo(photo, caption=caption, reply_markup=keyboard, parse_mode="HTML")
            if telegram_length(ScheduleHTML(text).plain_text()) > CAPTION_LIMIT:
                await message.answer(ScheduleHTML(text).limited(MESSAGE_LIMIT), parse_mode="HTML")
        else:
            await message.answer(ScheduleHTML(text).limited(MESSAGE_LIMIT), reply_markup=keyboard, parse_mode="HTML")
        return
    if arg == "schedule":
        text = "📅 <b>Раздел «Расписание»</b>\n\nВыбери день, на который хочешь посмотреть пары:"
        await message.answer(text, reply_markup=kb_schedule_menu(), parse_mode="HTML")
        return

    if arg in group_interface.SECTIONS:
        await message.answer(
            group_interface.section_text(arg),
            reply_markup=kb_stub_back(),
            parse_mode="HTML",
        )
        return

    await message.answer(MAIN_MENU_TEXT, reply_markup=kb_main_menu(message.from_user.id), parse_mode="HTML")


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_screen(callback.message, MAIN_MENU_TEXT, kb_main_menu(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "menu:schedule")
async def cb_schedule_menu(callback: CallbackQuery):
    text = "📅 <b>Раздел «Расписание»</b>\n\nВыбери день, на который хочешь посмотреть пары:"
    await render_screen(callback.message, text, kb_schedule_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sched:"))
async def cb_view_schedule(callback: CallbackQuery):
    day_key = callback.data.split(":")[1]
    text, photo_id, _ = build_schedule_view(day_key)
    await render_screen(
        callback.message,
        text,
        kb_schedule_view(day_key),
        photo_id=photo_id,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stub:"))
async def cb_stub(callback: CallbackQuery):
    key = callback.data.split(":")[1]
    text = group_interface.section_text(key) if key in group_interface.SECTIONS else "Раздел недоступен."
    await render_screen(callback.message, text, kb_stub_back())
    await callback.answer()


# --- АДМИНКА ---
@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await state.clear()
    chat_id, _ = get_topic_config()
    topic_status = "🟢 Тема группы привязана" if chat_id else "⚪️ Тема не привязана (напиши /set_topic в нужной теме)"

    if callback.from_user.id == SPECIAL_ADMIN_ID:
        header = "🔥 <b>Привет, ты сегодня на все 100 (градусов)!</b>"
    else:
        header = "🔐 <b>Панель старосты</b>"

    text = f"{header}\n{group_interface.BRAND}\n\n{topic_status}\n\nВыбери действие:"
    await render_screen(callback.message, text, kb_admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:breaks")
async def cb_admin_breaks(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await state.clear()
    text = (
        "⏱ <b>Настройка звонков и перерывов</b>\n\n"
        "• <b>Будни:</b> начало в 08:30 (с классным часом: кл. час 08:30–09:00, 1 пара с 09:05)\n"
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
    chat_id, _ = get_topic_config()

    text = f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}"
    await render_screen(callback.message, text, kb_admin_day(day_key, has_sched, bool(chat_id)), photo_id=photo_id)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:push:(today|tomorrow)(?::[0-9-]+)?$"))
async def cb_admin_push_topic(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
    date_str = callback.data.rsplit(":", 1)[-1]
    row = get_schedule(date_str)
    if not row:
        await callback.answer("Сначала добавь расписание.", show_alert=True)
        return

    status, content, updated_at, photo_id, old_topic_msg_id, _, _ = row
    _, err = await sync_schedule_to_topic(
        bot, date_str, status, content, photo_id, old_topic_msg_id, updated_at, force_new=True
    )
    if err:
        await callback.answer(f"❌ {err[:180]}", show_alert=True)
    else:
        await callback.answer("✅ Отправлено новым сообщением в тему группы!", show_alert=True)


@router.callback_query(F.data.regexp(r"^admin:holiday:(today|tomorrow)(?::[0-9-]+)?$"))
async def cb_admin_holiday(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return
    day_key = callback.data.split(":")[2]
    date_str = callback.data.rsplit(":", 1)[-1]

    old_row = get_schedule(date_str)
    old_topic_msg_id = old_row[4] if old_row else None
    save_schedule(date_str, "holiday")
    _, err = await sync_schedule_to_topic(bot, date_str, "holiday", None, None, old_topic_msg_id)
    log("👑 Староста", f"поставил ВЫХОДНОЙ на {date_str}")

    if err:
        await callback.answer(f"🏖 Выходной сохранён в боте, но в тему не ушло:\n{err[:140]}", show_alert=True)
    else:
        await callback.answer("🏖 Установлен статус «Выходной / занятий нет»", show_alert=True)

    current_text, _, has_sched = build_schedule_view(day_key)
    chat_id, _ = get_topic_config()
    await render_screen(
        callback.message,
        f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}",
        kb_admin_day(day_key, has_sched, bool(chat_id)),
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
    chat_id, _ = get_topic_config()
    await render_screen(
        callback.message,
        f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}",
        kb_admin_day(day_key, has_sched, bool(chat_id)),
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
            f"В {day_name} будет <b>классный час</b> (08:30 – 09:00)?"
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

    ch_notice = (
        "🔔 <b>Классный час (08:30 – 09:00) я подставлю сам — писать его НЕ нужно!</b> "
        "Начни сразу с 1-й пары (09:05).\n\n"
        if has_class_hour
        else ""
    )

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
    chat_topic_id, _ = get_topic_config()
    now_str = get_now().strftime("%Y-%m-%d %H:%M")
    preview_body = format_post_body(date_str, "published", content, now_str)
    preview_text = f"👀 <b>Предпросмотр:</b>\n\n{preview_body}"

    if photo_id:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=preview_text,
            reply_markup=kb_admin_preview(day_key, bool(chat_topic_id)),
            parse_mode="HTML",
        )
    else:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=preview_text,
            reply_markup=kb_admin_preview(day_key, bool(chat_topic_id)),
            parse_mode="HTML",
        )
    await state.update_data(menu_msg_id=sent.message_id)
    await remove_private_menu(bot, chat_id, menu_msg_id)


@router.callback_query(F.data.in_({"admin:confirm:bot", "admin:confirm:topic"}), AdminStates.confirm_schedule)
async def cb_admin_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    mode = callback.data.split(":")[2]
    data = await state.get_data()
    day_key = data["day_key"]
    date_str = data["date_str"]
    draft_content = data.get("draft_content")
    raw_content = data.get("raw_content")
    photo_id = data.get("photo_id")
    has_ch = 1 if data.get("has_class_hour") else 0
    learned_rooms = tuple(data.get("learned_rooms", ()))
    old_row = get_schedule(date_str)
    old_topic_msg_id = old_row[4] if old_row else None
    save_schedule(
        date_str, "published", draft_content, photo_id, old_topic_msg_id,
        raw_content, has_ch, learned_rooms,
    )
    await state.clear()
    topic_err = None

    if mode == "topic":
        _, topic_err = await sync_schedule_to_topic(
            bot, date_str, "published", draft_content, photo_id, old_topic_msg_id
        )


    log("👑 Староста", f"опубликовал расписание на {date_str} (в тему={'Да' if mode == 'topic' and not topic_err else 'Нет'})")

    if topic_err:
        await callback.answer(f"⚠️ В боте сохранено, но в тему не отправилось:\n{topic_err[:130]}", show_alert=True)
    elif mode == "topic":
        await callback.answer("✅ Опубликовано в боте и в теме!", show_alert=True)
    else:
        await callback.answer("✅ Опубликовано только в боте!", show_alert=True)

    current_text, current_photo, has_sched = build_schedule_view(day_key)
    chat_id, _ = get_topic_config()
    await render_screen(
        callback.message,
        f"⚙️ <b>Управление расписанием</b>\n\nСейчас студенты видят:\n— — — — —\n{current_text}",
        kb_admin_day(day_key, has_sched, bool(chat_id)),
        photo_id=current_photo,
    )


@router.callback_query()
async def stale_callback(callback: CallbackQuery):
    await callback.answer("Кнопка устарела. Открой меню заново через /start.", show_alert=True)


async def main():
    global ADMIN_IDS, BOT_USERNAME
    token, ADMIN_IDS, storage.DB_PATH = load_config()
    storage.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with storage.polling_lock(storage.DB_PATH):
        storage.init_db()
        async with Bot(token=token) as bot:
            BOT_USERNAME = (await bot.get_me()).username
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
