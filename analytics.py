import hmac
import html
from datetime import date, datetime, timedelta
from math import ceil
from statistics import median

from homework import subject_label
from schedule import get_now, truncate_plain
from storage import connection, prune_homework_views, prune_schedule_requests

DAYS = ("today", "tomorrow")
ACTION_LABELS = {
    "schedule": "вход из меню",
    "today": "«Сегодня»",
    "tomorrow": "«Завтра»",
    "text": "текстовая версия",
}


def record_request(
    callback_id: str, user_id: int, started_at: datetime, action: str,
    status: str | None, result: str, duration_ms: int,
):
    with connection() as conn:
        secret, = conn.execute("SELECT value FROM settings WHERE key = 'analytics_secret'").fetchone()
        key = bytes.fromhex(secret)
        event_key = hmac.digest(key, f"request:{callback_id}".encode(), "sha256")
        student_key = hmac.digest(key, f"student:{user_id}".encode(), "sha256")
        prune_schedule_requests(conn)
        conn.execute("""
            INSERT OR IGNORE INTO schedule_requests
                (event_key, student_key, occurred_at, action, schedule_status, result, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (event_key, student_key, started_at.isoformat(timespec="seconds"), action, status, result, duration_ms))


def record_homework_view(callback_id: str, user_id: int, started_at: datetime, assignment_id: int):
    with connection() as conn:
        secret, = conn.execute("SELECT value FROM settings WHERE key = 'analytics_secret'").fetchone()
        key = bytes.fromhex(secret)
        event_key = hmac.digest(key, f"homework-view:{callback_id}".encode(), "sha256")
        student_key = hmac.digest(key, f"homework-student:{user_id}".encode(), "sha256")
        prune_homework_views(conn)
        conn.execute("""
            INSERT OR IGNORE INTO homework_views (event_key, student_key, occurred_at, assignment_id)
            SELECT ?, ?, ?, id FROM homework WHERE id = ?
        """, (event_key, student_key, started_at.isoformat(timespec="seconds"), assignment_id))


def report_dates() -> tuple[date, date, date]:
    today = get_now().date()
    return today - timedelta(days=6), today, today + timedelta(days=1)


def homework_view_counts(assignment_id: int) -> tuple[int, int]:
    first_day, _, tomorrow = report_dates()
    with connection() as conn:
        return conn.execute("""
            SELECT COUNT(DISTINCT student_key), COUNT(*)
            FROM homework_views WHERE assignment_id = ? AND occurred_at >= ? AND occurred_at < ?
        """, (assignment_id, first_day.isoformat(), tomorrow.isoformat())).fetchone()


def homework_report() -> str:
    first_day, today, tomorrow = report_dates()
    bounds = (first_day.isoformat(), tomorrow.isoformat())
    with connection() as conn:
        prune_homework_views(conn)
        students, students_today, views, assignments = conn.execute("""
            SELECT COUNT(DISTINCT student_key),
                   COUNT(DISTINCT CASE WHEN occurred_at >= ? THEN student_key END),
                   COUNT(*), COUNT(DISTINCT assignment_id)
            FROM homework_views WHERE occurred_at >= ? AND occurred_at < ?
        """, (today.isoformat(), *bounds)).fetchone()
        top = conn.execute("""
            SELECT h.subject, h.body, COUNT(DISTINCT v.student_key) AS students, COUNT(*) AS views
            FROM homework_views v JOIN homework h ON h.id = v.assignment_id
            WHERE v.occurred_at >= ? AND v.occurred_at < ?
            GROUP BY h.id
            ORDER BY students DESC, views DESC, h.id DESC LIMIT 5
        """, bounds).fetchall()

    lines = [
        f"📊 <b>Домашние задания за 7 дней</b> · {first_day:%d.%m}–{today:%d.%m}",
        "",
        f"Уникальных студентов за неделю · <b>{students}</b>",
        f"Уникальных студентов сегодня · {students_today}",
        f"Открытий карточек · {views}",
        f"Заданий с просмотрами · {assignments}",
    ]
    if top:
        lines.extend(("", "<b>Задания с наибольшим охватом</b>"))
        for subject, body, reach, opens in top:
            title = truncate_plain(" ".join(body.split()) or "Материалы", 80)
            lines.append(f"• {html.escape(subject_label(subject, short=True))} · {html.escape(title)}")
            lines.append(f"  {reach} студ. · {opens} откр.")
    else:
        lines.extend(("", "Данные появятся после первых открытий карточек студентами."))
    return "\n".join(lines)


def weekly_report() -> str:
    first_day, today, tomorrow = report_dates()
    since = first_day.isoformat()
    until = tomorrow.isoformat()
    with connection() as conn:
        prune_schedule_requests(conn)
        rows = conn.execute("""
            SELECT student_key, occurred_at, action, schedule_status, result, duration_ms
            FROM schedule_requests
            WHERE occurred_at >= ? AND occurred_at < ?
        """, (since, until)).fetchall()
        publications = conn.execute("""
            SELECT schedule_date, published_at FROM first_schedule_publications
            WHERE published_at >= ? AND published_at < ?
            ORDER BY schedule_date
        """, (since, until)).fetchall()

    day_requests = [row for row in rows if row[2] in DAYS]
    unique_week = {row[0] for row in day_requests}
    unique_today = {row[0] for row in day_requests if row[1][:10] == today.isoformat()}
    missing = sum(row[3] == "missing" for row in day_requests)
    holidays = sum(row[3] == "holiday" for row in day_requests)
    today_requests = sum(row[2] == "today" for row in day_requests)
    tomorrow_requests = len(day_requests) - today_requests
    morning = {row[0] for row in day_requests if row[2] == "today" and "06" <= row[1][11:13] < "12"}
    evening = {row[0] for row in day_requests if row[2] == "tomorrow" and "17" <= row[1][11:13]}

    lines = [
        f"📊 <b>Статистика за 7 дней</b> · {first_day:%d.%m}–{today:%d.%m}",
        "",
        f"Уникальных студентов за неделю · <b>{len(unique_week)}</b>",
        f"Уникальных студентов сегодня · <b>{len(unique_today)}</b>",
        f"Запросы по дням · {len(day_requests)}",
        f"• Сегодня · {today_requests}, утром обращались {len(morning)} студентов (06:00–11:59)",
        f"• Завтра · {tomorrow_requests}, вечером обращались {len(evening)} студентов (17:00–23:59)",
        f"Без опубликованного расписания · {missing} из {len(day_requests)}"
        + (f" ({missing / len(day_requests):.0%})" if day_requests else ""),
        f"Запросы на выходной · {holidays}",
        "",
        "<b>Первые публикации</b>",
    ]
    for schedule_date, published_at in publications:
        day = datetime.fromisoformat(schedule_date)
        published = datetime.fromisoformat(published_at)
        lines.append(f"• На {day:%d.%m} · {published:%d.%m в %H:%M}")
    if not publications:
        lines.append("За период новых публикаций нет.")

    errors = {action: 0 for action in ACTION_LABELS}
    photo_errors = 0
    durations = []
    for _, _, action, _, result, duration_ms in rows:
        if result == "error":
            errors[action] += 1
        else:
            durations.append(duration_ms)
        if result == "photo_fallback":
            photo_errors += 1
    total_errors = sum(errors.values())
    lines.extend((
        "",
        "<b>Надёжность</b>",
        f"Ошибки открытия · {total_errors} из {len(rows)}"
        + (f" ({total_errors / len(rows):.0%})" if rows else ""),
        f"Фото заменено текстом · {photo_errors}",
    ))
    if total_errors:
        lines.extend(f"• {ACTION_LABELS[action]} · {count}" for action, count in errors.items() if count)
    if durations:
        durations.sort()
        p95_at = ceil(len(durations) * 0.95) - 1
        lines.append(f"Ответ Telegram · медиана {median(durations):g} мс, 95% до {durations[p95_at]} мс")
    if not rows:
        lines.append("Данные появятся после первых нажатий студентов.")
    return "\n".join(lines)
