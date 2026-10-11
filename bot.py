import asyncio
import logging
import os
import io
import re
import json
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

# OCR
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image

# Генерация PDF из текста
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ============================================================
# НАСТРОЙКИ
# ============================================================
TOKEN = "8898464745:AAGsD4g7lKw2t03hZPXWeB1yjjir2DtkJnc"  # ← при необходимости замени
SCHEDULE_URL = "https://urgt66.ru/partition/136056/"
DB_NAME = "users.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ============================================================
# ШРИФТ ДЛЯ КИРИЛЛИЦЫ
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
        logging.warning("Кириллический шрифт не найден")
except Exception as e:
    logging.warning(f"Ошибка загрузки шрифта: {e}")


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
    """Жёсткая нормализация: убираем пробелы, дефисы, тире, точки, запятые и т.п."""
    if not s:
        return ""
    s = re.sub(r"[\s\u00a0\u200b\u00ad\-–—_.,:;()/\\|!\"'`]+", "", s)
    return s.upper()


def parse_date_from_title(title: str):
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", title.lower())
    if m:
        day = int(m.group(1))
        month = MONTHS_RU.get(m.group(2))
        year = int(m.group(3))
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
# OCR — УСКОРЕННЫЙ ПАРСИНГ PDF
# ============================================================
def extract_pdf_lines_via_ocr(pdf_bytes: bytes):
    """OCR через Tesseract с ускорением: DPI 150 + обрезка верхних 60% + таймаут."""
    lines_all = []
    try:
        logging.info("[OCR] Конвертируем PDF в изображения (dpi=150)...")
        images = convert_from_bytes(
            pdf_bytes,
            dpi=150,
            fmt="jpeg",
            thread_count=1,
        )
        logging.info(f"[OCR] Получено страниц: {len(images)}")

        for i, img in enumerate(images):
            try:
                w, h = img.size
                cropped = img.crop((0, 0, w, int(h * 0.6)))
                logging.info(f"[OCR] Страница {i}: обрезали до {cropped.size}")

                text = pytesseract.image_to_string(
                    cropped,
                    lang="rus+eng",
                    config="--psm 6 --oem 1",
                    timeout=45,
                )
                logging.info(f"[OCR] Страница {i}: {len(text)} символов")
                for line in text.split("\n"):
                    lines_all.append(line.rstrip())
            except RuntimeError as e:
                logging.error(f"[OCR] Таймаут на странице {i}: {e}")
            except Exception as e:
                logging.error(f"[OCR] Ошибка на странице {i}: {e}", exc_info=True)

        non_empty = [l for l in lines_all if l.strip()]
        for s in non_empty[:15]:
            logging.info(f"[OCR] sample: {s[:120]!r}")
    except Exception as e:
        logging.error(f"[OCR] Общая ошибка: {e}", exc_info=True)
    logging.info(f"[OCR] Извлечено строк: {len(lines_all)}")
    return lines_all


def extract_pdf_lines(pdf_bytes: bytes) -> list:
    """Пробуем OCR. Если пусто — PyMuPDF как last resort."""
    lines = extract_pdf_lines_via_ocr(pdf_bytes)
    if not lines:
        logging.warning("[ПАРСИНГ] OCR не дал результата, пробуем PyMuPDF")
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for page in doc:
                text = page.get_text("text")
                if text:
                    for line in text.split("\n"):
                        lines.append(line.rstrip())
            doc.close()
        except Exception as e:
            logging.error(f"PyMuPDF fallback: {e}")
    return lines


# ============================================================
# СОХРАНЕНИЕ В БД
# ============================================================
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
    logging.info(f"[БД] Сохранено строк: {len(rows)}")


