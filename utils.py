import html
import logging
import os
from datetime import datetime, timezone
from typing import Any

import telebot
from telebot import types

import config

logger = logging.getLogger(__name__)

TOKEN = str(getattr(config, "BOT_TOKEN", "") or "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в настройках")

bot = telebot.TeleBot(
    TOKEN,
    parse_mode="HTML",
    threaded=True,
    num_threads=8,
)


def h(value: Any) -> str:
    """Безопасное HTML-экранирование текста."""
    return html.escape(str(value or ""), quote=True)


def uzs(value: Any) -> str:
    """Форматирование суммы в узбекских сумах."""
    try:
        amount = int(value or 0)
    except (TypeError, ValueError):
        amount = 0

    return f"{amount:,}".replace(",", " ") + " сум"


def fmt_dt(value: Any) -> str:
    """Форматирование даты и времени."""
    if not value:
        return "не указано"

    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        raw = str(value).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return h(raw)

    return dt.strftime("%d.%m.%Y %H:%M")


def btn(text: str, callback_data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text=str(text),
        callback_data=str(callback_data),
    )


def kb(rows: list) -> types.InlineKeyboardMarkup:
    """Создание inline-клавиатуры."""
    markup = types.InlineKeyboardMarkup()

    for row in rows:
        if not isinstance(row, (list, tuple)):
            row = [row]

        markup.row(*row)

    return markup


def show(call, text: str, reply_markup=None) -> None:
    """Обновление сообщения по callback."""
    try:
        if getattr(call.message, "content_type", "") == "photo":
            bot.edit_message_caption(
                caption=text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=reply_markup,
            )
        else:
            bot.edit_message_text(
                text=text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
    except Exception:
        logger.exception("Не удалось обновить сообщение")


def safe_answer(
    callback_id: str,
    text: str = "",
    show_alert: bool = False,
) -> None:
    try:
        bot.answer_callback_query(
            callback_query_id=callback_id,
            text=str(text or ""),
            show_alert=show_alert,
        )
    except Exception:
        logger.exception("Не удалось ответить на callback")


def notify(user_id: int, text: str) -> bool:
    """Отправка уведомления пользователю."""
    try:
        bot.send_message(
            int(user_id),
            text,
            disable_web_page_preview=True,
        )
        return True
    except Exception:
        logger.exception("Не удалось отправить уведомление")
        return False


def normalize_recipient(value: Any) -> str:
    """Нормализация получателя заказа."""
    if value is None:
        return ""

    recipient = " ".join(str(value).strip().split())

    if len(recipient) > 150:
        raise ValueError("Слишком длинный идентификатор получателя")

    return recipient


def safe_send_message(
    chat_id: int,
    text: str,
    reply_markup=None,
) -> bool:
    try:
        bot.send_message(
            int(chat_id),
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception:
        logger.exception("Не удалось отправить сообщение")
        return False