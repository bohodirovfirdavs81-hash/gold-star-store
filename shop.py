
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
    return (
        max(1, min(int(limit), MAX_PAGE_SIZE)),
        max(0, int(offset)),
    )


def list_categories() -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT id, slug, title, description, sort_order, is_active
        FROM categories
        WHERE is_active = 1
        ORDER BY sort_order, id
        """
    )


def get_category(category_id: int):
    return db.q_one(
        "SELECT * FROM categories WHERE id = ?",
        (int(category_id),),
    )


def list_products(
    category_slug: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit, offset = _page(limit, offset)

    sql = """
        SELECT p.id, p.category_id, p.sku, p.title,
               p.description, p.price_uzs, p.fulfillment_mode,
               p.is_active, p.sort_order,
               c.slug AS category_slug,
               c.title AS category_title
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.is_active = 1 AND c.is_active = 1
    """
    params = []

    if category_slug:
        sql += " AND c.slug = ?"
        params.append(category_slug)

    sql += " ORDER BY p.sort_order, p.id LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    return db.q_all(sql, tuple(params))


def get_product(product_id: int):
    return db.q_one(
        """
        SELECT p.*, c.slug AS category_slug,
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
               p.is_active
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
    quantity = int(qty)

    if quantity < 1 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество товара")

    product = get_product(pid)
    if not product or not product["is_active"]:
        raise ShopError("Товар недоступен")

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

        conn.execute(
            """
            INSERT INTO cart_items (user_id, product_id, qty)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, product_id)
            DO UPDATE SET qty = excluded.qty
            """,
            (uid, pid, new_qty),
        )

    return get_cart(uid)


def cart_set_qty(user_id: int, product_id: int, qty: int):
    uid = int(user_id)
    pid = int(product_id)
    quantity = int(qty)

    if quantity < 0 or quantity > MAX_CART_QTY:
        raise ShopError("Некорректное количество")

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
            conn.execute(
                """
                INSERT INTO cart_items (user_id, product_id, qty)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, product_id)
                DO UPDATE SET qty = excluded.qty
                """,
                (uid, pid, quantity),
            )

    return get_cart(uid)


def cart_clear(user_id: int):
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM cart_items WHERE user_id = ?",
            (int(user_id),),
        )


def _normalize_idempotency_key(user_id: int, key: str) -> str:
    raw = str(key or "").strip()

    if not raw:
        raw = uuid.uuid4().hex

    if len(raw) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raw = hashlib.sha256(raw.encode()).hexdigest()

    return f"u{int(user_id)}_{raw}"


