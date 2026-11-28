# -*- coding: utf-8 -*-
"""
Gold Star Store — главный файл проекта.

Стек:
- Flask
- pyTelegramBotAPI
- SQLite
- Telegram Webhook
- Telegram Mini App
- Telegram Stars (XTR)

Запуск:
gunicorn bot:app --workers 1 --threads 8
"""

import logging
import secrets
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from telebot import types

import config
import database as db
import shop
import payments
import admin
import api

from utils import (
    bot,
    h,
    uzs,
    btn,
    kb,
    notify,
    safe_answer,
    show,
)


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("goldstar.bot")


# ---------------------------------------------------------
# TELEGRAM BOT
# ---------------------------------------------------------

# Webhook обрабатывается последовательно.
# Это позволяет дождаться выполнения обработчиков.
bot.threaded = False


# ---------------------------------------------------------
# FLASK
# ---------------------------------------------------------

app = Flask(
    __name__,
    static_folder="static",
    static_url_path="/static",
)

app.config["SECRET_KEY"] = (
    config.FLASK_SECRET_KEY or secrets.token_hex(32)
)

app.register_blueprint(api.api)


# ---------------------------------------------------------
# DATABASE INITIALIZATION
# ---------------------------------------------------------

db.init_db()

logger.info("База данных инициализирована")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def user_name(user):
    name = " ".join(
        value
        for value in (
            getattr(user, "first_name", ""),
            getattr(user, "last_name", ""),
        )
        if value
    )

    return name or "Пользователь"


def get_user_info(user):
    return {
        "id": user.id,
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", ""),
        "last_name": getattr(user, "last_name", ""),
        "language_code": getattr(user, "language_code", None),
        "is_premium": bool(
            getattr(user, "is_premium", False)
        ),
    }


def is_admin(user_id):
    return admin.is_admin(user_id)


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main_keyboard():
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )

    if config.WEBAPP_URL:
        markup.add(
            types.KeyboardButton(
                text="Открыть магазин",
                web_app=types.WebAppInfo(
                    url=config.WEBAPP_URL
                ),
            )
        )

    markup.add(
        types.KeyboardButton("Пополнить кошелёк"),
        types.KeyboardButton("Мой профиль"),
    )

    return markup


