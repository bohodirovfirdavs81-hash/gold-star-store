import hashlib
import time
import uuid
from typing import Any

import database as db
from utils import normalize_recipient


MAX_CART_QTY = 50
MAX_PAGE_SIZE = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 200


class ShopError(Exception):
    pass


def _page(limit: int, offset: int = 0):
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        raise ShopError("Некорректные параметры страницы")

    return max(1, min(limit, MAX_PAGE_SIZE)), max(0, offset)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _first(columns: set[str], *names: str) -> str | None:
    for name in names:
        if name in columns:
            return name
    return None


def _insert(conn, table: str, values: dict[str, Any]):
    columns = _columns(conn, table)
    selected = {key: value for key, value in values.items() if key in columns}

    if not selected:
        raise ShopError(f"Не удалось подготовить запись в {table}")

    names = list(selected)
    sql_names = ", ".join(f'"{name}"' for name in names)
    placeholders = ", ".join("?" for _ in names)

    return conn.execute(
        f'INSERT INTO "{table}" ({sql_names}) VALUES ({placeholders})',
        tuple(selected[name] for name in names),
    )


def _normalize_idempotency_key(user_id: int, key: str) -> str:
    raw = str(key or "").strip()

    if not raw:
        raw = uuid.uuid4().hex

    if len(raw) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raw = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return f"u{int(user_id)}_{raw}"


def _product_active(row) -> bool:
    if not row:
        return False

    keys = row.keys()

    if "is_active" in keys:
        return bool(row["is_active"])

    if "active" in keys:
        return bool(row["active"])

    return True


def list_categories() -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT id, slug, title, description, sort_order,
               COALESCE(is_active, active, 1) AS is_active
        FROM categories
        WHERE COALESCE(is_active, active, 1) = 1
        ORDER BY sort_order, id
        """
    )


def get_categories() -> list[dict[str, Any]]:
    return list_categories()


def get_category(category_id: int):
    return db.q_one(
        "SELECT * FROM categories WHERE id = ?",
        (int(category_id),),
    )


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
            "COALESCE(p.is_active, p.active, 1) = 1",
            "COALESCE(c.is_active, c.active, 1) = 1",
        ])

    if category_slug:
        conditions.append("c.slug = ?")
        params.append(str(category_slug))

    where = " AND ".join(conditions) if conditions else "1 = 1"

    sql = f"""
        SELECT p.*,
               COALESCE(p.is_active, p.active, 1) AS is_active,
               c.slug AS category_slug,
               c.title AS category_title
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE {where}
        ORDER BY p.sort_order, p.id
        LIMIT ? OFFSET ?
    """

    params.extend([limit, offset])
    return db.q_all(sql, tuple(params))


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
    return db.q_one(
        """
        SELECT p.*,
               COALESCE(p.is_active, p.active, 1) AS is_active,
               c.slug AS category_slug,
               c.title AS category_title
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.id = ?
        """,
        (int(product_id),),
    )


def get_cart(user_id: int) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT ci.product_id, ci.qty,
               p.title, p.price_uzs,
               p.fulfillment_mode,
               COALESCE(p.is_active, p.active, 1) AS is_active
        FROM cart_items ci
        JOIN products p ON p.id = ci.product_id
        WHERE ci.user_id = ?
        ORDER BY ci.rowid DESC
        """,
        (int(user_id),),
    )


