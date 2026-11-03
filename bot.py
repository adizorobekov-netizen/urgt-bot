import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import time
from datetime import date, timedelta
from urllib.parse import urljoin

import aiohttp
import pdfplumber
import pypdfium2 as pdfium
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram import BaseMiddleware
from PIL import Image


# ---------- настройки ----------
PAGE_URL = os.environ.get(
    "PAGE_URL",
    "https://urgt66.ru/partition/136056/"
)

PREFS_FILE = os.environ.get("PREFS_FILE", "prefs.json")
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")

CACHE_TTL = 24 * 3600
CHECK_EVERY = 1800

# Канал, на который пользователь должен подписаться
CHANNEL_ID = "@kirafear"
CHANNEL_URL = "https://t.me/kirafear"


logging.basicConfig(level=logging.INFO)
router = Router()


# ---------- настройки пользователей ----------
try:
    with open(PREFS_FILE, encoding="utf-8") as f:
        prefs = json.load(f)
except Exception:
    prefs = {}


def save_prefs():
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                prefs,
                f,
                ensure_ascii=False
            )
    except Exception as e:
        logging.warning("save prefs failed: %s", e)


def user(uid):
    return prefs.setdefault(
        str(uid),
        {
            "group": None,
            "date": None,
            "wait": None
        }
    )


# ---------- подписка на канал ----------
def subscription_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Подписаться на канал",
                    url=CHANNEL_URL
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Проверить подписку",
                    callback_data="check_sub"
                )
            ]
        ]
    )


async def is_subscribed(bot: Bot, uid: int) -> bool:
    """
    Проверяет подписку пользователя на канал.
    Бот должен иметь возможность получать информацию
    об участниках канала.
    """
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=uid
        )

        if member.status in (
            "creator",
            "administrator",
            "member"
        ):
            return True

        if (
            member.status == "restricted"
            and getattr(member, "is_member", False)
        ):
            return True

        return False

    except Exception as e:
        logging.warning(
            "subscription check failed for %s: %s",
            uid,
            e
        )
        return False


class SubscriptionMiddleware(BaseMiddleware):
    """
    Не пропускает пользователя к функциям бота,
    пока он не подписан на обязательный канал.
    """

    async def __call__(
        self,
        handler,
        event,
        data
    ):
        bot = data["bot"]

        # Проверка подписки обрабатывается отдельным handler
        if (
            isinstance(event, CallbackQuery)
            and event.data == "check_sub"
        ):
            return await handler(event, data)

        if not event.from_user:
            return await handler(event, data)

        uid = event.from_user.id

        if await is_subscribed(bot, uid):
            return await handler(event, data)

        text = (
            "🔒 Для использования бота необходимо подписаться "
            "на наш канал.\n\n"
            "1. Нажми «Подписаться на канал».\n"
            "2. Подпишись на канал.\n"
            "3. Вернись сюда и нажми «Проверить подписку»."
        )

        if isinstance(event, CallbackQuery):
            await event.answer()

            if event.message:
                await event.message.answer(
                    text,
                    reply_markup=subscription_keyboard()
                )

        elif isinstance(event, Message):
            await event.answer(
                text,
                reply_markup=subscription_keyboard()
            )

        return


# ---------- названия групп ----------
LAT, CYR = "ABCEHKMOPTXYI", "АВСЕНКМОРТХУИ"
TR = str.maketrans(LAT, CYR)


def norm(s: str) -> str:
    return re.sub(
        r"[\s\-–—−]",
        "",
        s.upper().translate(TR)
    )


HEAD_RE = re.compile(r"^([^\d\-]{0,3})-(\d{3})$")


def decode_group(prefix: str, digits: str) -> str:
    p = prefix.replace('"', "Т").replace("!", "С")
    p = re.sub(
        r"[^А-ЯA-Z]",
        "",
        p.upper().translate(TR)
    )
    return (p or "П") + digits


