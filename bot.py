import asyncio
import logging
import os
import io
import re
import aiohttp
from datetime import datetime
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

# Генерация PDF из текста
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ============================================================
# НАСТРОЙКИ — ЗАМЕНИ ТОКЕН НА СВОЙ
# ============================================================
TOKEN = "8898464745:AAGsD4g7lKw2t03hZPXWeB1yjjir2DtkJnc"
SCHEDULE_URL = "https://urgt66.ru/partition/136056/"
DB_NAME = "users.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ============================================================
# ШРИФТ ДЛЯ КИРИЛЛИЦЫ В PDF
# ============================================================
FONT_NAME = "Helvetica"
try:
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "./DejaVuSans.ttf",
    ]:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("DejaVu", path))
            FONT_NAME = "DejaVu"
            logging.info(f"Шрифт для кириллицы загружен: {path}")
            break
    else:
        logging.warning("Кириллический шрифт не найден — PDF может отображаться квадратиками")
except Exception as e:
    logging.warning(f"Не удалось загрузить кириллический шрифт: {e}")


# ============================================================
# СОСТОЯНИЯ FSM
# ============================================================
class RegisterState(StatesGroup):
    waiting_for_group = State()


class GetScheduleState(StatesGroup):
    waiting_for_date = State()


# ============================================================
# УТИЛИТЫ
# ============================================================
MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def normalize(s: str) -> str:
    """Убирает пробелы/дефисы и переводит в верхний регистр."""
    return re.sub(r"[\s\-–—]+", "", s).upper()


def parse_date_from_title(title: str):
    """Пытается вытащить дату из текста ссылки.
    Пример: 'Расписание занятий на 10 октября 2026 года' -> date(2026, 10, 10)."""
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", title.lower())
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        year = int(m.group(3))
        month = MONTHS_RU.get(month_name)
        if month:
            try:
                return datetime(year, month, day).date()
            except ValueError:
                pass
    return None


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_url TEXT NOT NULL,
                file_title TEXT,
                line_no INTEGER,
                line_text TEXT,
                line_norm TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_norm ON schedule_lines (line_norm)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_file ON schedule_lines (file_url)")
        await db.commit()


# ============================================================
# ПАРСИНГ PDF И СОХРАНЕНИЕ В БД
# ============================================================
def extract_pdf_lines(pdf_bytes: bytes):
    lines_all = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue
                for line in text.split("\n"):
                    lines_all.append(line.rstrip())
    except Exception as e:
        logging.error(f"Ошибка чтения PDF: {e}")
    logging.info(f"[ПАРСИНГ] Извлечено строк из PDF: {len(lines_all)}")
    return lines_all


