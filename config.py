import os
from dotenv import load_dotenv

load_dotenv()


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            val = int(part)
            if val > 0:
                ids.add(val)
    return ids


def _parse_allowed_updates(raw: str) -> list[str]:
    default_updates = ["message", "callback_query", "pre_checkout_query"]
    if not raw:
        return default_updates
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items or default_updates


def _safe_int(env_name: str, default: int, min_val: int) -> int:
    raw = (os.getenv(env_name, "") or "").strip()
    if not raw:
        return max(min_val, default)
    try:
        val = int(raw)
    except ValueError:
        return max(min_val, default)
    return max(min_val, val)


BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or "").strip()
WEBHOOK_URL = (os.getenv("WEBHOOK_URL", "") or "").strip().rstrip("/")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET", "") or "").strip()
FLASK_SECRET_KEY = (
    (os.getenv("FLASK_SECRET_KEY", "") or "").strip()
    or (BOT_TOKEN[:24] if BOT_TOKEN else "gold-star-dev-secret-change-in-prod")
)
WEBAPP_URL = (os.getenv("WEBAPP_URL", "") or "").strip()
if not WEBAPP_URL and WEBHOOK_URL:
    WEBAPP_URL = WEBHOOK_URL

DATABASE_PATH = (os.getenv("DATABASE_PATH", "/var/data/gold_star.db") or "").strip() or "gold_star.db"
BRAND = (os.getenv("BRAND_NAME", "Gold Star") or "").strip() or "Gold Star"
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))

TOPUP_REQUISITES = (
    os.getenv("TOPUP_REQUISITES", "")
    or "Реквизиты не заданы. Укажите TOPUP_REQUISITES в переменных окружения."
).strip()
TOPUP_MIN_UZS = _safe_int("TOPUP_MIN_UZS", 5000, 1)
TOPUP_MAX_UZS = max(TOPUP_MIN_UZS, _safe_int("TOPUP_MAX_UZS", 10000000, TOPUP_MIN_UZS))

INIT_DATA_MAX_AGE_SEC = _safe_int("INIT_DATA_MAX_AGE_SEC", 86400, 60)
WORKER_THREADS = _safe_int("WORKER_THREADS", 8, 2)
AUTO_SET_WEBHOOK = (os.getenv("AUTO_SET_WEBHOOK", "true") or "").strip().lower() in ("1", "true", "yes", "on")
ALLOWED_UPDATES = _parse_allowed_updates(os.getenv("ALLOWED_UPDATES", ""))


def problems() -> list[str]:
    errs: list[str] = []
    if not BOT_TOKEN:
        errs.append("Не задан BOT_TOKEN")
    if AUTO_SET_WEBHOOK and not WEBHOOK_URL:
        errs.append("Не задан WEBHOOK_URL при включённом AUTO_SET_WEBHOOK")
    if AUTO_SET_WEBHOOK and not WEBHOOK_SECRET:
        errs.append("Не задан WEBHOOK_SECRET")
    if not ADMIN_IDS:
        errs.append("Не задан ADMIN_IDS (нет ни одного администратора)")
    if not os.getenv("TOPUP_REQUISITES", "").strip():
        errs.append("Не задан TOPUP_REQUISITES")
    return errs