# ---------- вырезание колонок ----------
def _render(pg, bbox, res):
    """Рисует только нужный кусок страницы."""
    W, H = pg.get_size()

    x0, t, x1, b = bbox

    x0 = max(0, x0)
    t = max(0, t)
    x1 = min(W, x1)
    b = min(H, b)

    bm = pg.render(
        scale=res / 72,
        crop=(x0, H - b, W - x1, t)
    )

    return bm.to_pil().convert("RGB")


def find_headers(page):
    heads = []

    for w in page.extract_words(
        extra_attrs=["fontname"]
    ):
        if "bold" not in w["fontname"].lower():
            continue

        m = HEAD_RE.match(w["text"])

        if m:
            w["g"] = decode_group(m[1], m[2])
            heads.append(w)

    heads.sort(key=lambda w: w["top"])

    rows = []

    for w in heads:
        if (
            rows
            and abs(rows[-1][0]["top"] - w["top"]) < 3
        ):
            rows[-1].append(w)
        else:
            rows.append([w])

    return [
        sorted(r, key=lambda w: w["x0"])
        for r in rows
        if len(r) >= 3
    ]


def _build_sync(sha: str, pdf_bytes: bytes):
    """
    Вырезает все группы из PDF один раз
    и сохраняет в cache/<sha>/ГРУППА.png.
    """

    final = os.path.join(CACHE_DIR, sha)
    tmp = final + ".tmp"

    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    doc = pdfium.PdfDocument(pdf_bytes)
    count = 0

    try:
        with pdfplumber.open(
            io.BytesIO(pdf_bytes)
        ) as pdf:

            for pi, page in enumerate(pdf.pages):
                rows = find_headers(page)
                words = page.extract_words()

                if not words:
                    continue

                table_left = (
                    min(w["x0"] for w in words) - 3
                )

                page_bottom = (
                    max(w["bottom"] for w in words) + 4
                )

                pg = doc[pi]

                for ri, row in enumerate(rows):
                    cx = [
                        (r["x0"] + r["x1"]) / 2
                        for r in row
                    ]

                    pitch = (
                        (cx[-1] - cx[0]) / (len(cx) - 1)
                        if len(cx) > 1
                        else 40
                    )

                    first_left = cx[0] - pitch / 2

                    for ci, w in enumerate(row):
                        path = os.path.join(
                            tmp,
                            w["g"] + ".png"
                        )

                        if os.path.exists(path):
                            continue

                        left = (
                            (cx[ci - 1] + cx[ci]) / 2
                            if ci > 0
                            else cx[ci] - pitch / 2
                        )

                        right = (
                            (cx[ci] + cx[ci + 1]) / 2
                            if ci + 1 < len(cx)
                            else cx[ci] + pitch / 2
                        )

                        top = w["top"] - 3

                        bottom = (
                            rows[ri + 1][0]["top"] - 3
                            if ri + 1 < len(rows)
                            else page_bottom
                        )

                        cell = [
                            x
                            for x in words
                            if (
                                x["x0"] >= left - 1
                                and x["x1"] <= right + 1
                                and x["top"] >= top - 1
                                and x["bottom"] <= bottom + 1
                            )
                        ]

                        cell.sort(
                            key=lambda x: (
                                round(x["top"]),
                                x["x0"]
                            )
                        )

                        sig = hashlib.sha1(
                            " ".join(
                                x["text"] for x in cell
                            ).encode()
                        ).hexdigest()

                        with open(
                            os.path.join(
                                tmp,
                                w["g"] + ".sig"
                            ),
                            "w"
                        ) as f:
                            f.write(sig)

                        h = max(
                            w["bottom"] - w["top"],
                            3
                        )

                        res = int(
                            min(
                                450,
                                max(250, 2000 / h)
                            )
                        )

                        i1 = _render(
                            pg,
                            (
                                table_left,
                                top,
                                first_left,
                                bottom
                            ),
                            res
                        )

                        i2 = _render(
                            pg,
                            (
                                left,
                                top,
                                right,
                                bottom
                            ),
                            res
                        )

                        out = Image.new(
                            "RGB",
                            (
                                i1.width + i2.width,
                                max(i1.height, i2.height)
                            ),
                            "white"
                        )

                        out.paste(i1, (0, 0))
                        out.paste(i2, (i1.width, 0))

                        out.save(
                            path,
                            "PNG",
                            optimize=True
                        )

                        count += 1

    finally:
        doc.close()

    with open(
        os.path.join(tmp, ".ok"),
        "w"
    ) as f:
        f.write(str(time.time()))

    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)

    logging.info(
        "cache built: %s, groups: %d",
        sha[:8],
        count
    )


