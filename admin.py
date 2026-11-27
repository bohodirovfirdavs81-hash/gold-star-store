# -*- coding: utf-8 -*-
"""
Gold Star Store — административная панель.
"""

from typing import Any

import config
import database as db
import shop
import payments


# --------------------------------------------------
# ДОСТУП
# --------------------------------------------------

def is_admin(user_id: Any) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False

    if uid <= 0:
        return False

    admin_ids = getattr(config, "ADMIN_IDS", set())

    try:
        return uid in {int(x) for x in admin_ids}
    except (TypeError, ValueError):
        return False


def require_admin(user_id: int) -> None:
    if not is_admin(user_id):
        raise PermissionError("Недостаточно прав администратора")


def _admin_action(
    admin_id: int,
    action: str,
    target_type: str = "",
    target_id: Any = "",
    details: Any = "",
) -> None:
    require_admin(admin_id)

    db.record_admin_action(
        admin_id,
        action,
        target_type or None,
        str(target_id) if target_id else None,
        details,
    )


def _dict(row):
    return dict(row) if row is not None else None


# --------------------------------------------------
# СТАТИСТИКА
# --------------------------------------------------

def get_stats(admin_id: int) -> dict:
    require_admin(admin_id)

    users = db.fetchone(
        "SELECT COUNT(*) AS total FROM users"
    )["total"]

    products = db.fetchone(
        "SELECT COUNT(*) AS total FROM products WHERE active = 1"
    )["total"]

    pending_topups = db.fetchone(
        "SELECT COUNT(*) AS total FROM topups WHERE status = 'pending'"
    )["total"]

    pending_orders = db.fetchone(
        "SELECT COUNT(*) AS total FROM wallet_orders WHERE status = 'pending'"
    )["total"]

    paid_orders = db.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM wallet_orders
        WHERE status IN ('paid', 'processing', 'delivered')
        """
    )["total"]

    topup_total = db.fetchone(
        """
        SELECT COALESCE(SUM(amount_uzs), 0) AS total
        FROM topups
        WHERE status = 'approved'
        """
    )["total"]

    wallet_total = db.fetchone(
        """
        SELECT COALESCE(SUM(balance_uzs), 0) AS total
        FROM wallets
        """
    )["total"]

    bonus_total = db.fetchone(
        """
        SELECT COALESCE(SUM(bonus_uzs), 0) AS total
        FROM wallets
        """
    )["total"]

    return {
        "users": int(users),
        "products": int(products),
        "pending_topups": int(pending_topups),
        "pending_orders": int(pending_orders),
        "paid_orders": int(paid_orders),
        "approved_topups_uzs": int(topup_total),
        "wallet_balance_uzs": int(wallet_total),
        "wallet_bonus_uzs": int(bonus_total),
    }


# --------------------------------------------------
# ПОЛЬЗОВАТЕЛИ
# --------------------------------------------------

def get_users(
    admin_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:

    require_admin(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    rows = db.fetchall(
        """
        SELECT
            u.telegram_id,
            u.username,
            u.first_name,
            u.created_at,
            w.balance_uzs,
            w.bonus_uzs
        FROM users u
        LEFT JOIN wallets w ON w.user_id = u.telegram_id
        ORDER BY u.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )

    return [_dict(row) for row in rows]


def get_user_details(
    admin_id: int,
    user_id: int,
) -> dict:

    require_admin(admin_id)

    user_id = int(user_id)

    user = db.fetchone(
        """
        SELECT
            u.*,
            w.balance_uzs,
            w.bonus_uzs
        FROM users u
        LEFT JOIN wallets w ON w.user_id = u.telegram_id
        WHERE u.telegram_id = ?
        """,
        (user_id,),
    )

    if not user:
        raise ValueError("Пользователь не найден")

    orders = db.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM wallet_orders
        WHERE user_id = ?
        """,
        (user_id,),
    )["total"]

    topups = db.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM topups
        WHERE user_id = ?
        """,
        (user_id,),
    )["total"]

    return {
        "user": _dict(user),
        "orders_count": int(orders),
        "topups_count": int(topups),
    }


# --------------------------------------------------
# ЗАКАЗЫ
# --------------------------------------------------

