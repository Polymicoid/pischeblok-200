import html
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNotFound
from aiogram.types import (
    CallbackQuery, EphemeralMessageParameters, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from schedule import MONTHS_GEN, ScheduleHTML, format_day_btn, format_updated_human, get_target_date, telegram_length
from storage import get_schedule, get_topic_screen, save_topic_screen

BRAND = "👹 ПИЩЕБЛОК-200 | Учебный помощник"
MENU = f"<b>{BRAND}</b>\n\n🍳 Для группы ПКД-1. Выбери нужный раздел:"
SECTIONS = {
    "hw": ("📝 Домашние задания", "Здесь будут задания по предметам и сроки сдачи."),
    "notes": ("📚 Конспекты", "Здесь будут записи занятий и материалы, к которым можно вернуться."),
    "books": ("📖 Учебники и методички", "Здесь будут учебники, методички и ссылки на материалы."),
    "news": ("📢 Объявления", "Здесь будут важные новости группы и изменения в учебных планах."),
}
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
PAIR_ICONS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
DAYS = {"today", "tomorrow"}
SCREENS = {"main", "schedule", *DAYS, *SECTIONS}


def section_text(section: str) -> str:
    title, description = SECTIONS[section]
    return f"<b>{title}</b>\n\n{description}\n\n🚧 <i>Раздел в разработке</i>"


def button(text: str, screen: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"topic:{screen}")


def menu_keyboard(username: str, *, pinned: bool = False) -> InlineKeyboardMarkup:
    sections = [button(title, key) for key, (title, _) in SECTIONS.items()]
    rows = [[button("📅 Расписание", "schedule")], sections[:2], sections[2:]]
    if pinned:
        rows.append([InlineKeyboardButton(text="💬 Открыть бота в ЛС", url=f"https://t.me/{username}?start=schedule")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_screen(day: str) -> tuple[str, str | None]:
    target = get_target_date(day).date()
    row = get_schedule(target.isoformat())
    header = f"📅 <b>{WEEKDAYS[target.weekday()].upper()}, {target.day} {MONTHS_GEN[target.month].upper()}</b>"
    if not row:
        return f"{header}\n\n⏳ <i>Расписание пока не опубликовано</i>", None

    status, content, updated, photo, *_ = row
    if status == "holiday":
        body = "🏖 <b>Выходной / занятий нет</b>"
    else:
        body = content or ("📸 Расписание на фото" if photo else "⏳ Текст расписания отсутствует")

        def pair_heading(match: re.Match) -> str:
            number = int(match[1])
            label = PAIR_ICONS[number - 1] if 1 <= number <= len(PAIR_ICONS) else f"{number}."
            timing = re.sub(r"\s*–\s*", "–", match[2])
            return f"<b>{label} {timing}</b>"

        body = re.sub(r"\n+(?=<b>\d+ пара\b)", "\n\n", body)
        body = re.sub(r"(?m)^<b>(\d+) пара \((\d\d:\d\d\s*–\s*\d\d:\d\d)\)</b>$", pair_heading, body)
        body = body.replace("📍 Каб. ", "📍 Кабинет ")

    footer = f"\n\n🕒 Обновлено: {html.escape(format_updated_human(updated))}" if updated else ""
    return f"{header}\n\n{body}{footer}", photo


def render(screen: str, username: str) -> tuple[str, str | None, InlineKeyboardMarkup]:
    if screen == "main":
        return MENU, None, menu_keyboard(username)

    back = [button("⬅️ Главное меню", "main")]
    if screen in SECTIONS:
        return section_text(screen), None, InlineKeyboardMarkup(inline_keyboard=[back])

    rows = [[button(format_day_btn("today"), "today"), button(format_day_btn("tomorrow"), "tomorrow")]]
    if screen in DAYS:
        text, photo = schedule_screen(screen)
        rows.append([button("🔄 Обновить", screen), button("⬅️ К расписанию", "schedule")])
    else:
        text, photo = "📅 <b>Расписание</b>\n\nВыбери день.", None
    return text, photo, InlineKeyboardMarkup(inline_keyboard=[*rows, back])


async def remove_screen(bot: Bot, chat_id: int, user_id: int, screen_id: int):
    try:
        await bot.delete_ephemeral_message(
            chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=screen_id,
        )
    except TelegramAPIError:
        # Устаревший экран уже мог исчезнуть у пользователя.
        pass


async def show_screen(
    callback: CallbackQuery, bot: Bot, message: Message, screen: str, username: str, thread_id: int | None,
):
    text, photo, markup = render(screen, username)
    safe = ScheduleHTML(text)
    # Telegram сжимает интервалы между Rich-параграфами. HTML сохраняет пустые строки.
    text = safe.limited(4096)
    caption = safe.limited(1024, "\n… Открой полное расписание в ЛС.")
    if photo and telegram_length(safe.plain_text()) > 1024:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            *markup.inline_keyboard,
            [InlineKeyboardButton(text="💬 Полный текст в ЛС", url=f"https://t.me/{username}?start={screen}")],
        ])

    chat_id, user_id = message.chat.id, callback.from_user.id
    scope = (chat_id, thread_id or 0, user_id)
    clicked_id = message.ephemeral_message_id
    active = get_topic_screen(*scope)
    if not active and clicked_id:
        active = (clicked_id, bool(message.photo))
    active_id, active_has_photo = active or (None, False)
    stale_ids = {value for value in (active_id, clicked_id) if value}
    target = {
        "chat_id": chat_id,
        "receiver_user_id": user_id,
        "ephemeral_message_id": active_id,
    }
    # Из закрепа отправляем экран клиенту, который нажал кнопку. Правка старого
    # экрана может пройти на сервере, даже если клиент больше его не показывает.
    if clicked_id and active_id and active_has_photo == bool(photo):
        try:
            if photo:
                media = InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML")
                await bot.edit_ephemeral_message_media(**target, media=media, reply_markup=markup)
            else:
                await bot.edit_ephemeral_message_text(**target, text=text, parse_mode="HTML", reply_markup=markup)
        except (TelegramBadRequest, TelegramNotFound) as error:
            edited = "message is not modified" in str(error).lower()
        else:
            edited = True
        if edited:
            save_topic_screen(*scope, active_id, active_has_photo)
            for stale_id in stale_ids - {active_id}:
                await remove_screen(bot, chat_id, user_id, stale_id)
            return

    send = {
        "chat_id": chat_id, "message_thread_id": thread_id, "reply_markup": markup,
        "ephemeral_message_parameters": EphemeralMessageParameters(
            receiver_user_id=user_id, callback_query_id=callback.id, replace_callback_query_message=False,
        ),
    }
    sent_photo = bool(photo)
    if photo:
        try:
            sent = await bot.send_photo(**send, photo=photo, caption=caption, parse_mode="HTML")
        except (TelegramBadRequest, TelegramNotFound):
            sent_photo = False
            hint = "\n\n📸 Фото временно недоступно. Попробуй открыть его в ЛС."
            markup = InlineKeyboardMarkup(inline_keyboard=[
                *markup.inline_keyboard,
                [InlineKeyboardButton(text="💬 Фото в ЛС", url=f"https://t.me/{username}?start={screen}")],
            ])
            sent = await bot.send_message(
                **(send | {"reply_markup": markup}),
                text=ScheduleHTML(text + hint).limited(4096), parse_mode="HTML",
            )
    else:
        sent = await bot.send_message(**send, text=text, parse_mode="HTML")

    if sent.ephemeral_message_id:
        save_topic_screen(*scope, sent.ephemeral_message_id, sent_photo)
        for stale_id in stale_ids - {sent.ephemeral_message_id}:
            await remove_screen(bot, chat_id, user_id, stale_id)
