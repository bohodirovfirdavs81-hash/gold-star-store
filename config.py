import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()

    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()

        if part.isdigit():
            value = int(part)
            if value > 0:
                ids.add(value)

    return ids


def _parse_allowed_updates(raw: str) -> list[str]:
    default_updates = [
        "message",
        "callback_query",
        "pre_checkout_query",
    ]

    if not raw:
        return default_updates

    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or default_updates


def _safe_int(env_name: str, default: int, min_val: int) -> int:
    raw = (os.getenv(env_name, "") or "").strip()

    if not raw:
        return max(min_val, default)

    try:
        value = int(raw)
    except ValueError:
        return max(min_val, default)

    return max(min_val, value)


BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or "").strip()

WEBHOOK_URL = (
    os.getenv("WEBHOOK_URL", "") or ""
).strip().rstrip("/")

WEBHOOK_SECRET = (
    os.getenv("WEBHOOK_SECRET", "") or ""
).strip()

FLASK_SECRET_KEY = (
    os.getenv("FLASK_SECRET_KEY", "") or ""
).strip()

WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "") or ""
).strip().rstrip("/")

DATABASE_PATH = (
    os.getenv("DATABASE_PATH", "/var/data/gold_star.db") or ""
).strip() or "gold_star.db"

BRAND = (
    os.getenv("BRAND_NAME", "Gold Star") or ""
).strip() or "Gold Star"

ADMIN_IDS = _parse_admin_ids(
    os.getenv("ADMIN_IDS", "")
)

TOPUP_REQUISITES = (
    os.getenv("TOPUP_REQUISITES", "") or ""
).strip()

TOPUP_MIN_UZS = _safe_int(
    "TOPUP_MIN_UZS", 5000, 1
)

TOPUP_MAX_UZS = max(
    TOPUP_MIN_UZS,
    _safe_int("TOPUP_MAX_UZS", 10000000, TOPUP_MIN_UZS),
)

INIT_DATA_MAX_AGE_SEC = _safe_int(
    "INIT_DATA_MAX_AGE_SEC", 86400, 60
)

WORKER_THREADS = _safe_int(
    "WORKER_THREADS", 8, 2
)

AUTO_SET_WEBHOOK = (
    os.getenv("AUTO_SET_WEBHOOK", "true") or ""
).strip().lower() in ("1", "true", "yes", "on")

ALLOWED_UPDATES = _parse_allowed_updates(
    os.getenv("ALLOWED_UPDATES", "")
)


def _valid_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.netloc)
            and not parsed.username
            and not parsed.password
        )
    except Exception:
        return False


def problems() -> list[str]:
    errors: list[str] = []

    if not BOT_TOKEN:
        errors.append("Не задан BOT_TOKEN")

    if AUTO_SET_WEBHOOK:
        if not _valid_https_url(WEBHOOK_URL):
            errors.append("WEBHOOK_URL должен быть корректным HTTPS URL")

        if len(WEBHOOK_SECRET) < 16:
            errors.append("WEBHOOK_SECRET должен содержать минимум 16 символов")

    if not _valid_https_url(WEBAPP_URL):
        errors.append("WEBAPP_URL должен быть корректным HTTPS URL")

    if len(FLASK_SECRET_KEY) < 32:
        errors.append("Задайте отдельный FLASK_SECRET_KEY длиной минимум 32 символа")

    if not ADMIN_IDS:
        errors.append("Не задан ADMIN_IDS")

    if not TOPUP_REQUISITES:
        errors.append("Не задан TOPUP_REQUISITES")

    if TOPUP_MIN_UZS <= 0:
        errors.append("TOPUP_MIN_UZS должен быть больше нуля")

    if TOPUP_MAX_UZS < TOPUP_MIN_UZS:
        errors.append("TOPUP_MAX_UZS меньше TOPUP_MIN_UZS")

    return errors