def create_wallet_order(
    user_id: int,
    product_id: int,
    qty: int,
    recipient: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    uid = int(user_id)
    pid = int(product_id)
    quantity = int(qty)

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

    order_uid = _normalize_idempotency_key(uid, idempotency_key)

    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM wallet_orders WHERE order_uid = ?",
            (order_uid,),
        ).fetchone()

        if existing:
            if (
                int(existing["user_id"]) != uid
                or int(existing["product_id"]) != pid
                or int(existing["qty"]) != quantity
                or existing["recipient"] != recipient
            ):
                raise ShopError("Ключ заказа уже использован с другими данными")
            return dict(existing)

        product = conn.execute(
            """
            SELECT p.*, c.slug AS category_slug
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.id = ? AND p.is_active = 1 AND c.is_active = 1
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

        cur = conn.execute(
            """
            INSERT INTO wallet_orders (
                order_uid, user_id, product_id, sku,
                product_title, category_slug, fulfillment_mode,
                recipient, qty, unit_price_uzs, total_uzs,
                paid_from_balance_uzs, paid_from_bonus_uzs,
                status, refunded, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                order_uid, uid, pid, product["sku"],
                product["title"], product["category_slug"],
                product["fulfillment_mode"], recipient,
                quantity, unit_price, total, balance_used,
                bonus_used, status, now, now,
            ),
        )

        order_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, kind, amount_uzs, bonus_delta_uzs,
                balance_after_uzs, bonus_after_uzs,
                ref_type, ref_id, note, created_at
            )
            VALUES (?, 'purchase', ?, ?, ?, ?, 'wallet_order', ?, ?, ?)
            """,
            (
                uid, -balance_used, -bonus_used,
                new_balance, new_bonus, order_id,
                f"Покупка: {product['title']}", now,
            ),
        )

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

    with db.tx() as conn:
        order = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

        if not order:
            raise ShopError("Заказ не найден")

        if int(order["refunded"]):
            return {"order": dict(order), "already_refunded": True}

        if order["status"] == "delivered":
            raise ShopError("Нельзя вернуть средства за уже выданный заказ")

        uid = int(order["user_id"])
        wallet = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ?",
            (uid,),
        ).fetchone()

        if not wallet:
            raise ShopError("Кошелёк пользователя не найден")

        balance = int(wallet["balance_uzs"]) + int(order["paid_from_balance_uzs"])
        bonus = int(wallet["bonus_uzs"]) + int(order["paid_from_bonus_uzs"])
        now = int(time.time())

        updated = conn.execute(
            """
            UPDATE wallet_orders
            SET status = 'refunded',
                refunded = 1,
                admin_id = ?,
                updated_at = ?
            WHERE id = ? AND refunded = 0
              AND status != 'delivered'
            """,
            (aid, now, oid),
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

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, kind, amount_uzs, bonus_delta_uzs,
                balance_after_uzs, bonus_after_uzs,
                ref_type, ref_id, note, created_at
            )
            VALUES (?, 'refund', ?, ?, ?, ?, 'wallet_order', ?, ?, ?)
            """,
            (
                uid,
                int(order["paid_from_balance_uzs"]),
                int(order["paid_from_bonus_uzs"]),
                balance,
                bonus,
                oid,
                str(reason or "Возврат заказа")[:300],
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO admin_audit_log (
                admin_id, action, target_type, target_id,
                details, created_at
            )
            VALUES (?, 'refund_order', 'wallet_order', ?, ?, ?)
            """,
            (aid, oid, str(reason or "")[:300], now),
        )

        updated_order = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

    return {"order": dict(updated_order), "already_refunded": False}


def mark_wallet_order_delivered(order_id: int, admin_id: int) -> dict:
    oid = int(order_id)
    aid = int(admin_id)

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

        updated = conn.execute(
            """
            UPDATE wallet_orders
            SET status = 'delivered',
                admin_id = ?,
                updated_at = ?
            WHERE id = ? AND status IN ('paid', 'processing')
            """,
            (aid, now, oid),
        )

        if updated.rowcount != 1:
            raise ShopError("Не удалось обновить заказ")

        conn.execute(
            """
            INSERT INTO admin_audit_log (
                admin_id, action, target_type, target_id,
                details, created_at
            )
            VALUES (?, 'deliver_order', 'wallet_order', ?, '', ?)
            """,
            (aid, oid, now),
        )

        result = conn.execute(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (oid,),
        ).fetchone()

    return dict(result)


# Legacy orders used by the Telegram Stars flow.

def count_orders(user_id: int) -> int:
    row = db.q_one(
        "SELECT COUNT(*) AS total FROM legacy_orders WHERE user_id = ?",
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
                   p.is_active, c.slug AS category_slug
            FROM cart_items ci
            JOIN products p ON p.id = ci.product_id
            JOIN categories c ON c.id = p.category_id
            WHERE ci.user_id = ? AND p.is_active = 1 AND c.is_active = 1
            """,
            (uid,),
        ).fetchall()

        if not items:
            raise ShopError("Корзина пуста")

        if any(item["category_slug"] != "other" for item in items):
            raise ShopError(
                "Оплата Telegram Stars разрешена только для категории «Другие товары»"
            )

        total = sum(
            int(item["price_uzs"]) * int(item["qty"])
            for item in items
        )

        if total <= 0:
            raise ShopError("Некорректная сумма заказа")

        now = int(time.time())

        cur = conn.execute(
            """
            INSERT INTO legacy_orders (
                user_id, recipient, total_amount,
                currency, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'XTR', 'pending', ?, ?)
            """,
            (uid, recipient, total, now, now),
        )

        order_id = cur.lastrowid

        for item in items:
            conn.execute(
                """
                INSERT INTO legacy_order_items (
                    order_id, product_id, title, price, qty
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["product_id"],
                    item["title"],
                    item["price_uzs"],
                    item["qty"],
                ),
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
