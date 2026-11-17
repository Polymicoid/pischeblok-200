import html
import re
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNotFound
from aiogram.types import (
    CallbackQuery, EphemeralMessageParameters, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, InputRichMessage, Message,
)

from schedule import MONTHS_GEN, ScheduleHTML, format_day_btn, format_updated_human, get_target_date, telegram_length
from storage import get_schedule

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
        rows.extend([
            [InlineKeyboardButton(text=f"👀 Быстро: {label}", callback_data=f"quick:{day}")
             for day, label in (("today", "сегодня"), ("tomorrow", "завтра"))],
            [InlineKeyboardButton(text="💬 Открыть бота в ЛС", url=f"https://t.me/{username}?start=schedule")],
        ])
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


def rich_text(text: str) -> InputRichMessage:
    # Превращаем только уже очищенный HTML. Пользовательский ввод остаётся экранированным.
    first, _, rest = text.partition("\n\n")
    heading = ScheduleHTML(first).plain_text()
    paragraphs = [block.replace("\n", "<br>") for block in rest.split("\n\n") if block]
    detail = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return InputRichMessage(html=f"<h3>{html.escape(heading)}</h3>{detail}", skip_entity_detection=True)


async def show_screen(
    callback: CallbackQuery, bot: Bot, message: Message, screen: str, username: str, thread_id: int | None,
):
    text, photo, markup = render(screen, username)
    safe = ScheduleHTML(text)
    text = safe.limited(4096)
    caption = safe.limited(1024, "\n… Открой полное расписание в ЛС.")
    if photo and telegram_length(safe.plain_text()) > 1024:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            *markup.inline_keyboard,
            [InlineKeyboardButton(text="💬 Полный текст в ЛС", url=f"https://t.me/{username}?start={screen}")],
        ])

    reply = EphemeralMessageParameters(
        receiver_user_id=callback.from_user.id, callback_query_id=callback.id,
        replace_callback_query_message=False,
    )
    send = {
        "chat_id": message.chat.id, "message_thread_id": thread_id,
        "ephemeral_message_parameters": reply, "reply_markup": markup,
    }
    target = {
        "chat_id": message.chat.id,
        "receiver_user_id": callback.from_user.id,
        "ephemeral_message_id": message.ephemeral_message_id,
    }
    # Rich Messages используются для расписания с текстом. Фото отправляется штатным методом.
    rich = rich_text(text) if screen in DAYS and not photo else None
    if message.ephemeral_message_id and bool(message.photo) == bool(photo):
        try:
            if photo:
                media = InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML")
                await bot.edit_ephemeral_message_media(**target, media=media, reply_markup=markup)
            else:
                if rich:
                    try:
                        await bot.edit_ephemeral_message_text(**target, rich_message=rich, reply_markup=markup)
                        return
                    except (TelegramBadRequest, TelegramNotFound) as error:
                        if "message is not modified" in str(error).lower():
                            return
                await bot.edit_ephemeral_message_text(**target, text=text, parse_mode="HTML", reply_markup=markup)
            return
        except (TelegramBadRequest, TelegramNotFound) as error:
            if "message is not modified" in str(error).lower():
                return

    if photo:
        try:
            sent = await bot.send_photo(**send, photo=photo, caption=caption, parse_mode="HTML")
        except (TelegramBadRequest, TelegramNotFound):
            hint = "\n\n📸 Фото временно недоступно. Попробуй открыть его в ЛС."
            markup = InlineKeyboardMarkup(inline_keyboard=[
                *markup.inline_keyboard,
                [InlineKeyboardButton(text="💬 Фото в ЛС", url=f"https://t.me/{username}?start={screen}")],
            ])
            sent = await bot.send_message(
                **(send | {"reply_markup": markup}),
                text=ScheduleHTML(text + hint).limited(4096), parse_mode="HTML",
            )
    elif rich:
        try:
            sent = await bot.send_rich_message(**send, rich_message=rich)
        except (TelegramBadRequest, TelegramNotFound):
            sent = await bot.send_message(**send, text=text, parse_mode="HTML")
    else:
        sent = await bot.send_message(**send, text=text, parse_mode="HTML")

    # Смена типа экрана или восстановление после исчезновения. Общее сообщение сюда не попадает.
    if message.ephemeral_message_id and sent.ephemeral_message_id:
        try:
            await bot.delete_ephemeral_message(**target)
        except TelegramAPIError:
            pass
