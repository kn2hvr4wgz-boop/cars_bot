import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Минимальный загрузчик .env, чтобы не тянуть лишнюю зависимость."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    tz: ZoneInfo
    db_path: str


def load_config() -> Config:
    _load_dotenv()
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    admins = frozenset(
        int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=token,
        admin_ids=admins,
        tz=ZoneInfo(os.environ.get("TZ", "Europe/Moscow")),
        db_path=os.environ.get("DB_PATH", "data/cars_bot.db"),
    )
