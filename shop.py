import hashlib
import time
import uuid
from typing import Any

import database as db
from utils import normalize_recipient, uzs


class ShopError(Exception):
    pass


def list_categories() -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT id, slug, title, description, sort_order, is_active
        FROM categories
        WHERE is_active = 1
        ORDER BY sort_order ASC, id ASC
        """
    )


def get_category(category_id: int) -> dict[str, Any] | None:
    return db.q_one("SELECT * FROM categories WHERE id = ?", (int(category_id),))


def list_products(category_id: int | None = None, category_slug: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT
            p.id,
            p.category_id,
            c.slug AS category_slug,
            c.title AS category_title,
            p.sku,
            p.title,
            p.description,
            p.price_uzs,
            p.fulfillment_mode,
            p.is_active,
            p.sort_order
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.is_active = 1 AND c.is_active = 1
    """
    params: list[Any] = []
    if category_id is not None:
        sql += " AND p.category_id = ?"
        params.append(int(category_id))
    if category_slug:
        sql += " AND c.slug = ?"
        params.append(category_slug.strip().lower())
    sql += " ORDER BY c.sort_order ASC, p.sort_order ASC, p.id ASC"
    return db.q_all(sql, tuple(params))


def get_product(product_id: int, active_only: bool = True) -> dict[str, Any] | None:
    sql = """
        SELECT
            p.*,
            c.slug AS category_slug,
            c.title AS category_title
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.id = ?
    """
    params: list[Any] = [int(product_id)]
    if active_only:
        sql += " AND p.is_active = 1 AND c.is_active = 1"
    return db.q_one(sql, tuple(params))


def get_cart(user_id: int) -> dict[str, Any]:
    rows = db.q_all(
        """
        SELECT
            ci.product_id,
            ci.qty,
            p.title,
            p.price_uzs,
            p.is_active
        FROM cart_items ci
        JOIN products p ON p.id = ci.product_id
        WHERE ci.user_id = ?
        ORDER BY p.id ASC
        """,
        (int(user_id),),
    )
    items: list[dict[str, Any]] = []
    total = 0
    count = 0
    for r in rows:
        qty = int(r["qty"])
        price = int(r["price_uzs"])
        avail = bool(r["is_active"])
        subtotal = price * qty
        if avail:
            total += subtotal
            count += qty
        items.append(
            {
                "product_id": int(r["product_id"]),
                "title": r["title"],
                "price": price,
                "qty": qty,
                "subtotal": subtotal,
                "available": avail,
            }
        )
    return {"items": items, "total": total, "count": count}


def cart_add(user_id: int, product_id: int, delta: int = 1) -> int:
    prod = get_product(product_id, active_only=True)
    if not prod:
        raise ShopError("Товар недоступен")
    with db.tx() as conn:
        db.ensure_wallet(conn, int(user_id))
        cur = conn.execute(
            "SELECT qty FROM cart_items WHERE user_id = ? AND product_id = ?",
            (int(user_id), int(product_id)),
        ).fetchone()
        new_qty = (int(cur["qty"]) if cur else 0) + int(delta)
        if new_qty <= 0:
            conn.execute(
                "DELETE FROM cart_items WHERE user_id = ? AND product_id = ?",
                (int(user_id), int(product_id)),
            )
            return 0
        conn.execute(
            """
            INSERT INTO cart_items (user_id, product_id, qty) VALUES (?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET qty = excluded.qty
            """,
            (int(user_id), int(product_id), new_qty),
        )
        return new_qty


def cart_set_qty(user_id: int, product_id: int, qty: int) -> int:
    if qty <= 0:
        with db.tx() as conn:
            conn.execute(
                "DELETE FROM cart_items WHERE user_id = ? AND product_id = ?",
                (int(user_id), int(product_id)),
            )
        return 0
    prod = get_product(product_id, active_only=True)
    if not prod:
        raise ShopError("Товар недоступен")
    with db.tx() as conn:
        conn.execute(
            """
            INSERT INTO cart_items (user_id, product_id, qty) VALUES (?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET qty = excluded.qty
            """,
            (int(user_id), int(product_id), int(qty)),
        )
    return int(qty)


