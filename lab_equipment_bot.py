# Lab Equipment Usage Bot — расширенная версия
# Telegram-бот для учёта времени работы оборудования
# Функции: кнопки, номер образца, отчёты по периодам, напоминания, экспорт CSV

import asyncio
import sqlite3
import logging
import csv
from io import StringIO
from datetime import datetime, timedelta
from typing import List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import os
from pathlib import Path

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ОТ_BOTFATHER")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

EQUIPMENT_LIST = [
    "NMR",
    "Sorptometer",
    "HPLC",
    "Flow reactor",
    "High pressure reactor 50 mL",
    "High pressure reactor 100 mL",
    "ICP-MS",
    "XRD"
]

# На Railway лучше использовать Volume, чтобы база не терялась
DB_PATH = os.getenv("DB_PATH", "/data/equipment_log.db" if os.getenv("RAILWAY_ENVIRONMENT") else "equipment_log.db")
REMINDER_HOURS = int(os.getenv("REMINDER_HOURS", "10"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))

# Создаём папку для базы, если её нет
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# ========== ИНИЦИАЛИЗАЦИЯ ==========
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== FSM для номера образца ==========
class SessionForm(StatesGroup):
    waiting_for_note = State()

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Сессии оборудования
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            equipment TEXT,
            work_type TEXT,
            note TEXT,
            start_time TEXT,
            end_time TEXT,
            duration_hours REAL,
            status TEXT DEFAULT 'active',
            reminded INTEGER DEFAULT 0
        )
    ''')
    # Пользователи и права
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            is_admin INTEGER DEFAULT 0,
            is_allowed INTEGER DEFAULT 0,
            invited_at TEXT
        )
    ''')
    # Приглашения
    c.execute('''
        CREATE TABLE IF NOT EXISTS invites (
            token TEXT PRIMARY KEY,
            created_by INTEGER,
            created_at TEXT,
            used_by INTEGER,
            used_at TEXT,
            max_uses INTEGER DEFAULT 1
        )
    ''')
    conn.commit()

    # Гарантируем, что начальный админ есть в базе
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT OR IGNORE INTO users (user_id, is_admin, is_allowed, invited_at)
        VALUES (?, 1, 1, ?)
    ''', (ADMIN_ID, now))
    c.execute('UPDATE users SET is_admin = 1, is_allowed = 1 WHERE user_id = ?', (ADMIN_ID,))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])

def is_allowed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_allowed FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])

def add_or_update_user(user_id: int, username: str, full_name: str, allowed: bool = True, admin: bool = False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO users (user_id, username, full_name, is_admin, is_allowed, invited_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            is_allowed = excluded.is_allowed OR users.is_allowed,
            is_admin = excluded.is_admin OR users.is_admin
    ''', (user_id, username, full_name, int(admin), int(allowed), now))
    conn.commit()
    conn.close()

def create_invite(created_by: int, max_uses: int = 1) -> str:
    import secrets
    token = secrets.token_urlsafe(8)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO invites (token, created_by, created_at, max_uses)
        VALUES (?, ?, ?, ?)
    ''', (token, created_by, now, max_uses))
    conn.commit()
    conn.close()
    return token

def use_invite(token: str, user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT used_by, max_uses FROM invites WHERE token = ?", (token,))
    row = c.fetchone()
    if not row or row[0] is not None:  # уже использован или не существует
        conn.close()
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE token = ?", (user_id, now, token))
    conn.commit()
    conn.close()
    return True

def add_session(user_id: int, user_name: str, equipment: str, work_type: str, note: str = "") -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO sessions (user_id, user_name, equipment, work_type, note, start_time, status)
        VALUES (?, ?, ?, ?, ?, ?, 'active')
    ''', (user_id, user_name, equipment, work_type, note, now))
    session_id = c.lastrowid
    conn.commit()
    conn.close()
    return session_id

def end_session(session_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT start_time FROM sessions WHERE id = ? AND status = 'active'", (session_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    start_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds() / 3600
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        UPDATE sessions SET end_time = ?, duration_hours = ?, status = 'finished'
        WHERE id = ?
    ''', (end_str, round(duration, 2), session_id))
    conn.commit()
    conn.close()
    return {"duration": round(duration, 2), "end_time": end_str}

def get_active_sessions(user_id: int) -> List[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, equipment, work_type, start_time, note FROM sessions
        WHERE user_id = ? AND status = 'active'
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "equipment": r[1], "work_type": r[2], "start_time": r[3], "note": r[4]} for r in rows]

def get_long_sessions(hours: int) -> List[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    threshold = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        SELECT id, user_id, user_name, equipment, start_time FROM sessions
        WHERE status = 'active' AND start_time < ? AND reminded = 0
    ''', (threshold,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "user_id": r[1], "user_name": r[2], "equipment": r[3], "start_time": r[4]} for r in rows]

