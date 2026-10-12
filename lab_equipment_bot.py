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

def clean_sessions() -> int:
    """Удаляет все завершённые сессии. Пользователей не трогает."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE status = 'finished'")
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def get_estonian_holidays(year: int):
    """Возвращает список эстонских государственных праздников на год."""
    from datetime import date, timedelta

    def easter(y):
        # Алгоритм Гаусса для Пасхи
        a = y % 19
        b = y // 100
        c = y % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return date(y, month, day)

    easter_date = easter(year)
    good_friday = easter_date - timedelta(days=2)
    pentecost = easter_date + timedelta(days=49)

    holidays = [
        (date(year, 1, 1), "01. jaanuar – Uusaasta"),
        (date(year, 2, 24), "24. veebruar – Iseseisvuspäev, Eesti Vabariigi aastapäev"),
        (good_friday, f"{good_friday.day}. {['jaanuar','veebruar','märts','aprill','mai','juuni','juuli','august','september','oktoober','november','detsember'][good_friday.month-1]} – Suur reede"),
        (easter_date + timedelta(days=1), f"{(easter_date + timedelta(days=1)).day}. {['jaanuar','veebruar','märts','aprill','mai','juuni','juuli','august','september','oktoober','november','detsember'][(easter_date + timedelta(days=1)).month-1]} – Ülestõusmispühade 1. püha"),
        (date(year, 5, 1), "01. mai – Kevadpüha"),
        (pentecost, f"{pentecost.day}. {['jaanuar','veebruar','märts','aprill','mai','juuni','juuli','august','september','oktoober','november','detsember'][pentecost.month-1]} – Nelipühade 1. püha"),
        (date(year, 6, 23), "23. juuni – Võidupüha"),
        (date(year, 6, 24), "24. juuni – Jaanipäev"),
        (date(year, 8, 20), "20. august – Taasiseseisvumispäev"),
        (date(year, 12, 24), "24. detsember – Jõululaupäev"),
        (date(year, 12, 25), "25. detsember – Esimene jõulupüha"),
        (date(year, 12, 26), "26. detsember – Teine jõulupüha"),
    ]
    return holidays

def get_working_days_and_hours(year: int, month: int, holidays: list):
    """Считает рабочие дни (пн-пт без праздников) и часы (8 ч/день, с сокращениями)."""
    from datetime import date, timedelta
    import calendar

    holiday_dates = {h[0] for h in holidays}
    # Дни, предшествующие определённым праздникам, сокращаются на 3 часа
    shortened_before = {
        date(year, 1, 1),
        date(year, 2, 24),
        date(year, 6, 23),
        date(year, 12, 24),
    }

    first = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)

    work_days = 0
    work_hours = 0
    current = first
    while current <= last:
        if current.weekday() < 5 and current not in holiday_dates:  # пн-пт
            work_days += 1
            hours = 8
            # Проверяем, является ли следующий день праздником, перед которым сокращают
            next_day = current + timedelta(days=1)
            if next_day in shortened_before and next_day.weekday() < 5:
                hours -= 3
            work_hours += hours
        current += timedelta(days=1)
    return work_days, work_hours

def generate_excel_per_equipment() -> dict:
    """Возвращает словарь {название_прибора: bytes_excel}.
    Для каждого прибора: месячные листы (начало/конец) + свой aasta_koond.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO
    from collections import defaultdict
    from datetime import datetime as dt

    MONTHS_ET = {
        1: "Jaanuar", 2: "Veebruar", 3: "Märts", 4: "Aprill",
        5: "Mai", 6: "Juuni", 7: "Juuli", 8: "August",
        9: "September", 10: "Oktoober", 11: "November", 12: "Detsember"
    }

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    science_fill = PatternFill("solid", fgColor="C6EFCE")
    service_fill = PatternFill("solid", fgColor="FFEB9C")
    total_fill = PatternFill("solid", fgColor="D9E1F2")
    thin = Border(
        left=Side(style='thin', color='B0B0B0'),
        right=Side(style='thin', color='B0B0B0'),
        top=Side(style='thin', color='B0B0B0'),
        bottom=Side(style='thin', color='B0B0B0')
    )
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT user_name, equipment, work_type, note, start_time, end_time, duration_hours
        FROM sessions WHERE status = 'finished'
        ORDER BY start_time
    ''')
    all_rows = c.fetchall()
    conn.close()

    by_eq = defaultdict(lambda: defaultdict(list))
    for row in all_rows:
        try:
            start = dt.strptime(row[4], "%Y-%m-%d %H:%M:%S")
            end = dt.strptime(row[5], "%Y-%m-%d %H:%M:%S") if row[5] else None
            key = (start.year, start.month)
            by_eq[row[1]][key].append((row, start, end))
        except Exception:
            continue

    result = {}
    for equipment, by_month in by_eq.items():
        wb = Workbook()
        default_sheet = wb.active
        wb.remove(default_sheet)

        for (year, month) in sorted(by_month.keys()):
            sheet_name = f"{MONTHS_ET[month]} {year}"[:31]
            ws = wb.create_sheet(sheet_name)

            ws.merge_cells("A1:G1")
            ws["A1"] = f"LABORIPÄEVIK — {equipment} — {MONTHS_ET[month]} {year}"
            ws["A1"].font = Font(bold=True, size=13)
            ws["A1"].alignment = Alignment(horizontal="center")

            headers = ["Kuupäev", "Tüüp", "Töötaja", "Märkus", "Algus", "Lõpp", "Tunnid"]
            for col, h in enumerate(headers, 1):
                cell = ws.cell(3, col, h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = center
                cell.border = thin

            row_num = 4
            month_total = 0.0
            for (user_name, eq, work_type, note, start_str, end_str, hours), start_dt, end_dt in by_month[(year, month)]:
                type_et = "Teadus" if work_type == "science" else "Teenus"
                date_str = start_dt.strftime("%d.%m.%Y")
                start_time = start_dt.strftime("%H:%M")
                end_time = end_dt.strftime("%H:%M") if end_dt else "—"

                values = [date_str, type_et, user_name or "", note or "", start_time, end_time, hours or 0]
                for col, val in enumerate(values, 1):
                    cell = ws.cell(row_num, col, val)
                    cell.border = thin
                    cell.alignment = Alignment(vertical="center")
                    if col == 2:
                        cell.fill = science_fill if work_type == "science" else service_fill
                        cell.alignment = center
                    if col == 7:
                        cell.number_format = "0.00"
                        cell.alignment = center
                month_total += hours or 0
                row_num += 1

            row_num += 1
            ws.cell(row_num, 1, "KOKKU").font = Font(bold=True)
            ws.cell(row_num, 7, round(month_total, 2)).number_format = "0.00"
            ws.cell(row_num, 7).font = Font(bold=True)
            ws.cell(row_num, 7).fill = total_fill
            for col in range(1, 8):
                ws.cell(row_num, col).border = thin

            widths = [14, 12, 22, 25, 10, 10, 10]
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
            ws.freeze_panes = "A4"

        # Aasta kokkuvõte для этого прибора
        ws_sum = wb.create_sheet("Aasta kokkuvõte")
        ws_sum.merge_cells("A1:E1")
        ws_sum["A1"] = f"LABORIPÄEVIK — {equipment}"
        ws_sum["A1"].font = Font(bold=True, size=16)

        years = sorted(set(y for y, m in by_month.keys()))
        year_str = ", ".join(str(y) for y in years)
        ws_sum.merge_cells("A3:E3")
        ws_sum["A3"] = f"{year_str} aasta koond"
        ws_sum["A3"].font = Font(bold=True, size=12)

        ws_sum.merge_cells("A7:B8")
        ws_sum["A7"] = "Keemiliste\nprotsesside ja katalüsaatorite arendustaristu"
        ws_sum["A7"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws_sum["A7"].font = Font(bold=True, size=11)

        ws_sum["C7"] = "Projekti nimi:"
        ws_sum["C7"].alignment = Alignment(horizontal="right", vertical="center")
        ws_sum.merge_cells("D7:E7")
        ws_sum["D7"] = "Ida Viru ettevõtluse teadmusmahukuse suurendamise toetus: teadusvõimekuse pakkumise arendamine Ida Virumaal TA võrgustiku loomiseks"
        ws_sum["D7"].alignment = Alignment(wrap_text=True, vertical="center")
        ws_sum["D7"].font = Font(size=9)

        ws_sum["C8"] = "Projekti kood:"
        ws_sum["C8"].alignment = Alignment(horizontal="right", vertical="center")
        ws_sum.merge_cells("D8:E8")
        ws_sum["D8"] = "2021-2027.6.01.23-0034"
        ws_sum["D8"].font = Font(bold=True)

        for col, h in enumerate(["Kuu", "Majandustegevus (tundi)", "Muu töö (tundi)", "KOKKU"], 1):
            cell = ws_sum.cell(9, col, h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="548235")
            cell.alignment = center
            cell.border = thin

        row = 10
        total_service = 0.0
        total_science = 0.0
        for (year, month) in sorted(by_month.keys()):
            sci = 0.0
            srv = 0.0
            for (user_name, eq, work_type, note, start_str, end_str, hours), start_dt, end_dt in by_month[(year, month)]:
                if work_type == "science":
                    sci += hours or 0
                else:
                    srv += hours or 0
            total_science += sci
            total_service += srv
            month_name = f"{MONTHS_ET[month]} {year}"
            for col, val in enumerate([month_name, srv, sci, srv + sci], 1):
                cell = ws_sum.cell(row, col, val)
                cell.border = thin
                if col > 1:
                    cell.number_format = "0.00"
                    cell.alignment = center
            row += 1

        ws_sum.cell(row, 1, "Töötunnid kokku").font = Font(bold=True)
        ws_sum.cell(row, 2, round(total_service, 2)).number_format = "0.00"
        ws_sum.cell(row, 3, round(total_science, 2)).number_format = "0.00"
        ws_sum.cell(row, 4, round(total_service + total_science, 2)).number_format = "0.00"
        for col in range(1, 5):
            ws_sum.cell(row, col).font = Font(bold=True)
            ws_sum.cell(row, col).fill = total_fill
            ws_sum.cell(row, col).border = thin
            if col > 1:
                ws_sum.cell(row, col).alignment = center

        row += 2
        ws_sum.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        ws_sum.cell(row, 1, "Kinnitan andmed").font = Font(bold=True)
        ws_sum.cell(row, 1).fill = PatternFill("solid", fgColor="D9D9D9")
        for col in range(1, 5):
            ws_sum.cell(row, col).border = thin

        row += 1
        ws_sum.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        ws_sum.cell(row, 1, "Majandustegevuse osakaal (%):").alignment = Alignment(horizontal="right")
        total_all = total_service + total_science
        pct = (total_service / total_all * 100) if total_all > 0 else 0
        ws_sum.cell(row, 3, f"{pct:.2f}%")
        ws_sum.cell(row, 3).fill = PatternFill("solid", fgColor="FFFF00")
        ws_sum.cell(row, 3).font = Font(bold=True)
        ws_sum.cell(row, 3).alignment = center
        ws_sum.cell(row, 3).border = thin

        row += 2
        ws_sum.cell(row, 1, "Allkiri:")
        ws_sum.merge_cells(start_row=row+1, start_column=1, end_row=row+1, end_column=2)
        ws_sum.cell(row+1, 1, "Ees- ja\nperekonnanimi:")
        ws_sum.cell(row+1, 1).alignment = Alignment(wrap_text=True, vertical="center")
        ws_sum.cell(row, 3, "Kuupäev:")

        for col, w in zip("ABCD", [22, 24, 20, 14]):
            ws_sum.column_dimensions[col].width = w
        ws_sum.row_dimensions[7].height = 50

        for r in range(7, 9):
            for c in range(1, 6):
                ws_sum.cell(r, c).border = thin

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        result[equipment] = buf.getvalue()

    return result

def generate_overall_aasta_koond() -> bytes:
    """Общий aasta_koond по всем приборам."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from io import BytesIO
    from collections import defaultdict
    from datetime import datetime as dt

    MONTHS_ET = {
        1: "Jaanuar", 2: "Veebruar", 3: "Märts", 4: "Aprill",
        5: "Mai", 6: "Juuni", 7: "Juuli", 8: "August",
        9: "September", 10: "Oktoober", 11: "November", 12: "Detsember"
    }

    thin = Border(
        left=Side(style='thin', color='B0B0B0'),
        right=Side(style='thin', color='B0B0B0'),
        top=Side(style='thin', color='B0B0B0'),
        bottom=Side(style='thin', color='B0B0B0')
    )
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    total_fill = PatternFill("solid", fgColor="D9E1F2")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT equipment, work_type, start_time, duration_hours
        FROM sessions WHERE status = 'finished'
    ''')
    rows = c.fetchall()
    conn.close()

    # Группируем: месяц -> прибор -> science/service
    data = defaultdict(lambda: defaultdict(lambda: {"science": 0.0, "service": 0.0}))
    years = set()
    for equipment, work_type, start_str, hours in rows:
        try:
            start = dt.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            years.add(start.year)
            key = (start.year, start.month)
            if work_type == "science":
                data[key][equipment]["science"] += hours or 0
            else:
                data[key][equipment]["service"] += hours or 0
        except Exception:
            continue

    wb = Workbook()
    ws = wb.active
    ws.title = "Aasta kokkuvõte"

    ws.merge_cells("A1:F1")
    ws["A1"] = "LABORIPÄEVIK — KÕIK SEADMED"
    ws["A1"].font = Font(bold=True, size=16)

    year_str = ", ".join(str(y) for y in sorted(years)) if years else str(datetime.now().year)
    ws.merge_cells("A3:F3")
    ws["A3"] = f"{year_str} aasta koond"
    ws["A3"].font = Font(bold=True, size=12)

    ws.merge_cells("A7:B8")
    ws["A7"] = "Keemiliste\nprotsesside ja katalüsaatorite arendustaristu"
    ws["A7"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws["A7"].font = Font(bold=True, size=11)

    ws["C7"] = "Projekti nimi:"
    ws["C7"].alignment = Alignment(horizontal="right", vertical="center")
    ws.merge_cells("D7:F7")
    ws["D7"] = "Ida Viru ettevõtluse teadmusmahukuse suurendamise toetus: teadusvõimekuse pakkumise arendamine Ida Virumaal TA võrgustiku loomiseks"
    ws["D7"].alignment = Alignment(wrap_text=True, vertical="center")
    ws["D7"].font = Font(size=9)

    ws["C8"] = "Projekti kood:"
    ws["C8"].alignment = Alignment(horizontal="right", vertical="center")
    ws.merge_cells("D8:F8")
    ws["D8"] = "2021-2027.6.01.23-0034"
    ws["D8"].font = Font(bold=True)

    headers = ["Kuu", "Seade", "Majandustegevus (tundi)", "Muu töö (tundi)", "KOKKU"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(9, col, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="548235")
        cell.alignment = center
        cell.border = thin

    row = 10
    total_service = 0.0
    total_science = 0.0
    for key in sorted(data.keys()):
        year, month = key
        month_name = f"{MONTHS_ET[month]} {year}"
        for eq in sorted(data[key].keys()):
            sci = data[key][eq]["science"]
            srv = data[key][eq]["service"]
            total_science += sci
            total_service += srv
            for col, val in enumerate([month_name, eq, srv, sci, srv + sci], 1):
                cell = ws.cell(row, col, val)
                cell.border = thin
                if col > 2:
                    cell.number_format = "0.00"
                    cell.alignment = center
            row += 1

    ws.cell(row, 1, "KOKKU").font = Font(bold=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    ws.cell(row, 3, round(total_service, 2)).number_format = "0.00"
    ws.cell(row, 4, round(total_science, 2)).number_format = "0.00"
    ws.cell(row, 5, round(total_service + total_science, 2)).number_format = "0.00"
    for col in range(1, 6):
        ws.cell(row, col).font = Font(bold=True)
        ws.cell(row, col).fill = total_fill
        ws.cell(row, col).border = thin
        if col > 2:
            ws.cell(row, col).alignment = center

    row += 2
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    ws.cell(row, 1, "Kinnitan andmed").font = Font(bold=True)
    ws.cell(row, 1).fill = PatternFill("solid", fgColor="D9D9D9")
    for col in range(1, 6):
        ws.cell(row, col).border = thin

    row += 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    ws.cell(row, 1, "Majandustegevuse osakaal (%):").alignment = Alignment(horizontal="right")
    total_all = total_service + total_science
    pct = (total_service / total_all * 100) if total_all > 0 else 0
    ws.cell(row, 3, f"{pct:.2f}%")
    ws.cell(row, 3).fill = PatternFill("solid", fgColor="FFFF00")
    ws.cell(row, 3).font = Font(bold=True)
    ws.cell(row, 3).alignment = center
    ws.cell(row, 3).border = thin

    row += 2
    ws.cell(row, 1, "Allkiri:")
    ws.merge_cells(start_row=row+1, start_column=1, end_row=row+1, end_column=2)
    ws.cell(row+1, 1, "Ees- ja\nperekonnanimi:")
    ws.cell(row+1, 1).alignment = Alignment(wrap_text=True, vertical="center")
    ws.cell(row, 3, "Kuupäev:")

    for col, w in zip("ABCDE", [20, 32, 24, 20, 14]):
        ws.column_dimensions[col].width = w
    ws.row_dimensions[7].height = 50

    for r in range(7, 9):
        for c in range(1, 7):
            ws.cell(r, c).border = thin

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()
    """Возвращает словарь {название_прибора: bytes_excel}.
    Для каждого прибора: месячные листы (начало/конец) + свой aasta_koond.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO
    from collections import defaultdict
    from datetime import datetime as dt

    MONTHS_ET = {
        1: "Jaanuar", 2: "Veebruar", 3: "Märts", 4: "Aprill",
        5: "Mai", 6: "Juuni", 7: "Juuli", 8: "August",
        9: "September", 10: "Oktoober", 11: "November", 12: "Detsember"
    }

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    science_fill = PatternFill("solid", fgColor="C6EFCE")
    service_fill = PatternFill("solid", fgColor="FFEB9C")
    total_fill = PatternFill("solid", fgColor="D9E1F2")
    thin = Border(
        left=Side(style='thin', color='B0B0B0'),
        right=Side(style='thin', color='B0B0B0'),
        top=Side(style='thin', color='B0B0B0'),
        bottom=Side(style='thin', color='B0B0B0')
    )
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT user_name, equipment, work_type, note, start_time, end_time, duration_hours
        FROM sessions WHERE status = 'finished'
        ORDER BY start_time
    ''')
    all_rows = c.fetchall()
    conn.close()

    # Группируем по оборудованию, затем по году-месяцу
    by_eq = defaultdict(lambda: defaultdict(list))
    for row in all_rows:
        try:
            start = dt.strptime(row[4], "%Y-%m-%d %H:%M:%S")
            end = dt.strptime(row[5], "%Y-%m-%d %H:%M:%S") if row[5] else None
            key = (start.year, start.month)
            by_eq[row[1]][key].append((row, start, end))
        except Exception:
            continue

    result = {}
    for equipment, by_month in by_eq.items():
        wb = Workbook()
        default_sheet = wb.active
        wb.remove(default_sheet)

        for (year, month) in sorted(by_month.keys()):
            sheet_name = f"{MONTHS_ET[month]} {year}"[:31]
            ws = wb.create_sheet(sheet_name)

            ws.merge_cells("A1:H1")
            ws["A1"] = f"LABORIPÄEVIK — {equipment} — {MONTHS_ET[month]} {year}"
            ws["A1"].font = Font(bold=True, size=13)
            ws["A1"].alignment = Alignment(horizontal="center")

            headers = ["Kuupäev", "Tüüp", "Töötaja", "Märkus", "Algus", "Lõpp", "Tunnid"]
            for col, h in enumerate(headers, 1):
                cell = ws.cell(3, col, h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = center
                cell.border = thin

            row_num = 4
            month_total = 0.0
            for (user_name, eq, work_type, note, start_str, end_str, hours), start_dt, end_dt in by_month[(year, month)]:
                type_et = "Teadus" if work_type == "science" else "Teenus"
                date_str = start_dt.strftime("%d.%m.%Y")
                start_time = start_dt.strftime("%H:%M")
                end_time = end_dt.strftime("%H:%M") if end_dt else "—"

                values = [date_str, type_et, user_name or "", note or "", start_time, end_time, hours or 0]
                for col, val in enumerate(values, 1):
                    cell = ws.cell(row_num, col, val)
                    cell.border = thin
                    cell.alignment = Alignment(vertical="center")
                    if col == 2:
                        cell.fill = science_fill if work_type == "science" else service_fill
                        cell.alignment = center
                    if col == 7:
                        cell.number_format = "0.00"
                        cell.alignment = center
                month_total += hours or 0
                row_num += 1

            row_num += 1
            ws.cell(row_num, 1, "KOKKU").font = Font(bold=True)
            ws.cell(row_num, 7, round(month_total, 2)).number_format = "0.00"
            ws.cell(row_num, 7).font = Font(bold=True)
            ws.cell(row_num, 7).fill = total_fill
            for col in range(1, 8):
                ws.cell(row_num, col).border = thin

            widths = [14, 12, 22, 25, 10, 10, 10]
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
            ws.freeze_panes = "A4"

        # Aasta kokkuvõte для этого прибора
        ws_sum = wb.create_sheet("Aasta kokkuvõte")
        ws_sum.merge_cells("A1:E1")
        ws_sum["A1"] = f"LABORIPÄEVIK — {equipment}"
        ws_sum["A1"].font = Font(bold=True, size=16)

        years = sorted(set(y for y, m in by_month.keys()))
        year_str = ", ".join(str(y) for y in years)
        ws_sum.merge_cells("A3:E3")
        ws_sum["A3"] = f"{year_str} aasta koond"
        ws_sum["A3"].font = Font(bold=True, size=12)

        ws_sum.merge_cells("A7:B8")
        ws_sum["A7"] = "Keemiliste\nprotsesside ja katalüsaatorite arendustaristu"
        ws_sum["A7"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws_sum["A7"].font = Font(bold=True, size=11)

        ws_sum["C7"] = "Projekti nimi:"
        ws_sum["C7"].alignment = Alignment(horizontal="right", vertical="center")
        ws_sum.merge_cells("D7:E7")
        ws_sum["D7"] = "Ida Viru ettevõtluse teadmusmahukuse suurendamise toetus: teadusvõimekuse pakkumise arendamine Ida Virumaal TA võrgustiku loomiseks"
        ws_sum["D7"].alignment = Alignment(wrap_text=True, vertical="center")
        ws_sum["D7"].font = Font(size=9)

        ws_sum["C8"] = "Projekti kood:"
        ws_sum["C8"].alignment = Alignment(horizontal="right", vertical="center")
        ws_sum.merge_cells("D8:E8")
        ws_sum["D8"] = "2021-2027.6.01.23-0034"
        ws_sum["D8"].font = Font(bold=True)

        for col, h in enumerate(["Kuu", "Majandustegevus (tundi)", "Muu töö (tundi)", "KOKKU"], 1):
            cell = ws_sum.cell(9, col, h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="548235")
            cell.alignment = center
            cell.border = thin

        row = 10
        total_service = 0.0
        total_science = 0.0
        for (year, month) in sorted(by_month.keys()):
            sci = 0.0
            srv = 0.0
            for (user_name, eq, work_type, note, start_str, end_str, hours), start_dt, end_dt in by_month[(year, month)]:
                if work_type == "science":
                    sci += hours or 0
                else:
                    srv += hours or 0
            total_science += sci
            total_service += srv
            month_name = f"{MONTHS_ET[month]} {year}"
            for col, val in enumerate([month_name, srv, sci, srv + sci], 1):
                cell = ws_sum.cell(row, col, val)
                cell.border = thin
                if col > 1:
                    cell.number_format = "0.00"
                    cell.alignment = center
            row += 1

        ws_sum.cell(row, 1, "Töötunnid kokku").font = Font(bold=True)
        ws_sum.cell(row, 2, round(total_service, 2)).number_format = "0.00"
        ws_sum.cell(row, 3, round(total_science, 2)).number_format = "0.00"
        ws_sum.cell(row, 4, round(total_service + total_science, 2)).number_format = "0.00"
        for col in range(1, 5):
            ws_sum.cell(row, col).font = Font(bold=True)
            ws_sum.cell(row, col).fill = total_fill
            ws_sum.cell(row, col).border = thin
            if col > 1:
                ws_sum.cell(row, col).alignment = center

        row += 2
        ws_sum.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        ws_sum.cell(row, 1, "Kinnitan andmed").font = Font(bold=True)
        ws_sum.cell(row, 1).fill = PatternFill("solid", fgColor="D9D9D9")
        for col in range(1, 5):
            ws_sum.cell(row, col).border = thin

        row += 1
        ws_sum.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        ws_sum.cell(row, 1, "Majandustegevuse osakaal (%):").alignment = Alignment(horizontal="right")
        total_all = total_service + total_science
        pct = (total_service / total_all * 100) if total_all > 0 else 0
        ws_sum.cell(row, 3, f"{pct:.2f}%")
        ws_sum.cell(row, 3).fill = PatternFill("solid", fgColor="FFFF00")
        ws_sum.cell(row, 3).font = Font(bold=True)
        ws_sum.cell(row, 3).alignment = center
        ws_sum.cell(row, 3).border = thin

        row += 2
        ws_sum.cell(row, 1, "Allkiri:")
        ws_sum.merge_cells(start_row=row+1, start_column=1, end_row=row+1, end_column=2)
        ws_sum.cell(row+1, 1, "Ees- ja\nperekonnanimi:")
        ws_sum.cell(row+1, 1).alignment = Alignment(wrap_text=True, vertical="center")
        ws_sum.cell(row, 3, "Kuupäev:")

        for col, w in zip("ABCD", [22, 24, 20, 14]):
            ws_sum.column_dimensions[col].width = w
        ws_sum.row_dimensions[7].height = 50

        for r in range(7, 9):
            for c in range(1, 6):
                ws_sum.cell(r, c).border = thin

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        result[equipment] = buf.getvalue()

    return result
    """Полный отчёт: каждый месяц на отдельном листе, все приборы, по дням, на эстонском."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO
    from collections import defaultdict
    from datetime import datetime as dt

    MONTHS_ET = {
        1: "Jaanuar", 2: "Veebruar", 3: "Märts", 4: "Aprill",
        5: "Mai", 6: "Juuni", 7: "Juuli", 8: "August",
        9: "September", 10: "Oktoober", 11: "November", 12: "Detsember"
    }

    wb = Workbook()
    # Удаляем стандартный лист
    default_sheet = wb.active
    wb.remove(default_sheet)

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    science_fill = PatternFill("solid", fgColor="C6EFCE")
    service_fill = PatternFill("solid", fgColor="FFEB9C")
    total_fill = PatternFill("solid", fgColor="D9E1F2")
    thin = Border(
        left=Side(style='thin', color='B0B0B0'),
        right=Side(style='thin', color='B0B0B0'),
        top=Side(style='thin', color='B0B0B0'),
        bottom=Side(style='thin', color='B0B0B0')
    )
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT user_name, equipment, work_type, note, start_time, duration_hours
        FROM sessions WHERE status = 'finished'
        ORDER BY start_time
    ''')
    all_rows = c.fetchall()
    conn.close()

    # Группируем по году-месяцу
    by_month = defaultdict(list)
    for row in all_rows:
        try:
            start = dt.strptime(row[4], "%Y-%m-%d %H:%M:%S")
            key = (start.year, start.month)
            by_month[key].append((row, start))
        except Exception:
            continue

    if not by_month:
        ws = wb.create_sheet("Andmed puuduvad")
        ws["A1"] = "Lõpetatud sessioone pole."
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.getvalue()

    for (year, month) in sorted(by_month.keys()):
        sheet_name = f"{MONTHS_ET[month]} {year}"[:31]
        ws = wb.create_sheet(sheet_name)

        # Заголовок
        ws.merge_cells("A1:G1")
        ws["A1"] = f"LABORIPÄEVIK — {MONTHS_ET[month]} {year}"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A1"].alignment = Alignment(horizontal="center")

        # Заголовки таблицы
        headers = ["Kuupäev", "Seade", "Tüüp", "Töötaja", "Märkus", "Algus", "Tunnid"]
        for col, h in enumerate(headers, 1):
            cell = ws.cell(3, col, h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin

        row_num = 4
        month_total = 0.0
        eq_totals = defaultdict(lambda: {"science": 0.0, "service": 0.0})

        for (user_name, equipment, work_type, note, start_str, hours), start_dt in by_month[(year, month)]:
            type_et = "Teadus" if work_type == "science" else "Teenus"
            date_str = start_dt.strftime("%d.%m.%Y")
            time_str = start_dt.strftime("%H:%M")

            values = [date_str, equipment, type_et, user_name or "", note or "", time_str, hours or 0]
            for col, val in enumerate(values, 1):
                cell = ws.cell(row_num, col, val)
                cell.border = thin
                cell.alignment = Alignment(vertical="center")
                if col == 3:
                    cell.fill = science_fill if work_type == "science" else service_fill
                    cell.alignment = center
                if col == 7:
                    cell.number_format = "0.00"
                    cell.alignment = center

            month_total += hours or 0
            if work_type == "science":
                eq_totals[equipment]["science"] += hours or 0
            else:
                eq_totals[equipment]["service"] += hours or 0
            row_num += 1

        # Итог месяца
        row_num += 1
        ws.cell(row_num, 1, "KOKKU").font = Font(bold=True)
        ws.cell(row_num, 7, round(month_total, 2)).number_format = "0.00"
        ws.cell(row_num, 7).font = Font(bold=True)
        ws.cell(row_num, 7).fill = total_fill
        for col in range(1, 8):
            ws.cell(row_num, col).border = thin

        # Сводка по оборудованию
        row_num += 2
        ws.cell(row_num, 1, "KOKKUVÕTE SEADMETE KAUPA").font = Font(bold=True, size=12)
        ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=4)
        row_num += 1
        for i, h in enumerate(["Seade", "Teadus (h)", "Teenus (h)", "Kokku (h)"], 1):
            cell = ws.cell(row_num, i, h)
            cell.font = header_font
            cell.fill = PatternFill("solid", fgColor="548235")
            cell.alignment = center
            cell.border = thin
        row_num += 1
        for eq in sorted(eq_totals.keys()):
            sci = eq_totals[eq]["science"]
            srv = eq_totals[eq]["service"]
            tot = sci + srv
            for col, val in enumerate([eq, sci, srv, tot], 1):
                cell = ws.cell(row_num, col, val)
                cell.border = thin
                if col > 1:
                    cell.number_format = "0.00"
                    cell.alignment = center
            row_num += 1

        # Ширина колонок
        widths = [14, 32, 12, 22, 25, 10, 10]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A4"

    # Общий годовой лист в стиле aasta_koond (по приборам)
    if len(by_month) > 0:
        ws_sum = wb.create_sheet("Aasta kokkuvõte")
        # Заголовок
        ws_sum.merge_cells("A1:F1")
        ws_sum["A1"] = "LABORIPÄEVIK"
        ws_sum["A1"].font = Font(bold=True, size=16)

        years = sorted(set(y for y, m in by_month.keys()))
        year_str = ", ".join(str(y) for y in years)
        ws_sum.merge_cells("A3:F3")
        ws_sum["A3"] = f"{year_str} aasta koond"
        ws_sum["A3"].font = Font(bold=True, size=12)

        # Блок проекта
        ws_sum.merge_cells("A7:B8")
        ws_sum["A7"] = "Keemiliste\nprotsesside ja katalüsaatorite arendustaristu"
        ws_sum["A7"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws_sum["A7"].font = Font(bold=True, size=11)

        ws_sum["C7"] = "Projekti nimi:"
        ws_sum["C7"].alignment = Alignment(horizontal="right", vertical="center")
        ws_sum.merge_cells("D7:F7")
        ws_sum["D7"] = "Ida Viru ettevõtluse teadmusmahukuse suurendamise toetus: teadusvõimekuse pakkumise arendamine Ida Virumaal TA võrgustiku loomiseks"
        ws_sum["D7"].alignment = Alignment(wrap_text=True, vertical="center")
        ws_sum["D7"].font = Font(size=9)

        ws_sum["C8"] = "Projekti kood:"
        ws_sum["C8"].alignment = Alignment(horizontal="right", vertical="center")
        ws_sum.merge_cells("D8:F8")
        ws_sum["D8"] = "2021-2027.6.01.23-0034"
        ws_sum["D8"].font = Font(bold=True)

        # Заголовки таблицы по приборам
        headers_eq = ["Seade", "Majandustegevus (tundi)", "Muu töö (tundi)", "KOKKU"]
        for col, h in enumerate(headers_eq, 1):
            cell = ws_sum.cell(9, col, h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="548235")
            cell.alignment = center
            cell.border = thin

        # Считаем по каждому прибору
        eq_totals = defaultdict(lambda: {"service": 0.0, "science": 0.0})
        for rows in by_month.values():
            for (user_name, equipment, work_type, note, start_str, hours), start_dt in rows:
                if work_type == "service":
                    eq_totals[equipment]["service"] += hours or 0
                else:
                    eq_totals[equipment]["science"] += hours or 0

        row = 10
        total_service = 0.0
        total_science = 0.0
        for eq in sorted(eq_totals.keys()):
            srv = eq_totals[eq]["service"]
            sci = eq_totals[eq]["science"]
            tot = srv + sci
            total_service += srv
            total_science += sci
            values = [eq, srv, sci, tot]
            for col, val in enumerate(values, 1):
                cell = ws_sum.cell(row, col, val)
                cell.border = thin
                cell.alignment = Alignment(vertical="center")
                if col > 1:
                    cell.number_format = "0.00"
                    cell.alignment = center
            row += 1

        # Итог
        ws_sum.cell(row, 1, "Töötunnid kokku").font = Font(bold=True)
        ws_sum.cell(row, 2, round(total_service, 2)).number_format = "0.00"
        ws_sum.cell(row, 3, round(total_science, 2)).number_format = "0.00"
        ws_sum.cell(row, 4, round(total_service + total_science, 2)).number_format = "0.00"
        for col in range(1, 5):
            ws_sum.cell(row, col).font = Font(bold=True)
            ws_sum.cell(row, col).fill = PatternFill("solid", fgColor="D9E1F2")
            ws_sum.cell(row, col).border = thin
            if col > 1:
                ws_sum.cell(row, col).alignment = center
        total_row = row

        # Подтверждение
        row += 2
        ws_sum.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        ws_sum.cell(row, 1, "Kinnitan andmed").font = Font(bold=True)
        ws_sum.cell(row, 1).fill = PatternFill("solid", fgColor="D9D9D9")
        for col in range(1, 5):
            ws_sum.cell(row, col).border = thin

        row += 1
        ws_sum.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        ws_sum.cell(row, 1, "Majandustegevuse osakaal (%):").alignment = Alignment(horizontal="right")
        total_all = total_service + total_science
        pct = (total_service / total_all * 100) if total_all > 0 else 0
        ws_sum.cell(row, 3, f"{pct:.2f}%")
        ws_sum.cell(row, 3).fill = PatternFill("solid", fgColor="FFFF00")
        ws_sum.cell(row, 3).font = Font(bold=True)
        ws_sum.cell(row, 3).alignment = center
        ws_sum.cell(row, 3).border = thin

        # Подпись
        row += 2
        ws_sum.cell(row, 1, "Allkiri:")
        ws_sum.merge_cells(start_row=row+1, start_column=1, end_row=row+1, end_column=2)
        ws_sum.cell(row+1, 1, "Ees- ja\nperekonnanimi:")
        ws_sum.cell(row+1, 1).alignment = Alignment(wrap_text=True, vertical="center")
        ws_sum.cell(row, 3, "Kuupäev:")
        ws_sum.cell(row, 3).alignment = Alignment(horizontal="right")

        # Ширина и высота
        for col, w in zip("ABCD", [36, 22, 20, 14]):
            ws_sum.column_dimensions[col].width = w
        ws_sum.row_dimensions[7].height = 50
        ws_sum.row_dimensions[8].height = 20
        ws_sum.row_dimensions[row+1].height = 30

        # Рамки блока проекта
        for r in range(7, 9):
            for c in range(1, 7):
                ws_sum.cell(r, c).border = thin

    # Лист AASTA KALENDAARNE TÖÖAJAFOND (автоматически по эстонским праздникам)
    if len(by_month) > 0:
        years = sorted(set(y for y, m in by_month.keys()))
        for target_year in years:
            ws_cal = wb.create_sheet(f"{target_year} tööajafond")

            ws_cal.merge_cells("A1:E1")
            ws_cal["A1"] = f"{target_year}. AASTA KALENDAARNE TÖÖAJAFOND"
            ws_cal["A1"].font = Font(bold=True, size=14, color="1F4E79")

            cal_headers = ["Kuu", "Riigipüha (kalendripäev)", "Tööpäevi (tööaeg E-R)", "Töötunde (tööaeg E-R)", "Töötunde (erandid)"]
            for col, h in enumerate(cal_headers, 1):
                cell = ws_cal.cell(5, col, h)
                cell.font = Font(bold=True)
                cell.alignment = center
                cell.border = thin
                cell.fill = PatternFill("solid", fgColor="D6DCE4")

            holidays = get_estonian_holidays(target_year)
            month_names = ["Jaanuar","Veebruar","Märts","Aprill","Mai","Juuni","Juuli","August","September","Oktoober","November","Detsember"]

            total_days = 0
            total_hours = 0
            for m in range(1, 13):
                r = 5 + m
                days, hours = get_working_days_and_hours(target_year, m, holidays)
                total_days += days
                total_hours += hours

                # Праздники этого месяца
                month_holidays = [h[1] for h in holidays if h[0].month == m]
                holiday_text = "\n".join(month_holidays) if month_holidays else "–"

                ws_cal.cell(r, 1, month_names[m-1]).border = thin
                ws_cal.cell(r, 2, holiday_text).border = thin
                ws_cal.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="center")
                ws_cal.cell(r, 3, days).border = thin
                ws_cal.cell(r, 3).alignment = center
                ws_cal.cell(r, 4, hours).border = thin
                ws_cal.cell(r, 4).alignment = center
                ws_cal.cell(r, 5, "").border = thin
                ws_cal.row_dimensions[r].height = 40 if len(month_holidays) > 1 else 22

            # Итог
            total_row = 18
            ws_cal.cell(total_row, 1, "KOKKU").font = Font(bold=True)
            ws_cal.cell(total_row, 2, f"{len(holidays)} riigipüha").font = Font(bold=True)
            ws_cal.cell(total_row, 3, total_days).font = Font(bold=True)
            ws_cal.cell(total_row, 4, total_hours).font = Font(bold=True)
            for col in range(1, 6):
                ws_cal.cell(total_row, col).border = thin
                ws_cal.cell(total_row, col).fill = PatternFill("solid", fgColor="D9E1F2")

            # Сноска
            ws_cal.merge_cells("A20:E20")
            ws_cal["A20"] = "* Seadusest tulenevalt lühendatakse kolme tunni võrra tööpäeva, mis eelneb uusaastale, Eesti Vabariigi aastapäevale, võidupühale ja jõululaupäevale."
            ws_cal["A20"].font = Font(italic=True, size=9)
            ws_cal["A20"].alignment = Alignment(wrap_text=True)

            for col, w in zip("ABCDE", [16, 50, 24, 24, 30]):
                ws_cal.column_dimensions[col].width = w
            ws_cal.row_dimensions[20].height = 30

    # Финальное сохранение
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    data = buf.getvalue()
    if not data:
        raise ValueError("Empty file")
    return data

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

def main_menu_kb(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Начать работу", callback_data="start_work")
    builder.button(text="⏹ Закончить работу", callback_data="end_work")
    builder.button(text="📋 Мои активные", callback_data="my_active")
    if is_admin_user:
        builder.button(text="📊 Отчёт", callback_data="report_menu")
        builder.button(text="📊 Täielik aruanne (Excel)", callback_data="export_excel")
        builder.button(text="📊 Üldine aasta kokkuvõte", callback_data="export_overall")
        builder.button(text="🗑 Очистить сессии", callback_data="clean_confirm")
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

@dp.callback_query(F.data == "export_overall")
async def export_overall_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    try:
        excel_bytes = generate_overall_aasta_koond()
        file = BufferedInputFile(excel_bytes, filename="Aasta_kokkuvote_koik.xlsx")
        await callback.message.answer_document(
            file,
            caption="📊 Üldine aasta kokkuvõte\nKõik seadmed ühes failis"
        )
        await callback.answer("Готово")
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    # Проверяем, по каким приборам есть данные
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT equipment FROM sessions WHERE status='finished' ORDER BY equipment")
    eqs = [row[0] for row in c.fetchall()]
    conn.close()

    if not eqs:
        await callback.answer("Нет завершённых сессий", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for eq in eqs:
        builder.button(text=eq, callback_data=f"excel_eq:{eq}")
    builder.button(text="📥 Все приборы", callback_data="excel_eq:ALL")
    builder.button(text="« Назад", callback_data="cancel")
    builder.adjust(1)
    await safe_edit(callback, "Выберите оборудование для Excel-отчёта:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("excel_eq:"))
async def export_excel_selected(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    choice = callback.data.split(":", 1)[1]
    try:
        files = generate_excel_per_equipment()
        if not files:
            await callback.answer("Нет данных", show_alert=True)
            return
        if choice == "ALL":
            to_send = files.items()
        else:
            if choice not in files:
                await callback.answer("Нет данных по этому прибору", show_alert=True)
                return
            to_send = [(choice, files[choice])]

        for equipment, excel_bytes in to_send:
            safe_name = equipment.replace(" ", "_").replace("/", "-")[:40]
            file = BufferedInputFile(excel_bytes, filename=f"{safe_name}.xlsx")
            await callback.message.answer_document(
                file,
                caption=f"📊 {equipment}\n• Kuude kaupa\n• Algus ja lõpp\n• Aasta kokkuvõte"
            )
        await callback.answer("Готово")
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)

@dp.callback_query(F.data == "clean_confirm")
async def clean_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить все сессии", callback_data="clean_do")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)
    await safe_edit(
        callback,
        "⚠️ <b>Очистка базы</b>\n\n"
        "Будут удалены <b>все завершённые сессии</b>.\n"
        "Пользователи и активные сессии останутся.\n\n"
        "Продолжить?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "clean_do")
async def clean_do(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    deleted = clean_sessions()
    await safe_edit(
        callback,
        f"✅ Удалено завершённых сессий: <b>{deleted}</b>\n"
        "Пользователи сохранены.",
        reply_markup=main_menu_kb(True),
        parse_mode="HTML"
    )
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
