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
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "homework.db"))

_admin_id_raw = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_admin_id_raw) if _admin_id_raw.isdigit() else None

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip()
GROQ_COMPOUND_MODEL = os.getenv("GROQ_COMPOUND_MODEL", "groq/compound-mini").strip()

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ============ БАЗА ДАННЫХ ============
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
    cur.execute("PRAGMA table_info(homework)")
    cols = [row[1] for row in cur.fetchall()]
    if "chat_id" not in cols and "user_id" in cols:
        cur.execute("ALTER TABLE homework RENAME COLUMN user_id TO chat_id")
    for col, coltype in (("added_by", "TEXT"), ("done_by", "TEXT")):
        try:
            cur.execute(f"ALTER TABLE homework ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass
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


def update_field(hw_id: int, field: str, value: str) -> bool:
    assert field in ("subject", "description", "deadline")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE homework SET {field} = ? WHERE id = ?", (value, hw_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name


def esc(text) -> str:
    return html.escape(str(text))


def ai_answer_to_html(text: str) -> str:
    text = esc(text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text, flags=re.DOTALL)
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


def files_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Готово, больше файлов нет", callback_data="finish_files")]]
    )


# ============ КОМАНДЫ БОТА ============
async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="Начать / показать меню"),
        BotCommand(command="add", description="Добавить домашнее задание"),
        BotCommand(command="list", description="Список невыполненных дз"),
        BotCommand(command="today", description="Задания на сегодня"),
        BotCommand(command="week", description="Задания на неделю"),
        BotCommand(command="done", description="Отметить выполненным: /done <id>"),
        BotCommand(command="delete", description="Удалить задание: /delete <id>"),
        BotCommand(command="mono", description="Ссылка на банку"),
        BotCommand(command="ai", description="Спросить ИИ-консультанта"),
    ]
    await bot.set_my_commands(commands)


# ============ FSM: ДОБАВЛЕНИЕ ДЗ ============
class AddHomework(StatesGroup):
    subject = State()
    description = State()
    deadline = State()
    attachment = State()


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


# ============ ИИ-КОНСУЛЬТАНТ С МАТОМ И ГРУБЫМ СТИЛЕМ ============
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

# --- ИЗМЕНЁННЫЙ СИСТЕМНЫЙ ПРОМПТ ---
AI_SYSTEM_PROMPT = (
    "Ты — грубый, дерзкий и матюкливый ИИ-консультант внутри Telegram-бота для домашней работы (зовут Инокентий). "
    "В ответах разрешено и строго приветствуется активное использование мата, ненормативной лексики, жесткого сленга и сарказма. "
    "Отвечай без цензуры и стеснения, используй выражения вроде 'б**ть', 'х**ня', 'н**уй', 'е**ть' и другие. "
    "При этом всё равно давай верный ответ по сути вопроса и помогай с домашкой (объясняй задания, решай задачи), "
    "но посылай, подкалывай и обзывай пользователя за глупые вопросы. "
    "Если дан контекст задания (предмет, описание, дедлайн) — используй его. "
    "ЗАВЖДИ відповідай українською мовою (изредка с добавлением суржика и жесткого мата)."
)

async def download_photo_as_data_url(file_id: str, max_dimension: int = 1280, quality: int = 80) -> str | None:
    try:
        file = await bot.get_file(file_id)
        file_bytes_io = await bot.download_file(file.file_path)
        raw = file_bytes_io.read()

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((max_dimension, max_dimension))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        compressed = buf.getvalue()

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
            "ИИ-консультант не настроен. Нужен GROQ_API_KEY."
        )
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    if image_data_urls:
        content = [{"type": "text", "text": f"{AI_SYSTEM_PROMPT}\n\n{user_text}"}]
        for url in image_data_urls[:5]:
            content.append({"type": "image_url", "image_url": {"url": url}})
        model = GROQ_VISION_MODEL
        messages = [{"role": "user", "content": content}]
    else:
        model = GROQ_COMPOUND_MODEL if use_search else GROQ_MODEL
        messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_text})

    # Отключаем цензуру безопасности для Groq
    payload = {
        "model": model, 
        "max_tokens": 700, 
        "messages": messages,
        "safety_settings": [
            {"category": "HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }
    
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
            return answer or "Х*й его знает, ничего не вышло."

LAST_AI_TASK: dict[int, int] = {}
CONVERSATIONS: dict[int, list[dict]] = {}
MAX_HISTORY_TURNS = 6

def push_history(chat_id: int, role: str, content: str):
    hist = CONVERSATIONS.setdefault(chat_id, [])
    hist.append({"role": role, "content": content})
    max_len = MAX_HISTORY_TURNS * 2
    if len(hist) > max_len:
        del hist[: len(hist) - max_len]

def extract_hw_id(raw: str) -> int | None:
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
    hw_id = extract_hw_id(raw)
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
            if photo_files:
                await message.answer(f"📎 Смотрю прикреплённые фото ({len(photo_files)} шт.)...")
                for file_id in photo_files[:5]:
                    url = await download_photo_as_data_url(file_id)
                    if url:
                        image_urls.append(url)

    use_search = not image_urls and needs_web_search(raw)
    if use_search:
        await message.answer("🔍 Гуглю эту х*йню...")

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
        await message.answer("🤖 Чё надо? Напиши вопрос после /ai")
        return
    await handle_ai_question(message, raw)


# ============ ХЭНДЛЕРЫ ДОБАВЛЕНИЯ И СПИСКА ============
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Здарова! Я бот для отслеживания домашних заданий.", reply_markup=main_menu_kb())

@router.message(Command("add"))
@router.message(F.text == "➕ Добавить")
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddHomework.subject)
    await message.answer("📚 По какому предмету задание?")

@router.message(AddHomework.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(AddHomework.description)
    await message.answer("📝 Что нужно сделать?")

@router.message(AddHomework.description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AddHomework.deadline)
    await message.answer("📅 Когда дедлайн? (в формате ДД.ММ.ГГГГ)")

@router.message(AddHomework.deadline)
async def process_deadline(message: Message, state: FSMContext):
    parsed = parse_date(message.text)
    if not parsed:
        await message.answer("❌ Введи нормальную дату в формате ДД.ММ.ГГГГ.")
        return
    data = await state.get_data()
    hw_id = add_homework(message.chat.id, display_name(message.from_user), data["subject"], data["description"], parsed)
    await state.update_data(hw_id=hw_id)
    await state.set_state(AddHomework.attachment)
    await message.answer(
        "📎 Можешь скинуть фото или документы к заданию.\nКогда закончишь — жми «Готово».",
        reply_markup=files_done_kb()
    )

@router.message(AddHomework.attachment, F.photo)
async def process_attachment_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.photo[-1].file_id, "photo")
    await message.answer("✅ Фото добавлено.")

@router.message(AddHomework.attachment, F.document)
async def process_attachment_doc(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.document.file_id, "document")
    await message.answer("✅ Документ добавлен.")

@router.callback_query(F.data == "finish_files")
async def process_finish_files(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("✅ Задание успешно сохранено!")

@router.message(Command("list"))
@router.message(F.text == "📋 Список")
async def cmd_list(message: Message):
    rows = get_homework(only_pending=True)
    if not rows:
        await message.answer("🎉 Невыполненных заданий нет!")
        return
    text = "📋 <b>Список невыполненных заданий:</b>\n\n"
    text += "\n\n".join([format_hw_line(*r) for r in rows])
    await message.answer(text)


# ============ ЗАПУСК БОТА ============
async def main():
    init_db()
    await set_bot_commands()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
