# -*- coding: utf-8 -*-
"""
Gold Star Store — каталог, корзина и оформление заказов.
"""

import sqlite3
from typing import Any

import database as db


MAX_BONUS_PERCENT = 50
MAX_PRODUCT_QTY = 99


def _dict(row):
    return dict(row) if row is not None else None


def _user_id(value: Any) -> int:
    result = int(value or 0)
    if result <= 0:
        raise ValueError("Некорректный ID пользователя")
    return result


def _positive_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное значение: {name}")

    if result <= 0:
        raise ValueError(f"{name} должен быть больше нуля")

    return result


def _ensure_user(user_id: int) -> None:
    user = db.get_user(user_id)

    if not user:
        raise ValueError("Пользователь не найден")


# --------------------------------------------------
# КАТЕГОРИИ
# --------------------------------------------------

def get_categories(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM categories"

    if active_only:
        sql += " WHERE active = 1"

    sql += " ORDER BY sort_order ASC, id ASC"

    return [_dict(row) for row in db.fetchall(sql)]


def get_category(slug: str):
    if not slug:
        return None

    return _dict(
        db.fetchone(
            "SELECT * FROM categories WHERE slug = ? AND active = 1",
            (str(slug),),
        )
    )


def create_category(
    slug: str,
    title: str,
    description: str = "",
    sort_order: int = 0,
) -> int:
    slug = str(slug).strip().lower()
    title = str(title).strip()

    if not slug or not title:
        raise ValueError("Укажите название и идентификатор категории")

    with db.transaction():
        cursor = db.execute(
            """
            INSERT INTO categories(
                slug, title, description, sort_order,
                active, created_at
            )
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (slug, title, description, int(sort_order), db.now()),
        )

        return cursor.lastrowid


# --------------------------------------------------
# ТОВАРЫ
# --------------------------------------------------

def get_product(product_id: int, active_only: bool = True):
    product_id = _positive_int(product_id, "ID товара")

    sql = """
        SELECT
            p.*,
            c.slug AS category_slug,
            c.title AS category_title
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.id = ?
    """

    if active_only:
        sql += " AND p.active = 1"

    return _dict(db.fetchone(sql, (product_id,)))


def get_product_by_sku(sku: str):
    if not sku:
        return None

    return _dict(
        db.fetchone(
            """
            SELECT *
            FROM products
            WHERE sku = ? AND active = 1
            """,
            (str(sku).strip(),),
        )
    )


def get_products(
    category_slug: str | None = None,
    active_only: bool = True,
) -> list[dict]:

    sql = """
        SELECT
            p.*,
            c.slug AS category_slug,
            c.title AS category_title
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE 1 = 1
    """

    params = []

    if active_only:
        sql += " AND p.active = 1"

    if category_slug:
        sql += " AND c.slug = ?"
        params.append(str(category_slug))

    sql += " ORDER BY p.sort_order ASC, p.id DESC"

    return [
        _dict(row)
        for row in db.fetchall(sql, tuple(params))
    ]


def create_product(
    category_slug: str,
    title: str,
    description: str,
    price_uzs: int,
    fulfillment_mode: str = "manual",
    price_xtr: int = 0,
    sku: str | None = None,
    image_url: str | None = None,
    bonus_eligible: bool = True,
) -> int:

    title = str(title).strip()
    category_slug = str(category_slug).strip().lower()

    price_uzs = int(price_uzs)
    price_xtr = int(price_xtr)

    allowed_modes = {"manual", "wallet", "xtr", "request", "info"}

    if not title:
        raise ValueError("Название товара не может быть пустым")

    if price_uzs < 0 or price_xtr < 0:
        raise ValueError("Цена не может быть отрицательной")

    if fulfillment_mode not in allowed_modes:
        raise ValueError("Неизвестный способ выполнения заказа")

    category = db.fetchone(
        "SELECT id FROM categories WHERE slug = ?",
        (category_slug,),
    )

    if not category:
        raise ValueError("Категория не найдена")

    if fulfillment_mode == "xtr" and category_slug != "other":
        raise ValueError(
            "Оплата Telegram Stars разрешена только в категории «Другие товары»"
        )

    if fulfillment_mode == "xtr" and price_xtr <= 0:
        raise ValueError("Для товара XTR необходимо указать цену в Stars")

    with db.transaction():
        cursor = db.execute(
            """
            INSERT INTO products(
                category_id, sku, title, description,
                price_uzs, price_xtr, fulfillment_mode,
                active, bonus_eligible, image_url,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                category["id"],
                sku,
                title,
                description or "",
                price_uzs,
                price_xtr,
                fulfillment_mode,
                int(bool(bonus_eligible)),
                image_url,
                db.now(),
                db.now(),
            ),
        )

        return cursor.lastrowid


def set_product_active(product_id: int, active: bool) -> bool:
    product_id = _positive_int(product_id, "ID товара")

    with db.transaction():
        cursor = db.execute(
            """
            UPDATE products
            SET active = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(bool(active)), db.now(), product_id),
        )

        return cursor.rowcount > 0


# --------------------------------------------------
# КОРЗИНА
# --------------------------------------------------

def add_to_cart(
    user_id: int,
    product_id: int,
    qty: int = 1,
) -> dict:

    user_id = _user_id(user_id)
    product_id = _positive_int(product_id, "ID товара")
    qty = _positive_int(qty, "Количество")

    if qty > MAX_PRODUCT_QTY:
        raise ValueError("Слишком большое количество товара")

    _ensure_user(user_id)

    product = get_product(product_id)

    if not product:
        raise ValueError("Товар не найден или отключён")

    if product["fulfillment_mode"] in {"info", "request", "xtr"}:
        raise ValueError(
            "Этот товар нельзя добавить в обычную корзину"
        )

    if product["price_uzs"] <= 0:
        raise ValueError("У товара не указана корректная цена")

    if product["stock"] is not None and product["stock"] < qty:
        raise ValueError("Недостаточно товара на складе")

    with db.transaction():
        row = db.fetchone(
            """
            SELECT id, qty
            FROM cart_items
            WHERE user_id = ? AND product_id = ?
            """,
            (user_id, product_id),
        )

        new_qty = qty

        if row:
            new_qty += int(row["qty"])

        if new_qty > MAX_PRODUCT_QTY:
            raise ValueError("Превышено максимальное количество товара")

        if product["stock"] is not None and product["stock"] < new_qty:
            raise ValueError("Недостаточно товара на складе")

        timestamp = db.now()

        db.execute(
            """
            INSERT INTO cart_items(
                user_id, product_id, qty, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET
                qty = excluded.qty,
                updated_at = excluded.updated_at
            """,
            (user_id, product_id, new_qty, timestamp, timestamp),
        )

    return get_cart(user_id)


def set_cart_quantity(
    user_id: int,
    product_id: int,
    qty: int,
) -> dict:

    user_id = _user_id(user_id)
    product_id = _positive_int(product_id, "ID товара")
    qty = _positive_int(qty, "Количество")

    if qty > MAX_PRODUCT_QTY:
        raise ValueError("Слишком большое количество")

    product = get_product(product_id)

    if not product:
        raise ValueError("Товар недоступен")

    if product["stock"] is not None and product["stock"] < qty:
        raise ValueError("Недостаточно товара на складе")

    with db.transaction():
        cursor = db.execute(
            """
            UPDATE cart_items
            SET qty = ?, updated_at = ?
            WHERE user_id = ? AND product_id = ?
            """,
            (qty, db.now(), user_id, product_id),
        )

        if cursor.rowcount == 0:
            raise ValueError("Товар отсутствует в корзине")

    return get_cart(user_id)


def remove_from_cart(user_id: int, product_id: int) -> bool:
    user_id = _user_id(user_id)
    product_id = _positive_int(product_id, "ID товара")

    with db.transaction():
        cursor = db.execute(
            """
            DELETE FROM cart_items
            WHERE user_id = ? AND product_id = ?
            """,
            (user_id, product_id),
        )

        return cursor.rowcount > 0


def clear_cart(user_id: int) -> None:
    user_id = _user_id(user_id)

    with db.transaction():
        db.execute(
            "DELETE FROM cart_items WHERE user_id = ?",
            (user_id,),
        )


def get_cart(user_id: int) -> dict:
    user_id = _user_id(user_id)

    rows = db.fetchall(
        """
        SELECT
            ci.id AS cart_id,
            ci.qty,
            p.id AS product_id,
            p.title,
            p.description,
            p.price_uzs,
            p.fulfillment_mode,
            p.stock,
            p.bonus_eligible,
            p.active
        FROM cart_items ci
        JOIN products p ON p.id = ci.product_id
        WHERE ci.user_id = ?
        ORDER BY ci.id DESC
        """,
        (user_id,),
    )

    items = []
    total = 0

    for row in rows:
        item = _dict(row)

        item_total = int(item["price_uzs"]) * int(item["qty"])
        item["total_uzs"] = item_total

        items.append(item)
        total += item_total

    return {
        "items": items,
        "count": sum(int(item["qty"]) for item in items),
        "total_uzs": total,
    }


# --------------------------------------------------
# РАСЧЁТ ОПЛАТЫ
# --------------------------------------------------

def calculate_payment(
    total_uzs: int,
    balance_uzs: int,
    bonus_uzs: int,
    bonus_eligible: bool = True,
) -> dict:

    total = max(0, int(total_uzs))
    balance = max(0, int(balance_uzs))
    bonus = max(0, int(bonus_uzs))

    if total == 0:
        return {
            "total_uzs": 0,
            "bonus_used_uzs": 0,
            "balance_used_uzs": 0,
            "remaining_uzs": 0,
        }

    bonus_limit = (
        total * MAX_BONUS_PERCENT // 100
        if bonus_eligible
        else 0
    )

    bonus_used = min(bonus, bonus_limit)

    remaining = total - bonus_used
    balance_used = min(balance, remaining)

    return {
        "total_uzs": total,
        "bonus_used_uzs": bonus_used,
        "balance_used_uzs": balance_used,
        "remaining_uzs": total - bonus_used - balance_used,
    }


# --------------------------------------------------
# ОФОРМЛЕНИЕ ЗАКАЗА
# --------------------------------------------------

def create_wallet_order(
    user_id: int,
    product_id: int,
    qty: int = 1,
    recipient: str = "",
    idempotency_key: str | None = None,
) -> dict:

    user_id = _user_id(user_id)
    product_id = _positive_int(product_id, "ID товара")
    qty = _positive_int(qty, "Количество")

    if qty > MAX_PRODUCT_QTY:
        raise ValueError("Слишком большое количество товара")

    recipient = str(recipient or "").strip()

    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()

        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("Некорректный ключ заказа")

    timestamp = db.now()

    with db.transaction():
        _ensure_user(user_id)

        if idempotency_key:
            existing = db.fetchone(
                """
                SELECT *
                FROM wallet_orders
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            )

            if existing:
                return _dict(existing)

        product = db.fetchone(
            """
            SELECT *
            FROM products
            WHERE id = ? AND active = 1
            """,
            (product_id,),
        )

        if not product:
            raise ValueError("Товар не найден или отключён")

        if product["fulfillment_mode"] not in {"manual", "wallet"}:
            raise ValueError(
                "Этот товар нельзя оплатить через кошелёк"
            )

        unit_price = int(product["price_uzs"])

        if unit_price <= 0:
            raise ValueError("Некорректная цена товара")

        if product["stock"] is not None:
            if int(product["stock"]) < qty:
                raise ValueError("Недостаточно товара на складе")

        total = unit_price * qty

        wallet = db.fetchone(
            "SELECT * FROM wallets WHERE user_id = ?",
            (user_id,),
        )

        if not wallet:
            raise ValueError("Кошелёк пользователя не найден")

        bonus_eligible = bool(product["bonus_eligible"])

        payment = calculate_payment(
            total,
            wallet["balance_uzs"],
            wallet["bonus_uzs"],
            bonus_eligible,
        )

        if payment["remaining_uzs"] > 0:
            raise ValueError(
                "Недостаточно средств. "
                f"Не хватает {payment['remaining_uzs']} сум."
            )

        bonus_used = payment["bonus_used_uzs"]
        balance_used = payment["balance_used_uzs"]

        new_balance = int(wallet["balance_uzs"]) - balance_used
        new_bonus = int(wallet["bonus_uzs"]) - bonus_used

        db.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?,
                bonus_uzs = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (new_balance, new_bonus, timestamp, user_id),
        )

        cursor = db.execute(
            """
            INSERT INTO wallet_orders(
                user_id, product_id, product_title,
                qty, recipient, unit_price_uzs,
                total_price_uzs, bonus_used_uzs,
                balance_used_uzs, status,
                idempotency_key, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                user_id,
                product_id,
                product["title"],
                qty,
                recipient,
                unit_price,
                total,
                bonus_used,
                balance_used,
                idempotency_key,
                timestamp,
                timestamp,
            ),
        )

        order_id = cursor.lastrowid

        if product["stock"] is not None:
            db.execute(
                """
                UPDATE products
                SET stock = stock - ?, updated_at = ?
                WHERE id = ? AND stock >= ?
                """,
                (qty, timestamp, product_id, qty),
            )

            stock = db.fetchone(
                "SELECT stock FROM products WHERE id = ?",
                (product_id,),
            )

            if stock is None or stock["stock"] < 0:
                raise ValueError("Не удалось зарезервировать товар")

        db.execute(
            """
            INSERT INTO wallet_transactions(
                user_id, transaction_type,
                amount_uzs, balance_delta, bonus_delta,
                balance_after, bonus_after,
                reference_type, reference_id,
                description, created_at
            )
            VALUES (?, 'purchase', ?, ?, ?, ?, ?, 'wallet_order', ?, ?, ?)
            """,
            (
                user_id,
                total,
                -balance_used,
                -bonus_used,
                new_balance,
                new_bonus,
                str(order_id),
                f"Оплата заказа №{order_id}",
                timestamp,
            ),
        )

        order = db.fetchone(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (order_id,),
        )

        return _dict(order)


def get_order(order_id: int):
    order_id = _positive_int(order_id, "ID заказа")

    return _dict(
        db.fetchone(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (order_id,),
        )
    )


def get_user_orders(
    user_id: int,
    limit: int = 50,
) -> list[dict]:

    user_id = _user_id(user_id)
    limit = max(1, min(int(limit), 100))

    rows = db.fetchall(
        """
        SELECT *
        FROM wallet_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )

    return [_dict(row) for row in rows]


def update_order_status(
    order_id: int,
    status: str,
    admin_comment: str = "",
) -> bool:

    order_id = _positive_int(order_id, "ID заказа")

    allowed = {
        "pending",
        "paid",
        "processing",
        "delivered",
        "rejected",
        "refunded",
    }

    if status not in allowed:
        raise ValueError("Недопустимый статус заказа")

    with db.transaction():
        cursor = db.execute(
            """
            UPDATE wallet_orders
            SET status = ?,
                admin_comment = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, str(admin_comment), db.now(), order_id),
        )

        return cursor.rowcount > 0


# --------------------------------------------------
# СОВМЕСТИМОСТЬ С ДРУГИМИ МОДУЛЯМИ
# --------------------------------------------------

list_categories = get_categories
list_products = get_products
product = get_product
cart = get_cart
purchase = create_wallet_order
user_orders = get_user_orders