async def save_schedule_to_db(file_url: str, file_title: str, lines: list):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM schedule_lines WHERE file_url = ?", (file_url,))
        rows = [
            (file_url, file_title, i, line, normalize(line))
            for i, line in enumerate(lines)
        ]
        await db.executemany(
            "INSERT INTO schedule_lines (file_url, file_title, line_no, line_text, line_norm) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()
    logging.info(f"[БД] Сохранено {len(rows)} строк для файла '{file_title}'")


async def find_group_schedule_text(group_name: str) -> str:
    """Ищет расписание группы в БД с гибкой нормализацией и логами."""
    target = normalize(group_name)
    if not target:
        return ""

    # Варианты для поиска: например для "ИТ-122" → ["ИТ122", "122", "ИТ"]
    variants = [target]
    digits = re.findall(r"\d+", target)
    if digits:
        variants.append(digits[-1])
    letters = re.findall(r"[А-ЯЁA-Z]+", target)
    if letters:
        variants.append(letters[0])

    logging.info(f"[ПОИСК] Группа '{group_name}' → варианты: {variants}")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT DISTINCT file_url, file_title FROM schedule_lines ORDER BY id DESC"
        ) as cur:
            files = await cur.fetchall()

        logging.info(f"[ПОИСК] Всего файлов в БД: {len(files)}")
        if not files:
            logging.warning("[ПОИСК] В БД нет ни одной строки расписания!")
            return ""

        for file_url, file_title in files:
            logging.info(f"[ПОИСК] Проверяем файл: {file_title}")

            async with db.execute(
                "SELECT COUNT(*) FROM schedule_lines WHERE file_url = ?", (file_url,)
            ) as cur:
                cnt = (await cur.fetchone())[0]
            logging.info(f"[ПОИСК]   строк в файле: {cnt}")

            # Покажем первые 10 строк файла для отладки
            async with db.execute(
                "SELECT line_text FROM schedule_lines WHERE file_url = ? "
                "ORDER BY line_no LIMIT 10",
                (file_url,),
            ) as cur:
                sample = await cur.fetchall()
            for s in sample:
                logging.info(f"[ПОИСК]   sample: {s[0][:100]!r}")

            hit = None
            for variant in variants:
                async with db.execute(
                    "SELECT line_no, line_text FROM schedule_lines "
                    "WHERE file_url = ? AND line_norm LIKE ? "
                    "ORDER BY line_no LIMIT 1",
                    (file_url, f"%{variant}%"),
                ) as cur:
                    hit = await cur.fetchone()
                if hit:
                    logging.info(f"[ПОИСК]   ✅ Совпадение по '{variant}': {hit[1]!r}")
                    break

            if hit:
                start_line = hit[0]
                async with db.execute(
                    "SELECT line_text FROM schedule_lines "
                    "WHERE file_url = ? AND line_no >= ? "
                    "ORDER BY line_no LIMIT 40",
                    (file_url, start_line),
                ) as cur:
                    block = await cur.fetchall()

                result_lines = []
                for idx, (text,) in enumerate(block):
                    result_lines.append(text)
                    if idx > 0 and re.match(r"^\s*[А-ЯЁA-Z]{2,}[-\s]?\d", text.strip()) \
                            and target not in normalize(text):
                        result_lines.pop()
                        break
                return "\n".join(result_lines).strip()

    logging.warning(f"[ПОИСК] Группа '{group_name}' не найдена ни в одном файле.")
    return ""


# ============================================================
# ГЕНЕРАЦИЯ PDF ИЗ ТЕКСТА
# ============================================================
def text_to_pdf(text: str, title: str = "Расписание") -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    c.setFont(FONT_NAME, 12)
    c.drawString(40, height - 40, title)

    y = height - 70
    line_height = 16
    for raw_line in text.split("\n"):
        while len(raw_line) > 0:
            chunk, raw_line = raw_line[:110], raw_line[110:]
            if y < 40:
                c.showPage()
                c.setFont(FONT_NAME, 12)
                y = height - 40
            c.drawString(40, y, chunk)
            y -= line_height
        y -= 2

    c.save()
    buffer.seek(0)
    return buffer.read()


# ============================================================
# ПОЛУЧЕНИЕ ССЫЛОК С САЙТА
# ============================================================
async def fetch_pdf_links():
    """
    Возвращает список кортежей (url, title, date|None).
    Оставляет ТОЛЬКО расписания занятий, отбрасывает звонки/пересдачи/задолженности.
    """
    links = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with aiohttp.ClientSession() as session:
            async with session.get(SCHEDULE_URL, headers=headers) as resp:
                if resp.status != 200:
                    logging.error(f"[САЙТ] Статус {resp.status} при загрузке страницы")
                    return links
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if not text:
                continue

            low = text.lower()

            # Исключаем нерелевантные ссылки
            if "звонк" in low:
                continue
            if "задолженност" in low:
                continue
            if "пересдач" in low:
                continue

            # Обязательно "занятий"
            if "занятий" not in low:
                continue

            if ".pdf" not in href.lower() and "расписание" not in low:
                continue

            # Нормализуем URL
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://urgt66.ru" + href
            elif not href.startswith("http"):
                href = SCHEDULE_URL.rstrip("/") + "/" + href

            parsed_date = parse_date_from_title(text)
            links.append((href, text, parsed_date))

        logging.info(f"[САЙТ] Найдено ссылок на расписание занятий: {len(links)}")
        for url, title, d in links:
            logging.info(f"[САЙТ]   {title} | дата: {d} | {url}")
    except Exception as e:
        logging.error(f"Ошибка получения ссылок: {e}")
    return links


