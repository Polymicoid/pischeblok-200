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

    if has_class_hour:
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


def pick_subject_emoji(subject: str) -> str:
    s = subject.lower()
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

        if rest.lower() in ("-", "—", "окно", "нет", "нет пары", "свободно"):
            header = f"<b>{pair_num} пара ({time_str})</b>" if time_str else f"<b>{pair_num} пара</b>"
            blocks.append(f"{header}\n⏸ Окно / нет пары")
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

        subj = subj[0].upper() + subj[1:] if subj else ""

        if room_str:
            learned_rooms.append((subj, room_str))
        else:
            remembered = get_saved_room(subj)
            if remembered:
                room_str = remembered

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
