import os
import re
import logging
import aiosqlite
import asyncio
import requests
from logging.handlers import RotatingFileHandler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler,
    ConversationHandler, PreCheckoutQueryHandler
)
from telegram.error import BadRequest
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_TOKEN")  # Переименовано для ясности
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Проверка наличия обязательных ключей
if not TOKEN or not PAYMENT_PROVIDER_TOKEN or not DEEPSEEK_API_KEY:
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
GROUP_ID = -1002579257687
PRICE_RUB = 399
PRICE_STARS = 200

# Состояния для FSM
FIO, SOURCE, BANK, CARD, EMAIL, PHONE = range(6)

# Паттерны валидации
FIO_PATTERN = re.compile(r'^[А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+$')
CARD_PATTERN = re.compile(r'^\d{6}\*\d{4}$')
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_PATTERN = re.compile(r'^\+?\d{10,15}$')

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                fio TEXT, source TEXT, bank TEXT, card TEXT, email TEXT, phone TEXT,
                payment_status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        await db.commit()

async def save_data(user_id, data):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''INSERT OR REPLACE INTO users 
            (telegram_id, fio, source, bank, card, email, phone) 
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, data['fio'], data['source'], data['bank'], 
             data['card'], data['email'], data['phone'])
        )
        await db.commit()

async def update_payment_status(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET payment_status = ? WHERE telegram_id = ?",
            (status, user_id)
        )
        await db.commit()

async def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 500,
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Ошибка DeepSeek API: {e}")
        return "Произошла ошибка при обработке вашего запроса."

async def start(update: Update, context: CallbackContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Продолжить', callback_data='start_form')],
        [InlineKeyboardButton('📨 Связаться с админом', callback_data='contact_admin')],
        [InlineKeyboardButton('ℹ️ О сервисе', callback_data='about_service')]
    ])
    await update.message.reply_text(
        '👋 <b>Здравствуйте!</b>\nВы обратились в <i>Отписка Бот</i> для отмены нежелательных списаний.',
        reply_markup=kb,
        parse_mode='HTML'
    )

