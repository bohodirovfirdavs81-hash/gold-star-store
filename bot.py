# Изменения для bot.py — Gold Star Store
# Примените три замены ниже к существующему bot.py.
# Остальной код проекта не меняйте.

# 1. Полностью замените функцию help_keyboard():

def help_keyboard():
    """Inline-клавиатура со списком тем справки."""
    return kb([
        [btn("🛍 Как купить товар?", "help:buy")],
        [btn("💳 Как пополнить баланс?", "help:topup")],
        [btn("📦 Где посмотреть свой заказ?", "help:orders")],
        [btn("🎁 Как получить бонусы?", "help:bonuses")],
        [btn("⭐ Как работает Telegram Stars?", "help:stars")],
        [btn("🌐 Как изменить язык?", "help:language")],
        [
            types.InlineKeyboardButton(
                text="Другой вопрос",
                url="https://t.me/GoldStar_help",
            )
        ],
        [btn("⬅️ Главное меню", "help:menu")],
    ])


# 2. Полностью замените функцию send_help_menu():

def send_help_menu(chat_id):
    bot.send_message(
        chat_id,
        (
            "<b>❓ Помощь Gold Star Store</b>\n\n"
            "Выберите вопрос, чтобы открыть подробную инструкцию.\n\n"
            "Не нашли ответ на свой вопрос?\n"
            "Напишите в нашу поддержку: "
            "<a href=\"https://t.me/GoldStar_help\">@GoldStar_help</a>"
        ),
        reply_markup=help_keyboard(),
        disable_web_page_preview=True,
    )


# 3. Внутри help_callback() замените только ветку if action == "questions":
# Не создавайте второй обработчик. Замените существующий блок целиком.

        if action == "questions":
            show(
                call,
                (
                    "<b>❓ Помощь Gold Star Store</b>\n\n"
                    "Выберите вопрос, чтобы открыть подробную инструкцию.\n\n"
                    "Не нашли ответ на свой вопрос?\n"
                    "Напишите в нашу поддержку: "
                    "<a href=\"https://t.me/GoldStar_help\">@GoldStar_help</a>"
                ),
                help_keyboard(),
            )
            safe_answer(call.id)
            return