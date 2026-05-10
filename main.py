# -*- coding: utf-8 -*-
import os
import re
import time
import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import requests
import telebot
import schedule
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from requests.adapters import HTTPAdapter
from telebot import types

# =====================================================
# الإعدادات المتقدمة
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
ENC_KEY = os.getenv("ENC_KEY")
ADMIN_ID = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)
DB_PATH = "/app/data/users.db" # تم تعديل المسار ليعمل محلياً وسيرفر

if not TOKEN or not ENC_KEY:
    raise ValueError("برجاء ضبط TOKEN و ENC_KEY في متغيرات البيئة")

try:
    fernet = Fernet(ENC_KEY.encode())
except Exception as e:
    log.error(f"خطأ في مفتاح التشفير: {e}")
    raise

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
DB_LOCK = threading.Lock()
CACHE = {}
CACHE_TTL = 600 # 10 دقائق

# =====================================================
# قاعدة البيانات المطورة
# =====================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

DB_CONN = get_db_connection()

@contextmanager
def get_db():
    with DB_LOCK:
        try:
            yield DB_CONN
            DB_CONN.commit()
        except Exception as e:
            DB_CONN.rollback()
            log.error(f"Database Error: {e}")
            raise

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            password TEXT,
            expiry_date TEXT,
            is_vip INTEGER DEFAULT 0,
            last_hash TEXT,
            last_report TEXT,
            joined_at TEXT
        );
        """)

# =====================================================
# أدوات الحماية والتنسيق
# =====================================================
def enc(txt):
    return fernet.encrypt(txt.encode()).decode()

def dec(txt):
    try:
        return fernet.decrypt(txt.encode()).decode()
    except:
        return None

def esc(text):
    if not text: return ""
    chars = r"_*[]()~`>#+-=|{}.!"
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

# =====================================================
# منطق فحص المودل (محسن)
# =====================================================
def run_moodle(username, password):
    # فحص الكاش أولاً لتوفير الموارد
    if username in CACHE:
        ts, data = CACHE[username]
        if time.time() - ts < CACHE_TTL:
            return data

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=3))
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    try:
        login_page = session.get("https://moodle.alaqsa.edu.ps/login/index.php", timeout=15)
        soup = BeautifulSoup(login_page.text, "html.parser")
        token = soup.find("input", {"name": "logintoken"})
        
        if not token:
            return {"status": "error", "message": "⚠️ عذراً، موقع المودل لا يستجيب حالياً."}

        login_resp = session.post(
            "https://moodle.alaqsa.edu.ps/login/index.php",
            data={"username": username, "password": password, "logintoken": token["value"]},
            timeout=15
        )

        if "login" in login_resp.url:
            return {"status": "fail", "message": "❌ الرقم الجامعي أو كلمة المرور غير صحيحة."}

        # جلب التقويم
        cal_resp = session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming", timeout=15)
        soup = BeautifulSoup(cal_resp.text, "html.parser")
        
        events = {"exams": [], "tasks": [], "meets": [], "others": []}
        skipped = 0

        for ev_div in soup.find_all("div", {"class": "event"}):
            raw_txt = ev_div.get_text(" ", strip=True).lower()
            
            # تخطي المنجز
            if any(k in raw_txt for k in ["تم التسليم", "submitted", "graded", "finished", "تم الحل"]):
                skipped += 1
                continue

            h3 = ev_div.find("h3")
            name = h3.get_text(strip=True) if h3 else "نشاط"
            atag = ev_div.find("a", href=True)
            url = atag["href"] if atag else ""
            
            # استخراج الوقت وتعديل التوقيت
            time_tag = ev_div.find("time")
            time_str = "غير محدد"
            if time_tag:
                try:
                    dt = datetime.fromisoformat((time_tag.get("datetime") or "").replace("Z", "+00:00"))
                    dt += timedelta(hours=3) # توقيت فلسطين
                    time_str = dt.strftime("%Y-%m-%d %I:%M %p")
                except: pass

            fmt_txt = f"▪️ *{esc(name)}*\n🕐 {esc(time_str)}"
            
            if "quiz" in url or "اختبار" in raw_txt: events["exams"].append(fmt_txt)
            elif "assign" in url or "واجب" in raw_txt: events["tasks"].append(fmt_txt)
            elif "zoom" in url or "meet" in url: events["meets"].append(fmt_txt)
            else: events["others"].append(fmt_txt)

        # بناء الرسالة
        res_msg = [f"📅 *تحديث المودل:* `{datetime.now().strftime('%H:%M')}`\n"]
        res_msg.append(f"📝 *الاختبارات:* {len(events['exams']) or 'لا يوجد'}")
        if events['exams']: res_msg.append("\n".join(events['exams']))
        
        res_msg.append(f"\n⚠️ *التكاليف:* {len(events['tasks']) or 'لا يوجد'}")
        if events['tasks']: res_msg.append("\n".join(events['tasks']))

        if skipped: res_msg.append(f"\n_✅ تم إخفاء {skipped} مهام مكتملة_")

        final_res = {"status": "success", "message": "\n".join(res_msg)}
        CACHE[username] = (time.time(), final_res)
        return final_res

    except Exception as e:
        log.error(f"Moodle Error: {e}")
        return {"status": "error", "message": "⚠️ حدث خطأ أثناء الاتصال بالمودل."}

# =====================================================
# أوامر البوت
# =====================================================
@bot.message_handler(commands=["start"])
def start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص سريع", "📊 حالتي")
    bot.send_message(m.chat.id, "👋 أهلاً بك في بوت متابعة مودل الأقصى المطور.\n\nسيقوم البوت بتنبيهك تلقائياً عند وجود واجبات أو اختبارات جديدة.", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text in ["🔍 فحص سريع", "/check"])
def handle_check(m):
    with get_db() as conn:
        user = conn.execute("SELECT username, password FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()

    if not user or not user["username"]:
        msg = bot.send_message(m.chat.id, "📋 برجاء إرسال الرقم الجامعي:")
        bot.register_next_step_handler(msg, process_user)
        return

    wait_msg = bot.send_message(m.chat.id, "⏳ جاري جلب البيانات...")
    
    password = dec(user["password"])
    if not password:
        bot.edit_message_text("❌ خطأ في نظام التشفير، يرجى إعادة تسجيل الدخول.", m.chat.id, wait_msg.message_id)
        return

    res = run_moodle(user["username"], password)
    bot.edit_message_text(res["message"], m.chat.id, wait_msg.message_id)

def process_user(m):
    username = m.text.strip()
    msg = bot.send_message(m.chat.id, "🔐 الآن أرسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda ms: process_pass(ms, username))

def process_pass(m, username):
    password = m.text.strip()
    bot.send_message(m.chat.id, "⏳ يتم التحقق من بياناتك...")
    
    res = run_moodle(username, password)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO users (chat_id, username, password, joined_at) VALUES (?,?,?,?)",
                         (m.chat.id, username, enc(password), datetime.now().isoformat()))
        bot.send_message(m.chat.id, "✅ تم تفعيل الاشتراك التلقائي بنجاح!")
        bot.send_message(ADMIN_ID, f"🔔 مستخدم جديد: `{username}`")
    else:
        bot.send_message(m.chat.id, res["message"])

# =====================================================
# نظام التنبيهات الدوري (محسن للأداء)
# =====================================================
def check_single_user(row):
    try:
        password = dec(row["password"])
        if not password: return

        res = run_moodle(row["username"], password)
        if res["status"] != "success": return

        new_hash = hashlib.md5(res["message"].encode()).hexdigest()
        if row["last_hash"] != new_hash:
            bot.send_message(row["chat_id"], "🔔 *تحديث جديد في المودل:*\n\n" + res["message"])
            with get_db() as conn:
                conn.execute("UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?", 
                             (new_hash, datetime.now().strftime("%Y-%m-%d %H:%M"), row["chat_id"]))
    except Exception as e:
        log.warning(f"Error checking {row['chat_id']}: {e}")

def broadcast_reports():
    log.info("بدء جولة الفحص الدوري...")
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users WHERE username IS NOT NULL").fetchall()
    
    # استخدام ThreadPoolExecutor لعمل فحص متوازي لـ 5 مستخدمين في نفس الوقت
    with ThreadPoolExecutor(max_workers=5) as executor:
        executor.map(check_single_user, users)

def scheduler_thread():
    # الفحص الأول فور تشغيل البوت
    time.sleep(10)
    broadcast_reports()
    
    # ثم كل 4 ساعات
    schedule.every(4).hours.do(broadcast_reports)
    while True:
        schedule.run_pending()
        time.sleep(60)

# =====================================================
# التشغيل النهائي
# =====================================================
if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_thread, daemon=True).start()
    log.info("✅ Bot is online and scheduler started")
    bot.infinity_polling()
