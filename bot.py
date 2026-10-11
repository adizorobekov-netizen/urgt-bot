import asyncio
import logging
import os
import io
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
import aiosqlite
import pdfplumber

TOKEN = "8898464745:AAGsD4g7IkW2tO3hzPXWeB1yjjiR2DtWJnc"  # ЗАМЕНИТЕ НА СВОЙ ТОКЕН ИЗ BOTFATHER ПРИ НЕОБХОДИМОСТИ
SCHEDULE_URL = "https://urgt66.ru/partition/136056/"
DB_NAME = "users.db"

logging.basicConfig(level=logging.INFO)

class RegisterState(StatesGroup):
    waiting_for_group = State()

class GetScheduleState(StatesGroup):
    waiting_for_date = State()

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

async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await message.answer(
                    f"Привет! Твоя группа: **{row[0]}**.\n\n"
                    "📌 **Команды:**\n"
                    "• /get — запросить расписание на нужную дату (например, на 10 или 12.10)\n"
                    "• /change — сменить учебную группу",
                    parse_mode="Markdown"
                )
            else:
                await message.answer(
                    "Привет! Напиши название своей группы (например: ИТ-122 или ИС-21), чтобы я присылал расписание:",
                    parse_mode="Markdown"
                )
                await state.set_state(RegisterState.waiting_for_group)

async def cmd_change(message: Message, state: FSMContext):
    await message.answer("Введи новое название своей группы:")
    await state.set_state(RegisterState.waiting_for_group)

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

def extract_group_schedule(pdf_bytes: bytes, group_name: str) -> str:
    try:
        clean_target = group_name.replace("-", "").replace("–", "").replace(" ", "").upper()
        
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue
                
                lines = text.split('\n')
                block_lines = []
                capturing = False
                
                for line in lines:
                    clean_line = line.replace("-", "").replace("–", "").replace(" ", "").upper()
                    if clean_target in clean_line:
                        capturing = True
                    
                    if capturing:
                        block_lines.append(line)
                        if len(block_lines) >= 30:
                            break
                
                if block_lines:
                    return "\n".join(block_lines)
    except Exception as e:
        logging.error(f"Ошибка чтения PDF: {e}")
    return ""

async def fetch_pdf_links():
    links = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        async with aiohttp.ClientSession() as session:
            async with session.get(SCHEDULE_URL, headers=headers) as resp:
                if resp.status != 200:
                    return links
                html = await resp.text()

        soup = BeautifulSoup(html, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if '.pdf' in href.lower() or 'расписание' in text.lower():
                if href.startswith('//'):
                    href = 'https:' + href
                elif href.startswith('/'):
                    href = 'https://urgt66.ru' + href
                elif not href.startswith('http'):
                    href = SCHEDULE_URL.rstrip('/') + '/' + href
                links.append((href, text if text else "Расписание"))
    except Exception as e:
        logging.error(f"Ошибка получения ссылок: {e}")
    return links

async def download_pdf_content(session, url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            content = await resp.read()
            if content.startswith(b'%PDF') or b'obj' in content[:100]:
                return content
            
            soup = BeautifulSoup(content.decode('utf-8', errors='ignore'), 'html.parser')
            for a in soup.find_all('a', href=True):
                if '.pdf' in a['href'].lower():
                    pdf_url = a['href']
                    if pdf_url.startswith('//'):
                        pdf_url = 'https:' + pdf_url
                    elif pdf_url.startswith('/'):
                        pdf_url = 'https://urgt66.ru' + pdf_url
                    elif not pdf_url.startswith('http'):
                        pdf_url = url.rsplit('/', 1)[0] + '/' + pdf_url
                    
                    async with session.get(pdf_url, headers=headers, allow_redirects=True) as pdf_resp:
                        if pdf_resp.status == 200:
                            pdf_content = await pdf_resp.read()
                            if pdf_content.startswith(b'%PDF') or b'obj' in pdf_content[:100]:
                                return pdf_content
    except Exception as e:
        logging.error(f"Ошибка загрузки PDF {url}: {e}")
    return None

async def cmd_get_schedule(message: Message, state: FSMContext):
    await message.answer("Напиши число или дату, за которую нужно расписание (например: `10` или `12.10`):", parse_mode="Markdown")
    await state.set_state(GetScheduleState.waiting_for_date)

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
    status_msg = await message.answer(f"🔍 Ищу расписание на `{search_date}` для группы **{group_name}**...", parse_mode="Markdown")

    links = await fetch_pdf_links()
    if not links:
        await status_msg.edit_text("❌ Не удалось загрузить ссылки с сайта техникума.")
        return

    matched_links = []
    for url, text in links:
        combined_title = f"{text} {url}".lower()
        if search_date.lower() in combined_title:
            matched_links.append((url, text))

    if not matched_links:
        matched_links = [links[0]]

    pdf_link, link_title = matched_links[0]

    async with aiohttp.ClientSession() as session:
        pdf_bytes = await download_pdf_content(session, pdf_link)

    if not pdf_bytes:
        await status_msg.edit_text("❌ Не удалось скачать PDF-файл.")
        return

    schedule_text = extract_group_schedule(pdf_bytes, group_name)

    await status_msg.delete()

    # Если группа нашлась — отправляем текстом, если нет — отправляем PDF файл целиком
    if schedule_text:
        await message.answer(
            f"📅 **Расписание для группы {group_name} ({link_title}):**\n\n```{schedule_text}```",
            parse_mode="Markdown"
        )
    else:
        file_to_send = BufferedInputFile(pdf_bytes, filename=f"Schedule_{group_name}.pdf")
        await message.answer_document(
            file_to_send,
            caption=f"📄 Вот актуальное расписание (`{link_title}`) целиком:",
            parse_mode="Markdown"
        )

# Автоматическая проверка и рассылка новых файлов
async def check_schedule(bot: Bot):
    links = await fetch_pdf_links()
    if not links:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with aiohttp.ClientSession() as session:
            for pdf_link, link_text in links:
                async with db.execute("SELECT 1 FROM sent_files WHERE file_url = ?", (pdf_link,)) as cursor:
                    if await cursor.fetchone():
                        continue

                pdf_bytes = await download_pdf_content(session, pdf_link)
                if not pdf_bytes:
                    continue

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
                        else:
                            file_to_send = BufferedInputFile(pdf_bytes, filename="Schedule.pdf")
                            await bot.send_document(
                                user_id,
                                file_to_send,
                                caption=f"🔔 **Новое расписание!** ({link_text})",
                                parse_mode="Markdown"
                            )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logging.error(f"Ошибка отправки пользователю {user_id}: {e}")

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

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_change, Command("change"))
    dp.message.register(cmd_get_schedule, Command("get"))
    dp.message.register(process_group, RegisterState.waiting_for_group)
    dp.message.register(process_date_request, GetScheduleState.waiting_for_date)

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
