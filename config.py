import os
import re
from pathlib import Path


class ConfigError(ValueError):
    """Сообщение подходит для журнала и не содержит введённых значений."""


def parse_admin_ids(raw: str) -> frozenset[int]:
    parts = [part.strip() for part in raw.split(",")]
    if any(not re.fullmatch(r"[1-9][0-9]{0,15}", part) for part in parts):
        raise ConfigError("Укажи в ADMIN_IDS положительные Telegram ID через запятую.")
    ids = frozenset(map(int, parts))
    if len(ids) != len(parts) or any(value >= 2**52 for value in ids):
        raise ConfigError("Проверь диапазон ID в ADMIN_IDS и убери повторы.")
    return ids


def load_config() -> tuple[str, frozenset[int], Path]:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ConfigError("Укажи в BOT_TOKEN новый токен от BotFather.")
    ids = parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
    path = Path(os.environ.get("DB_PATH", "/data/pkd1_bot.db")).expanduser().resolve()
    if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if mount != "/data" or not path.is_relative_to(Path("/data")) or path == Path("/data"):
            raise ConfigError("Подключи том Railway по пути /data и укажи DB_PATH внутри него.")
    return token, ids, path