async def handle_buttons(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'start_form':
        await query.message.edit_text('👤 Введите ФИО (Три слова, первая буква заглавная):')
        return FIO
    elif query.data == 'contact_admin':
        await query.message.edit_text('📨 Напишите ваше сообщение для админа:')
        context.user_data['contact_admin'] = True
        return ConversationHandler.END
    elif query.data == 'about_service':
        await query.message.edit_text(
            'ℹ️ <b>О сервисе:</b>\n\nМы помогаем отменить нежелательные подписки.\nСреднее время обработки: 1-3 рабочих дня.',
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    return ConversationHandler.END

async def process_fio(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not FIO_PATTERN.match(user_input):
        correction = await ask_deepseek(f"Пользователь ввел ФИО: '{user_input}'. Попроси исправить на формат 'Фамилия Имя Отчество'.")
        await update.message.reply_text(correction)
        return FIO
    
    context.user_data['fio'] = user_input
    await update.message.reply_text('📃 Укажите источник списания (сайт или сервис):')
    return SOURCE

async def process_source(update: Update, context: CallbackContext):
    user_input = update.message.text
    context.user_data['source'] = user_input
    await update.message.reply_text('🏦 Введите название банка:')
    return BANK

async def process_bank(update: Update, context: CallbackContext):
    user_input = update.message.text
    context.user_data['bank'] = user_input
    await update.message.reply_text('💳 Введите карту (формат: 123456*7890):')
    return CARD

async def process_card(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not CARD_PATTERN.match(user_input):
        correction = await ask_deepseek(f"Пользователь ввел номер карты: '{user_input}'. Попроси исправить на формат '123456*7890'.")
        await update.message.reply_text(correction)
        return CARD
    
    context.user_data['card'] = user_input
    await update.message.reply_text('📧 Введите email:')
    return EMAIL

async def process_email(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not EMAIL_PATTERN.match(user_input):
        correction = await ask_deepseek(f"Пользователь ввел email: '{user_input}'. Попроси исправить на валидный email.")
        await update.message.reply_text(correction)
        return EMAIL
    
    context.user_data['email'] = user_input
    await update.message.reply_text('📱 Введите телефон (10-15 цифр, можно с +):')
    return PHONE

async def process_phone(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not PHONE_PATTERN.match(user_input):
        correction = await ask_deepseek(f"Пользователь ввел телефон: '{user_input}'. Попроси исправить на 10-15 цифр.")
        await update.message.reply_text(correction)
        return PHONE
    
    context.user_data['phone'] = user_input
    user_id = update.message.from_user.id
    
    await save_data(user_id, context.user_data)
    
    text = (
        f"<b>Новая заявка на отписку:</b>\n"
        f"👤 ФИО: {context.user_data['fio']}\n"
        f"📄 Источник: {context.user_data['source']}\n"
        f"🏦 Банк: {context.user_data['bank']}\n"
        f"💳 Карта: {context.user_data['card']}\n"
        f"📧 Email: {context.user_data['email']}\n"
        f"📱 Телефон: {context.user_data['phone']}"
    )
    await context.bot.send_message(GROUP_ID, text, parse_mode='HTML')
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {PRICE_RUB}₽", callback_data=f"pay_rub:{PRICE_RUB}")],
        [InlineKeyboardButton(f"⭐ Оплатить {PRICE_STARS}⭐", callback_data=f"pay_stars:{PRICE_STARS}")]
    ])
    await update.message.reply_text('💰 Оплатите услугу для завершения:', reply_markup=kb)
    
    context.user_data.clear()
    return ConversationHandler.END

async def handle_free_text(update: Update, context: CallbackContext):
    user_message = update.message.text
    user_id = update.message.from_user.id
    
    if context.user_data.get('contact_admin'):
        await context.bot.send_message(GROUP_ID, f"✉️ Сообщение от пользователя {user_id}:\n{user_message}")
        await update.message.reply_text("✅ Ваше сообщение отправлено администратору.")
        context.user_data['contact_admin'] = False
        return
    
    try:
        response = await ask_deepseek(user_message)
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Ошибка обработки вопроса: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при обработке вашего вопроса. Попробуйте позже.")

async def handle_payment_button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data.startswith('pay_rub:'):
            _, amount = query.data.split(':')
            await send_invoice(context.bot, query.from_user.id, int(amount), "RUB", "Отписка Бот (рубли)")
        elif query.data.startswith('pay_stars:'):
            _, amount = query.data.split(':')
            await send_invoice(context.bot, query.from_user.id, int(amount), "STAR", "Отписка Бот (звёзды)")
    except BadRequest as e:
        logger.error(f"Ошибка платежа: {e}")
        await query.message.reply_text("⚠️ Произошла ошибка при создании платежа. Пожалуйста, попробуйте позже.")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        await query.message.reply_text("⚠️ Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.")
    finally:
        await query.message.edit_reply_markup(reply_markup=None)

async def send_invoice(bot, chat_id, amount, currency, title):
    prices = [LabeledPrice(label="Услуга отписки", amount=amount * 100)]
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description="Услуга отмены нежелательных подписок и списаний",
        payload="unsubscription_service",
        provider_token=PAYMENT_PROVIDER_TOKEN,  # Исправлено имя переменной
        currency=currency,
        prices=prices
    )

async def pre_checkout(update: Update, context: CallbackContext):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    payment = update.message.successful_payment
    
    await update_payment_status(user_id, "completed")
    
    await context.bot.send_message(
        GROUP_ID,
        f"✅ Оплата прошла: {update.message.from_user.full_name} (ID {user_id})\nСумма: {payment.total_amount / 100} {payment.currency}"
    )
    
    await update.message.reply_text(
        "✅ Ваш платёж подтверждён! Мы уже начали работу над вашей отпиской.\nОбычно это занимает 1-3 рабочих дня."
    )

async def cancel(update: Update, context: CallbackContext):
    await update.message.reply_text('❌ Процесс отменён.')
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Ошибка при обработке обновления: {context.error}")
    if update.callback_query:
        await update.callback_query.answer("⚠️ Произошла ошибка. Пожалуйста, попробуйте снова.")
    elif update.message:
        await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте снова.")

async def main():
    await init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    # Удаление вебхука перед запуском
    await application.bot.delete_webhook(drop_pending_updates=True)
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_buttons, pattern='^start_form$')],
        states={
            FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_fio)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_source)],
            BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_bank)],
            CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_card)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_email)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_phone)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=True
    )
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_payment_button, pattern='^pay_(rub|stars):'))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))
    
    # Добавление обработчика ошибок
    application.add_error_handler(error_handler)
    
    logger.info("Бот для отписки запущен")
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
