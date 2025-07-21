import os
import re
import json
import logging
import aiosqlite
import asyncio
import requests
from datetime import datetime
from logging.handlers import RotatingFileHandler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler, ConversationHandler,
    ContextTypes, PreCheckoutQueryHandler
)
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
API_STARS_PROVIDER_TOKEN = os.getenv("PAYMENT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")  # API-ключ для DeepSeek

# Проверка наличия обязательных ключей
if not TOKEN or not API_STARS_PROVIDER_TOKEN or not DEEPSEEK_API_KEY:
    raise ValueError("Не найдены обязательные переменные окружения! Проверьте .env файл.")

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler = RotatingFileHandler('unsub_bot.log', maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Конфигурация
DB_PATH = 'unsub_data.db'
GROUP_ID = -1002579257687  # ID группы для уведомлений
PRICE_RUB = 399  # Цена в рублях
PRICE_STARS = 200  # Цена в звездах

# Состояния для FSM
FIO, SOURCE, BANK, CARD, EMAIL, PHONE = range(6)

# Паттерны валидации
FIO_PATTERN = re.compile(r'^[А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+$')
CARD_PATTERN = re.compile(r'^\d{6}\*\d{4}$')
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_PATTERN = re.compile(r'^\+?\d{10,15}$')

# Функции работы с БД
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                fio TEXT, source TEXT, bank TEXT, card TEXT, email TEXT, phone TEXT,
                payment_status TEXT DEFAULT 'pending'
            )''')
        await db.commit()

async def save_data(user_id, data):
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''INSERT INTO users (telegram_id, fio, source, bank, card, email, phone) VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, data['fio'], data['source'], data['bank'], data['card'], data['email'], data['phone'])
        )
        await db.commit()

async def update_payment_status(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET payment_status = ? WHERE telegram_id = ?",
            (status, user_id)
        )
        await db.commit()

# DeepSeek helper
def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    system_prompt = (
        "Ты — помощник бота для отмены подписок. Проверяй и корректируй ввод пользователя."
    )
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
    data = {"model": "deepseek-chat", "messages": messages, "temperature": 0.7, "max_tokens": 500}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=20)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return "Сервис недоступен, попробуйте позже."

# /start
async def start(update: Update, context: CallbackContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Продолжить', callback_data='start_form')],
        [InlineKeyboardButton('📨 Связаться с админом', callback_data='contact_admin')]
    ])
    await update.message.reply_text(
        '👋 <b>Здравствуйте!</b>\nВы в Отписка Бот для отмены списаний.',
        reply_markup=kb
    )
    return ConversationHandler.END

# Кнопки
async def handle_buttons(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    if query.data == 'start_form':
        await query.message.edit_text('👤 Введите ФИО (Три слова, первая буква заглавная):')
        return FIO
    if query.data == 'contact_admin':
        await query.message.edit_text('📨 Напишите сообщение для админа:')
        context.user_data['contact_admin'] = True
        return ConversationHandler.END
    return ConversationHandler.END

# Формы
async def process_fio(update: Update, context: CallbackContext):
    text = update.message.text
    if not FIO_PATTERN.match(text):
        corr = ask_deepseek(f"Проверь ФИО: '{text}'")
        await update.message.reply_text(corr)
        return FIO
    context.user_data['fio'] = text
    await update.message.reply_text('📃 Укажите источник списания:')
    return SOURCE

async def process_source(update: Update, context: CallbackContext):
    text = update.message.text
    if len(text) < 3:
        corr = ask_deepseek(f"Проверь источник: '{text}'")
        await update.message.reply_text(corr)
        return SOURCE
    context.user_data['source'] = text
    await update.message.reply_text('🏦 Введите название банка:')
    return BANK

async def process_bank(update: Update, context: CallbackContext):
    text = update.message.text
    if not re.match(r'^[\w\s]{3,}$', text):
        corr = ask_deepseek(f"Проверь банк: '{text}'")
        await update.message.reply_text(corr)
        return BANK
    context.user_data['bank'] = text
    await update.message.reply_text('💳 Введите карту (123456*7890):')
    return CARD

async def process_card(update: Update, context: CallbackContext):
    text = update.message.text
    if not CARD_PATTERN.match(text):
        corr = ask_deepseek(f"Проверь карту: '{text}'")
        await update.message.reply_text(corr)
        return CARD
    context.user_data['card'] = text
    await update.message.reply_text('📧 Введите email:')
    return EMAIL

async def process_email(update: Update, context: CallbackContext):
    text = update.message.text
    if not EMAIL_PATTERN.match(text):
        corr = ask_deepseek(f"Проверь email: '{text}'")
        await update.message.reply_text(corr)
        return EMAIL
    context.user_data['email'] = text
    await update.message.reply_text('📱 Введите телефон:')
    return PHONE

async def process_phone(update: Update, context: CallbackContext):
    text = update.message.text
    if not PHONE_PATTERN.match(text):
        corr = ask_deepseek(f"Проверь телефон: '{text}'")
        await update.message.reply_text(corr)
        return PHONE
    context.user_data['phone'] = text
    user_id = update.message.from_user.id
    await save_data(user_id, context.user_data)
    info = (
        f"<b>Новая заявка:</b>\n"
        f"👤 {context.user_data['fio']}\n"
        f"📄 {context.user_data['source']}\n"
        f"🏦 {context.user_data['bank']}\n"
        f"💳 {context.user_data['card']}\n"
        f"📧 {context.user_data['email']}\n"
        f"📱 {context.user_data['phone']}"
    )
    await context.bot.send_message(GROUP_ID, info)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {PRICE_RUB}₽", callback_data=f"pay_rub:{PRICE_RUB}" )],
        [InlineKeyboardButton(f"⭐ Оплатить {PRICE_STARS}⭐", callback_data=f"pay_stars:{PRICE_STARS}")]
    ])
    await update.message.reply_text('💰 Оплатите услугу:', reply_markup=kb)
    return ConversationHandler.END

async def handle_free_text(update: Update, context: CallbackContext):
    if context.user_data.get('contact_admin'):
        await context.bot.send_message(GROUP_ID, f"✉️ Сообщение от {update.effective_user.id}: {update.message.text}")
        await update.message.reply_text("✅ Отправлено администратору.")
        context.user_data.clear()
        return
    resp = ask_deepseek(update.message.text)
    await update.message.reply_text(resp)

async def handle_payment_button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith('pay_rub'):
        _, amt = data.split(':')
        prices = [LabeledPrice("Услуга отписки", int(amt)*100)]
        await query.bot.send_invoice(
            chat_id=query.from_user.id,
            title="Оплата RUB",
            description="Отписка Бот",
            payload="pay_rub",
            provider_token=API_STARS_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices
        )
    else:
        _, amt = data.split(':')
        prices = [LabeledPrice("Услуга отписки", int(amt))]
        await query.bot.send_invoice(
            chat_id=query.from_user.id,
            title="Оплата STAR",
            description="Отписка Бот",
            payload="pay_stars",
            provider_token=API_STARS_PROVIDER_TOKEN,
            currency="STAR",
            prices=prices
        )

async def pre_checkout(update: Update, context: CallbackContext):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    payment = update.message.successful_payment
    await update_payment_status(user_id, "completed")
    await context.bot.send_message(GROUP_ID,
        f"✅ Оплата: {update.effective_user.full_name} ID:{user_id} сумма:{payment.total_amount/100}{payment.currency}")
    await update.message.reply_text(
        "✅ Ваш платёж подтверждён! Ожидайте завершения услуги."
    )

async def cancel(update: Update, context: CallbackContext):
    await update.message.reply_text('❌ Отмена.')
    context.user_data.clear()
    return ConversationHandler.END

# Main
def main():
    asyncio.run(init_db())
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start), CallbackQueryHandler(handle_buttons)],
        states={
            FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_fio)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_source)],
            BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_bank)],
            CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_card)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_email)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_phone)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(handle_payment_button, pattern='^pay_'))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))
    logger.info("Бот запущен")
    app.run_polling()

if __name__ == '__main__':
    main()
