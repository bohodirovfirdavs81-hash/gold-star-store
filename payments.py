# -*- coding: utf-8 -*-
"""
Gold Star Store — платежи и пополнения.

Поддерживает:
- Ручные пополнения UZS с подтверждением администратора.
- Telegram Stars (XTR) только для категории other.
- Защиту от повторного начисления и повторной обработки платежей.

Важно:
Чек пользователя сам по себе не подтверждает оплату.
Начисление UZS выполняется только после решения администратора.
"""

import json
import secrets
import sqlite3
from typing import Any

import database as db
import shop


# --------------------------------------------------
# НАСТРОЙКИ
# --------------------------------------------------

def _config_int(name: str, default: int) -> int:
    import config

    try:
        return int(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


TOPUP_MIN_UZS = _config_int("TOPUP_MIN_UZS", 1000)
TOPUP_MAX_UZS = _config_int("TOPUP_MAX_UZS", 10000000)


def _uid(value: Any) -> int:
    value = int(value or 0)

    if value <= 0:
        raise ValueError("Некорректный ID пользователя")

    return value


def _positive(value: Any, name: str) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное значение: {name}")

    if value <= 0:
        raise ValueError(f"{name} должен быть больше нуля")

    return value


def _dict(row):
    return dict(row) if row is not None else None


# --------------------------------------------------
# РУЧНЫЕ ПОПОЛНЕНИЯ
# --------------------------------------------------

def create_topup(
    user_id: int,
    amount_uzs: int,
    idempotency_key: str | None = None,
) -> dict:
    """
    Создаёт заявку на пополнение.
    Деньги на этом этапе не начисляются.
    """

    user_id = _uid(user_id)
    amount_uzs = _positive(amount_uzs, "Сумма")

    if not TOPUP_MIN_UZS <= amount_uzs <= TOPUP_MAX_UZS:
        raise ValueError(
            f"Сумма должна быть от {TOPUP_MIN_UZS} "
            f"до {TOPUP_MAX_UZS} сум"
        )

    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()

        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("Некорректный ключ заявки")

    timestamp = db.now()

    with db.transaction():
        user = db.get_user(user_id)

        if not user:
            raise ValueError("Пользователь не найден")

        if idempotency_key:
            existing = db.fetchone(
                """
                SELECT *
                FROM topups
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            )

            if existing:
                return _dict(existing)

        cursor = db.execute(
            """
            INSERT INTO topups(
                user_id,
                amount_uzs,
                status,
                idempotency_key,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?)
            """,
            (
                user_id,
                amount_uzs,
                idempotency_key,
                timestamp,
                timestamp,
            ),
        )

        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (cursor.lastrowid,),
        )

        return _dict(topup)


def get_topup(topup_id: int):
    topup_id = _positive(topup_id, "ID пополнения")

    return _dict(
        db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )
    )


def get_user_topups(
    user_id: int,
    limit: int = 30,
) -> list[dict]:

    user_id = _uid(user_id)
    limit = max(1, min(int(limit), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM topups
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )

    return [_dict(row) for row in rows]


def get_pending_topups(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 100))

    rows = db.fetchall(
        """
        SELECT
            t.*,
            u.username,
            u.first_name
        FROM topups t
        LEFT JOIN users u ON u.telegram_id = t.user_id
        WHERE t.status = 'pending'
        ORDER BY t.id ASC
        LIMIT ?
        """,
        (limit,),
    )

    return [_dict(row) for row in rows]


def attach_topup_receipt(
    topup_id: int,
    user_id: int,
    file_id: str,
    note: str = "",
) -> dict:
    """
    Сохраняет Telegram file_id чека.
    Не подтверждает оплату.
    """

    topup_id = _positive(topup_id, "ID пополнения")
    user_id = _uid(user_id)

    file_id = str(file_id or "").strip()
    note = str(note or "").strip()

    if not file_id:
        raise ValueError("Не указан файл чека")

    if len(note) > 1000:
        raise ValueError("Комментарий слишком длинный")

    with db.transaction():
        topup = db.fetchone(
            """
            SELECT *
            FROM topups
            WHERE id = ? AND user_id = ?
            """,
            (topup_id, user_id),
        )

        if not topup:
            raise ValueError("Заявка не найдена")

        if topup["status"] != "pending":
            raise ValueError("Эта заявка уже обработана")

        db.execute(
            """
            UPDATE topups
            SET receipt_file_id = ?,
                receipt_note = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'pending'
            """,
            (
                file_id,
                note,
                db.now(),
                topup_id,
                user_id,
            ),
        )

        return get_topup(topup_id)


# --------------------------------------------------
# НАЧИСЛЕНИЕ ДЕНЕГ
# --------------------------------------------------

def approve_topup(
    topup_id: int,
    admin_id: int,
    admin_comment: str = "",
) -> dict:
    """
    Подтверждает пополнение и начисляет UZS один раз.

    Повторное подтверждение не создаёт повторного начисления.
    """

    topup_id = _positive(topup_id, "ID пополнения")
    admin_id = _uid(admin_id)

    timestamp = db.now()

    with db.transaction():
        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        if not topup:
            raise ValueError("Заявка не найдена")

        if topup["status"] == "approved":
            raise ValueError("Заявка уже подтверждена")

        if topup["status"] != "pending":
            raise ValueError("Заявка не ожидает подтверждения")

        amount = int(topup["amount_uzs"])

        if amount <= 0:
            raise ValueError("Некорректная сумма пополнения")

        user_id = int(topup["user_id"])

        wallet = db.fetchone(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        )

        if not wallet:
            raise ValueError("Кошелёк пользователя не найден")

        new_balance = int(wallet["balance_uzs"]) + amount
        new_bonus = int(wallet["bonus_uzs"])

        cursor = db.execute(
            """
            UPDATE topups
            SET status = 'approved',
                admin_id = ?,
                admin_comment = ?,
                confirmed_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                admin_id,
                str(admin_comment),
                timestamp,
                timestamp,
                topup_id,
            ),
        )

        if cursor.rowcount != 1:
            raise ValueError("Заявка уже была обработана")

        db.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (new_balance, timestamp, user_id),
        )

        db.execute(
            """
            INSERT INTO wallet_transactions(
                user_id,
                transaction_type,
                amount_uzs,
                balance_delta,
                bonus_delta,
                balance_after,
                bonus_after,
                reference_type,
                reference_id,
                description,
                created_at
            )
            VALUES (
                ?, 'topup', ?, ?, 0, ?, ?,
                'topup', ?, ?, ?
            )
            """,
            (
                user_id,
                amount,
                amount,
                new_balance,
                new_bonus,
                str(topup_id),
                f"Пополнение №{topup_id}",
                timestamp,
            ),
        )

        db.record_admin_action(
            admin_id,
            "approve_topup",
            "topup",
            str(topup_id),
            {
                "user_id": user_id,
                "amount_uzs": amount,
                "comment": admin_comment,
            },
        )

        return get_topup(topup_id)


