import os
if os.path.exists("userdata.db"):
    os.remove("userdata.db")

import asyncio
import logging
import aiosqlite
import re
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

API_TOKEN = '7514289103:AAEPjVet23FqF7VGsqKEE6rVw-H348w-_XQ'
API_STARS_PROVIDER_TOKEN = 'YOUR_STARS_PROVIDER_TOKEN_HERE'
GROUP_ID = -1002579257687

# Фиксированная стоимость услуг в рублях и звёздах
PRICE_RUB = 399
PRICE_STARS = 200

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class Form(StatesGroup):
    fio = State()
    source = State()
    bank = State()
    card = State()
    email = State()
    phone = State()


def is_valid_fio(text: str) -> bool:
    parts = text.strip().split()
    if len(parts) != 3:
        return False
    pattern = r'^[А-ЯЁ][а-яё]+'
    return all(re.match(pattern, part) for part in parts)

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    text = (
        "👋 <b>Здравствуйте!</b>\n"
        "Вы обратились в <i>«Отписка Бот»</i> — помощника в вопросах списаний.\n\n"
        "🔍 Мы помогаем:\n"
        "— выяснить источники списаний\n"
        "— отправить анкету на отмену услуг\n\n"
        "📋 Нажмите «Продолжить», чтобы начать"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Продолжить", callback_data="agree")],
        [InlineKeyboardButton(text="📨 Связаться с админом", callback_data="contact_admin")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "agree")
async def agree_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.fio)
    await callback.message.answer("👤 Введите своё <b>ФИО</b>:")
    await callback.answer()

@dp.callback_query(F.data == "contact_admin")
async def contact_admin(callback: CallbackQuery):
    await callback.message.answer("📨 Напишите ваше сообщение, и мы ответим вам.")
    await callback.answer()

@dp.message(Form.fio)
async def process_fio(message: Message, state: FSMContext):
    if not is_valid_fio(message.text):
        await message.answer("❗ Пожалуйста, введите корректное ФИО, например: <b>Иванов Петр Сергеевич</b>")
        return
    await state.update_data(fio=message.text)
    await state.set_state(Form.source)
    await message.answer("📃 Укажите, откуда идёт списание (сайт/сервис):")

@dp.message(Form.source)
async def process_source(message: Message, state: FSMContext):
    await state.update_data(source=message.text)
    await state.set_state(Form.bank)
    await message.answer("🏦 Введите название банка, с которого происходят списания:")

@dp.message(Form.bank)
async def process_bank(message: Message, state: FSMContext):
    await state.update_data(bank=message.text)
    await state.set_state(Form.card)
    await message.answer("💳 Введите 6 первых и 4 последних цифры карты (123456*7890):")

@dp.message(Form.card)
async def process_card(message: Message, state: FSMContext):
    await state.update_data(card=message.text)
    await state.set_state(Form.email)
    await message.answer("📧 Введите ваш email:")

@dp.message(Form.email)
async def process_email(message: Message, state: FSMContext):
    await state.update_data(email=message.text)
    await state.set_state(Form.phone)
    await message.answer("📱 Введите ваш номер телефона:")

@dp.message(Form.phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    data = await state.get_data()

    await recreate_db_if_needed()
    await save_to_db(message.from_user.id, data)

    text = (
        f"<b>Новая анкета:</b>\n"
        f"👤 ФИО: {data['fio']}\n"
        f"📄 Источник списания: {data['source']}\n"
        f"🏦 Банк: {data['bank']}\n"
        f"💳 Карта: {data['card']}\n"
        f"📧 Email: {data['email']}\n"
        f"📱 Телефон: {data['phone']}"
    )
    await bot.send_message(GROUP_ID, text)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {PRICE_RUB}₽", url="https://www.tinkoff.ru")],
        [InlineKeyboardButton(text=f"⭐ Оплатить {PRICE_STARS} звёзд", callback_data=f"pay_stars:{PRICE_STARS}")]
    ])
    await message.answer(
        f"💰 Для завершения процедуры оплатите {PRICE_RUB}₽ или {PRICE_STARS} звёзд:",
        reply_markup=kb
    )
    await message.answer("✅ Ваша заявка принята. Ожидайте ответ в течение 48 часов.")
    await state.clear()

@dp.callback_query(F.data.startswith("pay_stars:"))
async def pay_stars_callback(callback: CallbackQuery):
    _, amount = callback.data.split(":")
    stars = int(amount)
    prices = [LabeledPrice(label="Отписка Бот", amount=stars)]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Оплата звёздами",
        description="Оплата услуг по отмене подписок",
        payload="payment_stars",
        provider_token=API_STARS_PROVIDER_TOKEN,
        currency="STAR",
        prices=prices
    )
    await callback.answer()

@dp.pre_checkout_query(lambda query: True)
async def process_pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query_id=query.id, ok=True)

@dp.message(F.content_type == types.ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message):
    await message.answer("✅ Оплата прошла успешно! Ваша заявка будет обработана.")
    # Здесь можно запустить логику автоматической обработки

async def recreate_db_if_needed():
    async with aiosqlite.connect("userdata.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                fio TEXT,
                source TEXT,
                bank TEXT,
                card TEXT,
                email TEXT,
                phone TEXT
            )
        """
        )
        await db.commit()

async def save_to_db(user_id, data):
    async with aiosqlite.connect("userdata.db") as db:
        await db.execute("""
            INSERT INTO users (telegram_id, fio, source, bank, card, email, phone)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            data['fio'],
            data['source'],
            data['bank'],
            data['card'],
            data['email'],
            data['phone']
        ))
        await db.commit()

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    