def cart_add(user_id: int, product_id: int, qty: int = 1):
    uid = int(user_id)
    pid = int(product_id)

    try:
        quantity = int(qty)
    except (TypeError, ValueError):
        raise ShopError("Некорректное количество товара")

    if uid <= 0 or pid <= 0:
        raise ShopError("Некорректные данные")

    if quantity < 1 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество товара")

    product = get_product(pid)

    if not product or not _product_active(product):
        raise ShopError("Товар недоступен")

    now = int(time.time())

    with db.tx() as conn:
        row = conn.execute(
            """
            SELECT qty FROM cart_items
            WHERE user_id = ? AND product_id = ?
            """,
            (uid, pid),
        ).fetchone()

        new_qty = quantity + (int(row["qty"]) if row else 0)

        if new_qty > MAX_CART_QTY:
            raise ShopError("Превышено максимальное количество")

        columns = _columns(conn, "cart_items")

        if row:
            updates = {"qty": new_qty}
            if "updated_at" in columns:
                updates["updated_at"] = now

            assignments = ", ".join(
                f'"{key}" = ?' for key in updates
            )

            conn.execute(
                f"""
                UPDATE cart_items
                SET {assignments}
                WHERE user_id = ? AND product_id = ?
                """,
                (*updates.values(), uid, pid),
            )
        else:
            values = {
                "user_id": uid,
                "product_id": pid,
                "qty": new_qty,
                "created_at": now,
                "updated_at": now,
            }
            _insert(conn, "cart_items", values)

    return get_cart(uid)