def cart_clear(user_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM cart_items WHERE user_id = ?", (int(user_id),))


def _normalize_idempotency_key(user_id: int, raw_key: str | None) -> str:
    cleaned = (raw_key or "").strip()
    if not cleaned:
        return f"ord_{uuid.uuid4().hex}"
    # Привязываем ключ к user_id, чтобы чужой ключ не мог вернуть чужой заказ
    digest = hashlib.sha256(f"{int(user_id)}:{cleaned}".encode("utf-8")).hexdigest()[:32]
    return f"u{int(user_id)}_{digest}"


def create_wallet_order(
    user_id: int,
    product_id: int,
    qty: int = 1,
    recipient: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    uid = int(user_id)
    pid = int(product_id)
    qty_int = int(qty)
    if qty_int <= 0 or qty_int > 50:
        raise ShopError("Некорректное количество товара (допустимо от 1 до 50)")

    clean_recipient = normalize_recipient(recipient or "")
    if not clean_recipient:
        raise ShopError("Укажите корректный @username получателя (5–32 символа)")

    order_uid = _normalize_idempotency_key(uid, idempotency_key)
    now = int(time.time())

    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM wallet_orders WHERE order_uid = ?",
            (order_uid,),
        ).fetchone()
        if existing:
            if int(existing["user_id"]) != uid:
                raise ShopError("Конфликт идентификатора заказа")
            return dict(existing)

        prod = conn.execute(
            """
            SELECT p.*, c.slug AS category_slug
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.id = ? AND p.is_active = 1 AND c.is_active = 1
            """,
            (pid,),
        ).fetchone()
        if not prod:
            raise ShopError("Товар не найден или недоступен")

        unit_price = int(prod["price_uzs"])
        if unit_price <= 0:
            raise ShopError("Некорректная цена товара")
        total_uzs = unit_price * qty_int

        wallet = db.ensure_wallet(conn, uid)
        bal = int(wallet["balance_uzs"])
        bonus = int(wallet["bonus_uzs"])

        # Бонусами разрешено оплачивать не более 50% суммы заказа
        max_bonus_allowed = total_uzs // 2
        from_bonus = min(bonus, max_bonus_allowed, total_uzs)
        from_main = total_uzs - from_bonus

        if bal < from_main or bonus < from_bonus:
            raise ShopError(
                f"Недостаточно средств на балансе. Требуется {uzs(total_uzs)}, доступно {uzs(bal)} (бонусы: {uzs(bonus)})"
            )

        new_bal = bal - from_main
        new_bonus = bonus - from_bonus
        if new_bal < 0 or new_bonus < 0:
            raise ShopError("Недостаточно средств")

        upd = conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = balance_uzs - ?,
                bonus_uzs = bonus_uzs - ?,
                updated_at = ?
            WHERE user_id = ?
              AND balance_uzs >= ?
              AND bonus_uzs >= ?
            """,
            (from_main, from_bonus, now, uid, from_main, from_bonus),
        )
        if upd.rowcount != 1:
            raise ShopError("Недостаточно средств или конфликт обновления баланса")

        initial_status = "processing" if prod["fulfillment_mode"] == "manual" else "paid"
        cur = conn.execute(
            """
            INSERT INTO wallet_orders (
                order_uid, user_id, product_id, sku, product_title, category_slug,
                fulfillment_mode, recipient, qty, unit_price_uzs, total_uzs,
                paid_from_balance_uzs, paid_from_bonus_uzs, status, refunded,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                order_uid,
                uid,
                pid,
                prod["sku"],
                prod["title"],
                prod["category_slug"],
                prod["fulfillment_mode"],
                clean_recipient,
                qty_int,
                unit_price,
                total_uzs,
                from_main,
                from_bonus,
                initial_status,
                now,
                now,
            ),
        )
        order_id = int(cur.lastrowid)

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, kind, amount_uzs, bonus_delta_uzs,
                balance_after_uzs, bonus_after_uzs, ref_type, ref_id, note, created_at
            ) VALUES (?, 'purchase', ?, ?, ?, ?, 'wallet_order', ?, ?, ?)
            """,
            (
                uid,
                -from_main,
                -from_bonus,
                new_bal,
                new_bonus,
                str(order_id),
                f"Покупка: {prod['title']} × {qty_int} ({clean_recipient})",
                now,
            ),
        )

        row = conn.execute("SELECT * FROM wallet_orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row)


def list_wallet_orders(user_id: int, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT * FROM wallet_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (int(user_id), int(limit), int(offset)),
    )


def get_wallet_order(order_id: int) -> dict[str, Any] | None:
    return db.q_one("SELECT * FROM wallet_orders WHERE id = ?", (int(order_id),))


def refund_wallet_order_once(order_id: int, admin_id: int | None = None, reason: str = "Отмена заказа") -> dict[str, Any]:
    """Возвращает средства по заказу строго один раз."""
    now = int(time.time())
    with db.tx() as conn:
        order = conn.execute("SELECT * FROM wallet_orders WHERE id = ?", (int(order_id),)).fetchone()
        if not order:
            raise ShopError("Заказ не найден")
        if int(order["refunded"]) == 1 or order["status"] in ("cancelled", "refunded"):
            return {"already_refunded": True, "order": dict(order)}

        upd_order = conn.execute(
            """
            UPDATE wallet_orders
            SET status = 'cancelled', refunded = 1, admin_id = COALESCE(?, admin_id), updated_at = ?
            WHERE id = ? AND refunded = 0 AND status NOT IN ('cancelled', 'refunded')
            """,
            (admin_id, now, int(order_id)),
        )
        if upd_order.rowcount != 1:
            latest = conn.execute("SELECT * FROM wallet_orders WHERE id = ?", (int(order_id),)).fetchone()
            return {"already_refunded": True, "order": dict(latest)}

        uid = int(order["user_id"])
        refund_main = int(order["paid_from_balance_uzs"])
        refund_bonus = int(order["paid_from_bonus_uzs"])

        wallet = db.ensure_wallet(conn, uid)
        new_bal = int(wallet["balance_uzs"]) + refund_main
        new_bonus = int(wallet["bonus_uzs"]) + refund_bonus

        conn.execute(
            """
            UPDATE wallets
            SET balance_uzs = balance_uzs + ?,
                bonus_uzs = bonus_uzs + ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (refund_main, refund_bonus, now, uid),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, kind, amount_uzs, bonus_delta_uzs,
                balance_after_uzs, bonus_after_uzs, ref_type, ref_id, note, created_at
            ) VALUES (?, 'refund', ?, ?, ?, ?, 'wallet_order', ?, ?, ?)
            """,
            (
                uid,
                refund_main,
                refund_bonus,
                new_bal,
                new_bonus,
                str(order_id),
                f"Возврат за заказ #{order_id}: {reason}",
                now,
            ),
        )
        if admin_id is not None:
            conn.execute(
                """
                INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, details, created_at)
                VALUES (?, 'cancel_refund_order', 'wallet_order', ?, ?, ?)
                """,
                (int(admin_id), str(order_id), reason, now),
            )
        updated = conn.execute("SELECT * FROM wallet_orders WHERE id = ?", (int(order_id),)).fetchone()
        return {"already_refunded": False, "order": dict(updated)}


def mark_wallet_order_delivered(order_id: int, admin_id: int) -> dict[str, Any]:
    now = int(time.time())
    with db.tx() as conn:
        order = conn.execute("SELECT * FROM wallet_orders WHERE id = ?", (int(order_id),)).fetchone()
        if not order:
            raise ShopError("Заказ не найден")
        if int(order["refunded"]) == 1 or order["status"] in ("cancelled", "refunded"):
            raise ShopError("Нельзя выполнить отменённый заказ")
        if order["status"] == "delivered":
            return dict(order)

        conn.execute(
            """
            UPDATE wallet_orders
            SET status = 'delivered', admin_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(admin_id), now, int(order_id)),
        )
        conn.execute(
            """
            INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, details, created_at)
            VALUES (?, 'deliver_order', 'wallet_order', ?, 'Заказ отмечен выполненным', ?)
            """,
            (int(admin_id), str(order_id), now),
        )
        updated = conn.execute("SELECT * FROM wallet_orders WHERE id = ?", (int(order_id),)).fetchone()
        return dict(updated)


