"""
Кнопка «Поддержка» — отдельный модуль для бота «Напоминалка».
Ссылка берётся из переменной окружения SUPPORT_URL (вкладка «Переменные» на bothost).
"""
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

BTN_SUPPORT = "🆘 Поддержка"
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/ваш_канал")


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Нужна помощь? Напишите нам 👇",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🆘 Перейти в поддержку", url=SUPPORT_URL)]]
        ),
    )


def setup_support(app):
    """Регистрирует кнопку меню и команду /support."""
    app.add_handler(MessageHandler(filters.Regex("^" + re.escape(BTN_SUPPORT) + "$"), support))
    app.add_handler(CommandHandler("support", support))