_build_lock = asyncio.Lock()


async def build_cache(
    sha: str,
    data: bytes
):
    async with _build_lock:
        if os.path.exists(
            os.path.join(CACHE_DIR, sha, ".ok")
        ):
            return

        await asyncio.to_thread(
            _build_sync,
            sha,
            data
        )


async def cache_png(
    entry: dict,
    group: str
):
    """Готовая картинка группы из кэша."""

    path = os.path.join(
        CACHE_DIR,
        entry["sha"],
        norm(group) + ".png"
    )

    if not os.path.exists(
        os.path.join(
            CACHE_DIR,
            entry["sha"],
            ".ok"
        )
    ):
        await build_cache(
            entry["sha"],
            entry["data"]
        )

    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    return None


async def cache_sig(
    entry: dict,
    group: str
):
    """Подпись расписания группы."""

    path = os.path.join(
        CACHE_DIR,
        entry["sha"],
        norm(group) + ".sig"
    )

    if not os.path.exists(
        os.path.join(
            CACHE_DIR,
            entry["sha"],
            ".ok"
        )
    ):
        await build_cache(
            entry["sha"],
            entry["data"]
        )

    if os.path.exists(path):
        with open(path) as f:
            return f.read()

    return None


# ---------- сайт ----------
MONTHS = {
    "yanvarya": 1,
    "fevralya": 2,
    "marta": 3,
    "aprelya": 4,
    "maya": 5,
    "iyunya": 6,
    "iyulya": 7,
    "avgusta": 8,
    "sentyabrya": 9,
    "oktyabrya": 10,
    "noyabrya": 11,
    "dekabrya": 12
}

LINK_RE = re.compile(
    r'href="([^"]*raspisanie-zanyatij-na-'
    r'(\d{1,2})-([a-z]+)-(\d{4})[^"]*\.pdf)"',
    re.I
)

DAYS = [
    "пн",
    "вт",
    "ср",
    "чт",
    "пт",
    "сб",
    "вс"
]

_links = {
    "t": 0,
    "d": {}
}

_pdfs = {}


async def get_links() -> dict:
    if (
        time.time() - _links["t"] < 300
        and _links["d"]
    ):
        return _links["d"]

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                PAGE_URL,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                html = await r.text()

        found = {}

        for m in LINK_RE.finditer(html):
            mon = MONTHS.get(m[3].lower())

            if mon:
                found[
                    date(
                        int(m[4]),
                        mon,
                        int(m[2])
                    )
                ] = urljoin(
                    PAGE_URL,
                    m[1]
                )

        if found:
            _links.update(
                t=time.time(),
                d=found
            )

    except Exception as e:
        logging.warning(
            "page fetch failed: %s",
            e
        )

    return _links["d"]


async def ensure(
    url: str,
    max_age=None
):
    """Скачивает PDF и один раз вырезает все группы."""

    cur = _pdfs.get(url)

    if (
        cur
        and (
            max_age is None
            or time.time() - cur["t"] < max_age
        )
    ):
        return cur

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as r:
                data = await r.read()

        if data[:4] != b"%PDF":
            return cur

    except Exception as e:
        logging.warning(
            "pdf download failed: %s",
            e
        )
        return cur

    sha = hashlib.sha1(data).hexdigest()

    if cur and cur["sha"] == sha:
        cur["t"] = time.time()
        return cur

    entry = {
        "data": data,
        "sha": sha,
        "t": time.time()
    }

    await build_cache(
        sha,
        data
    )

    _pdfs[url] = entry

    if len(_pdfs) > 12:
        _pdfs.pop(
            min(
                _pdfs,
                key=lambda k: _pdfs[k]["t"]
            )
        )

    return entry


