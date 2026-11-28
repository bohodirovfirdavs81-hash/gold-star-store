# -*- coding: utf-8 -*-
"""
Gold Star Store — каталог, корзина и заказы.

SQLite:
- безопасные транзакции;
- защита от повторных заказов;
- списание баланса и бонусов;
- ограничение бонусной оплаты до 50%;
- учёт остатков товаров;
- однократный возврат;
- журнал финансовых операций.

Совместимо с текущей структурой database.py.
"""

import hashlib
import uuid
from typing import Any

import database as db
from utils import normalize_recipient


MAX_CART_QTY = 50
MAX_PAGE_SIZE = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 200


class ShopError(Exception):
    pass


# --------------------------------------------------
# ОБЩИЕ ФУНКЦИИ
# --------------------------------------------------

def _page(limit: int, offset: int = 0):
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        raise ShopError("Некорректные параметры страницы")

    return (
        max(1, min(limit, MAX_PAGE_SIZE)),
        max(0, offset),
    )


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall()

    return {row["name"] for row in rows}


def _insert(conn, table: str, values: dict[str, Any]):
    columns = _columns(conn, table)

    selected = {
        key: value
        for key, value in values.items()
        if key in columns
    }

    if not selected:
        raise ShopError(
            f"Не удалось подготовить запись в таблицу {table}"
        )

    names = list(selected)

    sql_names = ", ".join(
        f'"{name}"' for name in names
    )

    placeholders = ", ".join(
        "?" for _ in names
    )

    return conn.execute(
        f'INSERT INTO "{table}" ({sql_names}) '
        f"VALUES ({placeholders})",
        tuple(selected[name] for name in names),
    )


def _normalize_idempotency_key(
    user_id: int,
    key: str,
) -> str:
    raw = str(key or "").strip()

    if not raw:
        raw = uuid.uuid4().hex

    if len(raw) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raw = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

    return f"u{int(user_id)}_{raw}"


def _product_active(row) -> bool:
    if not row:
        return False

    keys = row.keys()

    if "active" in keys:
        return bool(row["active"])

    return True


def _timestamp() -> str:
    return db.now()


def _safe_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ShopError(f"Некорректное значение: {name}")


def _get_active_product(conn, product_id: int):
    return conn.execute(
        """
        SELECT
            p.*,
            c.slug AS category_slug,
            c.title AS category_title
        FROM products p
        LEFT JOIN categories c
            ON c.id = p.category_id
        WHERE p.id = ?
          AND p.active = 1
          AND (
              p.category_id IS NULL
              OR c.active = 1
          )
        """,
        (product_id,),
    ).fetchone()


# --------------------------------------------------
# КАТЕГОРИИ
# --------------------------------------------------

def list_categories() -> list[dict[str, Any]]:
    rows = db.q_all(
        """
        SELECT
            id,
            slug,
            title,
            description,
            sort_order,
            active
        FROM categories
        WHERE active = 1
        ORDER BY sort_order, id
        """
    )

    return [dict(row) for row in rows]


def get_categories() -> list[dict[str, Any]]:
    return list_categories()


def get_category(category_id: int):
    category_id = _safe_int(
        category_id,
        "ID категории",
    )

    return db.q_one(
        """
        SELECT *
        FROM categories
        WHERE id = ?
        """,
        (category_id,),
    )


# --------------------------------------------------
# ТОВАРЫ
# --------------------------------------------------