# ============================================================
# ПОИСК ГРУППЫ
# ============================================================
async def find_group_schedule_text(group_name: str) -> str:
    target = normalize(group_name)
    logging.info(f"[ПОИСК] Группа '{group_name}' (норм: '{target}')")

    variants = [target]
    letters = "".join(re.findall(r"[А-ЯЁA-Z]", target))
    digits = "".join(re.findall(r"\d", target))
    if letters and digits:
        variants.append(f"{letters}{digits}")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT DISTINCT file_url, file_title FROM schedule_lines ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            logging.warning("[ПОИСК] В БД нет файлов")
            return ""
        file_url, file_title = row
        logging.info(f"[ПОИСК] Файл: {file_title}")

        hit = None
        for v in variants:
            async with db.execute(
                "SELECT line_no, line_text FROM schedule_lines "
                "WHERE file_url = ? AND line_norm LIKE ? "
                "ORDER BY line_no LIMIT 1",
                (file_url, f"%{v}%"),
            ) as cur:
                hit = await cur.fetchone()
            if hit:
                logging.info(f"[ПОИСК] Совпадение по '{v}': {hit[1]!r}")
                break

        if not hit:
            logging.warning("[ПОИСК] Совпадений нет — выводим первые 60 строк файла:")
            async with db.execute(
                "SELECT line_no, line_text FROM schedule_lines "
                "WHERE file_url = ? ORDER BY line_no LIMIT 60",
                (file_url,),
            ) as cur:
                for ln, text in await cur.fetchall():
                    logging.info(f"[SAMPLE] {ln}: {text[:120]!r}")
            return ""

        start_line = hit[0]

        async with db.execute(
            "SELECT line_no, line_text FROM schedule_lines "
            "WHERE file_url = ? AND line_no >= ? "
            "ORDER BY line_no LIMIT 60",
            (file_url, start_line),
        ) as cur:
            block = await cur.fetchall()

        result = []
        for idx, (ln, text) in enumerate(block):
            if idx > 0:
                m = re.match(r"^\s*([А-ЯЁA-Z]{2,4}[\s\-–—]?\d{2,3})", text.strip())
                if m:
                    other = normalize(m.group(1))
                    if other != target and len(other) >= 4:
                        logging.info(f"[ПОИСК] Конец блока на строке {ln}")
                        break
            result.append(text)
        return "\n".join(result).strip()


# ============================================================
# PDF ИЗ ТЕКСТА
# ============================================================
def text_to_pdf(text: str, title: str = "Расписание") -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    c.setFont(FONT_NAME, 11)
    c.drawString(40, height - 40, title)

    y = height - 70
    for raw_line in text.split("\n"):
        line = raw_line
        while len(line) > 0:
            chunk, line = line[:115], line[115:]
            if y < 40:
                c.showPage()
                c.setFont(FONT_NAME, 11)
                y = height - 40
            c.drawString(40, y, chunk)
            y -= 15
        y -= 2

    c.save()
    buffer.seek(0)
    return buffer.read()


# ============================================================
# ССЫЛКИ С САЙТА
# ============================================================
async def fetch_pdf_links():
    links = []
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(SCHEDULE_URL, headers=headers) as resp:
                if resp.status != 200:
                    return links
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if not text:
                continue
            low = text.lower()

            if "звонк" in low or "задолженност" in low or "пересдач" in low:
                continue
            if "занятий" not in low:
                continue
            if ".pdf" not in href.lower() and "расписание" not in low:
                continue

            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://urgt66.ru" + href
            elif not href.startswith("http"):
                href = SCHEDULE_URL.rstrip("/") + "/" + href

            parsed_date = parse_date_from_title(text)
            links.append((href, text, parsed_date))

        logging.info(f"[САЙТ] Найдено ссылок: {len(links)}")
    except Exception as e:
        logging.error(f"Ошибка получения ссылок: {e}")
    return links


async def download_pdf_content(session, url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            content = await resp.read()
            if content.startswith(b"%PDF") or b"obj" in content[:100]:
                return content

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

                    async with session.get(pdf_url, headers=headers, allow_redirects=True) as r2:
                        if r2.status == 200:
                            c = await r2.read()
                            if c.startswith(b"%PDF") or b"obj" in c[:100]:
                                return c
    except Exception as e:
        logging.error(f"Ошибка скачивания {url}: {e}")
    return None


# ============================================================
# ХЕНДЛЕРЫ
# ============================================================
async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)
        ) as cur:
            row = await cur.fetchone()
    if row:
        await message.answer(
            f"Привет! Твоя группа: **{row[0]}**.\n\n"
            "📌 /get — расписание\n"
            "📌 /change — сменить группу",
            parse_mode="Markdown",
        )
    else:
        await message.answer("Привет! Напиши название своей группы (например: ИТ-122):")
        await state.set_state(RegisterState.waiting_for_group)


async def cmd_change(message: Message, state: FSMContext):
    await message.answer("Введи новое название группы:")
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
    await message.answer(f"✅ Группа **{group_name}** сохранена. /get", parse_mode="Markdown")


async def cmd_get_schedule(message: Message, state: FSMContext):
    await message.answer("Напиши дату (например: `10` или `12.10`):", parse_mode="Markdown")
    await state.set_state(GetScheduleState.waiting_for_date)