def mark_reminded(session_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE sessions SET reminded = 1 WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

def get_report(year: Optional[int] = None, month: Optional[int] = None) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = '''
        SELECT equipment, work_type, SUM(duration_hours) as total
        FROM sessions
        WHERE status = 'finished'
    '''
    params = []
    if year:
        query += " AND strftime('%Y', start_time) = ?"
        params.append(str(year))
    if month:
        query += " AND strftime('%m', start_time) = ?"
        params.append(f"{month:02d}")
    query += " GROUP BY equipment, work_type ORDER BY equipment"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    if not rows:
        period = ""
        if year and month:
            period = f" за {month:02d}.{year}"
        elif year:
            period = f" за {year} год"
        return f"Нет данных{period}."

    period_text = ""
    if year and month:
        period_text = f" за {month:02d}.{year}"
    elif year:
        period_text = f" за {year} год"
    else:
        period_text = " (всё время)"

    report = f"📊 <b>Сводный отчёт{period_text}</b>\n\n"
    current_eq = ""
    for eq, wtype, total in rows:
        if eq != current_eq:
            report += f"\n🔧 <b>{eq}</b>\n"
            current_eq = eq
        type_name = "Наука" if wtype == "science" else "Услуга"
        report += f"  • {type_name}: {total:.2f} ч\n"
    return report

def export_csv() -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, user_name, equipment, work_type, note, start_time, end_time, duration_hours
        FROM sessions WHERE status = 'finished'
        ORDER BY start_time DESC
    ''')
    rows = c.fetchall()
    conn.close()

    output = StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["ID", "Сотрудник", "Оборудование", "Тип", "Номер/заметка", "Начало", "Окончание", "Часы"])
    for row in rows:
        writer.writerow([
            row[0], row[1], row[2],
            "Наука" if row[3] == "science" else "Услуга",
            row[4] or "",
            row[5], row[6], row[7]
        ])
    return output.getvalue()

# ========== КЛАВИАТУРЫ ==========
async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None):
    """Редактирует сообщение, если это возможно, иначе отправляет новое."""
    try:
        if callback.message.photo:
            await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Начать работу", callback_data="start_work")
    builder.button(text="⏹ Закончить работу", callback_data="end_work")
    builder.button(text="📋 Мои активные", callback_data="my_active")
    if is_admin_user:
        builder.button(text="📊 Отчёт", callback_data="report_menu")
        builder.button(text="📥 Экспорт CSV", callback_data="export_csv")
        builder.button(text="👥 Пользователи", callback_data="users_menu")
        builder.button(text="🔗 Пригласить", callback_data="create_invite")
    builder.adjust(2)
    return builder.as_markup()

def equipment_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, eq in enumerate(EQUIPMENT_LIST):
        builder.button(text=eq, callback_data=f"eq:{i}")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)
    return builder.as_markup()

def work_type_kb(eq_index: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔬 Наука", callback_data=f"type:science:{eq_index}")
    builder.button(text="💼 Услуга", callback_data=f"type:service:{eq_index}")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()

def active_sessions_kb(sessions: List[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in sessions:
        type_emoji = "🔬" if s["work_type"] == "science" else "💼"
        note = f" | {s['note']}" if s['note'] else ""
        text = f"{type_emoji} {s['equipment']}{note}"
        builder.button(text=text[:60], callback_data=f"end:{s['id']}")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)
    return builder.as_markup()

def report_period_kb() -> InlineKeyboardMarkup:
    now = datetime.now()
    builder = InlineKeyboardBuilder()
    builder.button(text="Этот месяц", callback_data=f"rep:{now.year}:{now.month}")
    builder.button(text="Этот год", callback_data=f"rep:{now.year}:0")
    builder.button(text="Прошлый месяц", callback_data=f"rep:{(now.replace(day=1) - timedelta(days=1)).year}:{(now.replace(day=1) - timedelta(days=1)).month}")
    builder.button(text="Всё время", callback_data="rep:0:0")
    builder.button(text="« Назад", callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    invite_token = None
    if len(args) > 1 and args[1].startswith("invite_"):
        invite_token = args[1].replace("invite_", "")

    # Обработка приглашения
    if invite_token:
        if use_invite(invite_token, user.id):
            add_or_update_user(user.id, user.username, user.full_name, allowed=True)
            await message.answer("✅ Приглашение принято! Теперь у вас есть доступ к боту.")
        else:
            await message.answer("❌ Приглашение недействительно или уже использовано.")
            return

    # Проверка доступа
    if not is_allowed(user.id) and not is_admin(user.id):
        await message.answer(
            "🚫 Доступ ограничен.\n\n"
            "Попросите администратора прислать вам пригласительную ссылку."
        )
        return

    # Обновляем информацию о пользователе
    add_or_update_user(user.id, user.username, user.full_name, allowed=True, admin=is_admin(user.id))

    admin = is_admin(user.id)

    # Приветственная картинка (ищет welcome.jpg или welcome.jpg.JPG)
    possible_names = ["welcome.jpg", "welcome.jpg.JPG", "welcome.JPG"]
    welcome_path = None
    for name in possible_names:
        candidate = Path(__file__).parent / name
        if candidate.exists():
            welcome_path = candidate
            break

    caption = (
        "👋 <b>Добро пожаловать в LabaTrack!</b>\n\n"
        "Здесь фиксируется время работы оборудования.\n"
        "Выберите действие ниже:"
    )

    if welcome_path:
        from aiogram.types import FSInputFile
        photo = FSInputFile(welcome_path)
        await message.answer_photo(
            photo,
            caption=caption,
            reply_markup=main_menu_kb(admin),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            caption,
            reply_markup=main_menu_kb(admin),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "start_work")
async def start_work(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(callback, "Выберите оборудование:", reply_markup=equipment_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("eq:"))
async def select_equipment(callback: CallbackQuery):
    eq_index = int(callback.data.split(":")[1])
    eq_name = EQUIPMENT_LIST[eq_index]
    await safe_edit(
        callback,
        f"Оборудование: <b>{eq_name}</b>\n\nВыберите тип работы:",
        reply_markup=work_type_kb(eq_index),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("type:"))
async def select_type(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    work_type = parts[1]
    eq_index = int(parts[2])
    await state.update_data(work_type=work_type, eq_index=eq_index)
    await state.set_state(SessionForm.waiting_for_note)
    await safe_edit(
        callback,
        "Введите номер образца / заявки / комментарий\n"
        "(или отправьте «-» или «пропустить», чтобы не указывать):"
    )
    await callback.answer()

@dp.message(SessionForm.waiting_for_note)
async def process_note(message: Message, state: FSMContext):
    data = await state.get_data()
    note = message.text.strip()
    if note.lower() in ["-", "пропустить", "нет", "skip"]:
        note = ""
    work_type = data["work_type"]
    eq_index = data["eq_index"]
    eq_name = EQUIPMENT_LIST[eq_index]
    type_name = "Наука" if work_type == "science" else "Услуга"

    user = message.from_user
    session_id = add_session(
        user.id,
        user.full_name or user.username or str(user.id),
        eq_name,
        work_type,
        note
    )
    await state.clear()
    await message.answer(
        f"✅ <b>Сессия начата</b>\n\n"
        f"Оборудование: {eq_name}\n"
        f"Тип: {type_name}\n"
        f"Заметка: {note or '—'}\n"
        f"ID: {session_id}",
        reply_markup=main_menu_kb(is_admin(user.id)),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "end_work")
async def end_work(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    sessions = get_active_sessions(callback.from_user.id)
    if not sessions:
        await safe_edit(callback, "Нет активных сессий.", reply_markup=main_menu_kb(is_admin(callback.from_user.id)))
        await callback.answer()
        return
    await safe_edit(callback, "Выберите сессию для завершения:", reply_markup=active_sessions_kb(sessions))
    await callback.answer()

@dp.callback_query(F.data.startswith("end:"))
async def finish_session(callback: CallbackQuery):
    session_id = int(callback.data.split(":")[1])
    result = end_session(session_id)
    if not result:
        await callback.answer("Сессия уже завершена", show_alert=True)
        return
    await safe_edit(
        callback,
        f"✅ <b>Сессия завершена</b>\n\nДлительность: <b>{result['duration']} ч</b>",
        reply_markup=main_menu_kb(is_admin(callback.from_user.id)),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "my_active")
async def my_active(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    sessions = get_active_sessions(callback.from_user.id)
    if not sessions:
        text = "Нет активных сессий."
    else:
        text = "🟢 <b>Ваши активные сессии:</b>\n\n"
        for s in sessions:
            type_name = "Наука" if s["work_type"] == "science" else "Услуга"
            note = f"\n   Заметка: {s['note']}" if s['note'] else ""
            text += f"• {s['equipment']} ({type_name}) — с {s['start_time']}{note}\n"
    await safe_edit(callback, text, reply_markup=main_menu_kb(is_admin(callback.from_user.id)), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "report_menu")
async def report_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    await safe_edit(callback, "Выберите период отчёта:", reply_markup=report_period_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("rep:"))
async def show_period_report(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    year = int(parts[1]) if parts[1] != "0" else None
    month = int(parts[2]) if parts[2] != "0" else None
    report = get_report(year, month)
    await safe_edit(callback, report, reply_markup=main_menu_kb(True), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "export_csv")
async def export_csv_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    csv_data = export_csv()
    if len(csv_data.splitlines()) <= 1:
        await callback.answer("Нет данных", show_alert=True)
        return
    file = BufferedInputFile(csv_data.encode('utf-8-sig'), filename="equipment_log.csv")
    await callback.message.answer_document(file, caption="📥 Экспорт завершённых сессий")
    await callback.answer()

@dp.callback_query(F.data == "create_invite")
async def create_invite_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    token = create_invite(callback.from_user.id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=invite_{token}"
    await callback.message.answer(
        f"🔗 <b>Одноразовое приглашение</b>\n\n"
        f"Отправьте эту ссылку сотруднику:\n"
        f"<code>{link}</code>\n\n"
        f"Ссылка работает один раз.",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "users_menu")
async def users_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, full_name, username, is_admin FROM users WHERE is_allowed = 1 ORDER BY is_admin DESC, full_name")
    rows = c.fetchall()
    conn.close()

    if not rows:
        text = "Нет пользователей."
        builder = InlineKeyboardBuilder()
    else:
        text = "👥 <b>Пользователи:</b>\n\nНажмите на пользователя для управления:"
        builder = InlineKeyboardBuilder()
        for uid, name, uname, admin_flag in rows:
            role = "👑" if admin_flag else "👤"
            display = name or uname or str(uid)
            builder.button(text=f"{role} {display}", callback_data=f"manage:{uid}")
        builder.adjust(1)

    builder.button(text="🔗 Новое приглашение", callback_data="create_invite")
    builder.button(text="« Назад", callback_data="cancel")
    builder.adjust(1)

    # Если сообщение с фото — отправляем новое, иначе редактируем
    if callback.message.photo:
        await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await safe_edit(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("manage:"))
async def manage_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    target_id = int(callback.data.split(":")[1])
    if target_id == callback.from_user.id:
        await callback.answer("Нельзя управлять собой", show_alert=True)
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT full_name, username, is_admin FROM users WHERE user_id = ?", (target_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    name, uname, is_adm = row
    display = name or uname or str(target_id)
    role = "Админ" if is_adm else "Пользователь"

    builder = InlineKeyboardBuilder()
    if not is_adm:
        builder.button(text="👑 Сделать админом", callback_data=f"makeadmin:{target_id}")
    else:
        builder.button(text="👤 Снять админа", callback_data=f"removeadmin:{target_id}")
    builder.button(text="🚫 Убрать доступ", callback_data=f"revoke:{target_id}")
    builder.button(text="« Назад", callback_data="users_menu")
    builder.adjust(1)

    await safe_edit(
        callback,
        f"Управление: <b>{display}</b>\nРоль: {role}\nID: <code>{target_id}</code>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("makeadmin:"))
async def make_admin(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    target_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (target_id,))
    conn.commit()
    conn.close()
    await callback.answer("Пользователь стал админом", show_alert=True)
    await users_menu(callback)

@dp.callback_query(F.data.startswith("removeadmin:"))
async def remove_admin(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    target_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (target_id,))
    conn.commit()
    conn.close()
    await callback.answer("Права админа сняты", show_alert=True)
    await users_menu(callback)

@dp.callback_query(F.data.startswith("revoke:"))
async def revoke_access(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    target_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_allowed = 0, is_admin = 0 WHERE user_id = ?", (target_id,))
    conn.commit()
    conn.close()
    await callback.answer("Доступ отозван", show_alert=True)
    await users_menu(callback)

@dp.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    admin_flag = is_admin(callback.from_user.id)
    await safe_edit(callback, "Отменено.", reply_markup=main_menu_kb(admin_flag))
    await callback.answer()

# ========== НАПОМИНАНИЯ ==========
async def reminder_task():
    while True:
        try:
            long_sessions = get_long_sessions(REMINDER_HOURS)
            for s in long_sessions:
                try:
                    await bot.send_message(
                        s["user_id"],
                        f"⏰ Напоминание!\nСессия на <b>{s['equipment']}</b> длится уже более {REMINDER_HOURS} часов "
                        f"(начата {s['start_time']}).\nНе забудьте её завершить.",
                        parse_mode="HTML"
                    )
                    mark_reminded(s["id"])
                except Exception as e:
                    logging.warning(f"Не удалось отправить напоминание {s['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в reminder_task: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# ========== ЗАПУСК ==========
async def main():
    init_db()
    asyncio.create_task(reminder_task())
    print("Бот запущен (с напоминаниями и фильтрами отчётов)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