async def download_pdf_content(session, url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                logging.warning(f"[СКАЧ] Статус {resp.status} для {url}")
                return None
            content = await resp.read()
            if content.startswith(b"%PDF") or b"obj" in content[:100]:
                return content

            # Если это HTML-страница — ищем внутри ссылку на PDF
            logging.info(f"[СКАЧ] {url} — не PDF, ищем внутри ссылку на PDF")
            soup = BeautifulSoup(content.decode("utf-8", errors="ignore"), "html.parser")
            for a in soup.find_all("a", href=True):
                if ".pdf" in a["href"].lower():
                    pdf_url = a["href"]
                    if pdf_url.startswith("//"):
                        pdf_url = "https:" + pdf_url
                    elif pdf_url.startswith("/"):
                        pdf_url = "https://urgt66.ru" + pdf_url
                    elif not pdf_url.startswith("http"):
                        pdf_url = url.rsplit("/", 1)[0] + "/" + pdf_url

                    async with session.get(pdf_url, headers=headers, allow_redirects=True) as pdf_resp:
                        if pdf_resp.status == 200:
                            pdf_content = await pdf_resp.read()
                            if pdf_content.startswith(b"%PDF") or b"obj" in pdf_content[:100]:
                                logging.info(f"[СКАЧ] Найден PDF внутри: {pdf_url}")
                                return pdf_content
    except Exception as e:
        logging.error(f"Ошибка загрузки PDF {url}: {e}")
    return None


# ============================================================
# ХЕНДЛЕРЫ КОМАНД
# ============================================================
async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                await message.answer(
                    f"Привет! Твоя группа: **{row[0]}**.\n\n"
                    "📌 **Команды:**\n"
                    "• /get — запросить расписание на нужную дату\n"
                    "• /change — сменить учебную группу",
                    parse_mode="Markdown",
                )
            else:
                await message.answer(
                    "Привет! Напиши название своей группы (например: ИТ-122 или ИС-21), "
                    "чтобы я присылал расписание:"
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
            (message.from_user.id, group_name),
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"Готово! Группа **{group_name}** сохранена.\n"
        "Теперь ты можешь запрашивать расписание через /get",
        parse_mode="Markdown",
    )


async def cmd_get_schedule(message: Message, state: FSMContext):
    await message.answer(
        "Напиши число или дату, за которую нужно расписание (например: `10` или `12.10`):",
        parse_mode="Markdown",
    )
    await state.set_state(GetScheduleState.waiting_for_date)


async def process_date_request(message: Message, state: FSMContext):
    search_date = message.text.strip()
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)
        ) as cursor:
            user_row = await cursor.fetchone()

    if not user_row:
        await message.answer("Сначала укажи свою группу с помощью команды /start")
        return

    group_name = user_row[0]
    status_msg = await message.answer(
        f"🔍 Ищу расписание на `{search_date}` для группы **{group_name}**...",
        parse_mode="Markdown",
    )

    links = await fetch_pdf_links()
    if not links:
        await status_msg.edit_text("❌ Не удалось найти расписание занятий на сайте.")
        return

    # Разбираем ввод пользователя: "10", "10.10", "10.10.2026"
    user_day = None
    user_month = None
    m = re.match(r"^(\d{1,2})(?:[.\-/](\d{1,2}))?", search_date)
    if m:
        user_day = int(m.group(1))
        if m.group(2):
            user_month = int(m.group(2))

    # Ищем ссылку с подходящей датой
    matched = None
    for url, title, d in links:
        if d is None:
            continue
        if d.day == user_day and (user_month is None or d.month == user_month):
            matched = (url, title, d)
            break

    # Если не нашли — берём самое свежее расписание занятий
    if matched is None:
        dated = [(u, t, d) for (u, t, d) in links if d is not None]
        if dated:
            matched = max(dated, key=lambda x: x[2])
        else:
            matched = links[0]

    pdf_link, link_title, _ = matched
    logging.info(f"[ЗАПРОС] Пользователь {message.from_user.id}, группа {group_name}, "
                 f"выбран файл: {link_title} | {pdf_link}")

    # Проверяем, есть ли уже строки этого файла в БД
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT 1 FROM schedule_lines WHERE file_url = ? LIMIT 1", (pdf_link,)
        ) as cur:
            exists = await cur.fetchone()

    pdf_bytes = None
    if not exists:
        async with aiohttp.ClientSession() as session:
            pdf_bytes = await download_pdf_content(session, pdf_link)
        if not pdf_bytes:
            await status_msg.edit_text("❌ Не удалось скачать PDF-файл.")
            return
        lines = extract_pdf_lines(pdf_bytes)
        if not lines:
            await status_msg.edit_text("❌ PDF не содержит текста (возможно, это скан).")
            return
        await save_schedule_to_db(pdf_link, link_title, lines)

    schedule_text = await find_group_schedule_text(group_name)
    await status_msg.delete()

    if schedule_text:
        pdf_out = text_to_pdf(
            f"Группа: {group_name}\nИсточник: {link_title}\n\n{schedule_text}",
            title=f"Расписание группы {group_name}",
        )
        file_to_send = BufferedInputFile(pdf_out, filename=f"Schedule_{group_name}.pdf")
        await message.answer_document(
            file_to_send,
            caption=f"📄 Расписание для группы **{group_name}** (`{link_title}`):",
            parse_mode="Markdown",
        )
    else:
        # Фолбэк — полный PDF без выделения группы
        if pdf_bytes is None:
            async with aiohttp.ClientSession() as session:
                pdf_bytes = await download_pdf_content(session, pdf_link)
        if not pdf_bytes:
            await message.answer("❌ Не удалось получить PDF.")
            return
        file_to_send = BufferedInputFile(pdf_bytes, filename="Schedule_full.pdf")
        await message.answer_document(
            file_to_send,
            caption=(
                f"⚠️ Группа **{group_name}** не найдена в расписании "
                f"(`{link_title}`).\nОтправляю полное расписание:"
            ),
            parse_mode="Markdown",
        )


