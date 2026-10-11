import asyncio
import logging
import os
import re
import io
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
import aiosqlite
import pdfplumber

# ДАННЫЕ БОТА
TOKEN = "8898464745:AAGsD4g7IkW2tO3hzPXWeB1yjjiR2DtWJnc"
SCHEDULE_URL = "https://urgt66.ru/partition/136056/"
DB_NAME = "users.db"

logging.basicConfig(level=logging.INFO)

class RegisterState(StatesGroup):
    waiting_for_group = State()

class GetScheduleState(StatesGroup):
    waiting_for_date = State()

# Инициализация БД
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                group_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_files (
                file_url TEXT PRIMARY KEY
            )
        """)
        await db.commit()

# Команда /start
async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await message.answer(
                    f"Привет! Твоя группа: **{row[0]}**.\n\n"
                    "📌 **Команды:**\n"
                    "• /get — запросить расписание на нужную дату (например, на 10 число)\n"
                    "• /change — сменить учебную группу",
                    parse_mode="Markdown"
                )
            else:
                await message.answer(
                    "Привет! Напиши название своей группы (например: ИТ-12 или ИС-21), чтобы я присылал расписание:",
                    parse_mode="Markdown"
                )
                await state.set_state(RegisterState.waiting_for_group)

# Команда /change
async def cmd_change(message: Message, state: FSMContext):
    await message.answer("Введи новое название своей группы:")
    await state.set_state(RegisterState.waiting_for_group)

# Сохранение группы
async def process_group(message: Message, state: FSMContext):
    group_name = message.text.strip().upper()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, group_name) VALUES (?, ?)",
            (message.from_user.id, group_name)
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"Готово! Группа **{group_name}** сохранена.\n"
        "Теперь ты можешь запрашивать расписание через команду /get", 
        parse_mode="Markdown"
    )

# Поиск расписания группы в PDF
def extract_group_schedule(pdf_bytes: bytes, group_name: str) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            group_text_lines = []
            found = False
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue
                lines = text.split('\n')
                for line in lines:
                    if group_name in line.upper():
                        found = True
                    if found:
                        group_text_lines.append(line)
                        if len(group_text_lines) > 20:
                            break
                if found:
                    break
            if group_text_lines:
                return "\n".join(group_text_lines)
    except Exception as e:
        logging.error(f"Ошибка чтения PDF: {e}")
    return ""

# Получение всех PDF ссылок с сайта
async def fetch_pdf_links():
    links = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SCHEDULE_URL) as resp:
                if resp.status != 200:
                    return links
                html = await resp.text()

        soup = BeautifulSoup(html, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if '.pdf' in href.lower() or 'расписание' in text.lower():
                if href.startswith('/'):
                    base_site = SCHEDULE_URL.split('/')[0] + '//' + SCHEDULE_URL.split('/')[2]
                    href = base_site + href
                elif not href.startswith('http'):
                    href = SCHEDULE_URL.rsplit('/', 1)[0] + '/' + href
                links.append((href, text))
    except Exception as e:
        logging.error(f"Ошибка получения ссылок: {e}")
    return links

# Команда /get — Запрос расписания по дате
async def cmd_get_schedule(message: Message, state: FSMContext):
    await message.answer("Напиши число или дату, за которую нужно расписание (например: `10` или `10.10`):", parse_mode="Markdown")
    await state.set_state(GetScheduleState.waiting_for_date)

# Обработка введенной даты
async def process_date_request(message: Message, state: FSMContext):
    search_date = message.text.strip()
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            user_row = await cursor.fetchone()

    if not user_row:
        await message.answer("Сначала укажи свою группу с помощью команды /start")
        return

    group_name = user_row[0]
    status_msg = await message.answer(f"🔍 Ищу расписание на `{search_date}` число для группы **{group_name}**...", parse_mode="Markdown")

    links = await fetch_pdf_links()
    if not links:
        await status_msg.edit_text("❌ Не удалось загрузить файлы с сайта техникума.")
        return

    # Фильтруем ссылки по вхождению указанного числа/даты
    matched_links = []
    for url, text in links:
        combined_title = f"{text} {url}".lower()
        if search_date.lower() in combined_title:
            matched_links.append((url, text))

    if not matched_links:
        # Если совпадений нет, берем последний доступный PDF-файл с сайта
        matched_links = [links[0]]

    pdf_link, link_title = matched_links[0]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(pdf_link) as resp:
                if resp.status != 200:
                    await status_msg.edit_text("❌ Не удалось скачать PDF-файл.")
                    return
                pdf_bytes = await resp.read()

        schedule_text = extract_group_schedule(pdf_bytes, group_name)

        if schedule_text:
            await status_msg.edit_text(
                f"📅 **Расписание ({link_title}):**\n\n```{schedule_text}```",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"📄 Файл **{link_title}** найден, но строчек для группы `{group_name}` в нем не обнаружено.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logging.error(f"Ошибка обработки: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при обработке расписания.")

# Автоматическая рассылка по планировщику
async def check_schedule(bot: Bot):
    logging.info("Проверка новых файлов расписания...")
    links = await fetch_pdf_links()
    if not links:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        for pdf_link, link_text in links:
            async with db.execute("SELECT 1 FROM sent_files WHERE file_url = ?", (pdf_link,)) as cursor:
                if await cursor.fetchone():
                    continue

            logging.info(f"Найдено новое расписание: {link_text}")
            async with aiohttp.ClientSession() as session:
                async with session.get(pdf_link) as pdf_resp:
                    if pdf_resp.status != 200:
                        continue
                    pdf_bytes = await pdf_resp.read()

            await db.execute("INSERT INTO sent_files (file_url) VALUES (?)", (pdf_link,))
            await db.commit()

            async with db.execute("SELECT user_id, group_name FROM users") as cursor:
                users = await cursor.fetchall()

            for user_id, group_name in users:
                personal_schedule = extract_group_schedule(pdf_bytes, group_name)
                try:
                    if personal_schedule:
                        await bot.send_message(
                            user_id,
                            f"🔔 **Новое расписание!** ({link_text})\n\n```{personal_schedule}```",
                            parse_mode="Markdown"
                        )
                    await asyncio.sleep(0.2)
                except Exception as e:
                    logging.error(f"Ошибка отправки пользователю {user_id}: {e}")

# Веб-сервер для Render
async def handle(request):
    return web.Response(text="Бот запущен!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    await init_db()
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация команд
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_change, Command("change"))
    dp.message.register(cmd_get_schedule, Command("get"))
    dp.message.register(process_group, RegisterState.waiting_for_group)
    dp.message.register(process_date_request, GetScheduleState.waiting_for_date)

    # Планировщик
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_schedule, 'interval', minutes=30, args=(bot,))
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