def list_products(
    category_slug: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    limit, offset = _page(limit, offset)

    conditions = []
    params: list[Any] = []

    if active_only:
        conditions.extend([
            "p.active = 1",
            "c.active = 1",
        ])

    if category_slug:
        conditions.append("c.slug = ?")
        params.append(str(category_slug))

    where = (
        " AND ".join(conditions)
        if conditions
        else "1 = 1"
    )

    params.extend([limit, offset])

    rows = db.q_all(
        f"""
        SELECT
            p.*,
            p.active AS is_active,
            c.slug AS category_slug,
            c.title AS category_title
        FROM products p
        LEFT JOIN categories c
            ON c.id = p.category_id
        WHERE {where}
        ORDER BY p.sort_order, p.id
        LIMIT ? OFFSET ?
        """,
        tuple(params),
    )

    return [dict(row) for row in rows]


def get_products(
    category_slug: str | None = None,
    active_only: bool = True,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return list_products(
        category_slug=category_slug,
        limit=limit,
        offset=offset,
        active_only=active_only,
    )


def get_product(product_id: int):
    product_id = _safe_int(
        product_id,
        "ID товара",
    )

    row = db.q_one(
        """
        SELECT
            p.*,
            p.active AS is_active,
            c.slug AS category_slug,
            c.title AS category_title
        FROM products p
        LEFT JOIN categories c
            ON c.id = p.category_id
        WHERE p.id = ?
        """,
        (product_id,),
    )

    return row


# --------------------------------------------------
# КОРЗИНА
# --------------------------------------------------

def get_cart(user_id: int) -> list[dict[str, Any]]:
    user_id = _safe_int(
        user_id,
        "ID пользователя",
    )

    rows = db.q_all(
        """
        SELECT
            ci.product_id,
            ci.qty,
            ci.recipient,
            p.title,
            p.price_uzs,
            p.fulfillment_mode,
            p.active AS is_active
        FROM cart_items ci
        JOIN products p
            ON p.id = ci.product_id
        WHERE ci.user_id = ?
        ORDER BY ci.id DESC
        """,
        (user_id,),
    )

    return [dict(row) for row in rows]


def cart_add(
    user_id: int,
    product_id: int,
    qty: int = 1,
):
    uid = _safe_int(user_id, "ID пользователя")
    pid = _safe_int(product_id, "ID товара")
    quantity = _safe_int(qty, "Количество")

    if uid <= 0 or pid <= 0:
        raise ShopError("Некорректные данные")

    if quantity < 1 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество товара")

    timestamp = _timestamp()

    with db.tx() as conn:
        product = _get_active_product(conn, pid)

        if not product:
            raise ShopError("Товар недоступен")

        row = conn.execute(
            """
            SELECT qty
            FROM cart_items
            WHERE user_id = ?
              AND product_id = ?
            """,
            (uid, pid),
        ).fetchone()

        new_qty = quantity

        if row:
            new_qty += int(row["qty"])

        if new_qty > MAX_CART_QTY:
            raise ShopError(
                "Превышено максимальное количество товара"
            )

        if product["stock"] is not None:
            if new_qty > int(product["stock"]):
                raise ShopError(
                    "Недостаточно товара на складе"
                )

        if row:
            conn.execute(
                """
                UPDATE cart_items
                SET qty = ?,
                    updated_at = ?
                WHERE user_id = ?
                  AND product_id = ?
                """,
                (
                    new_qty,
                    timestamp,
                    uid,
                    pid,
                ),
            )
        else:
            _insert(
                conn,
                "cart_items",
                {
                    "user_id": uid,
                    "product_id": pid,
                    "qty": new_qty,
                    "recipient": "",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )

    return get_cart(uid)


def cart_set_qty(
    user_id: int,
    product_id: int,
    qty: int,
):
    uid = _safe_int(user_id, "ID пользователя")
    pid = _safe_int(product_id, "ID товара")
    quantity = _safe_int(qty, "Количество")

    if uid <= 0 or pid <= 0:
        raise ShopError("Некорректные данные")

    if quantity < 0 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество")

    timestamp = _timestamp()

    with db.tx() as conn:
        if quantity == 0:
            conn.execute(
                """
                DELETE FROM cart_items
                WHERE user_id = ?
                  AND product_id = ?
                """,
                (uid, pid),
            )

        else:
            product = _get_active_product(conn, pid)

            if not product:
                raise ShopError("Товар недоступен")

            if product["stock"] is not None:
                if quantity > int(product["stock"]):
                    raise ShopError(
                        "Недостаточно товара на складе"
                    )

            row = conn.execute(
                """
                SELECT id
                FROM cart_items
                WHERE user_id = ?
                  AND product_id = ?
                """,
                (uid, pid),
            ).fetchone()

            if row:
                conn.execute(
                    """
                    UPDATE cart_items
                    SET qty = ?,
                        updated_at = ?
                    WHERE user_id = ?
                      AND product_id = ?
                    """,
                    (
                        quantity,
                        timestamp,
                        uid,
                        pid,
                    ),
                )
            else:
                _insert(
                    conn,
                    "cart_items",
                    {
                        "user_id": uid,
                        "product_id": pid,
                        "qty": quantity,
                        "recipient": "",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                )

    return get_cart(uid)


def cart_clear(user_id: int):
    uid = _safe_int(user_id, "ID пользователя")

    if uid <= 0:
        raise ShopError("Некорректный пользователь")

    with db.tx() as conn:
        conn.execute(
            """
            DELETE FROM cart_items
            WHERE user_id = ?
            """,
            (uid,),
        )


# --------------------------------------------------
# ПОКУПКА ЗА БАЛАНС
# --------------------------------------------------

def create_wallet_order(
    user_id: int,
    product_id: int,
    qty: int,
    recipient: str,
    idempotency_key: str = "",
) -> dict[str, Any]:

    uid = _safe_int(user_id, "ID пользователя")
    pid = _safe_int(product_id, "ID товара")
    quantity = _safe_int(qty, "Количество")

    if uid <= 0 or pid <= 0:
        raise ShopError("Некорректные данные заказа")

    if quantity < 1 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество товара")

    try:
        recipient = normalize_recipient(recipient)
    except ValueError as exc:
        raise ShopError(str(exc)) from exc

    if not recipient:
        raise ShopError("Укажите получателя")

    order_key = _normalize_idempotency_key(
        uid,
        idempotency_key,
    )

    timestamp = _timestamp()

    with db.tx() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM wallet_orders
            WHERE user_id = ?
              AND idempotency_key = ?
            """,
            (uid, order_key),
        ).fetchone()

        if existing:
            if (
                int(existing["product_id"]) != pid
                or int(existing["qty"]) != quantity
                or str(existing["recipient"]) != recipient
            ):
                raise ShopError(
                    "Ключ заказа уже использован с другими данными"
                )

            return dict(existing)

        product = _get_active_product(conn, pid)

        if not product:
            raise ShopError("Товар не найден или недоступен")

        unit_price = int(product["price_uzs"])

        if unit_price <= 0:
            raise ShopError("Некорректная цена товара")

        total = unit_price * quantity

        if total <= 0:
            raise ShopError("Некорректная сумма заказа")

        stock = product["stock"]

        if stock is not None:
            stock = int(stock)

            if stock < quantity:
                raise ShopError("Недостаточно товара на складе")

            stock_cursor = conn.execute(
                """
                UPDATE products
                SET stock = stock - ?,
                    updated_at = ?
                WHERE id = ?
                  AND active = 1
                  AND stock >= ?
                """,
                (
                    quantity,
                    timestamp,
                    pid,
                    quantity,
                ),
            )

            if stock_cursor.rowcount != 1:
                raise ShopError("Недостаточно товара на складе")

        wallet = conn.execute(
            """
            SELECT *
            FROM wallets
            WHERE user_id = ?
            """,
            (uid,),
        ).fetchone()

        if not wallet:
            raise ShopError("Кошелёк пользователя не найден")

        balance = int(wallet["balance_uzs"])
        bonus = int(wallet["bonus_uzs"])

        if balance < 0 or bonus < 0:
            raise ShopError(
                "Обнаружен некорректный баланс кошелька"
            )

        bonus_eligible = bool(
            product["bonus_eligible"]
        )

        if bonus_eligible:
            bonus_used = min(
                bonus,
                total // 2,
            )
        else:
            bonus_used = 0

        balance_used = total - bonus_used

        if balance < balance_used:
            raise ShopError("Недостаточно средств на балансе")

        new_balance = balance - balance_used
        new_bonus = bonus - bonus_used

        wallet_cursor = conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?,
                bonus_uzs = ?,
                updated_at = ?
            WHERE user_id = ?
              AND balance_uzs = ?
              AND bonus_uzs = ?
            """,
            (
                new_balance,
                new_bonus,
                timestamp,
                uid,
                balance,
                bonus,
            ),
        )

        if wallet_cursor.rowcount != 1:
            raise ShopError(
                "Не удалось обновить кошелёк"
            )

        status = (
            "processing"
            if product["fulfillment_mode"] == "manual"
            else "paid"
        )

        cursor = _insert(
            conn,
            "wallet_orders",
            {
                "user_id": uid,
                "product_id": pid,
                "product_title": product["title"],
                "qty": quantity,
                "recipient": recipient,
                "unit_price_uzs": unit_price,
                "total_price_uzs": total,
                "bonus_used_uzs": bonus_used,
                "balance_used_uzs": balance_used,
                "status": status,
                "idempotency_key": order_key,
                "admin_comment": "",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

        order_id = cursor.lastrowid

        _insert(
            conn,
            "wallet_transactions",
            {
                "user_id": uid,
                "transaction_type": "purchase",
                "amount_uzs": total,
                "balance_delta": -balance_used,
                "bonus_delta": -bonus_used,
                "balance_after": new_balance,
                "bonus_after": new_bonus,
                "reference_type": "wallet_order",
                "reference_id": str(order_id),
                "description": (
                    f"Покупка: {product['title']}"
                ),
                "created_at": timestamp,
            },
        )

        order = conn.execute(
            """
            SELECT *
            FROM wallet_orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        return dict(order)


# --------------------------------------------------
# ИСТОРИЯ ЗАКАЗОВ
# --------------------------------------------------

def list_wallet_orders(
    user_id: int,
    limit: int = 30,
) -> list[dict]:

    uid = _safe_int(user_id, "ID пользователя")
    limit, _ = _page(limit)

    rows = db.q_all(
        """
        SELECT *
        FROM wallet_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (uid, limit),
    )

    return [dict(row) for row in rows]


def get_user_orders(
    user_id: int,
    limit: int = 30,
) -> list[dict]:
    return list_wallet_orders(user_id, limit)


def get_wallet_order(order_id: int):
    oid = _safe_int(order_id, "ID заказа")

    return db.q_one(
        """
        SELECT *
        FROM wallet_orders
        WHERE id = ?
        """,
        (oid,),
    )


# --------------------------------------------------
# ВОЗВРАТ СРЕДСТВ
# --------------------------------------------------

def refund_wallet_order_once(
    order_id: int,
    admin_id: int,
    reason: str = "",
) -> dict:

    oid = _safe_int(order_id, "ID заказа")
    aid = _safe_int(admin_id, "ID администратора")

    if oid <= 0 or aid <= 0:
        raise ShopError("Некорректные данные возврата")

    reason = str(reason or "").strip()[:300]
    timestamp = _timestamp()

    with db.tx() as conn:
        order = conn.execute(
            """
            SELECT *
            FROM wallet_orders
            WHERE id = ?
            """,
            (oid,),
        ).fetchone()

        if not order:
            raise ShopError("Заказ не найден")

        if order["status"] == "refunded":
            return {
                "order": dict(order),
                "already_refunded": True,
            }

        if order["status"] == "delivered":
            raise ShopError(
                "Нельзя вернуть средства за выданный заказ"
            )

        if order["status"] not in (
            "paid",
            "processing",
        ):
            raise ShopError(
                "Заказ нельзя вернуть в текущем статусе"
            )

        uid = int(order["user_id"])
        pid = int(order["product_id"])
        quantity = int(order["qty"])

        balance_refund = int(
            order["balance_used_uzs"]
        )

        bonus_refund = int(
            order["bonus_used_uzs"]
        )

        if balance_refund < 0 or bonus_refund < 0:
            raise ShopError("Некорректная сумма возврата")

        wallet = conn.execute(
            """
            SELECT *
            FROM wallets
            WHERE user_id = ?
            """,
            (uid,),
        ).fetchone()

        if not wallet:
            raise ShopError("Кошелёк пользователя не найден")

        new_balance = (
            int(wallet["balance_uzs"])
            + balance_refund
        )

        new_bonus = (
            int(wallet["bonus_uzs"])
            + bonus_refund
        )

        updated = conn.execute(
            """
            UPDATE wallet_orders
            SET status = 'refunded',
                updated_at = ?,
                admin_comment = ?
            WHERE id = ?
              AND status IN ('paid', 'processing')
            """,
            (
                timestamp,
                reason,
                oid,
            ),
        )

        if updated.rowcount != 1:
            raise ShopError("Заказ уже обработан")

        conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?,
                bonus_uzs = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                new_balance,
                new_bonus,
                timestamp,
                uid,
            ),
        )

        # Возвращаем товар на склад.
        conn.execute(
            """
            UPDATE products
            SET stock = stock + ?,
                updated_at = ?
            WHERE id = ?
              AND stock IS NOT NULL
            """,
            (
                quantity,
                timestamp,
                pid,
            ),
        )

        _insert(
            conn,
            "wallet_transactions",
            {
                "user_id": uid,
                "transaction_type": "refund",
                "amount_uzs": (
                    balance_refund + bonus_refund
                ),
                "balance_delta": balance_refund,
                "bonus_delta": bonus_refund,
                "balance_after": new_balance,
                "bonus_after": new_bonus,
                "reference_type": "wallet_order",
                "reference_id": str(oid),
                "description": (
                    reason or f"Возврат заказа №{oid}"
                ),
                "created_at": timestamp,
            },
        )

        db.record_admin_action(
            aid,
            "refund_order",
            "wallet_order",
            str(oid),
            {
                "user_id": uid,
                "balance_refund": balance_refund,
                "bonus_refund": bonus_refund,
                "reason": reason,
            },
        )

        updated_order = conn.execute(
            """
            SELECT *
            FROM wallet_orders
            WHERE id = ?
            """,
            (oid,),
        ).fetchone()

        return {
            "order": dict(updated_order),
            "already_refunded": False,
        }


# --------------------------------------------------
# ВЫДАЧА ЗАКАЗА
# --------------------------------------------------

def mark_wallet_order_delivered(
    order_id: int,
    admin_id: int,
) -> dict:

    oid = _safe_int(order_id, "ID заказа")
    aid = _safe_int(admin_id, "ID администратора")

    if oid <= 0 or aid <= 0:
        raise ShopError("Некорректные данные")

    timestamp = _timestamp()

    with db.tx() as conn:
        order = conn.execute(
            """
            SELECT *
            FROM wallet_orders
            WHERE id = ?
            """,
            (oid,),
        ).fetchone()

        if not order:
            raise ShopError("Заказ не найден")

        if order["status"] == "delivered":
            raise ShopError("Заказ уже выдан")

        if order["status"] not in (
            "paid",
            "processing",
        ):
            raise ShopError(
                "Заказ нельзя выдать в текущем статусе"
            )

        updated = conn.execute(
            """
            UPDATE wallet_orders
            SET status = 'delivered',
                updated_at = ?
            WHERE id = ?
              AND status IN ('paid', 'processing')
            """,
            (
                timestamp,
                oid,
            ),
        )

        if updated.rowcount != 1:
            raise ShopError("Не удалось обновить заказ")

        db.record_admin_action(
            aid,
            "deliver_order",
            "wallet_order",
            str(oid),
            "",
        )

        result = conn.execute(
            """
            SELECT *
            FROM wallet_orders
            WHERE id = ?
            """,
            (oid,),
        ).fetchone()

        return dict(result)


# --------------------------------------------------
# СТАРЫЕ ЗАКАЗЫ
# --------------------------------------------------

def count_orders(user_id: int) -> int:
    uid = _safe_int(user_id, "ID пользователя")

    row = db.q_one(
        """
        SELECT COUNT(*) AS total
        FROM legacy_orders
        WHERE user_id = ?
        """,
        (uid,),
    )

    return int(row["total"]) if row else 0


def list_orders(
    user_id: int,
    limit: int = 20,
) -> list[dict]:

    uid = _safe_int(user_id, "ID пользователя")
    limit, _ = _page(limit)

    rows = db.q_all(
        """
        SELECT *
        FROM legacy_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (uid, limit),
    )

    return [dict(row) for row in rows]


def get_order(order_id: int):
    oid = _safe_int(order_id, "ID заказа")

    return db.q_one(
        """
        SELECT *
        FROM legacy_orders
        WHERE id = ?
        """,
        (oid,),
    )


def create_order(
    user_id: int,
    recipient: str,
) -> dict:
    """
    Старый способ оформления заказов отключён.

    Telegram Stars должны обрабатываться через
    payments.create_xtr_invoice() и
    payments.process_xtr_success().

    Это предотвращает создание заказа без оплаты.
    """

    raise ShopError(
        "Старый способ оформления отключён. "
        "Используйте актуальную оплату Telegram Stars."
    )


def cancel_order(
    order_id: int,
    user_id: int,
) -> bool:

    oid = _safe_int(order_id, "ID заказа")
    uid = _safe_int(user_id, "ID пользователя")

    timestamp = _timestamp()

    with db.tx() as conn:
        updated = conn.execute(
            """
            UPDATE legacy_orders
            SET status = 'cancelled',
                updated_at = ?
            WHERE id = ?
              AND user_id = ?
              AND status = 'pending'
            """,
            (
                timestamp,
                oid,
                uid,
            ),
        )

        return updated.rowcount == 1