def reject_topup(
    topup_id: int,
    admin_id: int,
    reason: str = "",
) -> dict:
    """
    Отклоняет заявку без изменения баланса.
    """

    topup_id = _positive(topup_id, "ID пополнения")
    admin_id = _uid(admin_id)

    with db.transaction():
        topup = db.fetchone(
            "SELECT * FROM topups WHERE id = ?",
            (topup_id,),
        )

        if not topup:
            raise ValueError("Заявка не найдена")

        if topup["status"] != "pending":
            raise ValueError("Заявка уже обработана")

        cursor = db.execute(
            """
            UPDATE topups
            SET status = 'rejected',
                admin_id = ?,
                admin_comment = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                admin_id,
                str(reason),
                db.now(),
                topup_id,
            ),
        )

        if cursor.rowcount != 1:
            raise ValueError("Заявка уже была обработана")

        db.record_admin_action(
            admin_id,
            "reject_topup",
            "topup",
            str(topup_id),
            {"reason": reason},
        )

        return get_topup(topup_id)


# --------------------------------------------------
# TELEGRAM STARS
# --------------------------------------------------

def _ensure_xtr_orders_table() -> None:
    """
    Отдельная таблица для заказов, оплаченных Telegram Stars.
    """

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS xtr_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_title TEXT NOT NULL,
            amount_xtr INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'XTR',
            telegram_charge_id TEXT NOT NULL UNIQUE,
            payment_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'paid',
            created_at TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_xtr_orders_user
        ON xtr_orders(user_id, created_at)
        """
    )


