import asyncio, io, json, logging, os, re, time
from datetime import date
from urllib.parse import urljoin

import aiohttp
import pdfplumber
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)
from PIL import Image

PAGE_URL = os.environ.get("PAGE_URL", "https://urgt66.ru/partition/136056/")
PREFS_FILE = os.environ.get("PREFS_FILE", "prefs.json")
CHECK_EVERY = 1800  # проверка сайта раз в 30 минут (в секундах)

logging.basicConfig(level=logging.INFO)
router = Router()

# ---------- настройки пользователей ----------
try:
    prefs = json.load(open(PREFS_FILE, encoding="utf-8"))
except Exception:
    prefs = {}


def save_prefs():
    try:
        json.dump(prefs, open(PREFS_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        logging.warning("save prefs failed: %s", e)


def user(uid):
    return prefs.setdefault(str(uid), {"group": None, "date": None, "wait": None})


# ---------- названия групп ----------
LAT, CYR = "ABCEHKMOPTXY", "АВСЕНКМОРТХУ"
TR = str.maketrans(LAT, CYR)


def norm(s: str) -> str:
    return re.sub(r"[\s\-–—−]", "", s.upper().translate(TR))


HEAD_RE = re.compile(r"^([^\d\-]{0,3})-(\d{3})$")


def decode_group(prefix: str, digits: str) -> str:
    p = prefix.replace('"', "Т").replace("!", "С")
    p = re.sub(r"[^А-ЯA-Z]", "", p.upper().translate(TR))
    return (p or "П") + digits


# ---------- вырезание колонки группы ----------
def _render(page, bbox, res):
    x0, t, x1, b = bbox
    bbox = (max(0, x0), max(0, t), min(page.width, x1), min(page.height, b))
    return page.crop(bbox).to_image(resolution=res).original.convert("RGB")


def find_headers(page):
    heads = []
    for w in page.extract_words(extra_attrs=["fontname"]):
        if "bold" not in w["fontname"].lower():
            continue
        m = HEAD_RE.match(w["text"])
        if m:
            w["g"] = decode_group(m[1], m[2])
            heads.append(w)
    heads.sort(key=lambda w: w["top"])
    rows = []
    for w in heads:
        if rows and abs(rows[-1][0]["top"] - w["top"]) < 3:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows if len(r) >= 3]


def crop_group(pdf_bytes: bytes, group: str):
    target = norm(group)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows = find_headers(page)
            words = page.extract_words()
            if not words:
                continue
            table_left = min(w["x0"] for w in words) - 3
            page_bottom = max(w["bottom"] for w in words) + 4
            for ri, row in enumerate(rows):
                for ci, w in enumerate(row):
                    if w["g"] != target:
                        continue
                    cx = [(r["x0"] + r["x1"]) / 2 for r in row]
                    pitch = (cx[-1] - cx[0]) / (len(cx) - 1) if len(cx) > 1 else 40
                    left = (cx[ci - 1] + cx[ci]) / 2 if ci > 0 else cx[ci] - pitch / 2
                    right = (cx[ci] + cx[ci + 1]) / 2 if ci + 1 < len(cx) else cx[ci] + pitch / 2
                    first_left = cx[0] - pitch / 2
                    top = w["top"] - 3
                    bottom = rows[ri + 1][0]["top"] - 3 if ri + 1 < len(rows) else page_bottom
                    h = max(w["bottom"] - w["top"], 3)
                    res = int(min(600, max(250, 2000 / h)))
                    i1 = _render(page, (table_left, top, first_left, bottom), res)
                    i2 = _render(page, (left, top, right, bottom), res)
                    out = Image.new("RGB", (i1.width + i2.width, max(i1.height, i2.height)), "white")
                    out.paste(i1, (0, 0))
                    out.paste(i2, (i1.width, 0))
                    buf = io.BytesIO()
                    out.save(buf, "PNG", optimize=True)
                    return buf.getvalue()
    return None


# ---------- сайт ----------
MONTHS = {"yanvarya": 1, "fevralya": 2, "marta": 3, "aprelya": 4, "maya": 5,
          "iyunya": 6, "iyulya": 7, "avgusta": 8, "sentyabrya": 9,
          "oktyabrya": 10, "noyabrya": 11, "dekabrya": 12}
LINK_RE = re.compile(r'href="([^"]*raspisanie-zanyatij-na-(\d{1,2})-([a-z]+)-(\d{4})[^"]*\.pdf)"', re.I)
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_links = {"t": 0, "d": {}}
_pdfs = {}


async def get_links() -> dict:
    if time.time() - _links["t"] < 300 and _links["d"]:
        return _links["d"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(PAGE_URL, timeout=aiohttp.ClientTimeout(total=30)) as r:
                html = await r.text()
        found = {}
        for m in LINK_RE.finditer(html):
            mon = MONTHS.get(m[3].lower())
            if mon:
                found[date(int(m[4]), mon, int(m[2]))] = urljoin(PAGE_URL, m[1])
        if found:
            _links.update(t=time.time(), d=found)
    except Exception as e:
        logging.warning("page fetch failed: %s", e)
    return _links["d"]


async def get_pdf(url: str):
    if url in _pdfs:
        return _pdfs[url]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = await r.read()
        if data[:4] == b"%PDF":
            if len(_pdfs) > 10:
                _pdfs.clear()
            _pdfs[url] = data
            return data
    except Exception as e:
        logging.warning("pdf download failed: %s", e)
    return None


def fmt(d: date) -> str:
    return f"{d:%d.%m} ({DAYS[d.weekday()]})"


def default_date(links: dict):
    today = date.today()
    future = sorted(d for d in links if d >= today)
    return future[0] if future else (max(links) if links else None)


# ---------- интерфейс ----------
def kb(links: dict):
    rows, cur = [], []
    for d in sorted(links)[-6:]:
        cur.append(InlineKeyboardButton(text=fmt(d), callback_data=f"d:{d.isoformat()}"))
        if len(cur) == 3:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([InlineKeyboardButton(text="🔁 Сменить группу", callback_data="chgroup")])
    rows.append([InlineKeyboardButton(text="📄 Всё расписание (PDF)", callback_data="full")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_schedule(msg: Message, uid, d=None, full=False):
    u = user(uid)
    links = await get_links()
    if not links:
        return await msg.answer("Не получилось открыть страницу расписания на сайте. Попробуй позже.")
    d = d or default_date(links)
    if d not in links:
        have = ", ".join(fmt(x) for x in sorted(links))
        return await msg.answer(f"На {d:%d.%m.%Y} расписания на сайте нет.\nЕсть: {have}",
                                reply_markup=kb(links))
    u["date"] = d.isoformat()
    save_prefs()
    pdf = await get_pdf(links[d])
    if not pdf:
        return await msg.answer("Не удалось скачать PDF. Попробуй позже.", reply_markup=kb(links))
    if not full and u["group"]:
        png = await asyncio.to_thread(crop_group, pdf, u["group"])
        if png:
            return await msg.answer_photo(
                BufferedInputFile(png, "schedule.png"),
                caption=f"Группа {u['group']} · {d:%d.%m.%Y} ({DAYS[d.weekday()]})",
                reply_markup=kb(links))
        await msg.answer(f"Группу «{u['group']}» на {d:%d.%m} не нашёл. Отправляю полное расписание.")
    await msg.answer_document(BufferedInputFile(pdf, f"Расписание_{d:%d.%m.%Y}.pdf"),
                              reply_markup=kb(links))


def parse_date(t: str):
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", t.strip())
    if not m:
        return None
    yr = m[3]
    yr = date.today().year if not yr else (int(yr) + 2000 if len(yr) == 2 else int(yr))
    try:
        return date(yr, int(m[2]), int(m[1]))
    except ValueError:
        return None


@router.message(CommandStart())
async def start(m: Message):
    user(m.from_user.id)["wait"] = "group"
    save_prefs()
    await m.answer("Привет! Напиши номер своей группы, например: ИТ-121\n\n"
                   "Я буду сам присылать новое расписание, как только оно появится на сайте.")


@router.callback_query(F.data.startswith("d:"))
async def cb_day(c: CallbackQuery):
    await c.answer()
    if not user(c.from_user.id)["group"]:
        user(c.from_user.id)["wait"] = "group"
        return await c.message.answer("Сначала напиши номер группы.")
    await send_schedule(c.message, c.from_user.id, date.fromisoformat(c.data[2:]))


@router.callback_query(F.data == "chgroup")
async def cb_group(c: CallbackQuery):
    await c.answer()
    user(c.from_user.id)["wait"] = "group"
    save_prefs()
    await c.message.answer("Напиши номер новой группы:")


@router.callback_query(F.data == "full")
async def cb_full(c: CallbackQuery):
    await c.answer()
    u = user(c.from_user.id)
    d = date.fromisoformat(u["date"]) if u["date"] else None
    await send_schedule(c.message, c.from_user.id, d, full=True)


@router.message(F.text)
async def text(m: Message):
    u = user(m.from_user.id)
    t = m.text.strip()
    d = parse_date(t)
    if d and u["group"]:
        return await send_schedule(m, m.from_user.id, d)
    u["group"] = t.upper()
    u["wait"] = None
    save_prefs()
    await send_schedule(m, m.from_user.id)


# ---------- автоматическая проверка сайта ----------
async def watcher(bot: Bot):
    """Раз в 30 минут проверяет сайт и рассылает новые расписания."""
    known = None
    while True:
        try:
            _links["t"] = 0  # сбросить кэш, чтобы реально сходить на сайт
            links = await get_links()
            if known is None:
                # первый запуск: запоминаем уже имеющиеся файлы, без рассылки
                if links:
                    known = set(links)
            else:
                for d in sorted(x for x in links if x not in known):
                    pdf = await get_pdf(links[d])
                    if not pdf:
                        continue  # попробуем в следующий раз
                    known.add(d)
                    for uid, u in list(prefs.items()):
                        if not u.get("group"):
                            continue
                        try:
                            png = await asyncio.to_thread(crop_group, pdf, u["group"])
                            if png:
                                await bot.send_photo(
                                    int(uid), BufferedInputFile(png, "schedule.png"),
                                    caption=f"🆕 Новое расписание · {u['group']} · "
                                            f"{d:%d.%m.%Y} ({DAYS[d.weekday()]})")
                        except Exception as e:
                            logging.warning("send to %s failed: %s", uid, e)
                        await asyncio.sleep(0.1)
        except Exception as e:
            logging.warning("watcher error: %s", e)
        await asyncio.sleep(CHECK_EVERY)


# ---------- запуск ----------
async def health(request):
    return web.Response(text="ok")


async def main():
    # маленький веб-сервер, чтобы Render видел открытый порт
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()

    bot = Bot(os.environ["BOT_TOKEN"])  # токен берётся из Render, не из кода
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    watcher_task = asyncio.create_task(watcher(bot))
    try:
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