def notify_admins(text, reply_markup=None):
    sent = 0

    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(
                int(admin_id),
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            sent += 1

        except Exception:
            logger.exception(
                "Не удалось уведомить администратора %s",
                admin_id,
            )

    return sent


def ask_for_topup_receipt(message, topup_id):
    prompt = bot.send_message(
        message.chat.id,
        (
            f"Заявка №{topup_id} ожидает чек.\n\n"
            "Отправьте фотографию или документ "
            "с подтверждением перевода."
        ),
        reply_markup=types.ForceReply(
            selective=True,
            input_field_placeholder="Отправьте чек",
        ),
    )

    bot.register_next_step_handler(
        prompt,
        process_topup_receipt,
        topup_id,
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


# ---------------------------------------------------------
# USER COMMANDS
# ---------------------------------------------------------

@bot.message_handler(commands=["start"])
def start_handler(message):
    try:
        logger.info(
            "START_HANDLER вызван: user_id=%s, text=%r",
            message.from_user.id,
            message.text,
        )

        db.upsert_user(
            get_user_info(message.from_user)
        )

        text = (
            f"⭐ <b>{h(config.BRAND)}</b>\n\n"
            "Добро пожаловать в магазин!\n\n"
            "Здесь можно просматривать товары, оформлять "
            "заказы и управлять кошельком.\n\n"
            "Для открытия магазина нажмите кнопку ниже."
        )

        bot.send_message(
            message.chat.id,
            text,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )

        logger.info(
            "Ответ /start отправлен: user_id=%s",
            message.from_user.id,
        )

    except Exception:
        logger.exception("Ошибка команды start")

        try:
            bot.send_message(
                message.chat.id,
                "Не удалось открыть магазин. Попробуйте позже.",
            )
        except Exception:
            logger.exception(
                "Не удалось отправить сообщение об ошибке /start"
            )


@bot.message_handler(commands=["help"])
def help_handler(message):
    bot.send_message(
        message.chat.id,
        (
            "<b>Gold Star Store</b>\n\n"
            "/start — открыть магазин\n"
            "/topup — пополнить кошелёк\n"
            "/profile — профиль и баланс\n"
            "/stars — товары за Telegram Stars\n"
            "/premium — запрос Premium\n"
            "/gift — запрос подарка\n"
            "/cancel — отменить текущую операцию\n"
            "/admin — панель администратора"
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["profile"])
def profile_handler(message):
    try:
        user_id = message.from_user.id

        db.upsert_user(
            get_user_info(message.from_user)
        )

        user = db.get_user(user_id)
        wallet = db.get_wallet(user_id)

        if not user:
            bot.send_message(
                message.chat.id,
                "Не удалось загрузить профиль.",
            )
            return

        balance = wallet["balance_uzs"] if wallet else 0
        bonus = wallet["bonus_uzs"] if wallet else 0

        bot.send_message(
            message.chat.id,
            (
                "<b>Мой профиль</b>\n\n"
                f"ID: <code>{user_id}</code>\n"
                f"Имя: {h(user_name(message.from_user))}\n\n"
                f"Основной баланс: <b>{uzs(balance)}</b>\n"
                f"Бонусы: <b>{uzs(bonus)}</b>"
            ),
            reply_markup=main_keyboard(),
        )

    except Exception:
        logger.exception("Ошибка профиля")

        bot.send_message(
            message.chat.id,
            "Не удалось загрузить профиль.",
        )


@bot.message_handler(
    func=lambda m: bool(m.text)
    and m.text == "Мой профиль"
)
def profile_button_handler(message):
    profile_handler(message)


# ---------------------------------------------------------
# MANUAL TOPUPS
# ---------------------------------------------------------

@bot.message_handler(commands=["topup"])
@bot.message_handler(
    func=lambda m: bool(m.text)
    and m.text == "Пополнить кошелёк"
)
def topup_start_handler(message):
    try:
        db.upsert_user(
            get_user_info(message.from_user)
        )

        prompt = bot.send_message(
            message.chat.id,
            (
                "<b>Пополнение кошелька</b>\n\n"
                f"Минимальная сумма: {uzs(config.TOPUP_MIN_UZS)}\n"
                f"Максимальная сумма: {uzs(config.TOPUP_MAX_UZS)}\n\n"
                "Отправьте сумму пополнения числом."
            ),
            reply_markup=types.ForceReply(
                selective=True,
                input_field_placeholder="Например: 50000",
            ),
        )

        bot.register_next_step_handler(
            prompt,
            process_topup_amount,
        )

    except Exception:
        logger.exception("Ошибка начала пополнения")

        bot.send_message(
            message.chat.id,
            "Не удалось начать пополнение.",
        )


def process_topup_amount(message):
    if not message.text:
        bot.send_message(
            message.chat.id,
            "Отправьте сумму обычным текстом.",
        )
        return

    try:
        amount = int(
            message.text.strip()
            .replace(" ", "")
            .replace(",", "")
        )

        if not (
            config.TOPUP_MIN_UZS
            <= amount
            <= config.TOPUP_MAX_UZS
        ):
            raise ValueError(
                "Сумма вне допустимого диапазона"
            )

        db.upsert_user(
            get_user_info(message.from_user)
        )

        topup = payments.create_topup(
            user_id=message.from_user.id,
            amount_uzs=amount,
            idempotency_key=secrets.token_urlsafe(24),
        )

        topup_id = int(topup["id"])

        bot.send_message(
            message.chat.id,
            (
                f"<b>Заявка №{topup_id}</b>\n\n"
                f"Сумма: <b>{uzs(amount)}</b>\n\n"
                "<b>Реквизиты для оплаты:</b>\n"
                f"{h(config.TOPUP_REQUISITES)}\n\n"
                "После перевода отправьте фотографию "
                "или файл чека.\n\n"
                "Баланс будет пополнен только после "
                "проверки платежа администратором."
            ),
        )

        ask_for_topup_receipt(
            message,
            topup_id,
        )

    except ValueError as exc:
        prompt = bot.send_message(
            message.chat.id,
            (
                f"Ошибка: {h(exc)}\n\n"
                "Введите корректную сумму."
            ),
            reply_markup=types.ForceReply(
                selective=True,
                input_field_placeholder="Сумма в UZS",
            ),
        )

        bot.register_next_step_handler(
            prompt,
            process_topup_amount,
        )

    except Exception:
        logger.exception("Ошибка создания пополнения")

        bot.send_message(
            message.chat.id,
            "Не удалось создать заявку. Попробуйте позже.",
        )


def process_topup_receipt(message, topup_id):
    try:
        file_id = None

        if message.photo:
            file_id = message.photo[-1].file_id

        elif message.document:
            file_id = message.document.file_id

        if not file_id:
            ask_for_topup_receipt(
                message,
                topup_id,
            )
            return

        topup = payments.attach_topup_receipt(
            topup_id=topup_id,
            user_id=message.from_user.id,
            file_id=file_id,
            note=message.caption or "",
        )

        text = (
            "<b>Получен чек пополнения</b>\n\n"
            f"Заявка: #{topup_id}\n"
            f"Пользователь: {message.from_user.id}\n"
            f"Сумма: {uzs(topup['amount_uzs'])}\n\n"
            "Проверьте фактическое поступление денег."
        )

        markup = kb([[
            btn("Подтвердить", f"topup:approve:{topup_id}"),
            btn("Отклонить", f"topup:reject:{topup_id}"),
        ]])

        sent = 0

        for admin_id in config.ADMIN_IDS:
            try:
                if message.photo:
                    bot.send_photo(
                        int(admin_id),
                        file_id,
                        caption=text,
                        reply_markup=markup,
                    )
                else:
                    bot.send_document(
                        int(admin_id),
                        file_id,
                        caption=text,
                        reply_markup=markup,
                    )

                sent += 1

            except Exception:
                logger.exception(
                    "Не удалось отправить чек администратору"
                )

        if sent == 0:
            bot.send_message(
                message.chat.id,
                (
                    "Чек сохранён, но не удалось уведомить "
                    "администраторов. Обратитесь в поддержку."
                ),
            )
            return

        bot.send_message(
            message.chat.id,
            (
                f"Чек по заявке №{topup_id} получен.\n"
                "Заявка ожидает проверки.\n\n"
                "Деньги пока не начислены."
            ),
        )

    except ValueError as exc:
        bot.send_message(
            message.chat.id,
            f"Ошибка: {h(exc)}",
        )

    except Exception:
        logger.exception("Ошибка обработки чека")

        bot.send_message(
            message.chat.id,
            (
                "Не удалось обработать чек. "
                "Обратитесь к администратору."
            ),
        )


@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    try:
        bot.clear_step_handler_by_chat_id(
            message.chat.id
        )
    except Exception:
        logger.exception("Ошибка отмены шага")

    bot.send_message(
        message.chat.id,
        "Текущая операция отменена.",
        reply_markup=main_keyboard(),
    )


# ---------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------

@bot.message_handler(commands=["admin"])
def admin_handler(message):
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        bot.send_message(
            message.chat.id,
            "Доступ запрещён.",
        )
        return

    try:
        stats = admin.get_stats(admin_id)

        completed_orders = stats.get(
            "paid_orders",
            stats.get("delivered_orders", 0),
        )

        text = (
            "<b>Панель администратора</b>\n\n"
            f"Пользователей: {stats['users']}\n"
            f"Активных товаров: {stats['products']}\n"
            f"Ожидающих пополнений: {stats['pending_topups']}\n"
            f"Ожидающих заказов: {stats['pending_orders']}\n"
            f"Завершённых заказов: {completed_orders}\n\n"
            f"Баланс пользователей: {uzs(stats['wallet_balance_uzs'])}\n"
            f"Бонусы пользователей: {uzs(stats['wallet_bonus_uzs'])}\n"
            f"Подтверждённые пополнения: {uzs(stats['approved_topups_uzs'])}"
        )

        markup = kb([
            [btn("Ожидающие пополнения", "admin:topups")],
            [btn("Последние заказы", "admin:orders")],
            [btn("Последние пользователи", "admin:users")],
        ])

        bot.send_message(
            message.chat.id,
            text,
            reply_markup=markup,
        )

    except Exception:
        logger.exception("Ошибка панели администратора")

        bot.send_message(
            message.chat.id,
            "Не удалось загрузить панель.",
        )


@bot.callback_query_handler(
    func=lambda c: bool(c.data)
    and c.data.startswith("admin:")
)
def admin_callback(call):
    admin_id = call.from_user.id

    if not is_admin(admin_id):
        safe_answer(call.id, "Доступ запрещён", True)
        return

    try:
        parts = call.data.split(":")
        action = parts[1]

        if action == "topups":
            rows = admin.get_topups(
                admin_id,
                status="pending",
                limit=20,
            )

            if not rows:
                show(
                    call,
                    "Ожидающих пополнений нет.",
                    kb([[btn("Назад", "admin:home")]]),
                )
                safe_answer(call.id)
                return

            text = "<b>Ожидающие пополнения</b>\n\n"
            buttons = []

            for row in rows:
                topup_id = row["id"]

                text += (
                    f"#{topup_id} — {uzs(row['amount_uzs'])}\n"
                    f"Пользователь: {row['user_id']}\n"
                    f"Чек: {'есть' if row['receipt_file_id'] else 'нет'}\n\n"
                )

                buttons.append([
                    btn(
                        f"Заявка #{topup_id}",
                        f"admin:topup:{topup_id}",
                    )
                ])

            buttons.append([btn("Назад", "admin:home")])

            show(call, text, kb(buttons))

        elif action == "topup":
            topup_id = int(parts[2])
            row = payments.get_topup(topup_id)

            if not row:
                safe_answer(call.id, "Заявка не найдена", True)
                return

            text = (
                f"<b>Заявка №{topup_id}</b>\n\n"
                f"Пользователь: {row['user_id']}\n"
                f"Сумма: {uzs(row['amount_uzs'])}\n"
                f"Статус: {h(row['status'])}\n"
                f"Чек: {'есть' if row['receipt_file_id'] else 'нет'}"
            )

            buttons = []

            if row["receipt_file_id"] and row["status"] == "pending":
                buttons.append([
                    btn("Подтвердить", f"topup:approve:{topup_id}"),
                    btn("Отклонить", f"topup:reject:{topup_id}"),
                ])

            buttons.append([btn("Назад", "admin:topups")])

            show(call, text, kb(buttons))

        elif action == "orders":
            rows = admin.get_orders(
                admin_id,
                limit=15,
            )

            if not rows:
                show(
                    call,
                    "Заказов пока нет.",
                    kb([[btn("Назад", "admin:home")]]),
                )
                safe_answer(call.id)
                return

            text = "<b>Последние заказы</b>\n\n"
            buttons = []

            for row in rows:
                text += (
                    f"Заказ #{row['id']}\n"
                    f"Пользователь: {row['user_id']}\n"
                    f"Товар: {h(row['product_title'])}\n"
                    f"Сумма: {uzs(row['total_price_uzs'])}\n"
                    f"Статус: {h(row['status'])}\n\n"
                )

                buttons.append([
                    btn(
                        f"Заказ #{row['id']}",
                        f"admin:order:{row['id']}",
                    )
                ])

            buttons.append([btn("Назад", "admin:home")])
            show(call, text, kb(buttons))

        elif action == "order":
            order_id = int(parts[2])

            row = admin.get_order_details(
                admin_id,
                order_id,
            )

            if not row:
                safe_answer(call.id, "Заказ не найден", True)
                return

            text = (
                f"<b>Заказ №{order_id}</b>\n\n"
                f"Пользователь: {row['user_id']}\n"
                f"Товар: {h(row['product_title'])}\n"
                f"Количество: {row['qty']}\n"
                f"Сумма: {uzs(row['total_price_uzs'])}\n"
                f"Баланс использован: {uzs(row['balance_used_uzs'])}\n"
                f"Бонусы использованы: {uzs(row['bonus_used_uzs'])}\n"
                f"Статус: {h(row['status'])}"
            )

            buttons = []

            if row["status"] in ("paid", "processing"):
                buttons.extend([
                    [btn("В обработку", f"admin:orderstatus:{order_id}:processing")],
                    [btn("Выдан", f"admin:orderstatus:{order_id}:delivered")],
                    [btn("Отклонить и вернуть средства", f"admin:orderstatus:{order_id}:rejected")],
                ])

            buttons.append([btn("Назад", "admin:orders")])
            show(call, text, kb(buttons))

        elif action == "orderstatus":
            order_id = int(parts[2])
            status = parts[3]

            if status not in ("processing", "delivered", "rejected"):
                safe_answer(call.id, "Недопустимый статус", True)
                return

            admin.change_order_status(
                admin_id,
                order_id,
                status,
                "Изменено через Telegram",
            )

            row = admin.get_order_details(admin_id, order_id)

            if row:
                actual_status = row["status"]

                status_labels = {
                    "processing": "В обработке",
                    "delivered": "Выдан",
                    "refunded": "Средства возвращены",
                    "rejected": "Отклонён",
                }

                notify(
                    row["user_id"],
                    (
                        f"Статус заказа №{order_id} изменён: "
                        f"<b>{h(status_labels.get(actual_status, actual_status))}</b>"
                    ),
                )

                show(
                    call,
                    (
                        f"Заказ №{order_id}.\n"
                        f"Текущий статус: "
                        f"{h(status_labels.get(actual_status, actual_status))}"
                    ),
                    kb([
                        [btn("К заказам", "admin:orders")],
                        [btn("В панель", "admin:home")],
                    ]),
                )
            else:
                show(
                    call,
                    "Заказ обновлён, но не удалось загрузить его данные.",
                    kb([[btn("Назад", "admin:orders")]]),
                )

        elif action == "users":
            rows = admin.get_users(admin_id, limit=15)
            text = "<b>Последние пользователи</b>\n\n"

            for row in rows:
                text += (
                    f"ID: <code>{row['telegram_id']}</code>\n"
                    f"Имя: {h(row['first_name'] or '—')}\n"
                    f"Баланс: {uzs(row['balance_uzs'])}\n\n"
                )

            show(
                call,
                text,
                kb([[btn("Назад", "admin:home")]]),
            )

        elif action == "home":
            stats = admin.get_stats(admin_id)

            text = (
                "<b>Панель администратора</b>\n\n"
                f"Пользователей: {stats['users']}\n"
                f"Товаров: {stats['products']}\n"
                f"Ожидающих пополнений: {stats['pending_topups']}\n"
                f"Ожидающих заказов: {stats['pending_orders']}"
            )

            show(
                call,
                text,
                kb([
                    [btn("Пополнения", "admin:topups")],
                    [btn("Заказы", "admin:orders")],
                    [btn("Пользователи", "admin:users")],
                ]),
            )

        else:
            safe_answer(call.id, "Неизвестное действие", True)
            return

        safe_answer(call.id)

    except Exception:
        logger.exception("Ошибка callback администратора")
        safe_answer(call.id, "Не удалось выполнить действие", True)


# ---------------------------------------------------------
# TOPUP ADMIN CALLBACKS
# ---------------------------------------------------------

@bot.callback_query_handler(
    func=lambda c: bool(c.data)
    and c.data.startswith("topup:")
)
def topup_admin_callback(call):
    if not is_admin(call.from_user.id):
        safe_answer(call.id, "Доступ запрещён", True)
        return

    try:
        _, action, raw_id = call.data.split(":")
        topup_id = int(raw_id)

        if action == "approve":
            result = admin.approve_topup(
                call.from_user.id,
                topup_id,
                "Подтверждено администратором",
            )

            notify(
                result["user_id"],
                (
                    f"Пополнение №{topup_id} подтверждено.\n"
                    f"Начислено: <b>{uzs(result['amount_uzs'])}</b>"
                ),
            )

            show(call, f"Пополнение №{topup_id} подтверждено.")
            safe_answer(call.id, "Баланс пополнен")

        elif action == "reject":
            result = admin.reject_topup(
                call.from_user.id,
                topup_id,
                "Отклонено администратором",
            )

            notify(
                result["user_id"],
                (
                    f"Пополнение №{topup_id} отклонено.\n"
                    "Баланс не изменён."
                ),
            )

            show(call, f"Пополнение №{topup_id} отклонено.")
            safe_answer(call.id, "Заявка отклонена")

        else:
            safe_answer(call.id, "Неизвестное действие", True)

    except ValueError as exc:
        safe_answer(call.id, str(exc), True)

    except Exception:
        logger.exception("Ошибка обработки пополнения")
        safe_answer(call.id, "Не удалось обработать заявку", True)


# ---------------------------------------------------------
# PREMIUM AND GIFTS
# ---------------------------------------------------------

def request_admin(message, request_type):
    try:
        db.upsert_user(
            get_user_info(message.from_user)
        )

        text = (
            f"<b>Новая заявка: {h(request_type)}</b>\n\n"
            f"Пользователь: {h(user_name(message.from_user))}\n"
            f"Telegram ID: <code>{message.from_user.id}</code>\n"
            f"Username: @{h(message.from_user.username or 'нет')}\n\n"
            "Пользователь хочет связаться по поводу заказа."
        )

        if notify_admins(text):
            bot.send_message(
                message.chat.id,
                "Заявка отправлена администраторам. Ожидайте ответа.",
            )
        else:
            bot.send_message(
                message.chat.id,
                "Не удалось отправить заявку. Попробуйте позже.",
            )

    except Exception:
        logger.exception("Ошибка создания заявки %s", request_type)
        bot.send_message(message.chat.id, "Не удалось отправить заявку.")


@bot.message_handler(commands=["premium"])
def premium_handler(message):
    request_admin(message, "Telegram Premium")


@bot.message_handler(commands=["gift"])
def gift_handler(message):
    request_admin(message, "Telegram Gifts")


# ---------------------------------------------------------
# TELEGRAM STARS
# ---------------------------------------------------------

@bot.message_handler(commands=["stars"])
def stars_handler(message):
    try:
        db.upsert_user(
            get_user_info(message.from_user)
        )

        products = shop.get_products(
            category_slug="other",
            active_only=True,
        )

        buttons = []

        for product in products:
            if product.get("fulfillment_mode") != "xtr":
                continue

            price = safe_int(product.get("price_xtr"))

            if price <= 0:
                continue

            buttons.append([
                btn(
                    f"{product['title']} — {price} ⭐",
                    f"stars:buy:{product['id']}",
                )
            ])

        if not buttons:
            bot.send_message(
                message.chat.id,
                "Сейчас нет товаров для оплаты Telegram Stars.",
            )
            return

        bot.send_message(
            message.chat.id,
            (
                "<b>Товары за Telegram Stars</b>\n\n"
                "Выберите товар для оплаты."
            ),
            reply_markup=kb(buttons),
        )

    except Exception:
        logger.exception("Ошибка списка Stars")
        bot.send_message(message.chat.id, "Не удалось загрузить товары.")


@bot.callback_query_handler(
    func=lambda c: bool(c.data)
    and c.data.startswith("stars:buy:")
)
def stars_buy_callback(call):
    try:
        product_id = int(call.data.split(":")[2])

        if not call.message:
            safe_answer(call.id, "Сообщение недоступно", True)
            return

        db.upsert_user(
            get_user_info(call.from_user)
        )

        payments.create_xtr_invoice(
            bot=bot,
            chat_id=call.message.chat.id,
            user_id=call.from_user.id,
            product_id=product_id,
        )

        safe_answer(call.id, "Счёт создан")

    except ValueError as exc:
        safe_answer(call.id, str(exc), True)

    except Exception:
        logger.exception("Ошибка создания Stars invoice")
        safe_answer(call.id, "Не удалось создать счёт", True)


@bot.pre_checkout_query_handler(
    func=lambda query: True
)
def pre_checkout_handler(query):
    try:
        valid, message = payments.validate_xtr_pre_checkout(
            user_id=query.from_user.id,
            payload=query.invoice_payload,
            currency=query.currency,
            total_amount=query.total_amount,
        )

        bot.answer_pre_checkout_query(
            query.id,
            ok=valid,
            error_message=None if valid else message,
        )

    except Exception:
        logger.exception("Ошибка pre-checkout")

        try:
            bot.answer_pre_checkout_query(
                query.id,
                ok=False,
                error_message="Не удалось проверить платёж",
            )
        except Exception:
            logger.exception("Не удалось ответить на pre-checkout")


@bot.message_handler(
    content_types=["successful_payment"]
)
def successful_payment_handler(message):
    payment = message.successful_payment
    charge_id = payment.telegram_payment_charge_id

    try:
        result = payments.process_xtr_success(
            user_id=message.from_user.id,
            payload=payment.invoice_payload,
            currency=payment.currency,
            total_amount=payment.total_amount,
            telegram_charge_id=charge_id,
        )

        bot.send_message(
            message.chat.id,
            (
                "<b>Оплата успешно получена!</b>\n\n"
                f"Товар: {h(result['product_title'])}\n"
                f"Сумма: {result['amount_xtr']} ⭐\n"
                f"Заказ: #{result['id']}"
            ),
        )

        notify_admins(
            (
                "<b>Новая оплата Telegram Stars</b>\n\n"
                f"Пользователь: {message.from_user.id}\n"
                f"Товар: {h(result['product_title'])}\n"
                f"Сумма: {result['amount_xtr']} ⭐\n"
                f"Заказ: #{result['id']}"
            )
        )

    except Exception:
        logger.exception("Ошибка обработки успешной оплаты Stars")

        try:
            bot.send_message(
                message.chat.id,
                (
                    "Telegram сообщил об успешной оплате, "
                    "но магазин не смог завершить обработку заказа.\n\n"
                    "Не оплачивайте повторно. Обратитесь к администратору "
                    "и укажите ID платежа."
                ),
            )
        except Exception:
            logger.exception("Не удалось уведомить пользователя о проблеме Stars")

        notify_admins(
            (
                "<b>Требуется сверка платежа Stars</b>\n\n"
                f"Пользователь: {message.from_user.id}\n"
                f"Telegram charge ID:\n<code>{h(charge_id)}</code>\n"
                f"Payload:\n<code>{h(payment.invoice_payload)}</code>\n"
                f"Сумма: {payment.total_amount} {h(payment.currency)}\n\n"
                "Проверьте платёж и состояние заказа в базе данных. "
                "Не начисляйте средства повторно без проверки."
            )
        )


# ---------------------------------------------------------
# TELEGRAM WEBHOOK
# ---------------------------------------------------------

@app.post("/telegram/webhook")
def telegram_webhook():
    expected_secret = config.WEBHOOK_SECRET

    if not expected_secret:
        logger.error("WEBHOOK_SECRET не задан")
        return jsonify({
            "ok": False,
            "error": "Webhook secret is not configured",
        }), 503

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )

    if not secrets.compare_digest(
        received_secret,
        expected_secret,
    ):
        logger.warning("Webhook: неверный secret token")
        return jsonify({"ok": False}), 403

    try:
        update_data = request.get_json(silent=True)

        if not isinstance(update_data, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid JSON",
            }), 400

        update = types.Update.de_json(update_data)

        if update is None or update.update_id is None:
            return jsonify({
                "ok": False,
                "error": "Invalid Telegram update",
            }), 400

        update_id = int(update.update_id)

        update_type = next(
            (
                name
                for name in (
                    "message",
                    "edited_message",
                    "callback_query",
                    "pre_checkout_query",
                    "shipping_query",
                )
                if getattr(update, name, None) is not None
            ),
            "unknown",
        )

        logger.info(
            "Получено обновление: id=%s, type=%s",
            update_id,
            update_type,
        )

        if update_type == "message":
            incoming_message = update.message

            logger.info(
                "Telegram message: user_id=%s, text=%r",
                (
                    incoming_message.from_user.id
                    if incoming_message.from_user
                    else None
                ),
                incoming_message.text,
            )

        with db.transaction():
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO inbox_updates(
                    update_id,
                    status,
                    received_at
                )
                VALUES (?, 'processing', ?)
                """,
                (update_id, utc_now()),
            )

            inserted = cursor.rowcount == 1

        if not inserted:
            logger.info(
                "Повторное обновление пропущено: %s",
                update_id,
            )

            return jsonify({
                "ok": True,
                "duplicate": True,
            }), 200

        try:
            bot.process_new_updates([update])

            db.execute(
                """
                UPDATE inbox_updates
                SET status = 'processed',
                    processed_at = ?,
                    error = NULL
                WHERE update_id = ?
                """,
                (utc_now(), update_id),
            )

            logger.info(
                "Обновление успешно обработано: id=%s",
                update_id,
            )

        except Exception as exc:
            logger.exception(
                "Ошибка обработки update_id=%s",
                update_id,
            )

            db.execute(
                """
                UPDATE inbox_updates
                SET status = 'failed',
                    error = ?
                WHERE update_id = ?
                """,
                (str(exc)[:1000], update_id),
            )

            return jsonify({
                "ok": False,
                "error": "Update processing failed",
            }), 500

        return jsonify({"ok": True}), 200

    except Exception:
        logger.exception("Ошибка обработки webhook")
        return jsonify({"ok": False}), 500


# ---------------------------------------------------------
# MINI APP AND HEALTH
# ---------------------------------------------------------

@app.get("/")
def index():
    try:
        return app.send_static_file("index.html")

    except Exception:
        logger.exception("Не удалось открыть static/index.html")

        return jsonify({
            "ok": False,
            "error": "Mini App frontend not found: static/index.html",
        }), 404


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "Gold Star Store",
    }), 200


@app.errorhandler(404)
def not_found(_error):
    return jsonify({
        "ok": False,
        "error": "Not found",
    }), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error("Ошибка Flask: %s", error)

    return jsonify({
        "ok": False,
        "error": "Internal server error",
    }), 500


# ---------------------------------------------------------
# WEBHOOK CONFIGURATION
# ---------------------------------------------------------

def configure_webhook():
    if not config.AUTO_SET_WEBHOOK:
        logger.info("Автоматическая установка webhook отключена")
        return

    if not config.WEBHOOK_URL:
        logger.error("WEBHOOK_URL не задан")
        return

    if not config.WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET не задан")
        return

    base_url = (
        config.WEBHOOK_URL
        .strip()
        .rstrip("/")
    )

    suffix = "/telegram/webhook"

    while base_url.endswith(suffix):
        base_url = base_url[:-len(suffix)].rstrip("/")

    webhook_url = base_url + suffix

    if not webhook_url.startswith("https://"):
        logger.error("WEBHOOK_URL должен начинаться с https://")
        return

    try:
        result = bot.set_webhook(
            url=webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=config.ALLOWED_UPDATES,
        )

        if result:
            logger.info("Webhook установлен: %s", webhook_url)
        else:
            logger.error("Telegram не подтвердил установку webhook")

    except Exception:
        logger.exception("Не удалось установить webhook")


if config.AUTO_SET_WEBHOOK:
    configure_webhook()


# ---------------------------------------------------------
# LOCAL START
# ---------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=False,
        threaded=True,
    )