def cart_set_qty(user_id: int, product_id: int, qty: int):
    uid = int(user_id)
    pid = int(product_id)

    try:
        quantity = int(qty)
    except (TypeError, ValueError):
        raise ShopError("Некорректное количество")

    if uid <= 0 or pid <= 0:
        raise ShopError("Некорректные данные")

    if quantity < 0 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество")

    now = int(time.time())

    with db.tx() as conn:
        if quantity == 0:
            conn.execute(
                """
                DELETE FROM cart_items
                WHERE user_id = ? AND product_id = ?
                """,
                (uid, pid),
            )
        else:
            product = conn.execute(
                """
                SELECT p.id, p.is_active, p.active
                FROM products p
                WHERE p.id = ?
                """,
                (pid,),
            ).fetchone()

            if not product or not _product_active(product):
                raise ShopError("Товар недоступен")

            row = conn.execute(
                """
                SELECT 1 FROM cart_items
                WHERE user_id = ? AND product_id = ?
                """,
                (uid, pid),
            ).fetchone()

            if row:
                columns = _columns(conn, "cart_items")
                if "updated_at" in columns:
                    conn.execute(
                        """
                        UPDATE cart_items
                        SET qty = ?, updated_at = ?
                        WHERE user_id = ? AND product_id = ?
                        """,
                        (quantity, now, uid, pid),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE cart_items
                        SET qty = ?
                        WHERE user_id = ? AND product_id = ?
                        """,
                        (quantity, uid, pid),
                    )
            else:
                _insert(
                    conn,
                    "cart_items",
                    {
                        "user_id": uid,
                        "product_id": pid,
                        "qty": quantity,
                        "created_at": now,
                        "updated_at": now,
                    },
                )

    return get_cart(uid)


def cart_clear(user_id: int):
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM cart_items WHERE user_id = ?",
            (int(user_id),),
        )


def create_wallet_order(
    user_id: int,
    product_id: int,
    qty: int,
    recipient: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    uid = int(user_id)
    pid = int(product_id)

    try:
        quantity = int(qty)
    except (TypeError, ValueError):
        raise ShopError("Некорректное количество товара")

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

    order_key = _normalize_idempotency_key(uid, idempotency_key)

    with db.tx() as conn:
        order_columns = _columns(conn, "wallet_orders")
        key_column = _first(
            order_columns,
            "idempotency_key",
            "order_uid",
        )

        if key_column:
            existing = conn.execute(
                f'SELECT * FROM wallet_orders WHERE "{key_column}" = ?',
                (order_key,),
            ).fetchone()

            if existing:
                if (
                    int(existing["user_id"]) != uid
                    or int(existing["product_id"]) != pid
                    or int(existing["qty"]) != quantity
                    or str(existing["recipient"]) != recipient
                ):
                    raise ShopError(
                        "Ключ заказа уже использован с другими данными"
                    )
                return dict(existing)

        product = conn.execute(
            """
            SELECT p.*,
                   COALESCE(p.is_active, p.active, 1) AS is_active,
                   c.slug AS category_slug
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.id = ?
              AND COALESCE(p.is_active, p.active, 1) = 1
              AND COALESCE(c.is_active, c.active, 1) = 1
            """,
            (pid,),
        ).fetchone()

        if not product:
            raise ShopError("Товар не найден или недоступен")

        unit_price = int(product["price_uzs"])
        total = unit_price * quantity

        if unit_price <= 0 or total <= 0:
            raise ShopError("Некорректная цена товара")

        wallet = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (uid,),
        ).fetchone()

        if not wallet:
            raise ShopError("Кошелёк пользователя не найден")

        balance = int(wallet["balance_uzs"])
        bonus = int(wallet["bonus_uzs"])

        if balance < 0 or bonus < 0:
            raise ShopError("Обнаружена некорректная сумма кошелька")

        bonus_used = min(bonus, total // 2)
        balance_used = total - bonus_used

        if balance < balance_used:
            raise ShopError("Недостаточно средств на балансе")

        now = int(time.time())
        new_balance = balance - balance_used
        new_bonus = bonus - bonus_used

        conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?, bonus_uzs = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (new_balance, new_bonus, now, uid),
        )

        status = (
            "processing"
            if product["fulfillment_mode"] == "manual"
            else "paid"
        )

        order_values = {
            "user_id": uid,
            "product_id": pid,
            "product_title": product["title"],
            "sku": product["sku"] if "sku" in product.keys() else "",
            "category_slug": product["category_slug"],
            "fulfillment_mode": product["fulfillment_mode"],
            "recipient": recipient,
            "qty": quantity,
            "unit_price_uzs": unit_price,
            "total_price_uzs": total,
            "total_uzs": total,
            "bonus_used_uzs": bonus_used,
            "paid_from_bonus_uzs": bonus_used,
            "balance_used_uzs": balance_used,
            "paid_from_balance_uzs": balance_used,
            "status": status,
            "idempotency_key": order_key,
            "order_uid": order_key,
            "refunded": 0,
            "created_at": now,
            "updated_at": now,
        }

        cur = _insert(conn, "wallet_orders", order_values)
        order_id = cur.lastrowid

        transaction_columns = _columns(conn, "wallet_transactions")

        transaction_values = {
            "user_id": uid,
            "transaction_type": "purchase",
            "kind": "purchase",
            "amount_uzs": -total,
            "balance_delta": -balance_used,
            "bonus_delta": -bonus_used,
            "bonus_delta_uzs": -bonus_used,
            "balance_after": new_balance,
            "balance_after_uzs": new_balance,
            "bonus_after": new_bonus,
            "bonus_after_uzs": new_bonus,
            "reference_type": "wallet_order",
            "ref_type": "wallet_order",
            "reference_id": order_id,
            "ref_id": order_id,
            "description": f"Покупка: {product['title']}",
            "note": f"Покупка: {product['title']}",
            "created_at": now,
        }

        _insert(conn, "wallet_transactions", transaction_values)

        order = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    return dict(order)


def list_wallet_orders(user_id: int, limit: int = 30) -> list:
    limit, _ = _page(limit)

    return db.q_all(
        """
        SELECT * FROM wallet_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), limit),
    )


def get_wallet_order(order_id: int):
    return db.q_one(
        "SELECT * FROM wallet_orders WHERE id = ?",
        (int(order_id),),
    )


