import asyncio
import logging
import sqlite3
import random
import re
import os
from datetime import datetime, timedelta

# Aiogram kutubxonalari
from aiogram import Bot, Dispatcher, types, F, html
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import WebAppInfo

# FastAPI va Web server kutubxonalari
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# --- KONFIGURATSIYA ---
# Railway Environment Variables
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("⚠️  BOT_TOKEN is not set!")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    print("⚠️  ADMIN_ID is invalid!")
    ADMIN_ID = 0

ADMIN_USERNAME = "@shaxzodbek_9224" # O'zgartirish shart emas, faqat display uchun

# Railway provides dynamic port
PORT = int(os.getenv("PORT", 8000))

# Web App URL from Railway (e.g. https://your-app.up.railway.app)
WEB_APP_URL = os.getenv("WEB_APP_URL", "")
if not WEB_APP_URL:
    print("⚠️  WEB_APP_URL is not set!") 
else:
    if not WEB_APP_URL.startswith("http"):
        WEB_APP_URL = "https://" + WEB_APP_URL
    WEB_APP_URL = WEB_APP_URL.rstrip("/") 

# --- DATA STORAGE (RAILWAY VOLUME) ---
DATA_DIR = os.getenv("DATA_DIR", ".")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "test_bot.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# FastAPI ilovasi
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- MA'LUMOTLAR BAZASI ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS test_bases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        owner_id INTEGER,
        created_at TIMESTAMP,
        is_admin_base INTEGER DEFAULT 0
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_id INTEGER,
        question_text TEXT,
        full_text TEXT,
        correct_answer TEXT,
        FOREIGN KEY (base_id) REFERENCES test_bases(id) ON DELETE CASCADE
    )''')
    # Yangi jadvallar
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        full_name TEXT,
        username TEXT,
        phone_number TEXT,
        is_approved INTEGER DEFAULT 0,
        joined_at TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    # Default sozlamalar
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_approve', '0')")
    conn.commit()
    conn.close()

# --- database helpers ---

def db_get_setting(key, default=None):
    conn = sqlite3.connect("test_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else default

def db_set_setting(key, value):
    conn = sqlite3.connect("test_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def db_get_user(telegram_id):
    conn = sqlite3.connect("test_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    res = cursor.fetchone()
    conn.close()
    return res

def db_upsert_user(user: types.User, is_approved=0):
    conn = sqlite3.connect("test_bot.db")
    cursor = conn.cursor()
    # Check if user exists to preserve is_approved if we are just updating info
    cursor.execute("SELECT is_approved FROM users WHERE telegram_id = ?", (user.id,))
    res = cursor.fetchone()
    
    current_approved = res[0] if res else is_approved
    
    joined_at = datetime.now().isoformat()
    cursor.execute('''INSERT OR REPLACE INTO users (telegram_id, full_name, username, is_approved, joined_at)
                      VALUES (?, ?, ?, ?, COALESCE((SELECT joined_at FROM users WHERE telegram_id=?), ?))''',
                   (user.id, user.full_name, user.username, current_approved, user.id, joined_at))
    conn.commit()
    conn.close()

def db_set_approval(telegram_id, status):
    conn = sqlite3.connect("test_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_approved = ? WHERE telegram_id = ?", (status, telegram_id))
    conn.commit()
    conn.close()

def db_get_all_users():
    conn = sqlite3.connect("test_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, full_name, is_approved FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows

class BotStates(StatesGroup):
    searching = State()
    uploading = State()
    subject_name = State()

def parse_test_file(content):
    tests = []
    lines = content.split('\n')
    current_q = ""
    full_text = ""
    for line in lines:
        if "ANSWER:" in line:
            correct = line.split("ANSWER:")[1].strip()
            first_line = full_text.strip().split('\n')[0] if full_text.strip() else "Savol"
            q_title = first_line[:50]
            tests.append({"q": q_title, "full": full_text.strip(), "ans": correct})
            current_q = ""
            full_text = ""
        else:
            if not current_q and line.strip():
                current_q = line
            full_text += line + "\n"
    return tests

# --- BOT HANDLERLARI ---

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Avto-ruxsat sozlamasini tekshirish
    auto_approve = db_get_setting("auto_approve", "0") == "1"
    
    # Foydalanuvchini bazaga yozish (agar yangi bo'lsa)
    user = db_get_user(message.from_user.id)
    is_new = user is None
    
    if is_new:
        # Yangi foydalanuvchi uchun status
        initial_status = 1 if (auto_approve or message.from_user.id == ADMIN_ID) else 0
        db_upsert_user(message.from_user, is_approved=initial_status)
        
        # Adminga xabar berish
        if message.from_user.id != ADMIN_ID:
            status_text = "✅ Tizimga kiritildi (Avto-ruxsat)" if initial_status else "⚠️ Ruxsat so'ramoqda"
            
            admin_msg = (
                f"🆕 <b>Yangi foydalanuvchi</b>\n"
                f"👤 {html.quote(message.from_user.full_name)}\n"
                f"🆔 <code>{message.from_user.id}</code>\n"
                f"🌐 @{message.from_user.username}\n\n"
                f"{status_text}"
            )
            
            kb = InlineKeyboardBuilder()
            if initial_status:
                kb.button(text="❌ Ruxsatni olish", callback_data=f"block_{message.from_user.id}")
            else:
                kb.button(text="✅ Ruxsat berish", callback_data=f"approve_{message.from_user.id}")
                kb.button(text="🚫 Bloklash", callback_data=f"block_{message.from_user.id}")
            kb.adjust(1)
            
            await bot.send_message(ADMIN_ID, admin_msg, reply_markup=kb.as_markup(), parse_mode="HTML")
            
    # Hozirgi statusni olish
    user_data = db_get_user(message.from_user.id)
    is_approved = user_data[4] # is_approved index
    
    if not is_approved and message.from_user.id != ADMIN_ID:
        await message.answer("🚫 <b>Sizga tizimdan foydalanish uchun ruxsat berilmagan.</b>\n\nIltimos, admin ruxsatini kuting.")
        return

    kb = [
        [types.KeyboardButton(text="📂 Test yuklash"), types.KeyboardButton(text="🔍 Test izlash")],
        [types.KeyboardButton(text="📚 Mavjud bazalar"), types.KeyboardButton(text="📝 Imtihon topshirish")],
        [types.KeyboardButton(text="🧠 Yodlash rejimi"), types.KeyboardButton(text="ℹ️ Ma'lumot")]
    ]
    
    if message.from_user.id == ADMIN_ID:
        kb.append([types.KeyboardButton(text="👨‍💼 Admin boshqaruv")])
        
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    text = (
        f"Xush kelibsiz! Bot rejimini tanlang:\n\n"
        f"1. <b>Test yuklash</b> - .txt fayl yuboring.\n"
        f"2. <b>Test izlash</b> - Bazadan qidirish.\n"
        f"3. <b>Imtihon topshirish</b> - 50 ta savol, vaqtga.\n"
        f"4. <b>Yodlash rejimi</b> - Barcha savollar, o'rgatuvchi rejim.\n\n"
        f"📢 Admin: {ADMIN_USERNAME}"
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@dp.message(F.text == "ℹ️ Ma'lumot")
async def info(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        "⚠️ <b>Qoidalar:</b>\n"
        "• Faqat .txt fayllar qabul qilinadi.\n"
        "• Format: Savol matni va tagida <code>ANSWER: A</code>.\n"
        "• Oddiy bazalar 3 kunda o'chib ketadi."
    )
    await message.answer(text, parse_mode="HTML")

def get_admin_content():
    auto_approve = db_get_setting("auto_approve", "0") == "1"
    status_emoji = "✅ YONIQ" if auto_approve else "🔴 O'CHIQ"
    
    # Statistikani olish
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_approved = 1")
    approved_users = cursor.fetchone()[0]
    conn.close()
    
    blocked_users = total_users - approved_users
    
    text = (
        f"👨‍💼 <b>Admin Boshqaruv Tizimi</b>\n\n"
        f"📊 <b>Statistika:</b>\n"
        f"👥 Jami: {total_users}\n"
        f"✅ Ruxsat: {approved_users}\n"
        f"🚫 Bloklangan: {blocked_users}\n\n"
        f"⚙️ <b>Avto-ruxsat:</b> {status_emoji}\n"
        f"<i>Yangi foydalanuvchilar avtomatik tasdiqlansinmi?</i>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Avto-ruxsatni o'zgartirish", callback_data="toggle_auto")
    kb.button(text="👥 Foydalanuvchilar", callback_data="user_list")
    kb.button(text="✅ Hammaga ruxsat", callback_data="users_manage_approve_all")
    kb.button(text="🚫 Hammani bloklash", callback_data="users_manage_revoke_all")
    kb.adjust(1)
    return text, kb.as_markup()

@dp.message(F.text == "👨‍💼 Admin boshqaruv")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text, reply_markup = get_admin_content()
    await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")

@dp.callback_query(F.data == "admin_main")
async def back_to_admin(call: types.CallbackQuery):
    text, reply_markup = get_admin_content()
    await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")

@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto_callback(call: types.CallbackQuery):
    current = db_get_setting("auto_approve", "0")
    new_val = "1" if current == "0" else "0"
    db_set_setting("auto_approve", new_val)
    
    status_msg = "✅ Yoqildi" if new_val == "1" else "🔴 O'chirildi"
    await call.answer(f"Avto-ruxsat {status_msg}")
    
    # Xabarni yangilash (edit)
    text, reply_markup = get_admin_content()
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "user_list")
async def show_users(call: types.CallbackQuery):
    users = db_get_all_users()
    kb = InlineKeyboardBuilder()
    
    # Bulk actions in list view
    kb.button(text="✅ Hammaga ruxsat", callback_data="users_manage_approve_all")
    kb.button(text="🚫 Hammani bloklash", callback_data="users_manage_revoke_all")

    if not users:
        # Agar userlar bo'lmasa ham bulk actionlar turgani ma'qul, yoki msg qaytarish mumkin
        pass

    text = "👥 <b>Foydalanuvchilar ro'yxati:</b>\nTanlash uchun bosing:"
    
    for u in users:
        # u: (telegram_id, full_name, is_approved)
        status = "✅" if u[2] else "🚫"
        kb.button(text=f"{status} {u[1]}", callback_data=f"manage_{u[0]}")
    
    kb.button(text="🔙 Orqaga", callback_data="admin_main")
    kb.adjust(2, 1)
    
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

def get_user_detail_content(user_id):
    user = db_get_user(user_id)
    if not user:
        return None, None
    
    status_icon = "✅ Ruxsat berilgan" if user[4] else "🚫 Bloklangan"
    
    text = (
        f"👤 <b>Foydalanuvchi:</b> {html.quote(user[1])}\n"
        f"🌐 @{user[2]}\n"
        f"🆔 <code>{user[0]}</code>\n"
        f"Holat: <b>{status_icon}</b>"
    )
    
    kb = InlineKeyboardBuilder()
    if user[4]:
        kb.button(text="🚫 Bloklash", callback_data=f"block_{user_id}")
    else:
        kb.button(text="✅ Ruxsat berish", callback_data=f"approve_{user_id}")
    
    kb.button(text="🔙 Ro'yxatga qaytish", callback_data="user_list")
    kb.adjust(1)
    
    return text, kb.as_markup()

@dp.callback_query(F.data.startswith("manage_"))
async def manage_user(call: types.CallbackQuery):
    user_id = int(call.data.split("_")[1])
    text, reply_markup = get_user_detail_content(user_id)
    
    if not text:
        await call.answer("Foydalanuvchi topilmadi")
        return
        
    await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("block_"))
async def change_user_status(call: types.CallbackQuery):
    action, user_id = call.data.split("_")
    user_id = int(user_id)
    
    is_approve = action == "approve"
    db_set_approval(user_id, 1 if is_approve else 0)
    
    user = db_get_user(user_id)
    
    # Adminga yangilash
    await call.answer("Status o'zgardi!")
    
    # Check if we are in the detailed user view
    # More robust check using "Holat:" which is distinctive or just "Foydalanuvchi"
    msg_text = call.message.text or ""
    if "Holat:" in msg_text or "Foydalanuvchi" in msg_text:
         # Refresh detail view
         text, reply_markup = get_user_detail_content(user_id)
         if text:
             await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        # Agar bildirishnomadagi tugma bo'lsa
        new_status_text = "✅ Ruxsat berildi" if is_approve else "🚫 Bloklandi"
        try:
            await call.message.edit_reply_markup(reply_markup=None) # Tugmalarni olib tashlash
            await call.message.reply(f"Foydalanuvchi <b>{user[1]}</b> statusi o'zgardi: {new_status_text}", parse_mode="HTML")
        except:
            pass

    # Foydalanuvchiga xabar yuborish
    try:
        if is_approve:
             await bot.send_message(user_id, "✅ <b>Tabriklaymiz!</b> Sizga admin tomonidan ruxsat berildi.\n\n/start ni bosing.", parse_mode="HTML")
        else:
             await bot.send_message(user_id, "🚫 <b>Sizning ruxsatingiz admin tomonidan bekor qilindi.</b>", parse_mode="HTML")
    except Exception as e:
        # print(f"Foydalanuvchiga xabar yetib bormadi: {str(e)}")
        pass

@dp.callback_query(F.data.startswith("users_manage_"))
async def bulk_users_manage(call: types.CallbackQuery):
    action = call.data.replace("users_manage_", "")
    
    if action == "approve_all":
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET is_approved = 1")
        conn.commit()
        conn.close()
        await call.answer("Barcha foydalanuvchilarga ruxsat berildi!", show_alert=True)
        # Barchaga xabar (ixtiyoriy, hozircha o'chirib turamiz spam bo'lmasligi uchun)
        
    elif action == "revoke_all":
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET is_approved = 0 WHERE telegram_id != ?", (ADMIN_ID,))
        conn.commit()
        conn.close()
        await call.answer("Barcha foydalanuvchilar bloklandi!", show_alert=True)
    
    # Check context: if we came from User List, refresh User List
    msg_text = call.message.text or ""
    if "Foydalanuvchilar ro'yxati" in msg_text:
        await show_users(call)
    else:
        text, reply_markup = get_admin_content()
        try:
            await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except:
            pass

@dp.message(F.text == "📂 Test yuklash")
async def upload_mode(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.subject_name)
    await message.answer("📝 <b>Test fanining nomini kiriting:</b>\n\nMasalan: <i>Ona Tili 5-sinf</i>", parse_mode="HTML")

@dp.message(BotStates.subject_name)
async def receive_subject_name(message: types.Message, state: FSMContext):
    await state.update_data(subject_name=message.text)
    await state.set_state(BotStates.uploading)
    await message.answer(f"✅ Fan nomi: <b>{message.text}</b> qabul qilindi.\n\nEndi test faylini (.txt) yuboring.")

@dp.message(BotStates.uploading, F.document)
async def process_file(message: types.Message, state: FSMContext):
    if not message.document.file_name.endswith(('.txt', '.text')):
        await message.answer("❌ Faqat .txt fayl yuklashingiz mumkin!")
        return

    data = await state.get_data()
    subject_name = data.get("subject_name", message.document.file_name)

    file = await bot.get_file(message.document.file_id)
    downloaded = await bot.download_file(file.file_path)
    content = downloaded.read().decode('utf-8')
    
    tests = parse_test_file(content)
    if not tests:
        await message.answer("❌ Xatolik: ANSWER: A formati topilmadi.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    is_admin = 1 if message.from_user.id == ADMIN_ID else 0
    
    created_at = datetime.now().isoformat()
    cursor.execute("INSERT INTO test_bases (name, owner_id, created_at, is_admin_base) VALUES (?, ?, ?, ?)",
                   (subject_name, message.from_user.id, created_at, is_admin))
    base_id = cursor.lastrowid
    
    for t in tests:
        cursor.execute("INSERT INTO questions (base_id, question_text, full_text, correct_answer) VALUES (?, ?, ?, ?)",
                       (base_id, t['q'], t['full'], t['ans']))
    conn.commit()
    conn.close()
    
    await message.answer(f"✅ <b>{subject_name}</b> bazasi saqlandi!\n{len(tests)} ta savol qo'shildi.", parse_mode="HTML")
    await state.clear()

@dp.message(F.text == "🔍 Test izlash")
async def search_mode(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.searching)
    await message.answer("🔎 Izlayotgan savolingizdan parcha yozing:")

@dp.message(BotStates.searching)
async def searching_process(message: types.Message):
    query = message.text
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, question_text, full_text, correct_answer FROM questions WHERE full_text LIKE ? LIMIT 15", (f'%{query}%',))
    results = cursor.fetchall()
    
    if not results:
        await message.answer("❌ Hech narsa topilmadi.")
    else:
        builder = InlineKeyboardBuilder()
        for r in results:
            builder.button(text=f"🔹 {r[1][:40]}...", callback_data=f"q_{r[0]}")
        builder.adjust(1)
        await message.answer(f"📚 {len(results)} ta natija:", reply_markup=builder.as_markup())
    conn.close()

@dp.callback_query(F.data.startswith("q_"))
async def show_q(call: types.CallbackQuery):
    q_id = call.data.split("_")[1]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT full_text, correct_answer FROM questions WHERE id = ?", (q_id,))
    res = cursor.fetchone()
    if res:
        await call.message.answer(f"✅ <b>Savol:</b>\n\n{html.quote(res[0])}\n\n🎯 <b>Javob: {res[1]}</b>", parse_mode="HTML")
    conn.close()

@dp.message(F.text == "📚 Mavjud bazalar")
async def list_bases(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 3 kundan oshgan oddiy bazalarni tozalash
    limit = (datetime.now() - timedelta(days=3)).isoformat()
    cursor.execute("DELETE FROM test_bases WHERE is_admin_base = 0 AND created_at < ?", (limit,))
    conn.commit()
    
    cursor.execute("SELECT id, name, is_admin_base FROM test_bases")
    bases = cursor.fetchall()
    conn.close()

    if not bases:
        await message.answer("Bazalar mavjud emas.")
        return

    builder = InlineKeyboardBuilder()
    for b in bases:
        icon = "⭐" if b[2] == 1 else "📁"
        builder.button(text=f"{icon} {b[1]}", callback_data=f"bset_{b[0]}")
    builder.adjust(1)
    await message.answer("Test bazasini tanlang:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("bset_"))
async def base_options(call: types.CallbackQuery, state: FSMContext):
    base_id = call.data.split("_")[1]
    
    # Bazani nomini olish (chiroyli ko'rinishi uchun)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM test_bases WHERE id = ?", (base_id,))
    res = cursor.fetchone()
    conn.close()
    
    base_name = res[0] if res else "Tanlangan baza"

    # Agar admin bo'lsa, tanlov beramiz
    if call.from_user.id == ADMIN_ID:
        text = f"📂 <b>{base_name}</b>\n\nNima qilmoqchisiz?"
        kb = InlineKeyboardBuilder()
        kb.button(text="🔍 Qidirish", callback_data=f"searchbase_{base_id}")
        kb.button(text="🗑 O'chirib tashlash", callback_data=f"delbase_{base_id}")
        kb.adjust(1)
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        # Oddiy foydalanuvchi uchun to'g'ridan-to'g'ri qidirish
        await start_search_flow(call.message, state, base_name)
        await call.answer()

@dp.callback_query(F.data.startswith("searchbase_"))
async def search_base_callback(call: types.CallbackQuery, state: FSMContext):
    base_id = call.data.split("_")[1]
    # Bazani nomini olish
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM test_bases WHERE id = ?", (base_id,))
    res = cursor.fetchone()
    conn.close()
    base_name = res[0] if res else "Baza"
    
    await start_search_flow(call.message, state, base_name)
    await call.message.delete()

async def start_search_flow(message: types.Message, state: FSMContext, base_name: str):
    await state.set_state(BotStates.searching)
    await message.answer(f"🔎 <b>{base_name}</b> bo'yicha qidirishingiz mumkin.\n\nSavoldan parcha yozing:", parse_mode="HTML")

@dp.callback_query(F.data.startswith("delbase_"))
async def delete_base_confirm(call: types.CallbackQuery):
    base_id = call.data.split("_")[1]
    
    # Tasdiqlash
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=f"realdelete_{base_id}")
    kb.button(text="❌ Yo'q, qaytish", callback_data="admin_base_cancel")
    kb.adjust(1)
    
    await call.message.edit_text("⚠️ <b>Rostdan ham ushbu bazani o'chirmoqchimisiz?</b>\nIchidagi barcha savollar o'chib ketadi!", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_base_cancel")
async def cancel_delete(call: types.CallbackQuery):
    await call.message.delete()
    await call.message.answer("O'chirish bekor qilindi.")

@dp.callback_query(F.data.startswith("realdelete_"))
async def delete_base_handler(call: types.CallbackQuery):
    base_id = call.data.split("_")[1]
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Savollarni o'chirish
    cursor.execute("DELETE FROM questions WHERE base_id = ?", (base_id,))
    # Bazani o'chirish
    cursor.execute("DELETE FROM test_bases WHERE id = ?", (base_id,))
    conn.commit()
    conn.close()
    
    await call.message.edit_text("✅ <b>Baza muvaffaqiyatli o'chirildi!</b>", parse_mode="HTML")

@dp.message(F.text == "📝 Imtihon topshirish")
async def start_exam_list(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM test_bases")
    bases = cursor.fetchall()
    conn.close()

    if not bases:
        await message.answer("Imtihon uchun bazalar yo'q.")
        return

    builder = InlineKeyboardBuilder()
    for b in bases:
        builder.button(text=f"✍️ {b[1]}", web_app=WebAppInfo(url=f"{WEB_APP_URL}/exam/{b[0]}"))
    
    builder.adjust(1)
    await message.answer("Imtihon topshirish uchun bazani tanlang:", reply_markup=builder.as_markup())

@dp.message(F.text == "🧠 Yodlash rejimi")
async def start_memorize_list(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM test_bases")
    bases = cursor.fetchall()
    conn.close()

    if not bases:
        await message.answer("Bazalar mavjud emas.")
        return

    builder = InlineKeyboardBuilder()
    for b in bases:
        builder.button(text=f"🧠 {b[1]}", web_app=WebAppInfo(url=f"{WEB_APP_URL}/memorize/{b[0]}"))
    
    builder.adjust(1)
    await message.answer("Yodlash uchun bazani tanlang:", reply_markup=builder.as_markup())

# --- IMAGE GENERATION VA ULASHISH ---

from PIL import Image, ImageDraw, ImageFont
import os
from aiogram.types import InlineQueryResultPhoto
from fastapi.staticfiles import StaticFiles
import uuid

# Statik fayllar uchun papka (Kerak bo'lsa)
if not os.path.exists("static"):
    os.makedirs("static")

app.mount("/static", StaticFiles(directory="static"), name="static")

from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

@dp.inline_query(F.query.startswith("res_"))
async def inline_result_share(inline_query: types.InlineQuery):
    # Format: res_{correct}_{total}_{time_str}_{base_id}
    try:
        parts = inline_query.query.split('_')
        correct = parts[1]
        total = parts[2]
        time_str = parts[3]
        base_id = parts[4]
        percentage = round((int(correct) / int(total)) * 100)
        
        user_name = inline_query.from_user.full_name
        
        # Bazani nomini olish
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM test_bases WHERE id = ?", (base_id,))
        base_res = cursor.fetchone()
        conn.close()
        
        subject_name = base_res[0] if base_res else "Noma'lum fan"
        current_date = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Chiroyli matn shakllantirish
        result_text = (
            f"🎓 <b>IMTIHON NATIJASI</b>\n\n"
            f"👤 <b>Talaba:</b> {html.quote(user_name)}\n"
            f"📚 <b>Fan:</b> {html.quote(subject_name)}\n"
            f"📅 <b>Sana:</b> {current_date}\n"
            f"----------------------------------------\n"
            f"✅ <b>To'g'ri javoblar:</b> {correct} ta\n"
            f"❌ <b>Xatolar:</b> {int(total) - int(correct)} ta\n"
            f"📊 <b>Samaradorlik:</b> {percentage}%\n"
            f"⏱ <b>Sarflangan vaqt:</b> {time_str}\n"
            f"----------------------------------------\n"
            f"🤖 <b>Bot orqali test ishlang:</b>\n"
            f"👉 @study_helperv3_bot\n"
            f"👉 @study_helperv3_bot"
        )
        
        # Natija obyekti (Maqola ko'rinishida)
        result = InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"✅ Natija: {percentage}%",
            description=f"{subject_name} | {correct}/{total} to'g'ri",
            input_message_content=InputTextMessageContent(
                message_text=result_text,
                parse_mode="HTML"
            ),
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/2995/2995620.png" # Test icon
        )
        
        await bot.answer_inline_query(inline_query.id, results=[result], cache_time=1)
    except Exception as e:
        print(f"ERROR in inline handler: {e}")
        # import traceback
        # traceback.print_exc()

@dp.message(F.text.startswith("res_") | F.text.contains("res_"))
async def text_fallback(message: types.Message):
    # Agar foydalanuvchi adashib matnni yuborib qo'ysa
    try:
        text = message.text
        if "@" in text:
            parts = text.split()
            for p in parts:
                if p.startswith("res_"):
                    text = p
                    break
        
        parts = text.split('_')
        correct = parts[1]
        total = parts[2]
        time_str = parts[3]
        base_id = parts[4]
        percentage = round((int(correct) / int(total)) * 100)
        
        user_name = message.from_user.full_name
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM test_bases WHERE id = ?", (base_id,))
        base_res = cursor.fetchone()
        conn.close()
        subject_name = base_res[0] if base_res else "Noma'lum fan"
        current_date = datetime.now().strftime("%d.%m.%Y %H:%M")

        result_text = (
            f"🎓 <b>IMTIHON NATIJASI</b>\n\n"
            f"👤 <b>Talaba:</b> {html.quote(user_name)}\n"
            f"📚 <b>Fan:</b> {html.quote(subject_name)}\n"
            f"� <b>Sana:</b> {current_date}\n"
            f"----------------------------------------\n"
            f"✅ <b>To'g'ri javoblar:</b> {correct} ta\n"
            f"❌ <b>Xatolar:</b> {int(total) - int(correct)} ta\n"
            f"📊 <b>Samaradorlik:</b> {percentage}%\n"
            f"⏱ <b>Sarflangan vaqt:</b> {time_str}\n"
            f"----------------------------------------\n"
            f"🤖 <b>Bot orqali test ishlang:</b>\n"
            f"👉 @study_helperv3_bot"
        )
        
        await message.answer(result_text, parse_mode="HTML")
        
    except Exception as e:
        await message.answer(f"Xatolik bo'ldi: {e}")

# --- WEB SERVER QISMI ---

@app.get("/get-tests")
async def get_tests(base_id: int, mode: str = "exam"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT full_text, correct_answer FROM questions WHERE base_id = ?", (base_id,))
    rows = cursor.fetchall()
    conn.close()
    
    if mode == "memorize":
        selected = rows
    else:
        count = min(len(rows), 50)
        selected = random.sample(rows, count) if rows else []
    
    results = []
    for r in selected:
        full_text = r[0]
        # Savol va variantlarni ajratish mantiqi
        lines = full_text.split('\n')
        q_text = ""
        options = {"A": "", "B": "", "C": "", "D": ""}
        
        for line in lines:
            line = line.strip()
            # Variantlarni aniqlash (A. yoki A) shaklida)
            if re.match(r'^[A][.)]', line): 
                options["A"] = re.sub(r'^[A][.)]', '', line).strip()
            elif re.match(r'^[B][.)]', line): 
                options["B"] = re.sub(r'^[B][.)]', '', line).strip()
            elif re.match(r'^[C][.)]', line): 
                options["C"] = re.sub(r'^[C][.)]', '', line).strip()
            elif re.match(r'^[D][.)]', line): 
                options["D"] = re.sub(r'^[D][.)]', '', line).strip()
            elif line: 
                # Agar ANSWER: qatori bo'lsa uni savol matniga qo'shmaymiz
                if "ANSWER:" not in line:
                    q_text += line + " "
            
        results.append({
            "question": q_text.strip(),
            "options": options,
            "ans": r[1]
        })
    return results

@app.get("/exam/{base_id}", response_class=HTMLResponse)
async def exam_page(request: Request, base_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM test_bases WHERE id = ?", (base_id,))
    res = cursor.fetchone()
    conn.close()
    subject_name = res[0] if res else "Imtihon"
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "base_id": base_id, 
        "mode": "exam",
        "subject_name": subject_name
    })

@app.get("/memorize/{base_id}", response_class=HTMLResponse)
async def memorize_page(request: Request, base_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM test_bases WHERE id = ?", (base_id,))
    res = cursor.fetchone()
    conn.close()
    subject_name = res[0] if res else "Yodlash"

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "base_id": base_id, 
        "mode": "memorize",
        "subject_name": subject_name
    })

async def run_all():
    init_db()
    
    print("\n" + "="*50)
    print(f"⚠️  DIQQAT! Hozirgi WEB_APP_URL: {WEB_APP_URL}")
    print("Agar bu URL hozirgi ngrok manzilingiz bilan bir xil bo'lmasa, rasm chiqmaydi!")
    print("Code ichida WEB_APP_URL ni yangilang!")
    print("="*50 + "\n")

    # Pollingni alohida task sifatida ishga tushiramiz
    asyncio.create_task(dp.start_polling(bot))
    # Uvicorn serverini ishga tushiramiz
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(run_all())
    except (KeyboardInterrupt, SystemExit):
        pass


