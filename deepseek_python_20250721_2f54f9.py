import os
import re
import json
import logging
import aiosqlite
import asyncio
from datetime import datetime
from logging.handlers import RotatingFileHandler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler, JobQueue,
    ConversationHandler, ContextTypes
)
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("7514289103:AAEPjVet23FqF7VGsqKEE6rVw-H348w-_XQ")
API_STARS_PROVIDER_TOKEN = os.getenv("7514289103:AAEPjVet23FqF7VGsqKEE6rVw-H348w-_XQ")  # Токен для оплаты

# Проверка наличия обязательных ключей
if not TOKEN or not API_STARS_PROVIDER_TOKEN:
    raise ValueError("Не найдены обязательные переменные окружения! Проверьте .env файл.")

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Форматтер для логов
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Логирование в файл с ротацией
file_handler = RotatingFileHandler(
    'unsub_bot.log', 
    maxBytes=5*1024*1024,  # 5 MB
    backupCount=3
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Логирование в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Уменьшаем логирование сторонних библиотек
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''INSERT INTO users 
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

# Команда /start с кнопками
async def start(update: Update, context: CallbackContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Продолжить', callback_data='start_form')],
        [InlineKeyboardButton('📨 Связаться с админом', callback_data='contact_admin')]
    ])
    await update.message.reply_text(
        '👋 <b>Здравствуйте!</b>\n'
        'Вы обратились в <i>Отписка Бот</i> для отмены нежелательных списаний.',
        reply_markup=kb
    )
    return ConversationHandler.END

# Обработка кнопок стартового меню
async def handle_buttons(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'start_form':
        await query.message.edit_text('👤 Введите ФИО (Три слова, первая буква заглавная):')
        return FIO
    elif query.data == 'contact_admin':
        await query.message.edit_text('📨 Напишите ваше сообщение для админа:')
        # Здесь можно добавить логику пересылки сообщения админу
        return ConversationHandler.END
    
    return ConversationHandler.END

# Обработчики формы с валидацией
async def process_fio(update: Update, context: CallbackContext):
    user_input = update.message.text
    if not FIO_PATTERN.match(user_input):
        await update.message.reply_text('❗ Неверный формат ФИО. Например: Иванов Петр Сергеевич')
        return FIO
    
    context.user_data['fio'] = user_input
    await update.message.reply_text('📃 Укажите источник списания (сайт или сервис):')
    return SOURCE

async def process_source(update: Update, context: CallbackContext):
    user_input = update.message.text
    if len(user_input) < 3:
        await update.message.reply_text('❗ Опишите источник минимум 3 символами')
        return SOURCE
    
    context.user_data['source'] = user_input
    await update.message.reply_text('🏦 Введите название банка:')
    return BANK

async def process_bank(update: Update, context: CallbackContext):
    user_input = update.message.text
    if not re.match(r'^[\w\s]{3,}$', user_input):
        await update.message.reply_text('❗ Неверное название банка')
        return BANK
    
    context.user_data['bank'] = user_input
    await update.message.reply_text('💳 Введите карту (формат: 123456*7890):')
    return CARD

async def process_card(update: Update, context: CallbackContext):
    user_input = update.message.text
    if not CARD_PATTERN.match(user_input):
        await update.message.reply_text('❗ Формат карты: 6 цифр, звёздочка, 4 цифры. Пример: 123456*7890')
        return CARD
    
    context.user_data['card'] = user_input
    await update.message.reply_text('📧 Введите email:')
    return EMAIL

async def process_email(update: Update, context: CallbackContext):
    user_input = update.message.text
    if not EMAIL_PATTERN.match(user_input):
        await update.message.reply_text('❗ Некорректный email. Пример: user@example.com')
        return EMAIL
    
    context.user_data['email'] = user_input
    await update.message.reply_text('📱 Введите телефон (10-15 цифр, можно с +):')
    return PHONE

async def process_phone(update: Update, context: CallbackContext):
    user_input = update.message.text
    if not PHONE_PATTERN.match(user_input):
        await update.message.reply_text('❗ Некорректный телефон. Только цифры, 10-15 символов')
        return PHONE
    
    context.user_data['phone'] = user_input
    user_id = update.message.from_user.id
    
    # Сохраняем данные
    await save_data(user_id, context.user_data)
    
    # Отправляем уведомление в группу
    text = (
        f"<b>Новая заявка на отписку:</b>\n"
        f"👤 ФИО: {context.user_data['fio']}\n"
        f"📄 Источник: {context.user_data['source']}\n"
        f"🏦 Банк: {context.user_data['bank']}\n"
        f"💳 Карта: {context.user_data['card']}\n"
        f"📧 Email: {context.user_data['email']}\n"
        f"📱 Телефон: {context.user_data['phone']}"
    )
    await context.bot.send_message(GROUP_ID, text)
    
    # Опции оплаты
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {PRICE_RUB}₽", callback_data=f"pay_rub:{PRICE_RUB}")],
        [InlineKeyboardButton(f"⭐ Оплатить {PRICE_STARS}⭐", callback_data=f"pay_stars:{PRICE_STARS}")]
    ])
    await update.message.reply_text('💰 Оплатите услугу для завершения:', reply_markup=kb)
    
    # Очищаем данные
    context.user_data.clear()
    return ConversationHandler.END

# Обработчики оплаты
async def handle_payment_button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('pay_rub:'):
        _, amount = query.data.split(':')
        await send_invoice(query.from_user.id, int(amount), "RUB", "Отписка Бот (рубли)")
        
    elif query.data.startswith('pay_stars:'):
        _, amount = query.data.split(':')
        await send_invoice(query.from_user.id, int(amount), "STAR", "Отписка Бот (звёзды)")
    
    await query.message.edit_reply_markup(reply_markup=None)

async def send_invoice(chat_id, amount, currency, title):
    prices = [LabeledPrice(label="Услуга отписки", amount=amount * 100)]
    
    await Application.builder().token(TOKEN).build().bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description="Услуга отмены нежелательных подписок и списаний",
        payload="unsubscription_service",
        provider_token=API_STARS_PROVIDER_TOKEN,
        currency=currency,
        prices=prices
    )

async def pre_checkout(update: Update, context: CallbackContext):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    payment = update.message.successful_payment
    
    # Обновляем статус платежа
    await update_payment_status(user_id, "completed")
    
    # Уведомление в группу
    await context.bot.send_message(
        GROUP_ID,
        f"✅ Оплата прошла: {update.message.from_user.full_name} (ID {user_id})\n"
        f"Сумма: {payment.total_amount / 100} {payment.currency}"
    )
    
    # Сообщение пользователю
    await update.message.reply_text(
        "✅ Ваш платёж подтверждён! Мы уже начали работу над вашей отпиской.\n"
        "Обычно это занимает 1-3 рабочих дня. Вы получите уведомление о результате."
    )

# Отмена формы
async def cancel(update: Update, context: CallbackContext):
    await update.message.reply_text('❌ Процесс отменён.')
    context.user_data.clear()
    return ConversationHandler.END

# Основная функция
def main():
    # Инициализация БД
    asyncio.run(init_db())
    
    # Создание приложения
    application = Application.builder().token(TOKEN).build()
    
    # Обработчики
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CallbackQueryHandler(handle_buttons, pattern='^(start_form|contact_admin)$')
        ],
        states={
            FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_fio)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_source)],
            BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_bank)],
            CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_card)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_email)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_phone)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_payment_button, pattern='^pay_(rub|stars):'))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    
    # Запуск бота
    logger.info("Бот для отписки запущен")
    application.run_polling()

# Точка входа
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")