import asyncio
import base64
import html
import io
import logging
import os
import re
import sqlite3
from datetime import datetime, date, timedelta

import aiohttp
from PIL import Image
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН")
# DB_PATH можно переопределить переменной окружения — например, указать путь
# внутри подключённого Railway Volume (/data/homework.db), чтобы данные
# переживали обновления кода. Без этой переменной база хранится рядом с
# bot.py — годится для запуска на своём компьютере, но НЕ для Railway.
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "homework.db"))
# ID администратора — только этот пользователь может удалять задания.
# Узнать свой id можно у бота @userinfobot. Задаётся переменной окружения ADMIN_ID.
_admin_id_raw = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_admin_id_raw) if _admin_id_raw.isdigit() else None
# Для ИИ-консультанта (/ai) — бесплатный ключ с console.groq.com
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# llama-3.3-70b-versatile отключена Groq 16.08.2026 — используем актуальную модель
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
# Модель с поддержкой изображений — для просмотра прикреплённых фото. Groq часто меняет
# состав моделей для картинок; если перестанет работать, поменяйте эту переменную на
# актуальное имя с console.groq.com/docs/vision, код трогать не нужно.
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip()
# Groq compound — модель со встроенным веб-поиском (сама решает, когда гуглить),
# не требует отдельных ключей/API — работает на том же GROQ_API_KEY.
GROQ_COMPOUND_MODEL = os.getenv("GROQ_COMPOUND_MODEL", "groq/compound-mini").strip()

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ============ БАЗА ДАННЫХ ============
# Список заданий общий для всех (не привязан к конкретному чату) — его видно
# и в личке с ботом, и в любой группе, куда бот добавлен. Добавлять новые
# задания можно только из личного чата с ботом (см. ниже).
# Файлы вынесены в отдельную таблицу — к одному заданию можно прикрепить
# сколько угодно файлов.
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            added_by TEXT,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            deadline TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            done_by TEXT,
            reminded INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS homework_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hw_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            FOREIGN KEY (hw_id) REFERENCES homework (id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Миграция со старых версий базы
    cur.execute("PRAGMA table_info(homework)")
    cols = [row[1] for row in cur.fetchall()]
    if "chat_id" not in cols and "user_id" in cols:
        cur.execute("ALTER TABLE homework RENAME COLUMN user_id TO chat_id")
    for col, coltype in (("added_by", "TEXT"), ("done_by", "TEXT")):
        try:
            cur.execute(f"ALTER TABLE homework ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass
    # Если в старой версии файл хранился прямо в homework (file_id/file_type) —
    # перенесём его в новую таблицу homework_files.
    cur.execute("PRAGMA table_info(homework)")
    cols = [row[1] for row in cur.fetchall()]
    if "file_id" in cols:
        cur.execute("SELECT id, file_id, file_type FROM homework WHERE file_id IS NOT NULL")
        for hw_id, file_id, file_type in cur.fetchall():
            cur.execute(
                "INSERT INTO homework_files (hw_id, file_id, file_type) VALUES (?, ?, ?)",
                (hw_id, file_id, file_type or "document"),
            )
    conn.commit()
    conn.close()


def add_homework(chat_id: int, added_by: str, subject: str, description: str, deadline: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO homework (chat_id, added_by, subject, description, deadline, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, added_by, subject, description, deadline, datetime.now().isoformat()),
    )
    hw_id = cur.lastrowid
    conn.commit()
    conn.close()
    return hw_id


def add_file(hw_id: int, file_id: str, file_type: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO homework_files (hw_id, file_id, file_type) VALUES (?, ?, ?)",
        (hw_id, file_id, file_type),
    )
    conn.commit()
    conn.close()


def get_files(hw_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT file_id, file_type FROM homework_files WHERE hw_id = ?", (hw_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


# ============ НАСТРОЙКИ (ссылка на монобанку и т.п.) ============
def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_setting(key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_homework(only_pending=True, days_ahead: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = ("SELECT id, subject, description, deadline, done, added_by, done_by "
              "FROM homework WHERE 1=1")
    params = []
    if only_pending:
        query += " AND done = 0"
    if days_ahead is not None:
        today = date.today()
        limit = today + timedelta(days=days_ahead)
        query += " AND date(deadline) BETWEEN date(?) AND date(?)"
        params += [today.isoformat(), limit.isoformat()]
    query += " ORDER BY date(deadline) ASC"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_homework_on_date(target: date, only_pending=True):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = ("SELECT id, subject, description, deadline, done, added_by, done_by "
              "FROM homework WHERE date(deadline) = ?")
    params = [target.isoformat()]
    if only_pending:
        query += " AND done = 0"
    query += " ORDER BY id ASC"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_one(hw_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, subject, description, deadline, done, added_by, done_by FROM homework WHERE id = ?",
        (hw_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def mark_done(hw_id: int, done_by: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE homework SET done = 1, done_by = ? WHERE id = ?", (done_by, hw_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def delete_homework(hw_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
    changed = cur.rowcount > 0
    cur.execute("DELETE FROM homework_files WHERE hw_id = ?", (hw_id,))
    conn.commit()
    conn.close()
    return changed


def get_overdue_undone():
    """Задания, у которых дедлайн уже прошёл (раньше сегодняшнего дня) и которые
    никто не отметил и не удалил вручную — кандидаты на автоудаление в 17:00."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, chat_id, subject, description FROM homework "
        "WHERE done = 0 AND date(deadline) < date('now')"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def update_field(hw_id: int, field: str, value: str) -> bool:
    assert field in ("subject", "description", "deadline")  # защита от произвольных имён колонок
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE homework SET {field} = ? WHERE id = ?", (value, hw_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_due_tomorrow_unreminded():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    cur.execute(
        "SELECT id, chat_id, subject, description FROM homework "
        "WHERE date(deadline) = ? AND done = 0 AND reminded = 0",
        (tomorrow,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_reminded(hw_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE homework SET reminded = 1 WHERE id = ?", (hw_id,))
    conn.commit()
    conn.close()


def display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name


def esc(text) -> str:
    """Экранирует текст перед вставкой в HTML-разметку Telegram —
    защищает от поломки сообщения, если в предмете/описании есть символы < > &."""
    return html.escape(str(text))


def ai_answer_to_html(text: str) -> str:
    """ИИ иногда всё равно отвечает Markdown'ом (**жирный**, ### заголовок), хотя его
    просят этого не делать. Тут это конвертируется в настоящий HTML, который Telegram
    реально отрисует жирным/курсивом, а не покажет звёздочки как есть."""
    text = esc(text)  # сначала экранируем спецсимволы, потом уже вставляем свои теги

    # Заголовки markdown (### Заголовок) — просто делаем жирными
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # **жирный** и __жирный__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    # *курсив* и _курсив_ (после жирного, чтобы не сломать друг друга)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text, flags=re.DOTALL)
    # Маркеры списков "- пункт" / "* пункт" в начале строки — заменяем на аккуратный buллет
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)
    return text


# ============ КЛАВИАТУРЫ ============
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📋 Список")],
            [KeyboardButton(text="🔥 Сегодня"), KeyboardButton(text="📅 На завтра")],
            [KeyboardButton(text="📆 Неделя")],
        ],
        resize_keyboard=True,
    )


def hw_actions_kb(hw_id: int, files_count: int) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{hw_id}")]]
    if files_count:
        word = "файл" if files_count == 1 else ("файла" if 2 <= files_count <= 4 else "файлов")
        buttons.insert(0, [InlineKeyboardButton(
            text=f"📎 Показать {files_count} {word}", callback_data=f"files:{hw_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def files_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Готово, больше файлов нет", callback_data="finish_files")]]
    )


def edit_choice_kb(hw_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Предмет", callback_data=f"editfield:{hw_id}:subject")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"editfield:{hw_id}:description")],
        [InlineKeyboardButton(text="📅 Дедлайн", callback_data=f"editfield:{hw_id}:deadline")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="edit_cancel")],
    ])


# ============ КОМАНДЫ БОТА (меню "/" в Telegram) ============
async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="Начать / показать меню"),
        BotCommand(command="add", description="Добавить домашнее задание"),
        BotCommand(command="list", description="Список невыполненных дз"),
        BotCommand(command="today", description="Задания на сегодня"),
        BotCommand(command="week", description="Задания на неделю"),
        BotCommand(command="done", description="Отметить выполненным: /done <id>"),
        BotCommand(command="delete", description="Удалить задание: /delete <id>"),
        BotCommand(command="dbinfo", description="[admin] Диагностика базы данных"),
        BotCommand(command="mono", description="Ссылка на банку"),
        BotCommand(command="ai", description="Спросить ИИ-консультанта"),
        BotCommand(command="setmono", description="[admin] Указать ссылку на банку"),
        BotCommand(command="edit", description="Изменить задание: /edit <id>"),
        BotCommand(command="files", description="Показать файлы задания: /files <id>"),
    ]
    await bot.set_my_commands(commands)


# ============ FSM: ДОБАВЛЕНИЕ ДЗ ============
class AddHomework(StatesGroup):
    subject = State()
    description = State()
    deadline = State()
    attachment = State()  # можно прислать несколько файлов подряд


# ============ FSM: РЕДАКТИРОВАНИЕ ДЗ ============
class EditHomework(StatesGroup):
    waiting_value = State()


def parse_date(text: str) -> str | None:
    text = text.strip()
    formats = ["%d.%m.%Y", "%d.%m.%y", "%d.%m"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%d.%m":
                dt = dt.replace(year=date.today().year)
            return dt.date().isoformat()
        except ValueError:
            continue
    return None


def format_hw_line(hw_id, subject, description, deadline, done, added_by=None, done_by=None) -> str:
    d = datetime.strptime(deadline, "%Y-%m-%d").date()
    days_left = (d - date.today()).days
    if days_left < 0:
        status = "⚠️ <b>просрочено</b>"
    elif days_left == 0:
        status = "🔥 <b>сегодня</b>"
    elif days_left == 1:
        status = "⏰ <b>завтра</b>"
    else:
        status = f"через {days_left} дн."
    mark = "✅" if done else "▫️"

    lines = [
        f"{mark} <b>#{hw_id} · {esc(subject)}</b>",
        f"{esc(description)}",
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
        f"📅 {d.strftime('%d.%m.%Y')} — {status}",
    ]
    footer = []
    if added_by:
        footer.append(f"добавил(а) {esc(added_by)}")
    if done and done_by:
        footer.append(f"выполнил(а) {esc(done_by)}")
    if footer:
        lines.append(f"<i>👤 {' · '.join(footer)}</i>")
    return "\n".join(lines)


# ============ БАЗОВЫЕ КОМАНДЫ ============
@router.message(CommandStart())
async def cmd_start(message: Message):
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        await message.answer(
            "👋 <b>Привет!</b>\n"
            "Список домашних заданий — общий для всех.\n\n"
            "<b>Доступно в этом чате:</b>\n"
            "📋 /list — посмотреть список\n"
            "🔥 /today — что сдавать сегодня\n"
            "📅 /tomorrow — что сдавать завтра\n"
            "📆 /week — что сдавать на неделе\n\n"
            "✏️ <i>Добавлять новые задания можно только в личном чате со мной.</i>"
        )
        return
    await message.answer(
        "👋 <b>Привет! Я бот для отслеживания домашних заданий.</b>\n\n"
        "<b>Основное — кнопками внизу или командами:</b>\n"
        "➕ /add — добавить дз\n"
        "📋 /list — список невыполненных дз\n"
        "🔥 /today — что сдавать сегодня\n"
        "📅 /tomorrow — что сдавать завтра\n"
        "📆 /week — что сдавать на неделе\n\n"
        "📎 <i>К заданию можно прикрепить сразу несколько файлов, а потом "
        "отредактировать предмет, описание или дедлайн в любой момент.</i>\n\n"
        "📢 <b>Добавь меня в общий чат класса</b> — там все смогут смотреть список "
        "командой /list, а добавлять новые задания сможешь только ты, в этой личке.",
        reply_markup=main_menu_kb(),
    )


# ============ БАНКА (MONOBANK) ============
def mono_message_text() -> str | None:
    link = get_setting("mono_link")
    if not link:
        return None
    note = get_setting("mono_note") or "На поддержку Инокентия"
    return f"💳 <b>{esc(note)}</b>\n👉 <a href=\"{esc(link)}\">Перейти в Monobank</a>"


@router.message(Command("mono"))
async def cmd_mono(message: Message):
    text = mono_message_text()
    if not text:
        extra = "\nНастроить: /setmono <ссылка на банку>" if ADMIN_ID is None or message.from_user.id == ADMIN_ID else ""
        await message.answer("📭 Ссылка на банку ещё не добавлена." + extra)
        return
    await message.answer(text, disable_web_page_preview=False)


@router.message(Command("setmono"))
async def cmd_setmono(message: Message):
    if ADMIN_ID is not None and message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Настраивать банку может только администратор.")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Использование: /setmono <ссылка> [подпись]\n"
            "Например:\n/setmono https://send.monobank.ua/jar/xxxxx Сбор на нужды группы"
        )
        return
    link = parts[1]
    note = parts[2] if len(parts) > 2 else None
    set_setting("mono_link", link)
    if note:
        set_setting("mono_note", note)
    await message.answer("✅ Ссылка на банку сохранена. Проверить: /mono")


# ============ ИИ-КОНСУЛЬТАНТ (/ai) ============
# ============ ВЕБ-ПОИСК (через встроенный инструмент Groq compound) ============
WEB_SEARCH_TRIGGERS = (
    "найди", "загугли", "погугли", "поищи", "пошукай", "знайди",
    "в интернете", "в інтернеті", "новост", "новини",
    "актуальн", "останн", "останні", "последн",
    "сейчас", "зараз", "сьогодні", "сегодня", "который час", "котра година",
    "какая погода", "яка погода", "курс валют", "курс долара", "цена на",
)


def needs_web_search(text: str) -> bool:
    low = text.lower()
    return any(trigger in low for trigger in WEB_SEARCH_TRIGGERS)


AI_SYSTEM_PROMPT = (
    "Ти — НЕ доброзичливий ШІ-консультант усередині Telegram-бота для відстеження домашніх завдань. "
    "Ти можеш відповідати на звичайні запитання і допомагати конкретно з домашкою: пояснювати завдання, "
    "підказувати хід розв'язання, розбирати тему. Якщо в повідомленні дано контекст завдання (предмет, "
    "опис, дедлайн) — використовуй його і відповідай стосовно цього завдання. "
    "Якщо є доступ до веб-пошуку і питання того потребує (актуальні події, погода, курси тощо) — "
    "користуйся ним і за можливості вказуй джерело інформації. "
    "ЗАВЖДИ відповідай українською мовою, навіть якщо запитання поставлено російською або іншою мовою. "
    "Відповідай стисло і по суті, без зайвої води та форматування зірочками."
)


async def download_photo_as_data_url(file_id: str, max_dimension: int = 1280, quality: int = 80) -> str | None:
    """Скачивает фото из Telegram, сжимает (иначе Groq возвращает 413 Request Entity Too Large
    на исходных фото высокого разрешения) и превращает в data-URL для отправки в ИИ."""
    try:
        file = await bot.get_file(file_id)
        file_bytes_io = await bot.download_file(file.file_path)
        raw = file_bytes_io.read()

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")  # на случай PNG с прозрачностью и т.п.
        img.thumbnail((max_dimension, max_dimension))  # уменьшаем, сохраняя пропорции

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        compressed = buf.getvalue()

        # Если всё ещё крупновато — сжимаем ещё агрессивнее
        if len(compressed) > 3_000_000:
            buf = io.BytesIO()
            img.thumbnail((800, 800))
            img.save(buf, format="JPEG", quality=60, optimize=True)
            compressed = buf.getvalue()

        b64 = base64.b64encode(compressed).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logging.warning(f"Не удалось скачать/сжать фото {file_id} для ИИ: {e}")
        return None


async def ask_ai(user_text: str, image_data_urls: list[str] | None = None,
                  history: list[dict] | None = None, use_search: bool = False) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError(
            "ИИ-консультант не настроен. Администратору нужно получить бесплатный ключ на "
            "console.groq.com и добавить переменную GROQ_API_KEY в настройках Railway."
        )
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    if image_data_urls:
        # Мультимодальный запрос: текст + картинки — нужна модель с поддержкой vision.
        # Vision-модели часто не принимают отдельную system-роль и длинную историю,
        # поэтому инструкцию вшиваем прямо в текст, а историю сюда не подмешиваем.
        content = [{"type": "text", "text": f"{AI_SYSTEM_PROMPT}\n\n{user_text}"}]
        for url in image_data_urls[:5]:  # у Groq лимит 5 изображений за запрос
            content.append({"type": "image_url", "image_url": {"url": url}})
        model = GROQ_VISION_MODEL
        messages = [{"role": "user", "content": content}]
    else:
        # groq/compound сам решает, когда погуглить — отдельный API для поиска не нужен
        model = GROQ_COMPOUND_MODEL if use_search else GROQ_MODEL
        messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_text})

    payload = {"model": model, "max_tokens": 700, "messages": messages}
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                raise RuntimeError(f"Ошибка API ({resp.status}): {err}")
            answer = data["choices"][0]["message"]["content"].strip()
            return answer or "Не получилось получить ответ, попробуйте переформулировать вопрос."


# Запоминаем, о каком задании шла речь в каждом чате — чтобы фразы вроде
# "а покажи его файлы" подхватывали контекст предыдущего вопроса.
LAST_AI_TASK: dict[int, int] = {}

# Память переписки с ИИ по каждому чату — чтобы бот помнил предыдущие сообщения
# в рамках одного разговора. Хранится в памяти процесса (не в базе), поэтому
# сбрасывается при перезапуске бота — это нормально.
CONVERSATIONS: dict[int, list[dict]] = {}
MAX_HISTORY_TURNS = 6  # сколько последних пар "вопрос-ответ" помнить


def push_history(chat_id: int, role: str, content: str):
    hist = CONVERSATIONS.setdefault(chat_id, [])
    hist.append({"role": role, "content": content})
    max_len = MAX_HISTORY_TURNS * 2
    if len(hist) > max_len:
        del hist[: len(hist) - max_len]


def clear_history(chat_id: int):
    CONVERSATIONS.pop(chat_id, None)

# Пока конкретный человек ведёт "активный разговор" с ИИ, обращаться по имени
# каждый раз не нужно. Ключ — (chat_id, user_id): сессия привязана к человеку,
# а не ко всему чату, иначе бот отвечал бы на чужие сообщения в группе,
# не имеющие к нему отношения.
AI_ACTIVE_UNTIL: dict[tuple[int, int], datetime] = {}
AI_SESSION_MINUTES = 5

MENU_BUTTON_TEXTS = {"➕ Добавить", "📋 Список", "🔥 Сегодня", "📅 На завтра", "📆 Неделя"}

# Фразы, после которых бот сам завершает активную сессию разговора — дальше снова
# нужно обращаться по имени, чтобы не отвечать на посторонние сообщения в чате.
AI_STOP_PHRASES = (
    "дякую", "дякуємо", "спасибо", "спасиб", "благодарю",
    "иди отдыхай", "йди відпочивай", "отключайся", "відключайся",
    "хватит", "досить", "все понятно", "все зрозуміло", "усе зрозуміло",
    "пока", "бувай", "до встречі",
)


def is_stop_phrase(text: str) -> bool:
    low = text.lower().strip(" .!?,")
    return any(phrase in low for phrase in AI_STOP_PHRASES)


def extract_hw_id(raw: str) -> int | None:
    """Находит номер задания в свободной фразе: '#13', '№13', '13 задание', 'задание 13'."""
    for pattern in (
        r"[#№]\s*(\d+)",
        r"(\d+)[-\s]*(?:го|му|м|е|ое|ую|ой)?\s*задани",
        r"задани\w*\s*[#№]?\s*(\d+)",
    ):
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


async def handle_ai_question(message: Message, raw: str):
    """Общая логика ИИ-консультанта — используется и командой /ai, и обычным текстом."""
    # Продлеваем "активный разговор" именно для этого человека — следующие его
    # сообщения без обращения по имени тоже будут доходить до ИИ, пока сессия
    # не истекла. На чужие сообщения в группе это не влияет.
    session_key = (message.chat.id, message.from_user.id)
    AI_ACTIVE_UNTIL[session_key] = datetime.now() + timedelta(minutes=AI_SESSION_MINUTES)

    hw_id = extract_hw_id(raw)
    # Если номера нет, но человек ссылается на "него/это" — берём последнее обсуждавшееся задание
    if hw_id is None and re.search(r"\b(его|это|этой|эту|этого|него|неё|ним|там)\b", raw, re.IGNORECASE):
        hw_id = LAST_AI_TASK.get(message.chat.id)

    prompt = raw
    image_urls = []
    if hw_id is not None:
        row = get_one(hw_id)
        if row:
            LAST_AI_TASK[message.chat.id] = hw_id
            _, subject, description, deadline, *_ = row
            prompt = (
                f"Контекст задания #{hw_id}:\nПредмет: {subject}\nЗадание: {description}\n"
                f"Дедлайн: {deadline}\n\nВопрос: {raw}"
            )
            all_files = get_files(hw_id)
            photo_files = [fid for fid, ftype in all_files if ftype == "photo"]
            video_files = [(fid, ftype) for fid, ftype in all_files if ftype == "video"]

            if photo_files:
                await message.answer(f"📎 Смотрю прикреплённые фото ({len(photo_files)} шт.)...")
                for file_id in photo_files[:5]:
                    url = await download_photo_as_data_url(file_id)
                    if url:
                        image_urls.append(url)

            if video_files:
                # Видео ИИ не анализирует (не поддерживается и раздувает запрос) —
                # просто пересылаем файл в чат, чтобы можно было посмотреть самому.
                prompt += "\n\n(К заданию также прикреплено видео — я прислал(а) его отдельным сообщением.)"
                for file_id, ftype in video_files:
                    await send_hw_file(message, hw_id, file_id, ftype)
        else:
            await message.answer(f"❌ Задание #{hw_id} не найдено, отвечаю без контекста задания.")

    # Если вопрос похож на "найди", "актуальное", "новости" и т.п. — используем модель
    # со встроенным веб-поиском (сама решит, гуглить ли, и что искать)
    use_search = not image_urls and needs_web_search(raw)
    if use_search:
        await message.answer("🔍 Ищу в интернете...")

    await bot.send_chat_action(message.chat.id, "typing")
    try:
        history = CONVERSATIONS.get(message.chat.id) if not image_urls else None
        answer = await ask_ai(prompt, image_urls or None, history, use_search)
    except Exception as e:
        logging.warning(f"Ошибка ИИ-консультанта: {e}")
        await message.answer(f"⚠️ {esc(str(e))}")
        return

    push_history(message.chat.id, "user", raw)
    push_history(message.chat.id, "assistant", answer)
    await message.answer(f"🤖 {ai_answer_to_html(answer)}")


@router.message(Command("ai"))
async def cmd_ai(message: Message):
    raw = message.text.partition(" ")[2].strip()
    if not raw:
        await message.answer(
            "🤖 Просто напишите мне вопрос обычным текстом — команда не обязательна.\n\n"
            "В группе позовите по имени:\n"
            "<code>Инокентий, объясни 13 задание</code>\n"
            "<code>Инокентий, что такое ковалентная связь?</code>\n\n"
            "<i>Если к заданию прикреплено фото — я его тоже посмотрю.</i>"
        )
        return
    await handle_ai_question(message, raw)


# ============ ДОБАВЛЕНИЕ ДЗ (только в личном чате с ботом) ============
@router.message(Command("add"), F.chat.type.in_({"group", "supergroup"}))
@router.message(F.text == "➕ Добавить", F.chat.type.in_({"group", "supergroup"}))
async def cmd_add_blocked_in_group(message: Message):
    me = await bot.me()
    await message.answer(
        "✋ <b>Добавлять задания можно только в личном чате с ботом.</b>\n"
        f"Напишите мне в личку: @{me.username}, и там используйте /add.\n"
        "А смотреть список — можно прямо здесь, командой /list."
    )


@router.message(Command("add"))
@router.message(F.text == "➕ Добавить")
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddHomework.subject)
    await message.answer("📚 По какому предмету задание?")


@router.message(AddHomework.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(AddHomework.description)
    await message.answer("📝 Что нужно сделать? (опишите задание)")


@router.message(AddHomework.description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AddHomework.deadline)
    await message.answer("📅 Когда дедлайн? (в формате ДД.ММ.ГГГГ, например 15.09.2026)")


@router.message(AddHomework.deadline)
async def process_deadline(message: Message, state: FSMContext):
    parsed = parse_date(message.text)
    if not parsed:
        await message.answer(
            "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ (например, 15.09.2026) или ДД.ММ."
        )
        return
    data = await state.get_data()
    # Создаём задание сразу, файлы будем прикреплять к уже существующей записи
    hw_id = add_homework(message.chat.id, display_name(message.from_user), data["subject"], data["description"], parsed)
    await state.update_data(hw_id=hw_id, deadline=parsed)
    await state.set_state(AddHomework.attachment)
    await message.answer(
        "📎 Можно прикрепить файлы к заданию — присылайте фото или документы одно за другим, "
        "сколько нужно.\n<i>Когда закончите — нажмите «Готово».</i>",
        reply_markup=files_done_kb(),
    )


@router.message(AddHomework.attachment, F.photo)
async def process_attachment_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.photo[-1].file_id, "photo")
    await message.answer("✅ Фото добавлено. Присылайте ещё, или нажмите «Готово».", reply_markup=files_done_kb())


@router.message(AddHomework.attachment, F.document)
async def process_attachment_document(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.document.file_id, "document")
    await message.answer("✅ Документ добавлен. Присылайте ещё, или нажмите «Готово».", reply_markup=files_done_kb())


@router.message(AddHomework.attachment, F.video)
async def process_attachment_video(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.video.file_id, "video")
    await message.answer(
        "✅ Видео добавлено. Присылайте ещё, или нажмите «Готово».\n"
        "<i>ИИ видео не анализирует — просто пришлёт его в чат по запросу.</i>",
        reply_markup=files_done_kb(),
    )


@router.callback_query(AddHomework.attachment, F.data == "finish_files")
async def process_finish_files(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    files_count = len(get_files(data["hw_id"]))
    await state.clear()
    d = datetime.strptime(data["deadline"], "%Y-%m-%d").date()
    text = (
        f"✅ <b>Задание добавлено!</b>\n"
        f"📚 <b>{esc(data['subject'])}</b>\n"
        f"{esc(data['description'])}\n"
        f"📅 {d.strftime('%d.%m.%Y')}"
    )
    if files_count:
        text += f"\n📎 Прикреплено файлов: {files_count}"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.message(AddHomework.attachment)
async def process_attachment_invalid(message: Message):
    await message.answer(
        "Пришлите фото, видео или документ, либо нажмите «✅ Готово, больше файлов нет» кнопкой выше."
    )


# ============ ПРОСМОТР СПИСКОВ (одно сообщение + листание кнопками) ============
PAGE_SIZE = 5


def fetch_rows_for_view(view: str):
    if view == "today":
        return get_homework(days_ahead=0)
    if view == "tomorrow":
        return get_homework_on_date(date.today() + timedelta(days=1))
    if view == "week":
        return get_homework(days_ahead=7)
    return get_homework()  # "list" и всё остальное — полный список


VIEW_TITLES = {
    "list": "📋 Общий список домашних заданий",
    "today": "🔥 На сегодня",
    "tomorrow": "📅 Дз на завтра",
    "week": "📆 На неделю",
}
VIEW_EMPTY = {
    "list": "Нет невыполненных заданий!",
    "today": "Сегодня сдавать ничего не нужно 👍",
    "tomorrow": "На завтра ничего не задано 👍",
    "week": "На этой неделе всё сдано или заданий нет 👍",
}


def build_list_page(view: str, page: int):
    rows = fetch_rows_for_view(view)
    if not rows:
        text = f"🎉 <b>{esc(VIEW_EMPTY.get(view, 'Пусто'))}</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 На поддержку Инокентия", callback_data="mono_info")]
        ])
        return text, kb

    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = rows[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    blocks = [f"<b>{VIEW_TITLES.get(view, 'Список')}</b>  ({page + 1}/{total_pages})", ""]
    files_buttons = []
    for hw_id, subject, description, deadline, done, added_by, done_by in chunk:
        blocks.append(format_hw_line(hw_id, subject, description, deadline, done, added_by, done_by))
        files_count = len(get_files(hw_id))
        if files_count:
            word = "файл" if files_count == 1 else ("файла" if 2 <= files_count <= 4 else "файлов")
            blocks[-1] += f"\n📎 файлов: {files_count}"
            files_buttons.append([InlineKeyboardButton(
                text=f"📎 Файлы к #{hw_id} ({files_count} {word})", callback_data=f"files:{hw_id}"
            )])
        blocks.append("")
    text = "\n".join(blocks).rstrip()

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"hwpage:{view}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"hwpage:{view}:{page + 1}"))
    keyboard = list(files_buttons)
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="💳 На поддержку Инокентия", callback_data="mono_info")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def send_hw_page(message: Message, view: str):
    text, kb = build_list_page(view, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("hwpage:"))
async def cb_hwpage(callback: CallbackQuery):
    _, view, page_str = callback.data.split(":")
    text, kb = build_list_page(view, int(page_str))
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "mono_info")
async def cb_mono_info(callback: CallbackQuery):
    text = mono_message_text()
    if not text:
        await callback.answer("Ссылка на банку ещё не добавлена", show_alert=True)
        return
    await callback.message.answer(text)
    await callback.answer()


@router.message(Command("list"))
@router.message(F.text == "📋 Список")
async def cmd_list(message: Message):
    await send_hw_page(message, "list")


@router.message(Command("today"))
@router.message(F.text == "🔥 Сегодня")
async def cmd_today(message: Message):
    await send_hw_page(message, "today")


@router.message(Command("tomorrow"))
@router.message(F.text == "📅 На завтра")
async def cmd_tomorrow(message: Message):
    await send_hw_page(message, "tomorrow")


@router.message(Command("week"))
@router.message(F.text == "📆 Неделя")
async def cmd_week(message: Message):
    await send_hw_page(message, "week")



# ============ ГОТОВО / УДАЛИТЬ — через команды ============
@router.message(Command("done"))
async def cmd_done(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /done <id>\nНапример: /done 3")
        return
    hw_id = int(parts[1])
    if mark_done(hw_id, display_name(message.from_user)):
        await message.answer(f"✅ Задание #{hw_id} отмечено как выполненное!")
    else:
        await message.answer("❌ Задание с таким id не найдено.")


@router.message(Command("dbinfo"))
async def cmd_dbinfo(message: Message):
    """Диагностика: показывает, где именно бот хранит базу данных прямо сейчас
    и сколько там записей — помогает найти проблему с Volume/DB_PATH на Railway."""
    if ADMIN_ID is not None and message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Эта команда только для администратора.")
        return

    exists = os.path.exists(DB_PATH)
    size = os.path.getsize(DB_PATH) if exists else 0

    total_rows = 0
    error = None
    if exists:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM homework")
            total_rows = cur.fetchone()[0]
            conn.close()
        except Exception as e:
            error = str(e)

    lines = [
        "🔧 <b>Диагностика базы данных</b>",
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
        f"📁 Путь: <code>{esc(DB_PATH)}</code>",
        f"📌 Взят из переменной DB_PATH: {'да' if os.getenv('DB_PATH') else 'нет (используется путь по умолчанию!)'}",
        f"📄 Файл существует: {'да ✅' if exists else 'НЕТ ❌'}",
        f"📦 Размер файла: {size} байт",
        f"📋 Всего заданий в базе: {total_rows}",
    ]
    if error:
        lines.append(f"⚠️ Ошибка чтения: {esc(error)}")
    await message.answer("\n".join(lines))


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    if ADMIN_ID is not None and message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Удалять задания может только администратор бота.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /delete <id>\nНапример: /delete 3")
        return
    hw_id = int(parts[1])
    if delete_homework(hw_id):
        await message.answer(f"🗑️ Задание #{hw_id} удалено.")
    else:
        await message.answer("❌ Задание с таким id не найдено.")


# ============ ГОТОВО / УДАЛИТЬ / ФАЙЛЫ — через кнопки ============
@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    who = display_name(callback.from_user)
    if mark_done(hw_id, who):
        await callback.message.edit_text(f"✅ Задание #{hw_id} отмечено как выполненное ({who})!")
    else:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.answer("Готово!")


@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(callback: CallbackQuery):
    if ADMIN_ID is not None and callback.from_user.id != ADMIN_ID:
        await callback.answer("🚫 Удалять задания может только администратор", show_alert=True)
        return
    hw_id = int(callback.data.split(":")[1])
    who = display_name(callback.from_user)
    if delete_homework(hw_id):
        await callback.message.edit_text(f"🗑️ Задание #{hw_id} удалено ({who}).")
    else:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.answer("Удалено")


async def send_hw_file(message: Message, hw_id: int, file_id: str, file_type: str):
    caption = f"📎 К заданию #{hw_id}"
    if file_type == "photo":
        await message.answer_photo(file_id, caption=caption)
    elif file_type == "video":
        await message.answer_video(file_id, caption=caption)
    else:
        await message.answer_document(file_id, caption=caption)


@router.callback_query(F.data.startswith("files:"))
async def cb_files(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    files = get_files(hw_id)
    if not files:
        await callback.answer("Файлов нет", show_alert=True)
        return
    for file_id, file_type in files:
        await send_hw_file(callback.message, hw_id, file_id, file_type)
    await callback.answer()


@router.message(Command("files"))
async def cmd_files(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /files <id>\nНапример: /files 3")
        return
    hw_id = int(parts[1])
    files = get_files(hw_id)
    if not files:
        await message.answer("📭 У этого задания нет файлов (или id не найден).")
        return
    for file_id, file_type in files:
        await send_hw_file(message, hw_id, file_id, file_type)


# ============ РЕДАКТИРОВАНИЕ ЗАДАНИЯ ============
@router.callback_query(F.data.startswith("edit:"))
async def cb_edit_start(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    if not get_one(hw_id):
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.message.answer("Что хотите изменить?", reply_markup=edit_choice_kb(hw_id))
    await callback.answer()


@router.message(Command("edit"))
async def cmd_edit(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /edit <id>\nНапример: /edit 3")
        return
    hw_id = int(parts[1])
    if not get_one(hw_id):
        await message.answer("❌ Задание с таким id не найдено.")
        return
    await message.answer("Что хотите изменить?", reply_markup=edit_choice_kb(hw_id))


@router.callback_query(F.data == "edit_cancel")
async def cb_edit_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("editfield:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext):
    _, hw_id, field = callback.data.split(":")
    hw_id = int(hw_id)
    prompts = {
        "subject": "📚 Введите новый предмет:",
        "description": "📝 Введите новое описание задания:",
        "deadline": "📅 Введите новый дедлайн (ДД.ММ.ГГГГ):",
    }
    await state.set_state(EditHomework.waiting_value)
    await state.update_data(hw_id=hw_id, field=field)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(prompts[field])
    await callback.answer()


@router.message(EditHomework.waiting_value)
async def process_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    hw_id, field = data["hw_id"], data["field"]
    value = message.text.strip()

    if field == "deadline":
        parsed = parse_date(value)
        if not parsed:
            await message.answer("❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ или ДД.ММ.")
            return
        value = parsed

    if update_field(hw_id, field, value):
        field_names = {"subject": "Предмет", "description": "Описание", "deadline": "Дедлайн"}
        shown_value = value
        if field == "deadline":
            shown_value = datetime.strptime(value, "%Y-%m-%d").date().strftime("%d.%m.%Y")
        await message.answer(f"✅ <b>{field_names[field]}</b> обновлён: {esc(shown_value)}")
    else:
        await message.answer("❌ Не удалось найти задание для изменения.")
    await state.clear()


# ============ РАЗГОВОР С ИИ БЕЗ КОМАНДЫ ============
# Регистрируется последним, чтобы не перехватывать команды и кнопки меню.
# Единственный способ "разбудить" бота с нуля — имя + слово-действие (см. WAKE_WORDS).
# Просто упоминание имени или случайный текст в чате разговор не запускают —
# все остальные текстовые триггеры отключены по просьбе пользователя.
WAKE_WORDS = (
    "вставай", "проснись", "прокинься", "прокидайся", "просыпайся",
    "ты тут", "ти тут", "отзовись", "озвися", "ау",
)


def is_wake_phrase(text: str) -> bool:
    low = text.lower()
    return "нокент" in low and any(w in low for w in WAKE_WORDS)


def wants_ai_stop(message: Message) -> bool:
    """Фразы вроде 'спасибо'/'дякую'/'отключайся' — завершают активную сессию,
    без обращения к ИИ (чтобы не тратить запрос на простое прощание)."""
    text = message.text or ""
    if not text or text.startswith("/") or not is_stop_phrase(text):
        return False
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot:
        return True
    active_until = AI_ACTIVE_UNTIL.get((message.chat.id, message.from_user.id))
    return bool(active_until and datetime.now() < active_until)


@router.message(wants_ai_stop)
async def ai_stop(message: Message):
    AI_ACTIVE_UNTIL.pop((message.chat.id, message.from_user.id), None)
    clear_history(message.chat.id)
    await message.answer("😊 Будь ласка! Звертайтесь знову, якщо що — просто покличте по імені.")


def wants_ai(message: Message) -> bool:
    text = message.text or ""
    if not text or text.startswith("/"):
        return False
    if text in MENU_BUTTON_TEXTS:
        return False

    # Явная команда "разбудить" — имя + слово-действие. Запускает новую сессию.
    if is_wake_phrase(text):
        return True

    # Продолжение уже идущего разговора: ответ на сообщение бота,
    # или сессия для этого конкретного человека ещё не истекла.
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot:
        return True
    active_until = AI_ACTIVE_UNTIL.get((message.chat.id, message.from_user.id))
    if active_until and datetime.now() < active_until:
        return True
    return False



@router.message(wants_ai)
async def ai_freeform(message: Message):
    # Убираем обращение по имени из вопроса, чтобы не путать модель
    cleaned = re.sub(r"\b[иИ]н+окент\w*\b[\s,!:—-]*", "", message.text, flags=re.IGNORECASE).strip()
    await handle_ai_question(message, cleaned or message.text)


# ============ НАПОМИНАНИЯ И АВТОУДАЛЕНИЕ ПРОСРОЧЕННЫХ ============
async def send_reminders():
    rows = get_due_tomorrow_unreminded()
    for hw_id, chat_id, subject, description in rows:
        try:
            await bot.send_message(
                chat_id,
                f"⏰ <b>Напоминание!</b> Завтра дедлайн:\n"
                f"📚 <b>{esc(subject)}</b>\n{esc(description)}",
            )
            mark_reminded(hw_id)
        except Exception as e:
            logging.warning(f"Не удалось отправить напоминание {hw_id}: {e}")


async def cleanup_overdue():
    """Каждый день в 17:00: если задание просрочено и никто не удалил его вручную,
    бот удаляет его сам и сообщает в чат, откуда оно было добавлено."""
    rows = get_overdue_undone()
    for hw_id, chat_id, subject, description in rows:
        if delete_homework(hw_id):
            logging.info(f"Автоудаление просроченного задания #{hw_id} ({subject})")
            try:
                await bot.send_message(
                    chat_id,
                    f"🗑️ <b>Автоматически удалено просроченное задание:</b>\n"
                    f"📚 <b>{esc(subject)}</b>\n{esc(description)}",
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить об автоудалении {hw_id}: {e}")


async def main():
    init_db()
    await set_bot_commands()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "cron", hour=20, minute=0)
    scheduler.add_job(cleanup_overdue, "cron", hour=17, minute=0)
    scheduler.start()

    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
