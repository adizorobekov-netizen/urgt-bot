import asyncio
import logging
import io
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
import aiosqlite
import pdfplumber

# ЗАМЕНИТЕ ЭТИ ДАННЫЕ НА СВОИ:
TOKEN ="8898464745:AAGsD4g7IkW2tO3hzPXWeB1yjjiR2DtWJnc"
SCHEDULE_URL ="https://urgt66.ru/partition/136056/"
DB_NAME = "users.db"

logging.basicConfig(level=logging.INFO)
router = Router()


class RegisterState(StatesGroup):
    waiting_for_group = State()


# Инициализация базы данных
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
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()

    if row:
        await message.answer(
            f"Привет! Твоя группа: **{row[0]}**.\n"
            "Я пришлю тебе персональное расписание для твоей группы, как только оно появится на сайте.\n"
            "Чтобы сменить группу, отправь команду /change",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "Привет! Напиши название своей группы (например: *ИТ-12* или *ИС-21*), чтобы я присылал расписание именно для неё:",
            parse_mode="Markdown")
        await state.set_state(RegisterState.waiting_for_group)


# Команда /change для смены группы
@router.message(Command("change"))
async def cmd_change(message: Message, state: FSMContext):
    await message.answer("Введи название своей новой группы:")
    await state.set_state(RegisterState.waiting_for_group)


# Сохранение группы в базу
@router.message(RegisterState.waiting_for_group)
async def process_group(message: Message, state: FSMContext):
    group_name = message.text.strip().upper()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, group_name) VALUES (?, ?)",
                         (message.from_user.id, group_name))
        await db.commit()

    await state.clear()
    await message.answer(f"Готово! Группа **{group_name}** сохранена. Жди персональное расписание.",
                         parse_mode="Markdown")


# Функция поиска расписания для конкретной группы из PDF
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
        logging.error(f"Ошибка при чтении PDF: {e}")

    return ""


# Функция автоматической проверки сайта и рассылки персонального расписания
async def check_schedule(bot: Bot):
    logging.info("Проверка новых файлов расписания на сайте...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SCHEDULE_URL) as response:
                if response.status != 200:
                    return
                html = await response.text()

        soup = BeautifulSoup(html, 'html.parser')

        links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if '.pdf' in href.lower() or 'расписание' in text.lower():
                if href.startswith('/'):
                    base_site = "/".join(SCHEDULE_URL.split('/')[:3])
                    href = base_site + href
                elif not href.startswith('http'):
                    href = SCHEDULE_URL.rsplit('/', 1)[0] + '/' + href

                links.append((href, text))

        if not links:
            return

        async with aiosqlite.connect(DB_NAME) as db:
            for pdf_link, link_text in links:
                async with db.execute("SELECT 1 FROM sent_files WHERE file_url = ?", (pdf_link,)) as cursor:
                    exists = await cursor.fetchone()

                if exists:
                    continue

                logging.info(f"Обнаружено новое расписание: {link_text} ({pdf_link})")

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
                                chat_id=user_id,
                                text=f"🔔 **Новое расписание!** ({link_text})\n\nВаше расписание:\n```\n{personal_schedule}\n```",
                                parse_mode="Markdown"
                            )
                        else:
                            await bot.send_message(
                                chat_id=user_id,
                                text=f"🔔 Появилось новое расписание ({link_text}), но для вашей группы `{group_name}` не удалось автоматически выделить строки. Проверьте сайт техникума.",
                                parse_mode="Markdown"
                            )
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

        logging.info("Проверка расписания завершена.")

    except Exception as e:
        logging.error(f"Ошибка при проверке сайта: {e}")


# --- ДОБАВЛЯЕМ ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render передает порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}")
# -----------------------------------------

async def main():
    # Инициализация бота и диспетчера
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await init_db()

    # Настройка планировщика
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_schedule, 'interval', minutes=30, args=(bot,))
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)

    # ЗАПУСКАЕМ ВЕБ-СЕРВЕР И БОТА ОДНОВРЕМЕННО
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