def create_xtr_invoice(
    bot,
    chat_id: int,
    user_id: int,
    product_id: int,
):
    """
    Создаёт Telegram Stars invoice.

    Оплата доступна только товарам категории other
    с fulfillment_mode='xtr'.
    """

    user_id = _uid(user_id)
    product_id = _positive(product_id, "ID товара")

    product = shop.get_product(product_id)

    if not product:
        raise ValueError("Товар не найден или отключён")

    if product.get("category_slug") != "other":
        raise ValueError(
            "Telegram Stars разрешены только в категории «Другие товары»"
        )

    if product.get("fulfillment_mode") != "xtr":
        raise ValueError("Этот товар не продаётся за Telegram Stars")

    amount = int(product.get("price_xtr") or 0)

    if amount <= 0:
        raise ValueError("Некорректная цена товара в Stars")

    _ensure_xtr_orders_table()

    nonce = secrets.token_urlsafe(18)
    payload = f"gss:xtr:{user_id}:{product_id}:{nonce}"

    timestamp = db.now()

    with db.transaction():
        payment_cursor = db.execute(
            """
            INSERT INTO payments(
                user_id,
                order_id,
                payment_type,
                provider,
                amount,
                currency,
                status,
                payload,
                created_at,
                updated_at
            )
            VALUES (?, NULL, 'product', 'telegram_stars',
                    ?, 'XTR', 'pending', ?, ?, ?)
            """,
            (
                user_id,
                amount,
                payload,
                timestamp,
                timestamp,
            ),
        )

        payment_id = payment_cursor.lastrowid

    try:
        import telebot

        prices = [
            telebot.types.LabeledPrice(
                label=str(product["title"])[:32],
                amount=amount,
            )
        ]

        bot.send_invoice(
            chat_id=chat_id,
            title=str(product["title"])[:32],
            description=str(
                product.get("description") or product["title"]
            )[:255],
            invoice_payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )

    except Exception:
        with db.transaction():
            db.execute(
                """
                UPDATE payments
                SET status = 'failed',
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (db.now(), payment_id),
            )
        raise

    return {
        "payment_id": payment_id,
        "payload": payload,
        "amount_xtr": amount,
        "currency": "XTR",
    }


def validate_xtr_pre_checkout(
    user_id: int,
    payload: str,
    currency: str,
    total_amount: int,
) -> tuple[bool, str]:
    """
    Проверяет данные Telegram pre_checkout_query.
    """

    user_id = _uid(user_id)

    if currency != "XTR":
        return False, "Неверная валюта платежа"

    try:
        total_amount = int(total_amount)
    except (TypeError, ValueError):
        return False, "Неверная сумма платежа"

    if total_amount <= 0:
        return False, "Неверная сумма платежа"

    payment = db.fetchone(
        """
        SELECT *
        FROM payments
        WHERE payload = ?
          AND user_id = ?
          AND provider = 'telegram_stars'
          AND currency = 'XTR'
          AND status = 'pending'
        """,
        (str(payload), user_id),
    )

    if not payment:
        return False, "Платёж не найден или уже обработан"

    if int(payment["amount"]) != total_amount:
        return False, "Сумма платежа не совпадает"

    parts = str(payload).split(":")

    if len(parts) != 5 or parts[0] != "gss" or parts[1] != "xtr":
        return False, "Некорректные данные платежа"

    try:
        payload_user_id = int(parts[2])
        product_id = int(parts[3])
    except (TypeError, ValueError):
        return False, "Некорректные данные платежа"

    if payload_user_id != user_id:
        return False, "Пользователь не совпадает"

    product = shop.get_product(product_id)

    if not product:
        return False, "Товар больше недоступен"

    if product.get("category_slug") != "other":
        return False, "Неверная категория товара"

    if product.get("fulfillment_mode") != "xtr":
        return False, "Неверный способ оплаты"

    if int(product.get("price_xtr") or 0) != total_amount:
        return False, "Цена товара изменилась"

    return True, ""


def process_xtr_success(
    user_id: int,
    payload: str,
    currency: str,
    total_amount: int,
    telegram_charge_id: str,
) -> dict:
    """
    Обрабатывает успешный платёж Telegram Stars.

    Проверяет пользователя, товар, сумму, валюту,
    уникальность Telegram charge ID и статус платежа.
    """

    user_id = _uid(user_id)
    total_amount = _positive(total_amount, "Сумма")
    telegram_charge_id = str(telegram_charge_id or "").strip()

    if currency != "XTR":
        raise ValueError("Неверная валюта платежа")

    if not telegram_charge_id:
        raise ValueError("Отсутствует ID платежа Telegram")

    valid, error = validate_xtr_pre_checkout(
        user_id,
        payload,
        currency,
        total_amount,
    )

    if not valid:
        raise ValueError(error)

    parts = str(payload).split(":")
    product_id = int(parts[3])

    _ensure_xtr_orders_table()

    timestamp = db.now()

    with db.transaction():
        payment = db.fetchone(
            """
            SELECT *
            FROM payments
            WHERE payload = ?
              AND user_id = ?
              AND provider = 'telegram_stars'
            """,
            (payload, user_id),
        )

        if not payment:
            raise ValueError("Платёж не найден")

        if payment["status"] == "paid":
            existing = db.fetchone(
                """
                SELECT *
                FROM xtr_orders
                WHERE telegram_charge_id = ?
                """,
                (telegram_charge_id,),
            )

            if existing:
                return _dict(existing)

            raise ValueError("Платёж уже обработан")

        if payment["status"] != "pending":
            raise ValueError("Платёж больше не ожидает подтверждения")

        if int(payment["amount"]) != total_amount:
            raise ValueError("Сумма платежа не совпадает")

        product = db.fetchone(
            """
            SELECT p.*, c.slug AS category_slug
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.id = ? AND p.active = 1
            """,
            (product_id,),
        )

        if not product:
            raise ValueError("Товар больше недоступен")

        if product["category_slug"] != "other":
            raise ValueError("Недопустимая категория товара")

        if product["fulfillment_mode"] != "xtr":
            raise ValueError("Недопустимый способ оплаты")

        if int(product["price_xtr"]) != total_amount:
            raise ValueError("Цена товара изменилась")

        if product["stock"] is not None:
            if int(product["stock"]) < 1:
                raise ValueError("Товар закончился")

            stock_cursor = db.execute(
                """
                UPDATE products
                SET stock = stock - 1,
                    updated_at = ?
                WHERE id = ? AND stock >= 1
                """,
                (timestamp, product_id),
            )

            if stock_cursor.rowcount != 1:
                raise ValueError("Товар закончился")

        cursor = db.execute(
            """
            INSERT INTO xtr_orders(
                user_id,
                product_id,
                product_title,
                amount_xtr,
                currency,
                telegram_charge_id,
                payment_id,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, 'XTR', ?, ?, 'paid', ?)
            """,
            (
                user_id,
                product_id,
                product["title"],
                total_amount,
                telegram_charge_id,
                payment["id"],
                timestamp,
            ),
        )

        order_id = cursor.lastrowid

        update_cursor = db.execute(
            """
            UPDATE payments
            SET status = 'paid',
                telegram_charge_id = ?,
                provider_payment_id = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                telegram_charge_id,
                telegram_charge_id,
                timestamp,
                payment["id"],
            ),
        )

        if update_cursor.rowcount != 1:
            raise ValueError("Платёж уже был обработан")

        return _dict(
            db.fetchone(
                "SELECT * FROM xtr_orders WHERE id = ?",
                (order_id,),
            )
        )


def get_user_xtr_orders(
    user_id: int,
    limit: int = 30,
) -> list[dict]:

    user_id = _uid(user_id)
    limit = max(1, min(int(limit), 100))

    _ensure_xtr_orders_table()

    rows = db.fetchall(
        """
        SELECT *
        FROM xtr_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )

    return [_dict(row) for row in rows]


# --------------------------------------------------
# СОВМЕСТИМОСТЬ С ДРУГИМИ МОДУЛЯМИ
# --------------------------------------------------

new_topup = create_topup
submit_topup_receipt = attach_topup_receipt
confirm_topup = approve_topup
decline_topup = reject_topup
pending_topups = get_pending_topups
create_stars_invoice = create_xtr_invoice
check_pre_checkout = validate_xtr_pre_checkout
successful_stars_payment = process_xtr_success