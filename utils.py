import datetime
import html
import logging
import re
import sys
from typing import Any

import telebot
from telebot import types

import config

STATUS_LABELS = {
    "pending": "⏳ Ожидает",
    "paid": "✅ Оплачен",
    "processing": "⚙️ В обработке",
    "delivered": "📦 Выполнен",
    "cancelled": "❌ Отменён",
    "refunded": "↩️ Возвращён",
    "approved": "✅ Подтверждено",
    "rejected": "❌ Отклонено",
}

bot = telebot.TeleBot(config.BOT_TOKEN or "0:invalid_token", parse_mode="HTML")


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)


def h(val: Any) -> str:
    return html.escape(str(val or ""), quote=True)


def uzs(amount: int) -> str:
    return f"{int(amount):,} UZS".replace(",", " ")


def stars(amount: int) -> str:
    return f"{int(amount):,} ⭐".replace(",", " ")


def fmt_dt(ts: int | None) -> str:
    if not ts:
        return "—"
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone(datetime.timedelta(hours=5)))
    return dt.strftime("%d.%m.%Y %H:%M")


def normalize_recipient(raw: str) -> str | None:
    cleaned = (raw or "").strip()
    if not cleaned.startswith("@"):
        cleaned = "@" + cleaned
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", cleaned):
        return cleaned
    return None


def btn(
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    web_app: str | None = None,
) -> types.InlineKeyboardButton:
    if web_app:
        return types.InlineKeyboardButton(text=text, web_app=types.WebAppInfo(url=web_app))
    if url:
        return types.InlineKeyboardButton(text=text, url=url)
    return types.InlineKeyboardButton(text=text, callback_data=callback_data or "u:noop")


def kb(*rows: list[types.InlineKeyboardButton]) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        if row:
            markup.row(*row)
    return markup


def safe_answer(call_id: str, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(call_id, text=text, show_alert=alert)
    except Exception:  # noqa: BLE001
        pass


def show(call: types.CallbackQuery, text: str, markup: types.InlineKeyboardMarkup | None = None) -> None:
    msg = call.message
    if not msg:
        return
    try:
        bot.edit_message_text(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        try:
            bot.send_message(msg.chat.id, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass


def notify(user_id: int, text: str, markup: types.InlineKeyboardMarkup | None = None) -> bool:
    try:
        bot.send_message(int(user_id), text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except Exception:  # noqa: BLE001
        logging.getLogger("goldstar.notify").warning("Не удалось отправить сообщение пользователю %s", user_id)
        return False


def notify_admins(text: str, markup: types.InlineKeyboardMarkup | None = None) -> None:
    for admin_id in config.ADMIN_IDS:
        notify(admin_id, text, markup=markup)