def refund_wallet_order_once(
    order_id: int,
    admin_id: int,
    reason: str = "",
) -> dict:
    oid = int(order_id)
    aid = int(admin_id)

    if oid <= 0 or aid <= 0:
        raise ShopError("Некорректные данные возврата")

    with db.tx() as conn:
        columns = _columns(conn, "wallet_orders")
        order = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

        if not order:
            raise ShopError("Заказ не найден")

        refunded_column = _first(columns, "refunded")
        refunded = (
            bool(order[refunded_column])
            if refunded_column
            else order["status"] == "refunded"
        )

        if refunded:
            return {
                "order": dict(order),
                "already_refunded": True,
            }

        if order["status"] == "delivered":
            raise ShopError(
                "Нельзя вернуть средства за уже выданный заказ"
            )

        uid = int(order["user_id"])
        wallet = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (uid,),
        ).fetchone()

        if not wallet:
            raise ShopError("Кошелёк пользователя не найден")

        balance_refund = int(
            order[_first(columns, "balance_used_uzs", "paid_from_balance_uzs")]
            or 0
        )
        bonus_refund = int(
            order[_first(columns, "bonus_used_uzs", "paid_from_bonus_uzs")]
            or 0
        )

        now = int(time.time())
        balance = int(wallet["balance_uzs"]) + balance_refund
        bonus = int(wallet["bonus_uzs"]) + bonus_refund

        updates = {
            "status": "refunded",
            "admin_id": aid,
            "updated_at": now,
        }

        if refunded_column:
            updates[refunded_column] = 1

        assignments = ", ".join(
            f'"{key}" = ?'
            for key in updates
            if key in columns
        )
        values = [
            value
            for key, value in updates.items()
            if key in columns
        ]

        if not assignments:
            raise ShopError("Структура заказа не поддерживает возврат")

        updated = conn.execute(
            f"""
            UPDATE wallet_orders
            SET {assignments}
            WHERE id = ? AND status != 'delivered'
            """,
            (*values, oid),
        )

        if updated.rowcount != 1:
            raise ShopError("Заказ уже обработан")

        conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = ?, bonus_uzs = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (balance, bonus, now, uid),
        )

        _insert(
            conn,
            "wallet_transactions",
            {
                "user_id": uid,
                "transaction_type": "refund",
                "kind": "refund",
                "amount_uzs": balance_refund + bonus_refund,
                "balance_delta": balance_refund,
                "bonus_delta": bonus_refund,
                "bonus_delta_uzs": bonus_refund,
                "balance_after": balance,
                "balance_after_uzs": balance,
                "bonus_after": bonus,
                "bonus_after_uzs": bonus,
                "reference_type": "wallet_order",
                "ref_type": "wallet_order",
                "reference_id": oid,
                "ref_id": oid,
                "description": str(reason or "Возврат заказа")[:300],
                "note": str(reason or "Возврат заказа")[:300],
                "created_at": now,
            },
        )

        _insert(
            conn,
            "admin_audit_log",
            {
                "admin_id": aid,
                "action": "refund_order",
                "target_type": "wallet_order",
                "target_id": oid,
                "details": str(reason or "")[:300],
                "created_at": now,
            },
        )

        updated_order = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

    return {
        "order": dict(updated_order),
        "already_refunded": False,
    }


def mark_wallet_order_delivered(
    order_id: int,
    admin_id: int,
) -> dict:
    oid = int(order_id)
    aid = int(admin_id)

    if oid <= 0 or aid <= 0:
        raise ShopError("Некорректные данные")

    with db.tx() as conn:
        order = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

        if not order:
            raise ShopError("Заказ не найден")

        if order["status"] == "delivered":
            raise ShopError("Заказ уже выдан")

        if order["status"] not in ("paid", "processing"):
            raise ShopError("Заказ нельзя выдать в текущем статусе")

        now = int(time.time())
        columns = _columns(conn, "wallet_orders")

        updates = {
            "status": "delivered",
            "admin_id": aid,
            "updated_at": now,
        }

        selected = {
            key: value
            for key, value in updates.items()
            if key in columns
        }

        assignments = ", ".join(
            f'"{key}" = ?'
            for key in selected
        )

        updated = conn.execute(
            f"""
            UPDATE wallet_orders
            SET {assignments}
            WHERE id = ? AND status IN ('paid', 'processing')
            """,
            (*selected.values(), oid),
        )

        if updated.rowcount != 1:
            raise ShopError("Не удалось обновить заказ")

        _insert(
            conn,
            "admin_audit_log",
            {
                "admin_id": aid,
                "action": "deliver_order",
                "target_type": "wallet_order",
                "target_id": oid,
                "details": "",
                "created_at": now,
            },
        )

        result = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

    return dict(result)


