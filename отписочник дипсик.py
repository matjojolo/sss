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
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler, JobQueue,
    ConversationHandler, ContextTypes, PreCheckoutQueryHandler
)
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
API_STARS_PROVIDER_TOKEN = os.getenv("PAYMENT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Проверка наличия обязательных ключей
if not TOKEN or not API_STARS_PROVIDER_TOKEN or not DEEPSEEK_API_KEY:
    raise ValueError("Не найдены обязательные переменные окружения! Проверьте .env файл.")

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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
                telegram_id INTEGER UNIQUE,
                fio TEXT, source TEXT, bank TEXT, card TEXT, email TEXT, phone TEXT,
                payment_status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_text TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
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

async def save_message(user_id, message_text, is_admin=False):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, message_text, is_admin) VALUES (?, ?, ?)",
            (user_id, message_text, is_admin)
        )
        await db.commit()

# Функция для общения с DeepSeek API
async def ask_deepseek(prompt, context=None):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    
    system_prompt = """
    Ты - помощник в боте для отмены подписок. Твоя задача:
    1. Помогать пользователям правильно формулировать запросы для формы отписки
    2. Отвечать на вопросы о процессе отписки
    3. Объяснять, как работает сервис
    4. Корректировать некорректные формулировки пользователя
    
    Важные правила:
    - Если пользователь спрашивает о процессе отписки, дай краткий ответ
    - Если пользователь ввел данные с ошибкой, вежливо попроси исправить
    - Не отвечай на вопросы, не связанные с отпиской
    - Сохраняй профессиональный и вежливый тон
    - Отвечай кратко, 1-3 предложения
    """
    
    messages = [{"role": "system", "content": system_prompt}]
    
    if context:
        messages.extend(context)
    
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 500,
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        logger.warning("Тайм-аут при запросе к DeepSeek API")
        return "Извините, сейчас не могу ответить. Пожалуйста, попробуйте позже."
    except Exception as e:
        logger.error(f"Ошибка DeepSeek API: {e}")
        return "Произошла ошибка при обработке вашего запроса."

# Команда /start с кнопками
async def start(update: Update, context: CallbackContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Продолжить', callback_data='start_form')],
        [InlineKeyboardButton('📨 Связаться с админом', callback_data='contact_admin')],
        [InlineKeyboardButton('ℹ️ О сервисе', callback_data='about_service')]
    ])
    await update.message.reply_text(
        '👋 <b>Здравствуйте!</b>\n'
        'Вы обратились в <i>Отписка Бот</i> для отмены нежелательных списаний.',
        reply_markup=kb,
        parse_mode='HTML'
    )

# Обработка кнопок стартового меню
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
            'ℹ️ <b>О сервисе:</b>\n\n'
            'Мы помогаем отменить нежелательные подписки и списания с ваших карт.\n'
            'Среднее время обработки заявки: 1-3 рабочих дня.\n'
            'Стоимость услуги: 399 руб или 200 звезд.',
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    return ConversationHandler.END

