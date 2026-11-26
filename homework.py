import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from schedule import TIMEZONE_OFFSET, get_now, normalize_subject, pick_subject_emoji, subject_key, truncate_plain
from storage import connection

# Индекс предмета используется в базе и кнопках. Новые предметы добавляются в конец.
SUBJECTS = (
    "Другое", "Математика", "Обществознание", "Литература", "Русский язык", "Физика",
    "Физическая культура", "Химия", "Английский язык", "Немецкий язык", "Иностранный язык",
    "Основы безопасности и защиты Родины", "Основы исследовательской деятельности",
    "История", "Информатика", "География", "Микробиология",
)
SUBJECT_BUTTONS = {11: "ОБЗР", 12: "ОИД"}
PAGE_SIZE = 6
NEXT_LESSON = "К следующей паре"
LIST_ROUTE = re.compile(r"hw:(active|archive):([0-9]{1,2}):([0-9]{1,8})")
ITEM_ROUTE = re.compile(r"hw:item:([0-9]{1,18}):(active|archive):([0-9]{1,2}):([0-9]{1,8})")
SUBJECT_ROUTE = re.compile(r"hw:subjects:(active|archive)")


@dataclass
class Assignment:
    id: int | None = None
    subject: int = 0
    body: str = ""
    due_on: str | None = None
    photo_id: str | None = None
    document_id: str | None = None
    material_url: str | None = None
    due_next_lesson: bool = False
    archived: bool = False


def assignment_subject(text: str) -> int | None:
    aliases = {
        "английский": 8, "англ": 8, "англ.": 8, "инглиш": 8, "английский язык": 8,
        "немецкий": 9, "нем": 9, "нем.": 9, "немецкий язык": 9,
    }
    key = subject_key(text)
    if key in aliases:
        return aliases[key]
    name = subject_key(normalize_subject(text))
    return next((index for index, subject in enumerate(SUBJECTS) if subject.casefold() == name), None)


def subject_label(index: int, *, short: bool = False) -> str:
    name = SUBJECTS[index]
    emoji = ("🇬🇧" if index == 8 else "🇩🇪" if index == 9 else pick_subject_emoji(name))
    return f"{emoji} {SUBJECT_BUTTONS.get(index, name) if short else name}"


def parse_deadline(value: str) -> date:
    value = value.strip()
    if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
        return datetime.strptime(value, "%d.%m.%Y").date()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    raise ValueError("Нужна дата в формате ДД.ММ.ГГГГ.")


def suggested_deadline(text: str, published_at: datetime) -> date | None:
    hits = set(re.findall(r"\bна\s+(сегодня|завтра)\b", text.casefold()))
    if len(hits) != 1:
        return None
    local_day = published_at.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET))).date()
    return local_day + timedelta(days=int("завтра" in hits))


def parse_material_url(value: str) -> str:
    url = value.strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and port != 0
            and not any(char.isspace() or ord(char) < 32 or char in '<>\\' for char in url)
        ):
            return url
    except ValueError:
        pass
    raise ValueError("Пришли одну ссылку на материал, начиная с https:// или http://.")


def attachment_ids(message: Message) -> dict:
    return dict(
        photo_id=message.photo[-1].file_id if message.photo else None,
        document_id=message.document.file_id if message.document else None,
    )


def parse_content(message: Message) -> dict:
    body = (message.text or message.caption or "").strip()
    attachment = attachment_ids(message)
    if not body and not any(attachment.values()):
        raise ValueError("Пришли текст, фото или документ.")
    if len(body.encode("utf-16-le")) // 2 > 3000:
        raise ValueError("Сократи описание до 3000 символов. Подробности можно оставить в файле.")
    forward = message.forward_origin
    due = suggested_deadline(body, forward.date if forward else message.date)
    return dict(body=body, **attachment, suggested_due=due.isoformat() if due else None)


def get_assignment(assignment_id: int) -> Assignment | None:
    with connection() as conn:
        row = conn.execute("""
            SELECT id, subject, body, due_on, photo_id, document_id, material_url, due_next_lesson, archived
            FROM homework WHERE id = ?
        """, (assignment_id,)).fetchone()
    if row is None:
        return None
    return Assignment(*row[:-2], due_next_lesson=bool(row[-2]), archived=bool(row[-1]))