# --- Совместимость со старыми заказами в БД ---
def count_orders(user_id: int) -> int:
    row = db.q_one("SELECT COUNT(*) AS c FROM legacy_orders WHERE user_id = ?", (int(user_id),))
    return int(row["c"]) if row else 0


def list_orders(user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    return db.q_all(
        """
        SELECT id, user_id, recipient, total_amount AS total, currency, status, created_at, updated_at
        FROM legacy_orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (int(user_id), int(limit), int(offset)),
    )


def get_order(order_id: int, user_id: int | None = None) -> dict[str, Any] | None:
    sql = """
        SELECT id, user_id, recipient, total_amount AS total, currency, status, created_at, updated_at
        FROM legacy_orders
        WHERE id = ?
    """
    params: list[Any] = [int(order_id)]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(int(user_id))
    order = db.q_one(sql, tuple(params))
    if not order:
        return None
    order["items"] = db.q_all(
        "SELECT id, product_id, title, price, qty FROM legacy_order_items WHERE order_id = ?",
        (int(order_id),),
    )
    return order


def create_order(user_id: int, recipient: str) -> dict[str, Any]:
    clean_rec = normalize_recipient(recipient)
    if not clean_rec:
        raise ShopError("Некорректный @username получателя")
    now = int(time.time())
    with db.tx() as conn:
        rows = conn.execute(
            """
            SELECT ci.product_id, ci.qty, p.title, p.price_uzs, p.is_active
            FROM cart_items ci
            JOIN products p ON p.id = ci.product_id
            WHERE ci.user_id = ?
            """,
            (int(user_id),),
        ).fetchall()
        if not rows:
            raise ShopError("Корзина пуста")
        total = 0
        for r in rows:
            if not r["is_active"]:
                raise ShopError(f"Товар «{r['title']}» больше недоступен")
            total += int(r["price_uzs"]) * int(r["qty"])
        cur = conn.execute(
            """
            INSERT INTO legacy_orders (user_id, recipient, total_amount, currency, status, created_at, updated_at)
            VALUES (?, ?, ?, 'UZS', 'pending', ?, ?)
            """,
            (int(user_id), clean_rec, total, now, now),
        )
        oid = int(cur.lastrowid)
        for r in rows:
            conn.execute(
                """
                INSERT INTO legacy_order_items (order_id, product_id, title, price, qty)
                VALUES (?, ?, ?, ?, ?)
                """,
                (oid, int(r["product_id"]), r["title"], int(r["price_uzs"]), int(r["qty"])),
            )
        conn.execute("DELETE FROM cart_items WHERE user_id = ?", (int(user_id),))
    return get_order(oid, user_id)  # type: ignore[return-value]


def cancel_order(order_id: int, user_id: int) -> bool:
    now = int(time.time())
    with db.tx() as conn:
        row = conn.execute(
            "SELECT status FROM legacy_orders WHERE id = ? AND user_id = ?",
            (int(order_id), int(user_id)),
        ).fetchone()
        if not row or row["status"] != "pending":
            return False
        conn.execute(
            "UPDATE legacy_orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (now, int(order_id)),
        )
        return True