def fmt(d: date) -> str:
    return f"{d:%d.%m} ({DAYS[d.weekday()]})"


def default_date(links: dict):
    today = date.today()

    future = sorted(
        d for d in links
        if d >= today
    )

    return (
        future[0]
        if future
        else max(links) if links else None
    )


# ---------- интерфейс ----------
def kb(links: dict):
    rows = []
    cur = []

    for d in sorted(links)[-6:]:
        cur.append(
            InlineKeyboardButton(
                text=fmt(d),
                callback_data=f"d:{d.isoformat()}"
            )
        )

        if len(cur) == 3:
            rows.append(cur)
            cur = []

    if cur:
        rows.append(cur)

    rows.append(
        [
            InlineKeyboardButton(
                text="🔁 Сменить группу",
                callback_data="chgroup"
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="📄 Всё расписание (PDF)",
                callback_data="full"
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


async def send_schedule(
    msg: Message,
    uid,
    d=None,
    full=False
):
    u = user(uid)
    links = await get_links()

    if not links:
        return await msg.answer(
            "Не получилось открыть страницу расписания "
            "на сайте. Попробуй позже."
        )

    d = d or default_date(links)

    if d not in links:
        have = ", ".join(
            fmt(x) for x in sorted(links)
        )

        return await msg.answer(
            f"На {d:%d.%m.%Y} расписания на сайте нет.\n"
            f"Есть: {have}",
            reply_markup=kb(links)
        )

    u["date"] = d.isoformat()
    save_prefs()

    entry = await ensure(links[d])

    if not entry:
        return await msg.answer(
            "Не удалось скачать PDF. Попробуй позже.",
            reply_markup=kb(links)
        )

    if not full and u["group"]:
        png = await cache_png(
            entry,
            u["group"]
        )

        if png:
            _mark_sent(
                u,
                d,
                await cache_sig(
                    entry,
                    u["group"]
                ) or hashlib.sha1(png).hexdigest()
            )

            save_prefs()

            return await msg.answer_photo(
                BufferedInputFile(
                    png,
                    "schedule.png"
                ),
                caption=(
                    f"Группа {u['group']} · "
                    f"{d:%d.%m.%Y} "
                    f"({DAYS[d.weekday()]})"
                ),
                reply_markup=kb(links)
            )

        await msg.answer(
            f"Группу «{u['group']}» на {d:%d.%m} "
            "не нашёл. Отправляю полное расписание."
        )

    await msg.answer_document(
        BufferedInputFile(
            entry["data"],
            f"Расписание_{d:%d.%m.%Y}.pdf"
        ),
        reply_markup=kb(links)
    )


def parse_date(t: str):
    m = re.match(
        r"^(\d{1,2})[.\-/](\d{1,2})"
        r"(?:[.\-/](\d{2,4}))?$",
        t.strip()
    )

    if not m:
        return None

    yr = m[3]

    yr = (
        date.today().year
        if not yr
        else (
            int(yr) + 2000
            if len(yr) == 2
            else int(yr)
        )
    )

    try:
        return date(
            yr,
            int(m[2]),
            int(m[1])
        )

    except ValueError:
        return None


# ---------- обработчики подписки ----------
@router.callback_query(F.data == "check_sub")
async def check_subscription(
    c: CallbackQuery,
    bot: Bot
):
    if not await is_subscribed(
        bot,
        c.from_user.id
    ):
        await c.answer(
            "Подписка не найдена. Подпишись на канал и попробуй ещё раз.",
            show_alert=True
        )

        if c.message:
            await c.message.answer(
                "🔒 Сначала подпишись на канал:",
                reply_markup=subscription_keyboard()
            )

        return

    await c.answer(
        "✅ Подписка подтверждена!"
    )

    u = user(c.from_user.id)
    u["wait"] = "group"
    save_prefs()

    if c.message:
        await c.message.answer(
            "Привет! Напиши номер своей группы, например: ИТ-121\n\n"
            "Я буду сам присылать новое расписание, "
            "как только оно появится на сайте."
        )


# ---------- запуск ----------
@router.message(CommandStart())
async def start(m: Message):
    user(m.from_user.id)["wait"] = "group"
    save_prefs()

    await m.answer(
        "Привет! Напиши номер своей группы, например: ИТ-121\n\n"
        "Я буду сам присылать новое расписание, "
        "как только оно появится на сайте."
    )


@router.callback_query(F.data.startswith("d:"))
async def cb_day(c: CallbackQuery):
    await c.answer()

    if not user(c.from_user.id)["group"]:
        user(c.from_user.id)["wait"] = "group"

        return await c.message.answer(
            "Сначала напиши номер группы."
        )

    await send_schedule(
        c.message,
        c.from_user.id,
        date.fromisoformat(c.data[2:])
    )


@router.callback_query(F.data == "chgroup")
async def cb_group(c: CallbackQuery):
    await c.answer()

    user(c.from_user.id)["wait"] = "group"
    save_prefs()

    await c.message.answer(
        "Напиши номер новой группы:"
    )


@router.callback_query(F.data == "full")
async def cb_full(c: CallbackQuery):
    await c.answer()

    u = user(c.from_user.id)

    d = (
        date.fromisoformat(u["date"])
        if u["date"]
        else None
    )

    await send_schedule(
        c.message,
        c.from_user.id,
        d,
        full=True
    )


@router.message(F.text)
async def text(m: Message):
    u = user(m.from_user.id)
    t = m.text.strip()

    d = parse_date(t)

    if d and u["group"]:
        return await send_schedule(
            m,
            m.from_user.id,
            d
        )

    u["group"] = t.upper()
    u["wait"] = None

    save_prefs()

    await send_schedule(
        m,
        m.from_user.id
    )


# ---------- автопроверка сайта и рассылка ----------
def _same_sent(
    u: dict,
    d: date,
    sig: str
) -> bool:
    """True, если это же расписание уже отправлялось пользователю."""

    return (
        u.setdefault("sent", {}).get(
            d.isoformat()
        ) == sig
    )


def _mark_sent(
    u: dict,
    d: date,
    sig: str
):
    sent = u.setdefault("sent", {})

    sent[d.isoformat()] = sig

    for k in sorted(sent)[:-10]:
        del sent[k]


async def notify(
    bot: Bot,
    d: date,
    entry: dict,
    only_mark=False
):
    """Шлёт расписание, только если оно реально изменилось."""

    for uid, u in list(prefs.items()):
        if not u.get("group"):
            continue

        try:
            # Не отправляем расписание тем,
            # кто отписался от обязательного канала.
            if not await is_subscribed(
                bot,
                int(uid)
            ):
                continue

            png = await cache_png(
                entry,
                u["group"]
            )

            if not png:
                continue

            sig = (
                await cache_sig(
                    entry,
                    u["group"]
                )
                or hashlib.sha1(png).hexdigest()
            )

            if _same_sent(u, d, sig):
                continue

            if not only_mark:
                await bot.send_photo(
                    int(uid),
                    BufferedInputFile(
                        png,
                        "schedule.png"
                    ),
                    caption=(
                        f"🆕 Новое расписание · "
                        f"{u['group']} · "
                        f"{d:%d.%m.%Y} "
                        f"({DAYS[d.weekday()]})"
                    )
                )

                await asyncio.sleep(0.1)

            _mark_sent(
                u,
                d,
                sig
            )

        except Exception as e:
            logging.warning(
                "send to %s failed: %s",
                uid,
                e
            )

    save_prefs()


async def watcher(bot: Bot):
    """
    Раз в 30 минут проверяет сайт.
    Новый или изменённый файл режет один раз и рассылает.
    """

    seen = {}
    first = True

    while True:
        try:
            _links["t"] = 0

            links = await get_links()

            for d, url in sorted(links.items()):
                if d < date.today() - timedelta(days=1):
                    continue

                entry = await ensure(
                    url,
                    max_age=0
                )

                if not entry:
                    continue

                if seen.get(url) != entry["sha"]:
                    seen[url] = entry["sha"]

                    # При первом запуске только запоминаем
                    # существующее расписание без рассылки.
                    await notify(
                        bot,
                        d,
                        entry,
                        only_mark=first
                    )

            if links:
                first = False

        except Exception as e:
            logging.warning(
                "watcher error: %s",
                e
            )

        await asyncio.sleep(CHECK_EVERY)


# ---------- очистка кэша раз в сутки ----------
async def cleanup_loop():
    """
    Раз в час удаляет картинки старше суток
    и сразу создаёт свежие.
    """

    while True:
        await asyncio.sleep(3600)

        try:
            now = time.time()

            actual = {
                e["sha"]
                for e in _pdfs.values()
            }

            for name in os.listdir(CACHE_DIR):
                d = os.path.join(
                    CACHE_DIR,
                    name
                )

                marker = os.path.join(
                    d,
                    ".ok"
                )

                age = now - (
                    os.path.getmtime(marker)
                    if os.path.exists(marker)
                    else os.path.getmtime(d)
                )

                if (
                    age > CACHE_TTL
                    or (
                        name not in actual
                        and not name.endswith(".tmp")
                    )
                ):
                    shutil.rmtree(
                        d,
                        ignore_errors=True
                    )

                    logging.info(
                        "cache removed: %s",
                        name[:8]
                    )

            for e in list(_pdfs.values()):
                await build_cache(
                    e["sha"],
                    e["data"]
                )

        except Exception as e:
            logging.warning(
                "cleanup error: %s",
                e
            )


# ---------- запуск ----------
async def health(request):
    return web.Response(text="ok")


async def main():
    shutil.rmtree(
        CACHE_DIR,
        ignore_errors=True
    )

    os.makedirs(
        CACHE_DIR,
        exist_ok=True
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    await web.TCPSite(
        runner,
        "0.0.0.0",
        int(os.environ.get("PORT", 10000))
    ).start()

    bot = Bot(
        os.environ["BOT_TOKEN"]
    )

    dp = Dispatcher()

    dp.include_router(router)

    # Обязательная проверка подписки
    # для всех сообщений и callback-запросов.
    dp.message.outer_middleware(
        SubscriptionMiddleware()
    )

    dp.callback_query.outer_middleware(
        SubscriptionMiddleware()
    )

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    tasks = [
        asyncio.create_task(
            watcher(bot)
        ),
        asyncio.create_task(
            cleanup_loop()
        )
    ]

    try:
        await dp.start_polling(bot)

    finally:
        for t in tasks:
            t.cancel()

# ---------- рассылка всем пользователям ----------

@router.message(F.text.startswith("/broadcast"))
async def broadcast(m: Message, bot: Bot):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("⛔ У тебя нет доступа к этой команде.")

    text_to_send = m.text.partition(" ")[2].strip()

    if not text_to_send:
        return await m.answer(
            "Использование:\n"
            "/broadcast Текст сообщения\n\n"
            "Например:\n"
            "/broadcast Привет! Это сообщение от администратора."
        )

    users = list(prefs.keys())
    sent = 0
    failed = 0

    status = await m.answer(
        f"📨 Начинаю рассылку для {len(users)} пользователей..."
    )

    for uid in users:
        try:
            await bot.send_message(
                chat_id=int(uid),
                text=text_to_send
            )
            sent += 1

        except Exception as e:
            failed += 1
            logging.warning(
                "Broadcast failed for %s: %s",
                uid,
                e
            )

        await asyncio.sleep(0.05)

    await status.edit_text(
        "✅ Рассылка завершена!\n\n"
        f"📬 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )
if __name__ == "__main__":
    asyncio.run(main())
