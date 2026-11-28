# -*- coding: utf-8 -*-
"""
Gold Star Store — API для Telegram Mini App.

Flask Blueprint:
- проверка Telegram WebApp initData;
- профиль и кошелёк;
- каталог товаров;
- история заказов и транзакций;
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
    except ValueError as exc:
        raise ValueError("Некорректный initData") from exc

    data = {}

    for key, value in pairs:
        if key in data:
            raise ValueError(
                "Повторяющиеся параметры initData"
            )

        data[key] = value

    received_hash = data.pop("hash", None)

    if not received_hash or not re.fullmatch(
        r"[0-9a-fA-F]{64}",
        received_hash,
    ):
        raise ValueError(
            "Отсутствует подпись Telegram"
        )

    bot_token = str(
        getattr(config, "BOT_TOKEN", "") or ""
    ).strip()

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
        raise ValueError(
            "Недействительная подпись Telegram"
        )

    try:
        auth_date = int(data.get("auth_date", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Некорректная дата авторизации"
        ) from exc

    now = int(time.time())

    try:
        max_age = int(
            getattr(
                config,
                "INIT_DATA_MAX_AGE_SEC",
                86400,
            )
        )
    except (TypeError, ValueError):
        max_age = 86400

    if auth_date <= 0 or auth_date > now + 60:
        raise ValueError(
            "Некорректная дата авторизации"
        )

    if max_age <= 0 or now - auth_date > max_age:
        raise ValueError(
            "Срок действия авторизации истёк"
        )

    try:
        user = json.loads(data.get("user", "{}"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Некорректные данные пользователя"
        ) from exc

    if not isinstance(user, dict):
        raise ValueError(
            "Некорректные данные пользователя"
        )

    try:
        user_id = int(user.get("id", 0))
    except (TypeError, ValueError):
        user_id = 0

    if user_id <= 0:
        raise ValueError(
            "Не удалось определить пользователя"
        )

    user["id"] = user_id

    return {
        "user": user,
        "auth_date": auth_date,
        "query_id": data.get("query_id"),
    }


# --------------------------------------------------
# АВТОРИЗАЦИЯ
# --------------------------------------------------

def require_telegram_user(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        init_data = request.headers.get(
            "X-Telegram-Init-Data",
            "",
        )

        if not init_data:
            authorization = request.headers.get(
                "Authorization",
                "",
            )

            if authorization.lower().startswith("tma "):
                init_data = authorization[4:]

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
            logger.exception(
                "Ошибка авторизации Mini App"
            )

            return error(
                "Ошибка авторизации",
                500,
            )

    return wrapper


def _user_id() -> int:
    return int(g.telegram_user_id)


def _body() -> dict:
    data = request.get_json(silent=True)

    return data if isinstance(data, dict) else {}


def _positive_int(
    value,
    name: str,
    maximum: int = 10**12,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Некорректное значение: {name}"
        ) from exc

    if number <= 0 or number > maximum:
        raise ValueError(
            f"Недопустимое значение: {name}"
        )

    return number


def _dict_or_empty(value):
    return dict(value) if value else {}


# --------------------------------------------------
# HEALTH
# --------------------------------------------------

@api.get("/health")
def health():
    return success({
        "service": "Gold Star Store API",
        "status": "online",
    })


# --------------------------------------------------
# ПРОФИЛЬ
# --------------------------------------------------

@api.get("/me")
@require_telegram_user
def me():
    user_id = _user_id()

    user = db.get_user(user_id)
    wallet = db.get_wallet(user_id)

    if not user:
        return error(
            "Пользователь не найден",
            404,
        )

    wallet_data = _dict_or_empty(wallet)

    if not wallet_data:
        wallet_data = {
            "balance_uzs": 0,
            "bonus_uzs": 0,
        }

    order_stats = db.q_one(
        """
        SELECT
            COUNT(*) AS total_orders,
            SUM(
                CASE
                    WHEN status = 'delivered' THEN 1
                    ELSE 0
                END
            ) AS completed_orders
        FROM wallet_orders
        WHERE user_id = ?
        """,
        (user_id,),
    )

    stats = {
        "total_orders": 0,
        "completed_orders": 0,
    }

    if order_stats:
        stats["total_orders"] = int(
            order_stats["total_orders"] or 0
        )

        stats["completed_orders"] = int(
            order_stats["completed_orders"] or 0
        )

    return success({
        "brand": getattr(
            config,
            "BRAND",
            "Gold Star Store",
        ),
        "user": dict(user),
        "wallet": wallet_data,
        "stats": stats,
        "topup_config": {
            "minimum": int(
                getattr(
                    config,
                    "TOPUP_MIN_UZS",
                    1000,
                )
            ),
            "maximum": int(
                getattr(
                    config,
                    "TOPUP_MAX_UZS",
                    100000000,
                )
            ),
        },
    })


# --------------------------------------------------
# КАТЕГОРИИ
# --------------------------------------------------

@api.get("/categories")
@require_telegram_user
def categories():
    rows = shop.get_categories()

    return success([
        dict(row) for row in rows
    ])


# --------------------------------------------------
# ТОВАРЫ
# --------------------------------------------------

@api.get("/products")
@require_telegram_user
def products():
    category = request.args.get("category")

    try:
        limit = _positive_int(
            request.args.get("limit", 100),
            "limit",
            maximum=100,
        )

        offset = int(
            request.args.get("offset", 0)
        )

        if offset < 0:
            raise ValueError(
                "Некорректное значение: offset"
            )

        rows = shop.get_products(
            category_slug=category or None,
            active_only=True,
            limit=limit,
            offset=offset,
        )

        return success([
            dict(row) for row in rows
        ])

    except ValueError as exc:
        return error(str(exc))


@api.get("/products/<int:product_id>")
@require_telegram_user
def product_details(product_id):
    product = shop.get_product(product_id)

    if not product:
        return error(
            "Товар не найден",
            404,
        )

    product_data = dict(product)

    if not product_data.get("active"):
        return error(
            "Товар недоступен",
            404,
        )

    return success(product_data)


# --------------------------------------------------
# ИСТОРИЯ
# --------------------------------------------------

@api.get("/history")
@require_telegram_user
def history():
    user_id = _user_id()

    orders = shop.list_wallet_orders(
        user_id,
        limit=100,
    )

    topups = payments.get_user_topups(
        user_id,
    )

    transactions = db.q_all(
        """
        SELECT *
        FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 100
        """,
        (user_id,),
    )

    return success({
        "orders": [
            dict(row) for row in orders
        ],
        "topups": [
            dict(row) for row in topups
        ],
        "transactions": [
            dict(row) for row in transactions
        ],
    })


@api.get("/orders")
@require_telegram_user
def orders():
    rows = shop.list_wallet_orders(
        _user_id(),
        limit=100,
    )

    return success([
        dict(row) for row in rows
    ])


@api.get("/orders/<int:order_id>")
@require_telegram_user
def order_details(order_id):
    order = shop.get_wallet_order(order_id)

    if not order:
        return error(
            "Заказ не найден",
            404,
        )

    if int(order["user_id"]) != _user_id():
        return error(
            "Заказ не найден",
            404,
        )

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
            data.get(
                "quantity",
                data.get("qty", 1),
            ),
            "quantity",
            maximum=50,
        )

        recipient = str(
            data.get("recipient", "")
        ).strip()

        idempotency_key = str(
            data.get("idempotency_key", "")
        ).strip()

        if not idempotency_key:
            return error(
                "Отсутствует ключ заказа"
            )

        if len(idempotency_key) > 128:
            return error(
                "Некорректный ключ заказа"
            )

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

    except shop.ShopError as exc:
        return error(str(exc))

    except Exception:
        logger.exception(
            "Ошибка создания заказа"
        )

        return error(
            "Не удалось оформить заказ",
            500,
        )


# --------------------------------------------------
# ПОПОЛНЕНИЯ
# --------------------------------------------------

@api.get("/topups")
@require_telegram_user
def topups():
    rows = payments.get_user_topups(
        _user_id(),
    )

    return success([
        dict(row) for row in rows
    ])


@api.post("/topups")
@require_telegram_user
def create_topup():
    data = _body()

    try:
        amount = _positive_int(
            data.get(
                "amount_uzs",
                data.get("amount"),
            ),
            "amount",
        )

        minimum = int(
            getattr(
                config,
                "TOPUP_MIN_UZS",
                1000,
            )
        )

        maximum = int(
            getattr(
                config,
                "TOPUP_MAX_UZS",
                100000000,
            )
        )

        if amount < minimum or amount > maximum:
            return error(
                f"Сумма должна быть от "
                f"{minimum} до {maximum} сум"
            )

        idempotency_key = str(
            data.get("idempotency_key", "")
        ).strip()

        if not idempotency_key:
            return error(
                "Отсутствует ключ заявки"
            )

        if len(idempotency_key) > 128:
            return error(
                "Некорректный ключ заявки"
            )

        result = payments.create_topup(
            user_id=_user_id(),
            amount_uzs=amount,
            idempotency_key=idempotency_key,
        )

        topup_data = dict(result)

        # Реквизиты только для ручного пополнения.
        requisites = str(
            getattr(
                config,
                "TOPUP_REQUISITES",
                "",
            ) or ""
        )

        response_data = dict(topup_data)
        response_data["requisites"] = requisites

        return success(
            response_data,
            "Заявка на пополнение создана",
            201,
        )

    except ValueError as exc:
        return error(str(exc))

    except Exception:
        logger.exception(
            "Ошибка создания пополнения"
        )

        return error(
            "Не удалось создать заявку",
            500,
        )


# --------------------------------------------------
# ДЕТАЛИ ПОПОЛНЕНИЯ
# --------------------------------------------------

@api.get("/topups/<int:topup_id>")
@require_telegram_user
def topup_details(topup_id):
    topup = payments.get_topup(topup_id)

    if not topup:
        return error(
            "Заявка не найдена",
            404,
        )

    if int(topup["user_id"]) != _user_id():
        return error(
            "Заявка не найдена",
            404,
        )

    return success(dict(topup))


# --------------------------------------------------
# ОШИБКИ
# --------------------------------------------------

@api.errorhandler(404)
def not_found(_exc):
    return error(
        "Маршрут не найден",
        404,
    )


@api.errorhandler(405)
def method_not_allowed(_exc):
    return error(
        "Метод не поддерживается",
        405,
    )


@api.errorhandler(Exception)
def internal_error(exc):
    logger.exception(
        "Необработанная ошибка API: %s",
        exc,
    )

    return error(
        "Внутренняя ошибка сервера",
        500,
    )