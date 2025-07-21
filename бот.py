import os
import re
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, ContentType
)
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# Telegram bot token
API_TOKEN = '7514289103:AAEPjVet23FqF7VGsqKEE6rVw-H348w-_XQ'
# Для оплаты звёздами переиспользуем тот же токен
API_STARS_PROVIDER_TOKEN = '7514289103:AAEPjVet23FqF7VGsqKEE6rVw-H348w-_XQ'

# ID группы для уведомлений
GROUP_ID = -1002579257687

# Путь к базе и цены
DB_PATH = 'userdata.db'
PRICE_RUB = 399
PRICE_STARS = 200

# Флаг проверки платежей через DeepSik
USE_DEEPSEEK = True
if USE_DEEPSEEK:
    import deepsik
    deepsik_client = deepsik.Client(api_key='YOUR_DEEPSEEK_API_KEY')
else:
    deepsik_client = None

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# FSM состояния
class Form(StatesGroup):
    fio = State()
    source = State()
    bank = State()
    card = State()
    email = State()
    phone = State()

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
                fio TEXT, source TEXT, bank TEXT, card TEXT, email TEXT, phone TEXT
            )''')
        await db.commit()

async def save_data(user_id, data):
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO users (telegram_id, fio, source, bank, card, email, phone) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (user_id, data['fio'], data['source'], data['bank'], data['card'], data['email'], data['phone'])
        )
        await db.commit()

# Команда /start с кнопками
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Продолжить', callback_data='start_form')],
        [InlineKeyboardButton('📨 Связаться с админом', callback_data='contact_admin')]
    ])
    await message.answer(
        '👋 <b>Здравствуйте!</b>\n'
        'Вы обратились в <i>Отписка Бот</i>.',
        reply_markup=kb
    )

@dp.callback_query(F.data=='start_form')
async def start_form(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Form.fio)
    await cb.message.edit_text('👤 Введите ФИО (Три слова, первая буква заглавная):')
    await cb.answer()

@dp.callback_query(F.data=='contact_admin')
async def contact_admin(cb: CallbackQuery):
    await cb.message.edit_text('📨 Напишите ваше сообщение для админа:')
    await cb.answer()

# Обработчики формы с валидацией
@dp.message(Form.fio)
async def process_fio(msg: Message, state: FSMContext):
    if not FIO_PATTERN.match(msg.text):
        return await msg.answer('❗ Неверный формат ФИО. Например: Иванов Петр Сергеевич')
    await state.update_data(fio=msg.text)
    await state.set_state(Form.source)
    await msg.answer('📃 Укажите источник списания (сайт или сервис):')

@dp.message(Form.source)
async def process_source(msg: Message, state: FSMContext):
    if len(msg.text) < 3:
        return await msg.answer('❗ Опишите источник минимум 3 символами')
    await state.update_data(source=msg.text)
    await state.set_state(Form.bank)
    await msg.answer('🏦 Введите название банка:')

@dp.message(Form.bank)
async def process_bank(msg: Message, state: FSMContext):
    if not re.match(r'^[\w\s]{3,}$', msg.text):
        return await msg.answer('❗ Неверное название банка')
    await state.update_data(bank=msg.text)
    await state.set_state(Form.card)
    await msg.answer('💳 Введите карту (123456*7890):')

@dp.message(Form.card)
async def process_card(msg: Message, state: FSMContext):
    if not CARD_PATTERN.match(msg.text):
        return await msg.answer('❗ Формат карты: 6 цифр, звёздочка, 4 цифры')
    await state.update_data(card=msg.text)
    await state.set_state(Form.email)
    await msg.answer('📧 Введите email:')

@dp.message(Form.email)
async def process_email(msg: Message, state: FSMContext):
    if not EMAIL_PATTERN.match(msg.text):
        return await msg.answer('❗ Некорректный email. Пример: user@example.com')
    await state.update_data(email=msg.text)
    await state.set_state(Form.phone)
    await msg.answer('📱 Введите телефон (10–15 цифр, можно с +):')

@dp.message(Form.phone)
async def process_phone(msg: Message, state: FSMContext):
    if not PHONE_PATTERN.match(msg.text):
        return await msg.answer('❗ Некорректный телефон. Только цифры, 10–15 символов')
    data = await state.get_data()
    await state.clear()
    await save_data(msg.from_user.id, data)
    # Отправляем админам
    text = (
        f"<b>Новая заявка:</b>\n"
        f"👤 ФИО: {data['fio']}\n"
        f"📄 Источник: {data['source']}\n"
        f"🏦 Банк: {data['bank']}\n"
        f"💳 Карта: {data['card']}\n"
        f"📧 Email: {data['email']}\n"
        f"📱 Телефон: {data['phone']}"
    )
    await bot.send_message(GROUP_ID, text)
    # Опции оплаты
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {PRICE_RUB}₽", url="https://www.tinkoff.ru")],
        [InlineKeyboardButton(f"⭐ Оплатить {PRICE_STARS}⭐", callback_data=f"pay_stars:{PRICE_STARS}")]
    ])
    await msg.answer('💰 Оплатите для завершения:', reply_markup=kb)

# Обработчики оплаты
@dp.callback_query(F.data.startswith('pay_stars:'))
async def pay_stars_cb(cb: CallbackQuery):
    _, amt = cb.data.split(':')
    prices = [LabeledPrice(label="Отписка Бот", amount=int(amt))]
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="Оплата звёздами",
        description="Оплата услуг по отмене списаний",
        payload="stars",
        provider_token=API_STARS_PROVIDER_TOKEN,
        currency="STAR",
        prices=prices
    )
    await cb.answer()

@dp.pre_checkout_query(lambda q: True)
async def pre_checkout(q: PreCheckoutQuery):
    # Проверка платежа через DeepSik (если включено)
    if USE_DEEPSEEK and deepsik_client:
        user_id = q.from_user.id
        amount = q.total_amount
        payload = getattr(q, 'invoice_payload', None)
        try:
            result = deepsik_client.verify_payment(user_id=user_id, amount=amount, payload=payload)
        except Exception:
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Ошибка проверки платежа.")
            return
        if not getattr(result, 'valid', False):
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Платеж не прошёл проверку.")
            await bot.send_message(GROUP_ID, f"⚠️ Платёж пользователя {user_id} не прошёл проверку DeepSik.")
            return
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_pay(msg: Message):
    await bot.send_message(GROUP_ID, f"✅ Оплата прошла: {msg.from_user.full_name} (ID {msg.from_user.id})")
    await msg.answer("✅ Ваш платёж подтвержён.")

# Запуск бота
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
