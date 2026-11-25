
import os
import telebot
from flask import Flask, request, abort

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
RENDER_URL = "https://gold-star-store-bot.onrender.com"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не настроен")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


@bot.message_handler(commands=["start"])
def start(message):
    args = message.text.split()

    if len(args) > 1 and args[1] == "stars":
        text = "⭐ Добро пожаловать в магазин Telegram Stars!"
    elif len(args) > 1 and args[1] == "premium":
        text = "💎 Добро пожаловать в магазин Telegram Premium!"
    else:
        text = (
            "⭐ Добро пожаловать в Gold Star!\n\n"
            "/stars - Telegram Stars\n"
            "/premium - Telegram Premium"
        )

    print("START RECEIVED:", message.text)

    try:
        bot.send_message(message.chat.id, text)
        print("MESSAGE SENT")
    except Exception as e:
        print("SEND ERROR:", repr(e))


@bot.message_handler(commands=["stars"])
def stars(message):
    bot.send_message(message.chat.id, "⭐ Раздел Telegram Stars")


@bot.message_handler(commands=["premium"])
def premium(message):
    bot.send_message(message.chat.id, "💎 Раздел Telegram Premium")


@app.route("/", methods=["GET"])
def home():
    return "Gold Star Bot is running!", 200


@app.route("/telegram-webhook", methods=["POST"])
def webhook():
    print("WEBHOOK RECEIVED")

    if WEBHOOK_SECRET:
        received = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
        if received != WEBHOOK_SECRET:
            print("SECRET MISMATCH")
            abort(403)

    try:
        raw_data = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(raw_data)

        if update.message:
    print("MESSAGE TEXT:", update.message.text)
    print("MESSAGE ENTITIES:", update.message.entities)
else:
    print("NO MESSAGE IN UPDATE")

        bot.process_new_updates([update])
        print("UPDATE PROCESSED")
        return "OK", 200

    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))
        return "ERROR", 500


if RENDER_URL:
    bot.remove_webhook()
    result = bot.set_webhook(
        url=RENDER_URL + "/telegram-webhook",
        secret_token=WEBHOOK_SECRET
    )
    print("WEBHOOK SET:", result)
    print("WEBHOOK INFO:", bot.get_webhook_info())


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000))
    )
