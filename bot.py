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
import urllib.parse
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

# Реферальная система.
# Каждый приглашённый пользователь может быть привязан
# только к одному пригласившему.
db.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    invitee_id INTEGER PRIMARY KEY,
    inviter_id INTEGER NOT NULL,
    subscription_rewarded INTEGER NOT NULL DEFAULT 0,
    first_topup_rewarded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""")

logger.info("База данных инициализирована")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

CHANNEL_USERNAME = "@GoldStarUz_News"
CHANNEL_URL = "https://t.me/GoldStarUz_News"


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
    """Главное меню с inline-кнопками под сообщением."""
    markup = types.InlineKeyboardMarkup(row_width=1)

    if config.WEBAPP_URL:
        markup.add(
            types.InlineKeyboardButton(
                text="🛍 Открыть магазин",
                web_app=types.WebAppInfo(
                    url=config.WEBAPP_URL
                ),
            )
        )

    markup.add(
        types.InlineKeyboardButton(
            text="💳 Пополнить кошелёк",
            callback_data="menu:topup",
        ),
        types.InlineKeyboardButton(
            text="🎁 Бонусы",
            callback_data="menu:bonuses",
        ),
        types.InlineKeyboardButton(
            text="👥 Пригласить друга",
            callback_data="menu:invite",
        ),
        types.InlineKeyboardButton(
            text="👤 Мой профиль",
            callback_data="menu:profile",
        ),
        types.InlineKeyboardButton(
            text="❓ Помощь",
            callback_data="menu:help",
        ),
    )

    return markup


def home_keyboard():
    return kb([
        [btn("🏠 Главное меню", "menu:home")]
    ])


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
# REFERRAL SYSTEM
# ---------------------------------------------------------

def register_referral(invitee_id, inviter_id):
    """
    Регистрирует приглашённого пользователя.
    Повторная привязка к другому пригласившему невозможна.
    """
    invitee_id = safe_int(invitee_id)
    inviter_id = safe_int(inviter_id)

    if not invitee_id or not inviter_id:
        return False

    if invitee_id == inviter_id:
        return False

    timestamp = utc_now()

    with db.transaction():
        db.execute(
            """
            INSERT OR IGNORE INTO referrals(
                invitee_id,
                inviter_id,
                subscription_rewarded,
                first_topup_rewarded,
                created_at,
                updated_at
            )
            VALUES (?, ?, 0, 0, ?, ?)
            """,
            (
                invitee_id,
                inviter_id,
                timestamp,
                timestamp,
            ),
        )

        row = db.fetchone(
            """
            SELECT inviter_id
            FROM referrals
            WHERE invitee_id = ?
            """,
            (invitee_id,),
        )

    return bool(
        row and int(row["inviter_id"]) == inviter_id
    )


def award_referral_bonus(
    invitee_id,
    reward_type,
    amount,
    description,
):
    """
    Однократно начисляет бонус пригласившему
    в бонусный баланс.

    Все изменения выполняются в одной транзакции.
    """
    columns = {
        "subscription": "subscription_rewarded",
        "first_topup": "first_topup_rewarded",
    }

    column = columns.get(reward_type)

    if not column:
        raise ValueError("Неизвестный тип реферального бонуса")

    invitee_id = int(invitee_id)
    amount = int(amount)
    timestamp = utc_now()

    with db.transaction():
        referral = db.fetchone(
            f"""
            SELECT inviter_id, {column} AS rewarded
            FROM referrals
            WHERE invitee_id = ?
            """,
            (invitee_id,),
        )

        if not referral:
            return False

        if int(referral["rewarded"] or 0):
            return False

        inviter_id = int(referral["inviter_id"])

        # Сначала резервируем начисление.
        # Если дальнейшая операция завершится ошибкой,
        # транзакция откатится целиком.
        cursor = db.execute(
            f"""
            UPDATE referrals
            SET {column} = 1,
                updated_at = ?
            WHERE invitee_id = ?
              AND {column} = 0
            """,
            (timestamp, invitee_id),
        )

        if cursor.rowcount != 1:
            return False

        wallet = db.fetchone(
            """
            SELECT balance_uzs, bonus_uzs
            FROM wallets
            WHERE user_id = ?
            """,
            (inviter_id,),
        )

        if not wallet:
            raise RuntimeError(
                f"Кошелёк пригласившего {inviter_id} не найден"
            )

        balance = int(wallet["balance_uzs"] or 0)
        old_bonus = int(wallet["bonus_uzs"] or 0)
        new_bonus = old_bonus + amount

        db.execute(
            """
            UPDATE wallets
            SET bonus_uzs = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                new_bonus,
                timestamp,
                inviter_id,
            ),
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
                ?,
                'referral_bonus',
                ?,
                0,
                ?,
                ?,
                ?,
                'referral',
                ?,
                ?,
                ?
            )
            """,
            (
                inviter_id,
                amount,
                amount,
                balance,
                new_bonus,
                str(invitee_id),
                description,
                timestamp,
            ),
        )

    try:
        notify(
            inviter_id,
            (
                "🎁 Вам начислен реферальный бонус!\n\n"
                f"Сумма: <b>{uzs(amount)}</b>\n"
                f"Причина: {h(description)}"
            ),
        )
    except Exception:
        logger.exception(
            "Не удалось отправить уведомление о реферальном бонусе"
        )

    logger.info(
        "Реферальный бонус начислен: inviter=%s invitee=%s amount=%s type=%s",
        inviter_id,
        invitee_id,
        amount,
        reward_type,
    )

    return True


def check_referral_subscription(user_id):
    """
    Проверяет подписку на обязательный канал.
    Бот должен быть администратором канала.
    """
    try:
        member = bot.get_chat_member(
            chat_id=CHANNEL_USERNAME,
            user_id=int(user_id),
        )

        status = getattr(member, "status", "")

        if status in ("member", "administrator", "creator"):
            return True

        if (
            status == "restricted"
            and bool(getattr(member, "is_member", False))
        ):
            return True

        return False

    except Exception:
        logger.exception(
            "Не удалось проверить подписку пользователя %s",
            user_id,
        )
        return False


def referral_link(user_id):
    me = bot.get_me()
    username = getattr(me, "username", None)

    if not username:
        raise RuntimeError(
            "Не удалось определить username бота"
        )

    return (
        f"https://t.me/{username}"
        f"?start=ref_{int(user_id)}"
    )


def referral_welcome_keyboard():
    return types.InlineKeyboardMarkup(row_width=1).add(
        types.InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url=CHANNEL_URL,
        ),
        types.InlineKeyboardButton(
            text="✅ Проверить подписку",
            callback_data="ref:verify",
        ),
        types.InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="menu:home",
        ),
    )


# ---------------------------------------------------------
# HELP SECTION
# ---------------------------------------------------------

HELP_TOPICS = {
    "buy": {
        "title": "Как купить товар?",
        "text": (
            "<b>🛍 Как купить товар?</b>\n\n"
            "1. Нажмите «Открыть магазин».\n"
            "2. Выберите нужную категорию.\n"
            "3. Откройте карточку товара и проверьте описание и цену.\n"
            "4. Если товар требует данные для выполнения заказа, "
            "введите их внимательно.\n"
            "5. Проверьте состав заказа и подтвердите покупку.\n\n"
            "После оформления дождитесь обработки заказа. "
            "Если возникла проблема, обратитесь к администратору "
            "через доступные контакты магазина."
        ),
    },
    "topup": {
        "title": "Как пополнить баланс?",
        "text": (
            "<b>💳 Как пополнить баланс?</b>\n\n"
            "1. Нажмите «Пополнить кошелёк».\n"
            "2. Введите сумму в пределах указанных ограничений.\n"
            "3. Получите реквизиты для перевода.\n"
            "4. Выполните перевод и отправьте фотографию или файл чека.\n"
            "5. Дождитесь проверки администратором.\n\n"
            "<b>Важно:</b> отправка чека сама по себе не пополняет баланс. "
            "Средства начисляются только после проверки фактического "
            "поступления платежа администратором. Не отправляйте "
            "повторный платёж, пока предыдущая заявка проверяется."
        ),
    },
    "orders": {
        "title": "Где посмотреть свой заказ?",
        "text": (
            "<b>📦 Где посмотреть свой заказ?</b>\n\n"
            "Откройте магазин через кнопку «Открыть магазин». "
            "Если в текущей версии Mini App доступен раздел заказов "
            "или история покупок, откройте его, чтобы посмотреть "
            "свои заказы и их статусы.\n\n"
            "Если нужный заказ не отображается или статус долго "
            "не меняется, обратитесь к администратору и укажите "
            "номер заказа."
        ),
    },
    "bonuses": {
        "title": "Как получить бонусы?",
        "text": (
            "<b>🎁 Как получить бонусы?</b>\n\n"
            "Приглашайте друзей в Gold Star Store:\n\n"
            "• <b>+250 сум</b> — когда приглашённый друг "
            "запустит бота и подтвердит подписку на @GoldStarUz_News.\n"
            "• <b>+500 сум</b> — после первого успешного пополнения "
            "кошелька приглашённым другом.\n\n"
            "Оба бонуса получает только пригласивший. "
            "Каждый бонус начисляется один раз за друга.\n\n"
            "Бонусный баланс можно посмотреть в профиле."
        ),
    },
    "stars": {
        "title": "Как работает Telegram Stars?",
        "text": (
            "<b>⭐ Как работает Telegram Stars?</b>\n\n"
            "Оплата Telegram Stars доступна только для товаров, "
            "которые администратор специально настроил для оплаты "
            "в категории «Другие товары».\n\n"
            "1. Отправьте команду /stars.\n"
            "2. Выберите доступный товар.\n"
            "3. Проверьте счёт Telegram.\n"
            "4. Подтвердите оплату средствами Telegram Stars.\n\n"
            "Не оплачивайте счёт, если товар или сумма указаны неверно. "
            "Telegram Premium и Gifts оформляются отдельно через заявки."
        ),
    },
    "language": {
        "title": "Как изменить язык?",
        "text": (
            "<b>🌐 Как изменить язык?</b>\n\n"
            "Переключение интерфейса между узбекским, русским "
            "и английским языками ещё не подключено в этой версии "
            "бота. Сейчас основные сообщения отображаются на русском.\n\n"
            "Выбор языка будет добавлен отдельно, чтобы настройка "
            "сохранялась для следующих запусков бота."
        ),
    },
}


def help_keyboard():
    return kb([
        [btn("🛍 Как купить товар?", "help:buy")],
        [btn("💳 Как пополнить баланс?", "help:topup")],
        [btn("📦 Где посмотреть свой заказ?", "help:orders")],
        [btn("🎁 Как получить бонусы?", "help:bonuses")],
        [btn("⭐ Как работает Telegram Stars?", "help:stars")],
        [btn("🌐 Как изменить язык?", "help:language")],
        [btn("❓ Другой вопрос", "help:other")],
        [btn("🏠 Главное меню", "help:menu")],
    ])


def help_topic_keyboard():
    return kb([
        [btn("⬅️ Назад к вопросам", "help:questions")],
        [btn("🏠 Главное меню", "help:menu")],
    ])


def other_help_keyboard():
    return types.InlineKeyboardMarkup(row_width=1).add(
        types.InlineKeyboardButton(
            text="💬 Написать в поддержку",
            url="https://t.me/GoldStar_help",
        ),
        types.InlineKeyboardButton(
            text="🔙 Назад к вопросам",
            callback_data="help:questions",
        ),
        types.InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="menu:home",
        ),
    )


def send_help_menu(chat_id):
    bot.send_message(
        chat_id,
        (
            "<b>❓ Помощь Gold Star Store</b>\n\n"
            "Выберите вопрос, чтобы открыть подробную инструкцию.\n\n"
            "Если не нашли нужный ответ, нажмите "
            "«❓ Другой вопрос»."
        ),
        reply_markup=help_keyboard(),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------
# MAIN MENU CALLBACKS
# ---------------------------------------------------------

@bot.callback_query_handler(
    func=lambda c: bool(c.data)
    and c.data.startswith("menu:")
)
def main_menu_callback(call):
    try:
        if not call.message:
            safe_answer(call.id, "Сообщение недоступно", True)
            return

        action = call.data.split(":", 1)[1]
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        if action == "home":
            show(
                call,
                (
                    f"⭐ <b>{h(config.BRAND)}</b>\n\n"
                    "Добро пожаловать в магазин!\n\n"
                    "Выберите нужный раздел:"
                ),
                main_keyboard(),
            )
            safe_answer(call.id)
            return

        if action == "help":
            show(
                call,
                (
                    "<b>❓ Помощь Gold Star Store</b>\n\n"
                    "Выберите вопрос, чтобы открыть подробную инструкцию."
                ),
                help_keyboard(),
            )
            safe_answer(call.id)
            return

        if action == "profile":
            db.upsert_user(get_user_info(call.from_user))

            user = db.get_user(user_id)
            wallet = db.get_wallet(user_id)

            if not user:
                safe_answer(call.id, "Профиль не найден", True)
                return

            balance = wallet["balance_uzs"] if wallet else 0
            bonus = wallet["bonus_uzs"] if wallet else 0

            show(
                call,
                (
                    "<b>👤 Мой профиль</b>\n\n"
                    f"ID: <code>{user_id}</code>\n"
                    f"Имя: {h(user_name(call.from_user))}\n\n"
                    f"Основной баланс: <b>{uzs(balance)}</b>\n"
                    f"Бонусы: <b>{uzs(bonus)}</b>"
                ),
                home_keyboard(),
            )
            safe_answer(call.id)
            return

        if action == "topup":
            db.upsert_user(get_user_info(call.from_user))

            prompt = bot.send_message(
                chat_id,
                (
                    "<b>💳 Пополнение кошелька</b>\n\n"
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

            safe_answer(call.id)
            return

        if action == "bonuses":
            show(
                call,
                (
                    "<b>🎁 Реферальные бонусы</b>\n\n"
                    "Приглашайте друзей в Gold Star Store:\n\n"
                    "• <b>+250 сум</b> — когда друг запустит бота "
                    "и подтвердит подписку на @GoldStarUz_News.\n"
                    "• <b>+500 сум</b> — после первого успешного "
                    "пополнения кошелька приглашённым другом.\n\n"
                    "Оба бонуса получает только пригласивший. "
                    "Каждое начисление выполняется один раз."
                ),
                kb([
                    [btn("👥 Пригласить друга", "menu:invite")],
                    [btn("🏠 Главное меню", "menu:home")],
                ]),
            )
            safe_answer(call.id)
            return

        if action == "invite":
            link = referral_link(user_id)

            share_text = (
                "Присоединяйся к Gold Star Store! "
                "Переходи по моей ссылке:"
            )

            share_url = (
                "https://t.me/share/url?url="
                + urllib.parse.quote(link, safe="")
                + "&text="
                + urllib.parse.quote(share_text, safe="")
            )

            markup = types.InlineKeyboardMarkup(row_width=1)

            markup.add(
                types.InlineKeyboardButton(
                    text="📨 Выбрать друга",
                    url=share_url,
                ),
                types.InlineKeyboardButton(
                    text="🔗 Моя реферальная ссылка",
                    url=link,
                ),
                types.InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="menu:home",
                ),
            )

            show(
                call,
                (
                    "<b>👥 Пригласить друга</b>\n\n"
                    "Отправьте другу свою персональную ссылку.\n\n"
                    "За каждого нового приглашённого друга:\n"
                    "• +250 сум — после подтверждения подписки на канал.\n"
                    "• +500 сум — после первого успешного пополнения.\n\n"
                    "Бонусы получает только пригласивший."
                ),
                markup,
            )

            safe_answer(call.id)
            return

        safe_answer(call.id, "Раздел не найден", True)

    except Exception:
        logger.exception("Ошибка главного меню")
        safe_answer(call.id, "Не удалось открыть раздел", True)


# ---------------------------------------------------------
# REFERRAL SUBSCRIPTION VERIFICATION
# ---------------------------------------------------------

@bot.callback_query_handler(
    func=lambda c: bool(c.data)
    and c.data == "ref:verify"
)
def referral_verify_callback(call):
    try:
        invitee_id = call.from_user.id

        referral = db.fetchone(
            """
            SELECT inviter_id, subscription_rewarded
            FROM referrals
            WHERE invitee_id = ?
            """,
            (invitee_id,),
        )

        if not referral:
            safe_answer(
                call.id,
                "Реферальная ссылка не найдена.",
                True,
            )
            return

        if int(referral["subscription_rewarded"] or 0):
            safe_answer(
                call.id,
                "Бонус за подписку уже был начислен.",
                True,
            )
            return

        if not check_referral_subscription(invitee_id):
            safe_answer(
                call.id,
                (
                    "Подписка не найдена. "
                    "Подпишитесь на канал и попробуйте снова."
                ),
                True,
            )
            return

        credited = award_referral_bonus(
            invitee_id,
            "subscription",
            250,
            "Бонус за подписку приглашённого пользователя",
        )

        if credited:
            try:
                bot.send_message(
                    invitee_id,
                    (
                        "✅ Подписка подтверждена!\n\n"
                        "Пригласившему начислено 250 сум."
                    ),
                )
            except Exception:
                logger.exception(
                    "Не удалось уведомить приглашённого"
                )

        show(
            call,
            (
                "<b>✅ Подписка подтверждена</b>\n\n"
                "Спасибо за подписку на @GoldStarUz_News.\n\n"
                "Пригласившему начислен бонус 250 сум, "
                "если это первое подтверждение."
            ),
            home_keyboard(),
        )

        safe_answer(call.id)

    except Exception:
        logger.exception("Ошибка проверки реферальной подписки")
        safe_answer(
            call.id,
            "Не удалось проверить подписку. Попробуйте позже.",
            True,
        )


# ---------------------------------------------------------
# USER COMMANDS
# ---------------------------------------------------------

@bot.message_handler(commands=["start"])
def start_handler(message):
    try:
        user_id = message.from_user.id

        logger.info(
            "START_HANDLER вызван: user_id=%s, text=%r",
            user_id,
            message.text,
        )

        # Проверяем, был ли пользователь в базе до этого запуска.
        existing_user = db.get_user(user_id)

        db.upsert_user(
            get_user_info(message.from_user)
        )

        # Регистрируем реферала только при первом появлении пользователя.
        referral_registered = False

        parts = (message.text or "").split(maxsplit=1)

        if not existing_user and len(parts) == 2:
            payload = parts[1].strip()

            if payload.startswith("ref_"):
                inviter_id = safe_int(payload[4:])

                referral_registered = register_referral(
                    user_id,
                    inviter_id,
                )

                if referral_registered:
                    logger.info(
                        "Реферал зарегистрирован: inviter=%s invitee=%s",
                        inviter_id,
                        user_id,
                    )

        text = (
            f"⭐ <b>{h(config.BRAND)}</b>\n\n"
            "Добро пожаловать в магазин!\n\n"
            "Здесь можно просматривать товары, оформлять "
            "заказы и управлять кошельком.\n\n"
            "Выберите нужный раздел:"
        )

        bot.send_message(
            message.chat.id,
            text,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )

        # Новый приглашённый получает отдельное сообщение
        # с кнопкой подписки и проверки.
        if referral_registered:
            bot.send_message(
                message.chat.id,
                (
                    "<b>🎁 Вас пригласили в Gold Star Store!</b>\n\n"
                    "Чтобы пригласивший получил бонус 250 сум, "
                    "подпишитесь на наш канал и нажмите "
                    "«Проверить подписку».\n\n"
                    "После вашего первого успешного пополнения "
                    "пригласившему дополнительно начислится 500 сум."
                ),
                reply_markup=referral_welcome_keyboard(),
                disable_web_page_preview=True,
            )

        logger.info(
            "Ответ /start отправлен: user_id=%s",
            user_id,
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
    send_help_menu(message.chat.id)


@bot.message_handler(
    func=lambda m: bool(m.text)
    and m.text.strip() in ("❓ Помощь", "Помощь")
)
def help_button_handler(message):
    send_help_menu(message.chat.id)


@bot.callback_query_handler(
    func=lambda c: bool(c.data)
    and c.data.startswith("help:")
)
def help_callback(call):
    try:
        if not call.message:
            safe_answer(call.id, "Сообщение недоступно", True)
            return

        action = call.data.split(":", 1)[1]

        if action == "questions":
            show(
                call,
                (
                    "<b>❓ Помощь Gold Star Store</b>\n\n"
                    "Выберите вопрос, чтобы открыть подробную инструкцию.\n\n"
                    "Если не нашли нужный ответ, нажмите "
                    "«❓ Другой вопрос»."
                ),
                help_keyboard(),
            )
            safe_answer(call.id)
            return

        if action == "other":
            show(
                call,
                (
                    "<b>❓ Другой вопрос</b>\n\n"
                    "Если вы не нашли подходящий ответ в разделе помощи, "
                    "напишите нам в личные сообщения.\n\n"
                    "💬 Поддержка: @GoldStar_help\n\n"
                    "Мы постараемся помочь вам."
                ),
                other_help_keyboard(),
            )
            safe_answer(call.id)
            return

        if action == "menu":
            show(
                call,
                (
                    f"⭐ <b>{h(config.BRAND)}</b>\n\n"
                    "Добро пожаловать в магазин!\n\n"
                    "Выберите нужный раздел:"
                ),
                main_keyboard(),
            )
            safe_answer(call.id)
            return

        topic = HELP_TOPICS.get(action)

        if not topic:
            safe_answer(call.id, "Раздел не найден", True)
            return

        show(
            call,
            topic["text"],
            help_topic_keyboard(),
        )

        safe_answer(call.id)

    except Exception:
        logger.exception("Ошибка раздела помощи")
        safe_answer(call.id, "Не удалось открыть раздел", True)


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
                reply_markup=home_keyboard(),
            )
            return

        balance = wallet["balance_uzs"] if wallet else 0
        bonus = wallet["bonus_uzs"] if wallet else 0

        bot.send_message(
            message.chat.id,
            (
                "<b>👤 Мой профиль</b>\n\n"
                f"ID: <code>{user_id}</code>\n"
                f"Имя: {h(user_name(message.from_user))}\n\n"
                f"Основной баланс: <b>{uzs(balance)}</b>\n"
                f"Бонусы: <b>{uzs(bonus)}</b>"
            ),
            reply_markup=home_keyboard(),
        )

    except Exception:
        logger.exception("Ошибка профиля")

        bot.send_message(
            message.chat.id,
            "Не удалось загрузить профиль.",
            reply_markup=home_keyboard(),
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
                "<b>💳 Пополнение кошелька</b>\n\n"
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
            reply_markup=home_keyboard(),
        )


def process_topup_amount(message):
    if not message.text:
        bot.send_message(
            message.chat.id,
            "Отправьте сумму обычным текстом.",
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
                reply_markup=home_keyboard(),
            )
            return

        bot.send_message(
            message.chat.id,
            (
                f"Чек по заявке №{topup_id} получен.\n"
                "Заявка ожидает проверки.\n\n"
                "Деньги пока не начислены."
            ),
            reply_markup=home_keyboard(),
        )

    except ValueError as exc:
        bot.send_message(
            message.chat.id,
            f"Ошибка: {h(exc)}",
            reply_markup=home_keyboard(),
        )

    except Exception:
        logger.exception("Ошибка обработки чека")

        bot.send_message(
            message.chat.id,
            (
                "Не удалось обработать чек. "
                "Обратитесь к администратору."
            ),
            reply_markup=home_keyboard(),
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

            # Реферальные +500 начисляются только после
            # успешного подтверждения первого пополнения.
            try:
                award_referral_bonus(
                    result["user_id"],
                    "first_topup",
                    500,
                    "Бонус за первое пополнение приглашённого пользователя",
                )
            except Exception:
                logger.exception(
                    "Не удалось начислить реферальный бонус за пополнение"
                )

            notify(
                result["user_id"],
                (
                    f"Пополнение №{topup_id} подтверждено.\n"
                    f"Начислено: <b>{uzs(result['amount_uzs'])}</b>"
                ),
            )

            show(
                call,
                f"Пополнение №{topup_id} подтверждено.",
                home_keyboard(),
            )
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

            show(
                call,
                f"Пополнение №{topup_id} отклонено.",
                home_keyboard(),
            )
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
                reply_markup=home_keyboard(),
            )
        else:
            bot.send_message(
                message.chat.id,
                "Не удалось отправить заявку. Попробуйте позже.",
                reply_markup=home_keyboard(),
            )

    except Exception:
        logger.exception("Ошибка создания заявки %s", request_type)
        bot.send_message(
            message.chat.id,
            "Не удалось отправить заявку.",
            reply_markup=home_keyboard(),
        )


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
                reply_markup=home_keyboard(),
            )
            return

        buttons.append([
            btn("🏠 Главное меню", "menu:home")
        ])

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
        bot.send_message(
            message.chat.id,
            "Не удалось загрузить товары.",
            reply_markup=home_keyboard(),
        )


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
            reply_markup=home_keyboard(),
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
                reply_markup=home_keyboard(),
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить пользователя о проблеме Stars"
            )

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