# Старый поток заказов Telegram Stars.

def count_orders(user_id: int) -> int:
    row = db.q_one(
        """
        SELECT COUNT(*) AS total
        FROM legacy_orders
        WHERE user_id = ?
        """,
        (int(user_id),),
    )
    return int(row["total"]) if row else 0


def list_orders(user_id: int, limit: int = 20) -> list:
    limit, _ = _page(limit)

    return db.q_all(
        """
        SELECT * FROM legacy_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), limit),
    )


def get_order(order_id: int):
    return db.q_one(
        "SELECT * FROM legacy_orders WHERE id = ?",
        (int(order_id),),
    )


def create_order(user_id: int, recipient: str) -> dict:
    uid = int(user_id)

    if uid <= 0:
        raise ShopError("Некорректный пользователь")

    try:
        recipient = normalize_recipient(recipient)
    except ValueError as exc:
        raise ShopError(str(exc)) from exc

    if not recipient:
        raise ShopError("Укажите получателя")

    with db.tx() as conn:
        items = conn.execute(
            """
            SELECT ci.product_id, ci.qty,
                   p.title, p.price_uzs,
                   p.fulfillment_mode,
                   COALESCE(p.is_active, p.active, 1) AS is_active,
                   c.slug AS category_slug
            FROM cart_items ci
            JOIN products p ON p.id = ci.product_id
            JOIN categories c ON c.id = p.category_id
            WHERE ci.user_id = ?
              AND COALESCE(p.is_active, p.active, 1) = 1
              AND COALESCE(c.is_active, c.active, 1) = 1
            """,
            (uid,),
        ).fetchall()

        if not items:
            raise ShopError("Корзина пуста")

        if any(item["category_slug"] != "other" for item in items):
            raise ShopError(
                "Оплата Telegram Stars разрешена только для категории «Другие товары»"
            )

        if any(item["fulfillment_mode"] != "xtr" for item in items):
            raise ShopError(
                "Один или несколько товаров не поддерживают оплату Telegram Stars"
            )

        if any(
            int(item["qty"]) < 1
            or int(item["qty"]) > MAX_CART_QTY
            or int(item["price_uzs"]) <= 0
            for item in items
        ):
            raise ShopError("В корзине обнаружен некорректный товар")

        total = sum(
            int(item["price_uzs"]) * int(item["qty"])
            for item in items
        )

        if total <= 0:
            raise ShopError("Некорректная сумма заказа")

        now = int(time.time())

        order_cur = _insert(
            conn,
            "legacy_orders",
            {
                "user_id": uid,
                "recipient": recipient,
                "total_uzs": total,
                "total_amount": total,
                "currency": "XTR",
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            },
        )

        order_id = order_cur.lastrowid

        for item in items:
            _insert(
                conn,
                "legacy_order_items",
                {
                    "order_id": order_id,
                    "product_id": int(item["product_id"]),
                    "product_title": item["title"],
                    "title": item["title"],
                    "unit_price_uzs": int(item["price_uzs"]),
                    "price": int(item["price_uzs"]),
                    "qty": int(item["qty"]),
                },
            )

        conn.execute(
            "DELETE FROM cart_items WHERE user_id = ?",
            (uid,),
        )

        order = conn.execute(
            "SELECT * FROM legacy_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    return dict(order)


def cancel_order(order_id: int, user_id: int) -> bool:
    with db.tx() as conn:
        updated = conn.execute(
            """
            UPDATE legacy_orders
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'pending'
            """,
            (int(time.time()), int(order_id), int(user_id)),
        )
        return updated.rowcount == 1