def save_assignment(assignment: Assignment) -> bool:
    values = (assignment.subject, assignment.body, assignment.due_on,
              assignment.photo_id, assignment.document_id, assignment.material_url, assignment.due_next_lesson)
    with connection() as conn:
        if assignment.id is None:
            conn.execute("""
                INSERT INTO homework (subject, body, due_on, photo_id, document_id, material_url, due_next_lesson)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, values)
            return True
        return conn.execute("""
            UPDATE homework SET subject=?, body=?, due_on=?, photo_id=?, document_id=?, material_url=?, due_next_lesson=?
            WHERE id=?
        """, (*values, assignment.id)).rowcount == 1


def set_archived(assignment_id: int, archived: bool) -> bool:
    with connection() as conn:
        return conn.execute(
            "UPDATE homework SET archived=? WHERE id=?", (archived, assignment_id),
        ).rowcount == 1


def deadline_label(due_on: str | None, next_lesson: bool, *, short: bool = False) -> str:
    if due_on:
        return date.fromisoformat(due_on).strftime("%d.%m" if short else "%d.%m.%Y")
    return NEXT_LESSON if next_lesson else "без срока"


def assignment_text(assignment: Assignment) -> str:
    due = deadline_label(assignment.due_on, assignment.due_next_lesson)
    body = f"\n\n{html.escape(assignment.body)}" if assignment.body else ""
    attachment = "\n\n📎 Материалы прикреплены к заданию." if assignment.photo_id or assignment.document_id else ""
    return f"<b>{subject_label(assignment.subject)}</b>\n📅 Срок · {due}{body}{attachment}"


def material_button(assignment: Assignment) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🔗 Открыть материал", url=assignment.material_url)] if assignment.material_url else []


def valid_student_screen(screen: str) -> bool:
    if screen == "hw" or SUBJECT_ROUTE.fullmatch(screen):
        return True
    match = LIST_ROUTE.fullmatch(screen) or ITEM_ROUTE.fullmatch(screen)
    return bool(match and int(match.groups()[-2]) <= len(SUBJECTS))


def key(text: str, action: str, admin: bool = False) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{'admin' if admin else 'topic'}:{action}")


def list_view(screen: str, *, admin: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    heading = "<b>📝 Домашние задания</b>"
    match = LIST_ROUTE.fullmatch(screen)
    subjects_menu = SUBJECT_ROUTE.fullmatch(screen)
    mode = match[1] if match else subjects_menu[1] if subjects_menu else "active"
    subject, page = (int(match[2]), int(match[3])) if match else (0, 0)
    archived = mode == "archive"
    predicate = "(archived = 1 OR due_on < ?)" if archived else "archived = 0 AND (due_on IS NULL OR due_on >= ?)"
    today = get_now().date().isoformat()
    args = (today,)
    back = [key("⬅️ Назад в админку", "menu", True)] if admin else [key("⬅️ В закреп", "main")]
    with connection() as conn:
        if subjects_menu:
            subjects = conn.execute(
                f"SELECT DISTINCT subject FROM homework WHERE {predicate} ORDER BY subject", args,
            ).fetchall()
            rows = [[key(subject_label(index, short=True), f"hw:{mode}:{index if index else len(SUBJECTS)}:0", admin)] for (index,) in subjects]
            rows.append([key("Все предметы", f"hw:{mode}:0:0", admin)])
            return f"{heading}\n\nВыбери предмет.", InlineKeyboardMarkup(inline_keyboard=[*rows, back])
        if subject:
            predicate += " AND subject = ?"
            args += (subject if subject < len(SUBJECTS) else 0,)
        ordering = "due_on DESC, id DESC" if archived else "due_on IS NULL, due_next_lesson DESC, due_on, id DESC"
        count, = conn.execute(f"SELECT COUNT(*) FROM homework WHERE {predicate}", args).fetchone()
        page = min(page, max(count - 1, 0) // PAGE_SIZE)
        rows = conn.execute(f"""
            SELECT id, subject, body, due_on, due_next_lesson FROM homework WHERE {predicate}
            ORDER BY {ordering} LIMIT ? OFFSET ?
        """, (*args, PAGE_SIZE, page * PAGE_SIZE)).fetchall()
    buttons = []
    if admin:
        buttons.append([key("➕ Добавить домашку", "hw:new", True)])
    for assignment_id, index, body, due, next_lesson in rows:
        title = " ".join(body.split()) or "Материалы"
        due_label = deadline_label(due, next_lesson, short=True)
        label = truncate_plain(f"{due_label} · {subject_label(index, short=True)} · {title}", 62)
        buttons.append([key(label, f"hw:item:{assignment_id}:{mode}:{subject}:{page}", admin)])
    pages = []
    if page:
        pages.append(key("◀️", f"hw:{mode}:{subject}:{page - 1}", admin))
    if (page + 1) * PAGE_SIZE < count:
        pages.append(key("▶️", f"hw:{mode}:{subject}:{page + 1}", admin))
    if pages:
        buttons.append(pages)
    other = "active" if archived else "archive"
    buttons.append([
        key("📚 Предметы", f"hw:subjects:{mode}", admin),
        key("📌 Актуальное" if archived else "🗂 Архив", f"hw:{other}:0:0", admin),
    ])
    view = "Архив" if archived else "Актуальное"
    if subject:
        view += f" · {subject_label(subject if subject < len(SUBJECTS) else 0)}"
    text = f"{heading}\n{html.escape(view)}\n\n" + ("Выбери задание." if rows else "Заданий пока нет.")
    return text, InlineKeyboardMarkup(inline_keyboard=[*buttons, back])
