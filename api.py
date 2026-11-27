# -*- coding: utf-8 -*-
"""
Gold Star Store — API для Telegram Mini App.

Flask Blueprint:
- проверка Telegram WebApp initData;
- профиль и кошелёк;
- каталог;
- история заказов;
- оформление заказов;
- заявки на пополнение.
"""

import hashlib
import hmac
import json
import logging
import re
import time

from functools import wraps
from urllib.parse import parse_qsl

from flask import Blueprint, jsonify, request, g

import config
import database as db
import shop
import payments


logger = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")


# --------------------------------------------------
# ОТВЕТЫ API
# --------------------------------------------------

def success(data=None, message="OK", status=200):
    return jsonify({
        "ok": True,
        "message": message,
        "data": data,
    }), status


def error(message, status=400):
    return jsonify({
        "ok": False,
        "error": str(message),
    }), status


# --------------------------------------------------
# TELEGRAM INIT DATA
# --------------------------------------------------

def validate_init_data(init_data: str) -> dict:
    """
    Проверяет подпись Telegram WebApp initData.
    Возвращает проверенные данные.
    """

    if not init_data or not isinstance(init_data, str):
        raise ValueError("Отсутствуют данные Telegram")

    if len(init_data) > 16384:
        raise ValueError("Слишком большой initData")

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        raise ValueError("Некорректный initData")

    data = {}

    for key, value in pairs:
        if key in data:
            raise ValueError("Повторяющиеся параметры initData")
        data[key] = value

    received_hash = data.pop("hash", None)

    if not received_hash or not re.fullmatch(
        r"[0-9a-fA-F]{64}",
        received_hash,
    ):
        raise ValueError("Отсутствует подпись Telegram")

    bot_token = getattr(config, "BOT_TOKEN", "")

    if not bot_token:
        raise ValueError("BOT_TOKEN не настроен")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(data.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash.lower(),
    ):
        raise ValueError("Недействительная подпись Telegram")

    try:
        auth_date = int(data.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise ValueError("Некорректная дата авторизации")

    now = int(time.time())

    max_age = int(
        getattr(config, "INIT_DATA_MAX_AGE_SEC", 86400)
    )

    if auth_date <= 0 or auth_date > now + 60:
        raise ValueError("Некорректная дата авторизации")

    if now - auth_date > max_age:
        raise ValueError("Срок действия авторизации истёк")

    try:
        user = json.loads(data.get("user", "{}"))
    except (TypeError, ValueError):
        raise ValueError("Некорректные данные пользователя")

    if not isinstance(user, dict):
        raise ValueError("Некорректные данные пользователя")

    try:
        user_id = int(user.get("id", 0))
    except (TypeError, ValueError):
        user_id = 0

    if user_id <= 0:
        raise ValueError("Не удалось определить пользователя")

    user["id"] = user_id

    return {
        "user": user,
        "auth_date": auth_date,
        "query_id": data.get("query_id"),
    }


# --------------------------------------------------
# АВТОРИЗАЦИЯ API
# --------------------------------------------------

def require_telegram_user(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        init_data = request.headers.get(
            "X-Telegram-Init-Data",
            "",
        )

        if not init_data:
            init_data = request.headers.get(
                "Authorization",
                "",
            )

            if init_data.lower().startswith("tma "):
                init_data = init_data[4:]

        try:
            verified = validate_init_data(init_data)

            telegram_user = verified["user"]

            db.upsert_user(telegram_user)

            g.telegram_user = telegram_user
            g.telegram_user_id = telegram_user["id"]

            return function(*args, **kwargs)

        except ValueError as exc:
            return error(str(exc), 401)

        except Exception:
            logger.exception("Ошибка авторизации Mini App")
            return error("Ошибка авторизации", 500)

    return wrapper


def _user_id():
    return int(g.telegram_user_id)


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _positive_int(value, name, maximum=10**12):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное значение: {name}")

    if number <= 0 or number > maximum:
        raise ValueError(f"Недопустимое значение: {name}")

    return number


# --------------------------------------------------
# ПРОВЕРКА РАБОТЫ API
# --------------------------------------------------

@api.get("/health")
def health():
    return success({
        "service": "Gold Star Store API",
        "status": "online",
    })


# --------------------------------------------------
# ПРОФИЛЬ И КОШЕЛЁК
# --------------------------------------------------

@api.get("/me")
@require_telegram_user
def me():
    user_id = _user_id()

    user = db.get_user(user_id)
    wallet = db.get_wallet(user_id)

    if not user:
        return error("Пользователь не найден", 404)

    return success({
        "user": dict(user),
        "wallet": dict(wallet) if wallet else {
            "balance_uzs": 0,
            "bonus_uzs": 0,
        },
    })


# --------------------------------------------------
# КАТЕГОРИИ
# --------------------------------------------------

@api.get("/categories")
@require_telegram_user
def categories():
    rows = shop.get_categories(active_only=True)

    return success([
        dict(row) for row in rows
    ])


# --------------------------------------------------
# КАТАЛОГ ТОВАРОВ
# --------------------------------------------------

@api.get("/products")
@require_telegram_user
def products():
    category = request.args.get("category")

    rows = shop.get_products(
        category_slug=category or None,
        active_only=True,
    )

    return success([
        dict(row) for row in rows
    ])


@api.get("/products/<int:product_id>")
@require_telegram_user
def product_details(product_id):
    product = shop.get_product(product_id)

    if not product or not product["active"]:
        return error("Товар не найден", 404)

    return success(dict(product))


# --------------------------------------------------
# ИСТОРИЯ ЗАКАЗОВ
# --------------------------------------------------

@api.get("/history")
@require_telegram_user
def history():
    rows = shop.get_user_orders(_user_id())

    return success([
        dict(row) for row in rows
    ])


@api.get("/orders")
@require_telegram_user
def orders():
    rows = shop.get_user_orders(_user_id())

    return success([
        dict(row) for row in rows
    ])


@api.get("/orders/<int:order_id>")
@require_telegram_user
def order_details(order_id):
    order = shop.get_order(order_id)

    if not order:
        return error("Заказ не найден", 404)

    if int(order["user_id"]) != _user_id():
        return error("Заказ не найден", 404)

    return success(dict(order))


# --------------------------------------------------
# ОФОРМЛЕНИЕ ЗАКАЗА
# --------------------------------------------------

@api.post("/orders")
@require_telegram_user
def create_order():
    data = _body()

    try:
        product_id = _positive_int(
            data.get("product_id"),
            "product_id",
        )

        quantity = _positive_int(
            data.get("quantity", data.get("qty", 1)),
            "quantity",
            maximum=100,
        )

        recipient = str(
            data.get("recipient", "")
        ).strip()

        idempotency_key = str(
            data.get("idempotency_key", "")
        ).strip()

        if not idempotency_key:
            return error("Отсутствует ключ заказа")

        if len(idempotency_key) > 128:
            return error("Некорректный ключ заказа")

        result = shop.create_wallet_order(
            user_id=_user_id(),
            product_id=product_id,
            qty=quantity,
            recipient=recipient,
            idempotency_key=idempotency_key,
        )

        return success(
            result,
            "Заказ оформлен",
            201,
        )

    except ValueError as exc:
        return error(str(exc))

    except PermissionError as exc:
        return error(str(exc), 403)

    except Exception:
        logger.exception("Ошибка создания заказа")
        return error("Не удалось оформить заказ", 500)


# --------------------------------------------------
# ЗАПРОСЫ НА ПОПОЛНЕНИЕ
# --------------------------------------------------

@api.get("/topups")
@require_telegram_user
def topups():
    rows = payments.get_user_topups(_user_id())

    return success([
        dict(row) for row in rows
    ])


@api.post("/topups")
@require_telegram_user
def create_topup():
    data = _body()

    try:
        amount = _positive_int(
            data.get("amount_uzs", data.get("amount")),
            "amount",
        )

        minimum = int(
            getattr(config, "TOPUP_MIN_UZS", 1000)
        )

        maximum = int(
            getattr(config, "TOPUP_MAX_UZS", 100000000)
        )

        if amount < minimum or amount > maximum:
            return error(
                f"Сумма должна быть от {minimum} до {maximum} сум"
            )

        idempotency_key = str(
            data.get("idempotency_key", "")
        ).strip()

        if not idempotency_key:
            return error("Отсутствует ключ заявки")

        if len(idempotency_key) > 128:
            return error("Некорректный ключ заявки")

        result = payments.create_topup(
            user_id=_user_id(),
            amount_uzs=amount,
            idempotency_key=idempotency_key,
        )

        return success(
            result,
            "Заявка на пополнение создана",
            201,
        )

    except ValueError as exc:
        return error(str(exc))

    except Exception:
        logger.exception("Ошибка создания пополнения")
        return error("Не удалось создать заявку", 500)


# --------------------------------------------------
# ИНФОРМАЦИЯ О ПОПОЛНЕНИИ
# --------------------------------------------------

@api.get("/topups/<int:topup_id>")
@require_telegram_user
def topup_details(topup_id):
    topup = payments.get_topup(topup_id)

    if not topup:
        return error("Заявка не найдена", 404)

    if int(topup["user_id"]) != _user_id():
        return error("Заявка не найдена", 404)

    return success(dict(topup))


# --------------------------------------------------
# ОБРАБОТКА ОШИБОК
# --------------------------------------------------

@api.errorhandler(404)
def not_found(_exc):
    return error("Маршрут не найден", 404)


@api.errorhandler(405)
def method_not_allowed(_exc):
    return error("Метод не поддерживается", 405)


@api.errorhandler(Exception)
def internal_error(exc):
    logger.exception("Необработанная ошибка API: %s", exc)
    return error("Внутренняя ошибка сервера", 500)