import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

TIMEZONE_OFFSET = 5

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]


# --- ДАТА И ВРЕМЯ ---
def get_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))


def get_target_date(day_key: str) -> datetime:
    now = get_now()
    return now if day_key == "today" else now + timedelta(days=1)


def format_date_human(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = get_now().date()
    tomorrow = today + timedelta(days=1)
    wd = WEEKDAYS_SHORT[dt.weekday()]
    date_part = f"{wd}, {dt.day} {MONTHS_GEN[dt.month]}"

    if dt == today:
        return f"сегодня ({date_part})"
    if dt == tomorrow:
        return f"завтра ({date_part})"
    return date_part


def format_day_btn(day_key: str) -> str:
    dt = get_target_date(day_key)
    prefix = "Сегодня" if day_key == "today" else "Завтра"
    return f"{prefix} ({dt.strftime('%d.%m')}, {WEEKDAYS_SHORT[dt.weekday()]})"


def format_updated_human(updated_str: str) -> str:
    if not updated_str:
        return ""
    try:
        dt = datetime.strptime(updated_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return updated_str

    today = get_now().date()
    yesterday = today - timedelta(days=1)
    t_str = dt.strftime("%H:%M")

    if dt.date() == today:
        return f"сегодня в {t_str}"
    if dt.date() == yesterday:
        return f"вчера в {t_str}"
    return f"{dt.day} {MONTHS_GEN[dt.month]} в {t_str}"


def calculate_pair_times(date_str: str, has_class_hour: bool) -> dict[int, str]:
    from storage import get_timing_int

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    is_saturday = dt.weekday() == 5

    pair_dur = get_timing_int("pair_duration")
    if is_saturday:
        b1 = get_timing_int("sat_break_1")
        b2 = get_timing_int("sat_break_2")
        bo = get_timing_int("sat_break_other")
    else:
        b1 = get_timing_int("wd_break_1")
        b2 = get_timing_int("wd_break_2")
        bo = get_timing_int("wd_break_other")

    # В понедельник и четверг сетка пар начинается в 09:05 даже при отмене классного часа.
    if dt.weekday() in (0, 3) or has_class_hour:
        cur = datetime(2000, 1, 1, 9, 5)
    else:
        cur = datetime(2000, 1, 1, 8, 30)

    times = {}
    for p in range(1, 11):
        end = cur + timedelta(minutes=pair_dur)
        times[p] = f"{cur.strftime('%H:%M')} – {end.strftime('%H:%M')}"
        brk = b1 if p == 1 else (b2 if p == 2 else bo)
        cur = end + timedelta(minutes=brk)
    return times


SUBJECT_EMOJIS = {
    "математика": "📐", "матан": "📐",
    "обществознание": "👥", "общество": "👥",
    "литература": "📖",
    "русский язык": "✍️",
    "физика": "⚛️",
    "физическая культура": "⚽", "физкультура": "⚽", "физра": "⚽",
    "химия": "🧪",
    "английский язык": "🇬🇧", "английский": "🇬🇧",
    "немецкий язык": "🇩🇪", "немецкий": "🇩🇪",
    "основы безопасности и защиты родины": "🛡️", "обзр": "🛡️",
    "основы исследовательской деятельности": "🔎", "оид": "🔎",
    "история": "🏛️",
    "информатика": "💻",
    "география": "🌍",
    "микробиология": "🔬", "микробио": "🔬",
    "иностранный язык": "🌐",
}
EMPTY_PAIRS = {"-", "–", "—", "окно", "нет", "нет пары", "свободно", "пропуск", "пропуски", "окно / нет пары"}
FOREIGN_LANGUAGE = "Иностранный язык"
FOREIGN_ROOMS = "Немецкий — каб. 13 · Английский — каб. 17"
SUBJECT_NAMES = {
    "матан": "Математика",
    "общество": "Обществознание",
    "физра": "Физическая культура",
    "физкультура": "Физическая культура",
    "обзр": "Основы безопасности и защиты Родины",
    "оид": "Основы исследовательской деятельности",
    "микробио": "Микробиология",
    **dict.fromkeys((
        "иностранный", "иностранный язык", "ин яз", "ин. яз.", "иняз",
        "английский", "английский язык", "англ", "англ.",
        "немецкий", "немецкий язык", "нем", "нем.",
    ), FOREIGN_LANGUAGE),
}


def subject_key(subject: str) -> str:
    return " ".join(subject.casefold().strip().removeprefix("#").replace("_", " ").split())


def normalize_subject(subject: str) -> str:
    key = subject_key(subject)
    if key in SUBJECT_NAMES:
        return SUBJECT_NAMES[key]
    if key in SUBJECT_EMOJIS:
        return key.capitalize()
    return subject[:1].upper() + subject[1:]


def pick_subject_emoji(subject: str) -> str:
    s = subject_key(subject)
    if s in EMPTY_PAIRS:
        return "❌"
    if s in SUBJECT_EMOJIS:
        return SUBJECT_EMOJIS[s]
    rules = [
        (["микробио", "биолог", "экол", "анатом", "генет"], "🔬"),
        (["хим"], "🧪"),
        (["матем", "алгеб", "геом", "высш", "стат", "дискрет"], "📐"),
        (["информ", "програм", "пкд", "баз", "веб", "алгоритм", "ооп", "питон", "python", "икт", "комп", "сеть", "сет"], "💻"),
        (["физик", "астрон", "электро", "схемот"], "⚛️"),
        (["истор", "обществ", "прав", "полит", "социо", "философ", "эконом"], "🏛"),
        (["англ", "немец", "иностр", "язык"], "🗣"),
        (["русск", "литерат", "речь", "документ"], "📖"),
        (["физк", "физр", "спорт", "фк"], "⚽️"),
        (["обж", "бжд"], "🛡"),
        (["практик", "уп", "пп"], "🛠"),
        (["основы исследовательской деятельности"], "🕵️‍♀️"),
    ]
    for keywords, emoji in rules:
        if any(k in s for k in keywords):
            return emoji
    return "📚"


def refresh_subjects(content: str) -> str:
    """Применяет текущие названия к сохранённым блокам без записи в SQLite."""
    def replace(match: re.Match) -> str:
        heading, old_emoji, old_subject, room = match.groups()
        key = subject_key(html.unescape(old_subject))
        if key in SUBJECT_NAMES or key in SUBJECT_EMOJIS:
            subject = normalize_subject(html.unescape(old_subject))
            old_subject = html.escape(subject)
            key = subject_key(subject)
            if subject == FOREIGN_LANGUAGE:
                room = f"\n📍 {FOREIGN_ROOMS}"
        emoji = "❌" if key in EMPTY_PAIRS else SUBJECT_EMOJIS.get(key, old_emoji)
        return f"{heading}{emoji} {old_subject}{room or ''}"

    return re.sub(
        r"(?m)^(<b>\d+ пара(?: \([^\n]*\))?</b>\n)([^\w\s]+) ([^\n]+)(\n📍 [^\n]+)?$",
        replace, content,
    )


def format_schedule_blocks(raw_text: str, date_str: str, has_class_hour: bool) -> tuple[str, list[tuple[str, str]]]:
    from storage import get_saved_room

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    pair_times = calculate_pair_times(date_str, has_class_hour)
    blocks = []
    comments = []
    learned_rooms = []
    pair_num = 0

    if has_class_hour:
        blocks.append("<b>Классный час (08:30 – 09:00)</b>\n🏫 Классный час")

    for line in lines:
        if line.startswith("-") and len(line.lstrip("- ").strip()) > 0:
            comment_text = line.lstrip("- ").strip()
            comments.append(comment_text)
            continue

        rest = line

        # Точка в ручном времени 9.05–10.40 не является номером пары.
        m_num = re.match(
            r"^\s*(10|[1-9])(?:\s*пара\b|\)|\.(?!\d{2}\s*[-–—]))\s*(.*?)$",
            rest, flags=re.IGNORECASE,
        )
        if m_num:
            pair_num = int(m_num.group(1))
            rest = (m_num.group(2) or "-").strip()
        else:
            pair_num += 1

        m_time = re.match(r"^\(?\s*(\d{1,2}[:.]\d{2}\s*[-–—]\s*\d{1,2}[:.]\d{2})\s*\)?\s+(.+)$", rest)
        if not m_time and re.match(r"^\(?\s*\d{1,2}[:.]\d{2}\s*[-–—]", rest):
            raise ValueError("Укажи время и предмет, например: 09:05–10:40 Биология.")
        if m_time:
            raw_t = m_time.group(1).replace(".", ":")
            t_parts = re.split(r"\s*[-–—]\s*", raw_t)
            try:
                start, end = (datetime.strptime(part, "%H:%M") for part in t_parts)
                if end <= start:
                    raise ValueError
            except ValueError:
                raise ValueError("Проверь ручное время пары: начало должно быть раньше конца.") from None
            time_str = f"{start:%H:%M} – {end:%H:%M}"
            rest = m_time.group(2).strip()
        else:
            time_str = pair_times.get(pair_num, "")

        if subject_key(rest) in EMPTY_PAIRS:
            header = f"<b>{pair_num} пара ({time_str})</b>" if time_str else f"<b>{pair_num} пара</b>"
            blocks.append(f"{header}\n❌ Пропуск")
            continue

        room_str = None
        m_room = re.match(
            r"^(.*?)(?:\s*[-–—|:]\s*|\s+)(?:каб\.?|ауд\.?|к\.?)?\s*(\d{1,4}[а-яёa-z]?)\s*(?:каб\.?|ауд\.?|к\.?)?$",
            rest,
            flags=re.IGNORECASE,
        )
        if m_room and m_room.group(1).strip():
            subj = m_room.group(1).strip(" -–—|:")
            room_str = f"Каб. {m_room.group(2).strip()}"
        else:
            parts = re.split(r"\s+-\s+|\s*[–—|]\s*", rest, maxsplit=1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                subj = parts[0].strip()
                room_str = parts[1].strip()
                room_str = room_str[0].upper() + room_str[1:]
            else:
                subj = rest.strip()

        original_subject = subj
        subj = normalize_subject(subj)
        if subj == FOREIGN_LANGUAGE:
            room_str = FOREIGN_ROOMS
        elif room_str:
            learned_rooms.append((subj, room_str))
        else:
            room_str = get_saved_room(subj) or get_saved_room(original_subject)
            if not room_str:
                for alias, name in SUBJECT_NAMES.items():
                    if name == subj and (room_str := get_saved_room(alias)):
                        break

        emoji = pick_subject_emoji(subj)
        header = f"<b>{pair_num} пара ({time_str})</b>" if time_str else f"<b>{pair_num} пара</b>"

        block_lines = [header, f"{emoji} {html.escape(subj)}"]
        if room_str:
            block_lines.append(f"📍 {html.escape(room_str)}")
        blocks.append("\n".join(block_lines))

    if comments:
        if len(comments) == 1:
            blocks.append(f"💬 <b>Комментарий:</b> {html.escape(comments[0])}")
        else:
            c_lines = "\n".join(f"• {html.escape(c)}" for c in comments)
            blocks.append(f"💬 <b>Комментарии:</b>\n{c_lines}")

    return "\n\n".join(blocks), learned_rooms


def telegram_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def truncate_plain(value: str, limit: int, suffix: str = "…") -> str:
    if telegram_length(value) <= limit:
        return value
    raw = value.encode("utf-16-le")[:2 * (limit - telegram_length(suffix))]
    return raw.decode("utf-16-le", errors="ignore").rstrip() + suffix


class ScheduleHTML(HTMLParser):
    # Старые записи содержат HTML. Пропускаем только теги, которые создаёт сам бот.
    allowed = {"b", "i", "code"}

    def __init__(self, value: str):
        super().__init__()
        self.parts = []
        self.plain = []
        self.tags = []
        self.feed(value)
        self.close()
        self.parts.extend(f"</{tag}>" for tag in reversed(self.tags))

    def handle_starttag(self, tag, attrs):
        if tag in self.allowed:
            self.parts.append(f"<{tag}>")
            self.tags.append(tag)

    def handle_endtag(self, tag):
        if self.tags and self.tags[-1] == tag:
            self.tags.pop()
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_data(self, data):
        self.plain.append(data)
        self.parts.append(html.escape(data, quote=False))

    def plain_text(self):
        return "".join(self.plain)

    def limited(self, limit: int, suffix: str = "…") -> str:
        plain = self.plain_text()
        if telegram_length(plain) <= limit:
            return "".join(self.parts)
        return html.escape(truncate_plain(plain, limit, suffix), quote=False)


def format_post_body(date_str: str, status: str, content: str | None, updated_at: str = None) -> str:
    date_human = format_date_human(date_str)
    header = f"📅 <b>Расписание на {date_human}</b>"

    if status == "holiday":
        body = f"{header}\n\n🏖 <b>Выходной / занятий нет.</b>"
    elif content:
        body = f"{header}\n\n{refresh_subjects(content)}"
    else:
        body = header

    if updated_at:
        upd_human = format_updated_human(updated_at)
        body += f"\n\n🕒 Обновлено: {html.escape(upd_human)}"
    return body


def build_schedule_view(day_key: str) -> tuple[str, str | None, bool]:
    from storage import get_schedule

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
