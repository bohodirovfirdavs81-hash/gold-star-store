import os
import telebot

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не настроен")

bot = telebot.TeleBot(BOT_TOKEN)


@bot.message_handler(commands=['start'])
def start(message):
    args = message.text.split()

    if len(args) > 1 and args[1] == "stars":
        bot.send_message(
            message.chat.id,
            "⭐ Добро пожаловать в магазин Telegram Stars!"
        )

    elif len(args) > 1 and args[1] == "premium":
        bot.send_message(
            message.chat.id,
            "💎 Добро пожаловать в магазин Telegram Premium!"
        )

    else:
        bot.send_message(
            message.chat.id,
            "⭐ Добро пожаловать в Gold Star!\n\n"
            "Выберите раздел:\n"
            "/stars - Telegram Stars\n"
            "/premium - Telegram Premium"
        )


@bot.message_handler(commands=['stars'])
def stars(message):
    bot.send_message(message.chat.id, "⭐ Раздел Telegram Stars")


@bot.message_handler(commands=['premium'])
def premium(message):
    bot.send_message(message.chat.id, "💎 Раздел Telegram Premium")


bot.infinity_polling()