async def process_date_request(message: Message, state: FSMContext):
    search_date = message.text.strip()
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,)
        ) as cur:
            user_row = await cur.fetchone()

    if not user_row:
        await message.answer("Сначала /start")
        return

    group_name = user_row[0]
    status_msg = await message.answer(f"🔍 Ищу расписание для **{group_name}**...", parse_mode="Markdown")

    links = await fetch_pdf_links()
    if not links:
        await status_msg.edit_text("❌ Ссылки не найдены.")
        return

    user_day = None
    user_month = None
    m = re.match(r"^(\d{1,2})(?:[.\-/](\d{1,2}))?", search_date)
    if m:
        user_day = int(m.group(1))
        if m.group(2):
            user_month = int(m.group(2))

    matched = None
    for url, title, d in links:
        if d and d.day == user_day and (user_month is None or d.month == user_month):
            matched = (url, title, d)
            break
    if not matched:
        dated = [(u, t, d) for (u, t, d) in links if d]
        matched = max(dated, key=lambda x: x[2]) if dated else links[0]

    pdf_link, link_title, _ = matched
    logging.info(f"[ЗАПРОС] group={group_name}, file={link_title}")

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
            await status_msg.edit_text("❌ Не удалось скачать PDF.")
            return
        await status_msg.edit_text("⏳ Распознаю PDF (30–90 сек)...")
        lines = extract_pdf_lines(pdf_bytes)
        if not lines:
            await status_msg.edit_text("❌ PDF не распознан.")
            return
        await save_schedule_to_db(pdf_link, link_title, lines)

    schedule_text = await find_group_schedule_text(group_name)
    await status_msg.delete()

    if schedule_text:
        pdf_out = text_to_pdf(
            f"Группа: {group_name}\nИсточник: {link_title}\n\n{schedule_text}",
            title=f"Расписание группы {group_name}",
        )
        await message.answer_document(
            BufferedInputFile(pdf_out, filename=f"Schedule_{group_name}.pdf"),
            caption=f"📄 Расписание для **{group_name}**",
            parse_mode="Markdown",
        )
    else:
        if pdf_bytes is None:
            async with aiohttp.ClientSession() as session:
                pdf_bytes = await download_pdf_content(session, pdf_link)
        await message.answer_document(
            BufferedInputFile(pdf_bytes, filename="Schedule_full.pdf"),
            caption=f"⚠️ Группа **{group_name}** не найдена. Полное расписание:",
            parse_mode="Markdown",
        )


# ============================================================
# АВТОРАССЫЛКА
# ============================================================
async def check_schedule(bot: Bot):
    links = await fetch_pdf_links()
    if not links:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with aiohttp.ClientSession() as session:
            dated = [l for l in links if l[2]]
            dated.sort(key=lambda x: x[2], reverse=True)
            for pdf_link, link_text, _ in dated:
                async with db.execute(
                    "SELECT 1 FROM sent_files WHERE file_url = ?", (pdf_link,)
                ) as c:
                    if await c.fetchone():
                        continue

                pdf_bytes = await download_pdf_content(session, pdf_link)
                if not pdf_bytes:
                    continue
                lines = extract_pdf_lines(pdf_bytes)
                if not lines:
                    continue
                await save_schedule_to_db(pdf_link, link_text, lines)
                await db.execute("INSERT INTO sent_files (file_url) VALUES (?)", (pdf_link,))
                await db.commit()

                async with db.execute("SELECT user_id, group_name FROM users") as c:
                    users = await c.fetchall()
                for uid, gname in users:
                    txt = await find_group_schedule_text(gname)
                    try:
                        if txt:
                            out = text_to_pdf(
                                f"Группа: {gname}\nИсточник: {link_text}\n\n{txt}",
                                title=f"Расписание группы {gname}",
                            )
                            await bot.send_document(
                                uid,
                                BufferedInputFile(out, filename=f"Schedule_{gname}.pdf"),
                                caption=f"🔔 Новое расписание ({link_text}) — **{gname}**",
                                parse_mode="Markdown",
                            )
                        else:
                            await bot.send_document(
                                uid,
                                BufferedInputFile(pdf_bytes, filename="Schedule_full.pdf"),
                                caption=f"🔔 Новое расписание ({link_text}). Группа **{gname}** не найдена.",
                                parse_mode="Markdown",
                            )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logging.error(f"Ошибка отправки {uid}: {e}")


# ============================================================
# ВЕБ-СЕРВЕР
# ============================================================
async def handle(request):
    return web.Response(text="Бот запущен!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()
    logging.info("Веб-сервер запущен")


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
    await asyncio.gather(start_web_server(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
