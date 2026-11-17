from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNotFound
from aiogram.types import (
    CallbackQuery, EphemeralMessageParameters, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from schedule import ScheduleHTML, build_schedule_view, format_day_btn, telegram_length
from storage import get_topic_screen, save_topic_screen



BRAND = "👹 ПИЩЕБЛОК-200 | Учебный помощник"
MENU = f"<b>{BRAND}</b>\n\n🍳 Для группы ПКД-1. Выбери нужный раздел:"
PINNED_MENU_TEXT = (
    "👹 <b>ПИЩЕБЛОК-200</b>\n"
    "<i>Учебный штаб группы ПКД-1</i>\n\n"
    "🔥 Здесь кипят не только кастрюли, но и дедлайны.\n\n"
    "📅 <b>Расписание</b> всегда под рукой.\n"
    "📝 <b>Домашка</b>, 📚 <b>конспекты</b> и 📢 <b>объявления</b> скоро появятся в меню.\n\n"
    "🍳 Всё необходимое для студенческой жизни постепенно собираем в одном месте.\n\n"
    "<b>Наша задача: дожить до диплома и ничего не забыть.</b> 💀\n\n"
    "👇 Выбирай нужный раздел!"
)
SECTIONS = {
    "hw": ("📝 Домашние задания", "Здесь будут задания по предметам и сроки сдачи."),
    "notes": ("📚 Конспекты", "Здесь будут записи занятий и материалы, к которым можно вернуться."),
    "books": ("📖 Учебники и методички", "Здесь будут учебники, методички и ссылки на материалы."),
    "news": ("📢 Объявления", "Здесь будут важные новости группы и изменения в учебных планах."),
}
DAYS = {"today", "tomorrow"}
TEXT_SCREENS = {f"text:{day}" for day in DAYS}
SCREENS = {"main", "schedule", *DAYS, *TEXT_SCREENS, *SECTIONS}


def section_text(section: str) -> str:
    title, description = SECTIONS[section]
    return f"<b>{title}</b>\n\n{description}\n\n🚧 <i>Раздел в разработке</i>"


def button(text: str, screen: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"topic:{screen}")


def menu_keyboard() -> InlineKeyboardMarkup:
    sections = [button(title, key) for key, (title, _) in SECTIONS.items()]
    rows = [[button("📅 Расписание", "schedule")], sections[:2], sections[2:]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render(screen: str) -> tuple[str, str | None, InlineKeyboardMarkup]:
    if screen == "main":
        return MENU, None, menu_keyboard()

    back = [button("⬅️ Главное меню", "main")]
    if screen in SECTIONS:
        return section_text(screen), None, InlineKeyboardMarkup(inline_keyboard=[back])

    rows = [[button(format_day_btn("today"), "today"), button(format_day_btn("tomorrow"), "tomorrow")]]
    day = screen.removeprefix("text:")
    if day in DAYS:
        text, photo, _ = build_schedule_view(day)
        rows.append([button("🔄 Обновить", screen), button("⬅️ К расписанию", "schedule")])
        if photo and screen in TEXT_SCREENS:
            rows.append([button("📸 К фото", day)])
            photo = None
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
    callback: CallbackQuery, bot: Bot, message: Message, screen: str, thread_id: int | None,
):
    text, photo, markup = render(screen)
    safe = ScheduleHTML(text)
    # Telegram сжимает интервалы между Rich-параграфами. HTML сохраняет пустые строки.
    text = safe.limited(4096)
    caption = safe.limited(1024, "\n… Полный текст доступен по кнопке ниже.")
    if photo and telegram_length(safe.plain_text()) > 1024:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            *markup.inline_keyboard,
            [button("📄 Полный текст", f"text:{screen}")],
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
            hint = "\n\n📸 Фото временно недоступно. Нажми «Обновить», чтобы повторить попытку."
            sent = await bot.send_message(
                **send,
                text=ScheduleHTML(text + hint).limited(4096), parse_mode="HTML",
            )
    else:
        sent = await bot.send_message(**send, text=text, parse_mode="HTML")

    if sent.ephemeral_message_id:
        save_topic_screen(*scope, sent.ephemeral_message_id, sent_photo)
        for stale_id in stale_ids - {sent.ephemeral_message_id}:
            await remove_screen(bot, chat_id, user_id, stale_id)