def get_orders(
    admin_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:

    require_admin(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    sql = """
        SELECT
            o.*,
            u.username,
            u.first_name
        FROM wallet_orders o
        LEFT JOIN users u ON u.telegram_id = o.user_id
        WHERE 1 = 1
    """

    params = []

    if status:
        sql += " AND o.status = ?"
        params.append(str(status))

    sql += " ORDER BY o.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    return [
        _dict(row)
        for row in db.fetchall(sql, tuple(params))
    ]


def get_order_details(
    admin_id: int,
    order_id: int,
) -> dict:

    require_admin(admin_id)

    order = db.fetchone(
        """
        SELECT
            o.*,
            u.username,
            u.first_name
        FROM wallet_orders o
        LEFT JOIN users u ON u.telegram_id = o.user_id
        WHERE o.id = ?
        """,
        (int(order_id),),
    )

    if not order:
        raise ValueError("Заказ не найден")

    return _dict(order)


def change_order_status(
    admin_id: int,
    order_id: int,
    status: str,
    comment: str = "",
) -> dict:

    require_admin(admin_id)

    order_id = int(order_id)

    allowed = {
        "pending",
        "paid",
        "processing",
        "delivered",
        "rejected",
    }

    if status not in allowed:
        raise ValueError("Недопустимый статус")

    comment = str(comment or "").strip()

    with db.transaction():
        order = db.fetchone(
            "SELECT * FROM wallet_orders WHERE id = ?",
            (order_id,),
        )

        if not order:
            raise ValueError("Заказ не найден")

        old_status = order["status"]

        if old_status in {"rejected", "refunded"}:
            raise ValueError("Закрытый заказ нельзя изменить")

        if old_status == "delivered" and status != "delivered":
            raise ValueError("Выданный заказ нельзя вернуть в работу")

        if old_status == status:
            return _dict(order)

        db.execute(
            """
            UPDATE wallet_orders
            SET status = ?,
                admin_comment = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, comment, db.now(), order_id),
        )

        _admin_action(
            admin_id,
            "change_order_status",
            "wallet_order",
            order_id,
            {
                "old_status": old_status,
                "new_status": status,
                "comment": comment,
            },
        )

        return shop.get_order(order_id)


# --------------------------------------------------
# ПОПОЛНЕНИЯ
# --------------------------------------------------

def get_topups(
    admin_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:

    require_admin(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    sql = """
        SELECT
            t.*,
            u.username,
            u.first_name
        FROM topups t
        LEFT JOIN users u ON u.telegram_id = t.user_id
        WHERE 1 = 1
    """

    params = []

    if status:
        sql += " AND t.status = ?"
        params.append(str(status))

    sql += " ORDER BY t.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    return [
        _dict(row)
        for row in db.fetchall(sql, tuple(params))
    ]


def approve_topup(
    admin_id: int,
    topup_id: int,
    comment: str = "",
) -> dict:

    require_admin(admin_id)

    result = payments.approve_topup(
        topup_id,
        admin_id,
        comment,
    )

    return result


def reject_topup(
    admin_id: int,
    topup_id: int,
    reason: str = "",
) -> dict:

    require_admin(admin_id)

    result = payments.reject_topup(
        topup_id,
        admin_id,
        reason,
    )

    return result


# --------------------------------------------------
# ТОВАРЫ И КАТЕГОРИИ
# --------------------------------------------------

def get_products(admin_id: int) -> list[dict]:
    require_admin(admin_id)

    return shop.get_products(active_only=False)


def add_product(
    admin_id: int,
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

    require_admin(admin_id)

    product_id = shop.create_product(
        category_slug=category_slug,
        title=title,
        description=description,
        price_uzs=price_uzs,
        fulfillment_mode=fulfillment_mode,
        price_xtr=price_xtr,
        sku=sku,
        image_url=image_url,
        bonus_eligible=bonus_eligible,
    )

    _admin_action(
        admin_id,
        "add_product",
        "product",
        product_id,
        {
            "category": category_slug,
            "title": title,
            "price_uzs": price_uzs,
            "price_xtr": price_xtr,
            "fulfillment_mode": fulfillment_mode,
        },
    )

    return product_id


def set_product_active(
    admin_id: int,
    product_id: int,
    active: bool,
) -> bool:

    require_admin(admin_id)

    result = shop.set_product_active(product_id, active)

    if result:
        _admin_action(
            admin_id,
            "set_product_active",
            "product",
            product_id,
            {"active": bool(active)},
        )

    return result


def get_categories(admin_id: int) -> list[dict]:
    require_admin(admin_id)

    return shop.get_categories(active_only=False)


def add_category(
    admin_id: int,
    slug: str,
    title: str,
    description: str = "",
    sort_order: int = 0,
) -> int:

    require_admin(admin_id)

    category_id = shop.create_category(
        slug,
        title,
        description,
        sort_order,
    )

    _admin_action(
        admin_id,
        "add_category",
        "category",
        category_id,
        {"slug": slug, "title": title},
    )

    return category_id


# --------------------------------------------------
# ЖУРНАЛ АДМИНИСТРАТОРА
# --------------------------------------------------

def get_audit_log(
    admin_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:

    require_admin(admin_id)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    rows = db.fetchall(
        """
        SELECT *
        FROM admin_audit_log
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )

    return [_dict(row) for row in rows]


# --------------------------------------------------
# СОВМЕСТИМОСТЬ С ДРУГИМИ МОДУЛЯМИ
# --------------------------------------------------

stats = get_stats
users = get_users
orders = get_orders
topups = get_topups
products = get_products
audit_log = get_audit_log