# ============================================================
# АВТОМАТИЧЕСКАЯ РАССЫЛКА
# ============================================================
async def check_schedule(bot: Bot):
    links = await fetch_pdf_links()
    if not links:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with aiohttp.ClientSession() as session:
            # Свежие сверху
            dated = [l for l in links if l[2] is not None]
            undated = [l for l in links if l[2] is None]
            dated.sort(key=lambda x: x[2], reverse=True)
            ordered = dated + undated

            for pdf_link, link_text, _date in ordered:
                async with db.execute(
                    "SELECT 1 FROM sent_files WHERE file_url = ?", (pdf_link,)
                ) as cursor:
                    if await cursor.fetchone():
                        continue

                pdf_bytes = await download_pdf_content(session, pdf_link)
                if not pdf_bytes:
                    continue

                lines = extract_pdf_lines(pdf_bytes)
                if not lines:
                    continue

                await save_schedule_to_db(pdf_link, link_text, lines)
                await db.execute(
                    "INSERT INTO sent_files (file_url) VALUES (?)", (pdf_link,)
                )
                await db.commit()

                async with db.execute("SELECT user_id, group_name FROM users") as cursor:
                    users = await cursor.fetchall()

                for user_id, group_name in users:
                    schedule_text = await find_group_schedule_text(group_name)
                    try:
                        if schedule_text:
                            pdf_out = text_to_pdf(
                                f"Группа: {group_name}\nИсточник: {link_text}\n\n{schedule_text}",
                                title=f"Расписание группы {group_name}",
                            )
                            file_to_send = BufferedInputFile(
                                pdf_out, filename=f"Schedule_{group_name}.pdf"
                            )
                            await bot.send_document(
                                user_id,
                                file_to_send,
                                caption=f"🔔 **Новое расписание!** ({link_text})\n"
                                        f"Ваша группа: **{group_name}**",
                                parse_mode="Markdown",
                            )
                        else:
                            file_to_send = BufferedInputFile(
                                pdf_bytes, filename="Schedule_full.pdf"
                            )
                            await bot.send_document(
                                user_id,
                                file_to_send,
                                caption=(
                                    f"🔔 **Новое расписание!** ({link_text})\n"
                                    f"⚠️ Группа **{group_name}** не найдена — "
                                    f"отправляю полное расписание."
                                ),
                                parse_mode="Markdown",
                            )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logging.error(f"Ошибка отправки пользователю {user_id}: {e}")


# ============================================================
# ВЕБ-СЕРВЕР ДЛЯ HEALTH-CHECK
# ============================================================
async def handle(request):
    return web.Response(text="Бот запущен!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")


# ============================================================
# ЗАПУСК
# ============================================================
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
    scheduler.add_job(check_schedule, "interval", minutes=30, args=(bot,))
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