# Обработчики формы с валидацией и коррекцией через DeepSeek
async def process_fio(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not FIO_PATTERN.match(user_input):
        correction_prompt = f"Пользователь ввел ФИО: '{user_input}'. Это не соответствует формату 'Фамилия Имя Отчество'. Попроси исправить."
        correction = await ask_deepseek(correction_prompt)
        await update.message.reply_text(correction)
        return FIO
    
    context.user_data['fio'] = user_input
    await update.message.reply_text('📃 Укажите источник списания (сайт или сервис):')
    return SOURCE

async def process_source(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if len(user_input) < 3:
        correction_prompt = f"Пользователь описал источник списания: '{user_input}'. Это слишком коротко. Попроси дать более подробное описание."
        correction = await ask_deepseek(correction_prompt)
        await update.message.reply_text(correction)
        return SOURCE
    
    context.user_data['source'] = user_input
    await update.message.reply_text('🏦 Введите название банка:')
    return BANK

async def process_bank(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not re.match(r'^[\w\s]{3,}$', user_input):
        correction_prompt = f"Пользователь ввел название банка: '{user_input}'. Это не похоже на реальное название банка. Попроси уточнить."
        correction = await ask_deepseek(correction_prompt)
        await update.message.reply_text(correction)
        return BANK
    
    context.user_data['bank'] = user_input
    await update.message.reply_text('💳 Введите карту (формат: 123456*7890):')
    return CARD

async def process_card(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not CARD_PATTERN.match(user_input):
        correction_prompt = f"Пользователь ввел номер карты: '{user_input}'. Требуется формат '123456*7890'. Попроси исправить."
        correction = await ask_deepseek(correction_prompt)
        await update.message.reply_text(correction)
        return CARD
    
    context.user_data['card'] = user_input
    await update.message.reply_text('📧 Введите email:')
    return EMAIL

async def process_email(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not EMAIL_PATTERN.match(user_input):
        correction_prompt = f"Пользователь ввел email: '{user_input}'. Это не похоже на валидный email. Попроси исправить."
        correction = await ask_deepseek(correction_prompt)
        await update.message.reply_text(correction)
        return EMAIL
    
    context.user_data['email'] = user_input
    await update.message.reply_text('📱 Введите телефон (10-15 цифр, можно с +):')
    return PHONE

async def process_phone(update: Update, context: CallbackContext):
    user_input = update.message.text
    
    if not PHONE_PATTERN.match(user_input):
        correction_prompt = f"Пользователь ввел телефон: '{user_input}'. Требуется формат из 10-15 цифр. Попроси исправить."
        correction = await ask_deepseek(correction_prompt)
        await update.message.reply_text(correction)
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
    await context.bot.send_message(GROUP_ID, text, parse_mode='HTML')
    
    # Опции оплаты
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {PRICE_RUB}₽", callback_data=f"pay_rub:{PRICE_RUB}")],
        [InlineKeyboardButton(f"⭐ Оплатить {PRICE_STARS}⭐", callback_data=f"pay_stars:{PRICE_STARS}")]
    ])
    await update.message.reply_text('💰 Оплатите услугу для завершения:', reply_markup=kb)
    
    # Очищаем данные
    context.user_data.clear()
    return ConversationHandler.END

# Обработка свободных вопросов через DeepSeek
async def handle_free_text(update: Update, context: CallbackContext):
    user_message = update.message.text
    user_id = update.message.from_user.id
    
    if context.user_data.get('contact_admin'):
        admin_text = f"✉️ Сообщение от пользователя {user_id}:\n{user_message}"
        await context.bot.send_message(GROUP_ID, admin_text)
        await save_message(user_id, user_message)
        await update.message.reply_text("✅ Ваше сообщение отправлено администратору.")
        context.user_data['contact_admin'] = False
        return
    
    try:
        response = await ask_deepseek(user_message)
        await update.message.reply_text(response)
        await save_message(user_id, user_message)
        await save_message(user_id, response, is_admin=True)
    except Exception as e:
        logger.error(f"Ошибка обработки вопроса: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при обработке вашего вопроса. Попробуйте позже.")

# Обработчики оплаты
async def handle_payment_button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('pay_rub:'):
        _, amount = query.data.split(':')
        await send_invoice(context.bot, query.from_user.id, int(amount), "RUB", "Отписка Бот (рубли)")
        
    elif query.data.startswith('pay_stars:'):
        _, amount = query.data.split(':')
        await send_invoice(context.bot, query.from_user.id, int(amount), "STAR", "Отписка Бот (звёзды)")
    
    await query.message.edit_reply_markup(reply_markup=None)

async def send_invoice(bot, chat_id, amount, currency, title):
    prices = [LabeledPrice(label="Услуга отписки", amount=amount * 100)]
    
    await bot.send_invoice(
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
    
    await update_payment_status(user_id, "completed")
    
    await context.bot.send_message(
        GROUP_ID,
        f"✅ Оплата прошла: {update.message.from_user.full_name} (ID {user_id})\n"
        f"Сумма: {payment.total_amount / 100} {payment.currency}"
    )
    
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
    # Создание нового event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Инициализация БД
        loop.run_until_complete(init_db())
        
        # Создание приложения
        application = Application.builder().token(TOKEN).build()
        
        # Обработчики
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CallbackQueryHandler(handle_buttons, pattern='^(start_form|contact_admin|about_service)$')
            ],
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
        
        application.add_handler(conv_handler)
        application.add_handler(CallbackQueryHandler(handle_payment_button, pattern='^pay_(rub|stars):'))
        application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
        application.add_handler(PreCheckoutQueryHandler(pre_checkout))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))
        
        # Запуск бота
        logger.info("Бот для отписки запущен")
        application.run_polling()
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
    finally:
        loop.close()

if __name__ == "__main__":
    main()
