import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNotFound
from aiogram.types import (
    CallbackQuery, EphemeralMessageParameters, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, InputRichMessage, InputRichMessageMedia, Message,
)

from schedule import ScheduleHTML, build_schedule_view, format_day_btn
from storage import clear_topic_screen, get_topic_screen, save_topic_screen

LOGGER = logging.getLogger("pischeblok")
BRAND = "👹 ПИЩЕБЛОК-200 | Учебный помощник"
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
CLOSE_SCREEN = "main"  # Совместимость с кнопками уже открытых экранов.
SCREENS = {"schedule", *DAYS, *TEXT_SCREENS, *SECTIONS}


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
    back = [button("⬅️ В закреп", CLOSE_SCREEN)]
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
        elif photo:
            rows.append([button("📄 Только текст", f"text:{day}")])
    else:
        text, photo = "📅 <b>Расписание</b>\n\nВыбери день.", None
    return text, photo, InlineKeyboardMarkup(inline_keyboard=[*rows, back])


async def remove_screen(bot: Bot, chat_id: int, user_id: int, screen_id: int, *, required: bool = False):
    try:
        await bot.delete_ephemeral_message(
            chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=screen_id,
        )
    except TelegramNotFound:
        return
    except TelegramAPIError as error:
        if required and "not found" not in str(error).lower():
            raise


async def close_screen(bot: Bot, message: Message, user_id: int, thread_id: int | None):
    scope = (message.chat.id, thread_id or 0, user_id)
    active = get_topic_screen(*scope)
    if message.ephemeral_message_id:
        await remove_screen(bot, message.chat.id, user_id, message.ephemeral_message_id, required=True)
    if active and active[0] != message.ephemeral_message_id:
        await remove_screen(bot, message.chat.id, user_id, active[0], required=True)
    clear_topic_screen(*scope)


def photo_message(text: str, photo: str) -> InputRichMessage:
    # Встроенное фото можно убрать следующей правкой того же временного сообщения.
    # Клиент сжимает отступы абзацев, поэтому пустая строка задаётся явно.
    lines = text.replace("\n\n", "<br>&#160;<br>").replace("\n", "<br>")
    return InputRichMessage(
        html=f'<img src="tg://photo?id=schedule-photo"/><p>{lines}</p>',
        media=[InputRichMessageMedia(id="schedule-photo", media=InputMediaPhoto(media=photo))],
        skip_entity_detection=True,
    )


async def show_screen(
    callback: CallbackQuery, bot: Bot, message: Message, screen: str, thread_id: int | None,
):
    text, photo, markup = render(screen)
    text = ScheduleHTML(text).limited(4096)
    payload = {"text": text, "parse_mode": "HTML"}
    if photo:
        payload = {"rich_message": photo_message(text, photo)}
    photo_fallback = {
        "text": ScheduleHTML(text + "\n\n📸 Фото временно недоступно.").limited(4096),
        "parse_mode": "HTML",
    }

    chat_id, user_id = message.chat.id, callback.from_user.id
    scope = (chat_id, thread_id or 0, user_id)
    clicked_id = message.ephemeral_message_id
    active = get_topic_screen(*scope)
    active_id = active[0] if active else clicked_id
    stale_ids = {value for value in (active_id, clicked_id) if value}
    target = {
        "chat_id": chat_id,
        "receiver_user_id": user_id,
        "ephemeral_message_id": active_id,
    }
    # Из закрепа отправляем экран клиенту, который нажал кнопку. Правка старого
    # экрана может пройти на сервере, даже если клиент больше его не показывает.
    if clicked_id:
        try:
            await bot.edit_ephemeral_message_text(**target, **payload, reply_markup=markup)
        except (TelegramBadRequest, TelegramNotFound) as error:
            if "message is not modified" not in str(error).lower():
                if not photo:
                    raise
                await bot.edit_ephemeral_message_text(**target, **photo_fallback, reply_markup=markup)
        save_topic_screen(*scope, active_id, False)
        for stale_id in stale_ids - {active_id}:
            await remove_screen(bot, chat_id, user_id, stale_id)
        return

    async def send_screen(**destination):
        if photo:
            try:
                return await bot.send_rich_message(**destination, **payload, reply_markup=markup)
            except (TelegramBadRequest, TelegramNotFound):
                return await bot.send_message(**destination, **photo_fallback, reply_markup=markup)
        return await bot.send_message(**destination, **payload, reply_markup=markup)

    try:
        sent = await send_screen(chat_id=None, ephemeral_message_parameters=EphemeralMessageParameters(
            callback_query_id=callback.id, replace_callback_query_message=True,
        ))
    except (TelegramBadRequest, TelegramNotFound) as error:
        # Повторяем только явный отказ API, чтобы сетевой сбой не создал второй экран.
        LOGGER.info("Замена закрепа отклонена (%s), открываем отдельный личный экран", type(error).__name__)
        sent = await send_screen(
            chat_id=chat_id, message_thread_id=thread_id,
            ephemeral_message_parameters=EphemeralMessageParameters(
                receiver_user_id=user_id, callback_query_id=callback.id, replace_callback_query_message=False,
            ),
        )

    if sent.ephemeral_message_id:
        save_topic_screen(*scope, sent.ephemeral_message_id, False)
        for stale_id in stale_ids - {sent.ephemeral_message_id}:
            await remove_screen(bot, chat_id, user_id, stale_id)
