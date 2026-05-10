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
ADMIN_ID = int(os.getenv("ADMIN_ID", "7840931571"))
FREE_TRIAL_END = datetime(2026, 6, 1)
DB_PATH = "/app/data/users.db"
BOT_START_TIME = datetime.now()

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
CACHE_TTL = 600  # 10 دقائق

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
            joined_at TEXT,
            last_check_time TEXT,
            notification_frequency INTEGER DEFAULT 4,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            action TEXT,
            detail TEXT,
            logged_at TEXT
        );
        """)
        # ترقية الجداول القديمة بإضافة الأعمدة الجديدة إن لم تكن موجودة
        for col, definition in [
            ("last_check_time", "TEXT"),
            ("notification_frequency", "INTEGER DEFAULT 4"),
            ("is_active", "INTEGER DEFAULT 1"),
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except Exception:
                pass  # العمود موجود مسبقاً

def log_action(chat_id, action, detail=""):
    """تسجيل إجراءات المستخدمين في جدول السجلات."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO action_log (chat_id, action, detail, logged_at) VALUES (?,?,?,?)",
                (chat_id, action, detail, datetime.now().isoformat())
            )
    except Exception as e:
        log.warning(f"log_action error: {e}")

# =====================================================
# أدوات الحماية والتنسيق
# =====================================================
def enc(txt):
    return fernet.encrypt(txt.encode()).decode()

def dec(txt):
    try:
        return fernet.decrypt(txt.encode()).decode()
    except Exception:
        return None

def esc(text):
    if not text:
        return ""
    chars = r"_*[]()~`>#+-=|{}.!"
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

def is_admin(chat_id):
    return int(chat_id) == ADMIN_ID

def get_uptime():
    delta = datetime.now() - BOT_START_TIME
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes = remainder // 60
    return f"{hours}س {minutes}د"

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
            return {"status": "error", "message": "⚠️ عذراً، موقع المودل لا يستجيب حالياً.\n\nيرجى المحاولة مرة أخرى بعد قليل."}

        login_resp = session.post(
            "https://moodle.alaqsa.edu.ps/login/index.php",
            data={"username": username, "password": password, "logintoken": token["value"]},
            timeout=15
        )

        if "login" in login_resp.url:
            return {"status": "fail", "message": "❌ الرقم الجامعي أو كلمة المرور غير صحيحة.\n\nتحقق من بياناتك وأعد المحاولة، أو استخدم /logout لإعادة الضبط."}

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
                    dt += timedelta(hours=3)  # توقيت فلسطين
                    time_str = dt.strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    pass

            fmt_txt = f"▪️ *{esc(name)}*\n🕐 {esc(time_str)}"

            if "quiz" in url or "اختبار" in raw_txt:
                events["exams"].append(fmt_txt)
            elif "assign" in url or "واجب" in raw_txt:
                events["tasks"].append(fmt_txt)
            elif "zoom" in url or "meet" in url:
                events["meets"].append(fmt_txt)
            else:
                events["others"].append(fmt_txt)

        # بناء الرسالة
        res_msg = [f"📅 *تحديث المودل:* `{datetime.now().strftime('%H:%M')}`\n"]
        res_msg.append(f"📝 *الاختبارات:* {len(events['exams']) or 'لا يوجد'}")
        if events["exams"]:
            res_msg.append("\n".join(events["exams"]))

        res_msg.append(f"\n⚠️ *التكاليف:* {len(events['tasks']) or 'لا يوجد'}")
        if events["tasks"]:
            res_msg.append("\n".join(events["tasks"]))

        if skipped:
            res_msg.append(f"\n_✅ تم إخفاء {skipped} مهام مكتملة_")

        final_res = {"status": "success", "message": "\n".join(res_msg)}
        CACHE[username] = (time.time(), final_res)
        return final_res

    except requests.exceptions.Timeout:
        log.error(f"Moodle Timeout for {username}")
        return {"status": "error", "message": "⚠️ انتهت مهلة الاتصال بالمودل.\n\nيرجى المحاولة مرة أخرى."}
    except requests.exceptions.ConnectionError:
        log.error(f"Moodle ConnectionError for {username}")
        return {"status": "error", "message": "⚠️ تعذّر الاتصال بالمودل.\n\nتحقق من اتصالك بالإنترنت وأعد المحاولة."}
    except Exception as e:
        log.error(f"Moodle Error for {username}: {e}")
        return {"status": "error", "message": "⚠️ حدث خطأ غير متوقع أثناء الاتصال بالمودل.\n\nيرجى المحاولة لاحقاً."}

# =====================================================
# لوحة المفاتيح الرئيسية
# =====================================================
def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص سريع", "📊 إحصائياتي")
    kb.row("⚙️ الإعدادات", "❓ المساعدة")
    return kb

# =====================================================
# أوامر البوت — المستخدم
# =====================================================
@bot.message_handler(commands=["start"])
def start(m):
    try:
        name = m.from_user.first_name or "مستخدم"
        bot.send_message(
            m.chat.id,
            f"👋 *أهلاً {esc(name)}!*\n\n"
            "🎓 بوت متابعة مودل جامعة الأقصى\n\n"
            "سيقوم البوت بتنبيهك تلقائياً عند وجود واجبات أو اختبارات جديدة.\n\n"
            "📌 *للبدء:* اضغط على *🔍 فحص سريع* وأدخل بياناتك الجامعية.\n"
            "📖 للمساعدة اكتب /help",
            reply_markup=main_keyboard()
        )
        log_action(m.chat.id, "start")
    except Exception as e:
        log.error(f"start handler error: {e}")

@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text == "❓ المساعدة")
def help_cmd(m):
    try:
        text = (
            "📖 *دليل استخدام البوت*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔍 *فحص المودل*\n"
            "• /check أو 🔍 فحص سريع — فحص فوري للمودل\n\n"
            "👤 *حسابك*\n"
            "• /stats أو 📊 إحصائياتي — عرض إحصائياتك الشخصية\n"
            "• /settings أو ⚙️ الإعدادات — ضبط تفضيلاتك\n"
            "• /logout — حذف بياناتك وإلغاء الاشتراك\n\n"
            "ℹ️ *معلومات*\n"
            "• /help أو ❓ المساعدة — عرض هذه الرسالة\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 *التنبيهات التلقائية*\n"
            "يفحص البوت المودل كل 4 ساعات ويرسل إشعاراً فور اكتشاف أي تغيير.\n\n"
            "🔒 *الخصوصية*\n"
            "كلمة مرورك مشفرة ولا يمكن لأحد الاطلاع عليها.\n\n"
            "💬 للدعم تواصل مع المطور."
        )
        bot.send_message(m.chat.id, text)
        log_action(m.chat.id, "help")
    except Exception as e:
        log.error(f"help handler error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ، يرجى المحاولة مرة أخرى.")

@bot.message_handler(commands=["stats"])
@bot.message_handler(func=lambda m: m.text == "📊 إحصائياتي")
def stats_cmd(m):
    try:
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()

        if not user or not user["username"]:
            bot.send_message(
                m.chat.id,
                "ℹ️ لم تقم بتسجيل الدخول بعد.\n\nاضغط على *🔍 فحص سريع* لتسجيل بياناتك.",
                reply_markup=main_keyboard()
            )
            return

        joined = user["joined_at"] or "غير معروف"
        last_check = user["last_check_time"] or user["last_report"] or "لم يتم بعد"
        freq = user["notification_frequency"] or 4
        status = "✅ نشط" if user["is_active"] else "⛔ موقوف"
        vip = "⭐ VIP" if user["is_vip"] else "🆓 مجاني"

        # عدد الفحوصات من السجل
        with get_db() as conn:
            check_count = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE chat_id=? AND action='check'",
                (m.chat.id,)
            ).fetchone()[0]

        text = (
            f"📊 *إحصائياتك الشخصية*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 *الرقم الجامعي:* `{esc(user['username'])}`\n"
            f"📅 *تاريخ الانضمام:* {esc(joined[:10] if joined != 'غير معروف' else joined)}\n"
            f"🕐 *آخر فحص:* {esc(str(last_check)[:16])}\n"
            f"🔁 *عدد الفحوصات:* {check_count}\n"
            f"⏱️ *تكرار التنبيه:* كل {freq} ساعات\n"
            f"📌 *الحالة:* {status}\n"
            f"🏷️ *النوع:* {vip}\n"
        )
        bot.send_message(m.chat.id, text)
        log_action(m.chat.id, "stats")
    except Exception as e:
        log.error(f"stats handler error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ أثناء جلب إحصائياتك، يرجى المحاولة مرة أخرى.")

@bot.message_handler(commands=["settings"])
@bot.message_handler(func=lambda m: m.text == "⚙️ الإعدادات")
def settings_cmd(m):
    try:
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()

        if not user or not user["username"]:
            bot.send_message(m.chat.id, "ℹ️ لم تقم بتسجيل الدخول بعد.\n\nاضغط على *🔍 فحص سريع* أولاً.")
            return

        freq = user["notification_frequency"] or 4
        markup = types.InlineKeyboardMarkup(row_width=3)
        markup.add(
            types.InlineKeyboardButton("كل ساعتين", callback_data="freq_2"),
            types.InlineKeyboardButton("كل 4 ساعات ✅" if freq == 4 else "كل 4 ساعات", callback_data="freq_4"),
            types.InlineKeyboardButton("كل 8 ساعات", callback_data="freq_8"),
        )
        markup.add(types.InlineKeyboardButton("🗑️ حذف حسابي", callback_data="confirm_logout"))

        text = (
            f"⚙️ *إعداداتك*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏱️ *تكرار التنبيه الحالي:* كل {freq} ساعات\n\n"
            "اختر تكرار التنبيهات التلقائية:"
        )
        bot.send_message(m.chat.id, text, reply_markup=markup)
        log_action(m.chat.id, "settings")
    except Exception as e:
        log.error(f"settings handler error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ، يرجى المحاولة مرة أخرى.")

@bot.message_handler(commands=["logout"])
def logout_cmd(m):
    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ نعم، احذف بياناتي", callback_data="confirm_logout"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_action"),
        )
        bot.send_message(
            m.chat.id,
            "⚠️ *هل أنت متأكد؟*\n\nسيتم حذف بياناتك وإيقاف التنبيهات التلقائية.",
            reply_markup=markup
        )
    except Exception as e:
        log.error(f"logout handler error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ، يرجى المحاولة مرة أخرى.")

@bot.message_handler(func=lambda m: m.text in ["🔍 فحص سريع", "/check"])
def handle_check(m):
    try:
        with get_db() as conn:
            user = conn.execute(
                "SELECT username, password FROM users WHERE chat_id=? AND is_active=1",
                (m.chat.id,)
            ).fetchone()

        if not user or not user["username"]:
            msg = bot.send_message(m.chat.id, "📋 *أدخل رقمك الجامعي:*")
            bot.register_next_step_handler(msg, process_user)
            return

        wait_msg = bot.send_message(m.chat.id, "⏳ جاري جلب البيانات من المودل...")

        password = dec(user["password"])
        if not password:
            bot.edit_message_text(
                "❌ خطأ في نظام التشفير.\n\nيرجى استخدام /logout ثم إعادة تسجيل الدخول.",
                m.chat.id, wait_msg.message_id
            )
            return

        res = run_moodle(user["username"], password)
        bot.edit_message_text(res["message"], m.chat.id, wait_msg.message_id)

        # تحديث وقت آخر فحص وتسجيل الإجراء
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET last_check_time=? WHERE chat_id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M"), m.chat.id)
            )
        log_action(m.chat.id, "check", res["status"])
    except Exception as e:
        log.error(f"handle_check error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ أثناء الفحص، يرجى المحاولة مرة أخرى.")

def process_user(m):
    try:
        username = m.text.strip()
        if not username:
            msg = bot.send_message(m.chat.id, "⚠️ الرقم الجامعي لا يمكن أن يكون فارغاً.\n\nأدخل رقمك الجامعي:")
            bot.register_next_step_handler(msg, process_user)
            return
        msg = bot.send_message(m.chat.id, "🔐 أرسل كلمة المرور:")
        bot.register_next_step_handler(msg, lambda ms: process_pass(ms, username))
    except Exception as e:
        log.error(f"process_user error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ، يرجى المحاولة مرة أخرى.")

def process_pass(m, username):
    try:
        password = m.text.strip()
        if not password:
            bot.send_message(m.chat.id, "⚠️ كلمة المرور لا يمكن أن تكون فارغة.\n\nاضغط /check للمحاولة مجدداً.")
            return

        wait_msg = bot.send_message(m.chat.id, "⏳ يتم التحقق من بياناتك...")

        res = run_moodle(username, password)
        if res["status"] == "success":
            with get_db() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO users
                       (chat_id, username, password, joined_at, is_active, last_check_time)
                       VALUES (?,?,?,?,1,?)""",
                    (m.chat.id, username, enc(password),
                     datetime.now().isoformat(),
                     datetime.now().strftime("%Y-%m-%d %H:%M"))
                )
            bot.edit_message_text(
                "✅ *تم تسجيل الدخول بنجاح!*\n\nسيتم إشعارك تلقائياً عند وجود أي تحديثات في المودل.",
                m.chat.id, wait_msg.message_id
            )
            bot.send_message(m.chat.id, res["message"])
            bot.send_message(ADMIN_ID, f"🔔 *مستخدم جديد:* `{esc(username)}`\n🆔 Chat ID: `{m.chat.id}`")
            log_action(m.chat.id, "login", username)
        else:
            bot.edit_message_text(res["message"], m.chat.id, wait_msg.message_id)
            log_action(m.chat.id, "login_fail", username)
    except Exception as e:
        log.error(f"process_pass error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ أثناء التحقق من بياناتك، يرجى المحاولة مرة أخرى.")

# =====================================================
# Callback Queries — أزرار Inline
# =====================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("freq_"))
def cb_freq(c):
    try:
        freq = int(c.data.split("_")[1])
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET notification_frequency=? WHERE chat_id=?",
                (freq, c.message.chat.id)
            )
        bot.answer_callback_query(c.id, f"✅ تم ضبط التنبيه كل {freq} ساعات")
        bot.edit_message_text(
            f"⚙️ *تم تحديث الإعدادات*\n\n⏱️ ستصلك التنبيهات كل *{freq} ساعات*.",
            c.message.chat.id, c.message.message_id
        )
        log_action(c.message.chat.id, "settings_freq", str(freq))
    except Exception as e:
        log.error(f"cb_freq error: {e}")
        bot.answer_callback_query(c.id, "⚠️ حدث خطأ، يرجى المحاولة.")

@bot.callback_query_handler(func=lambda c: c.data == "confirm_logout")
def cb_confirm_logout(c):
    try:
        chat_id = c.message.chat.id
        with get_db() as conn:
            user = conn.execute("SELECT username FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            username = user["username"] if user else "unknown"
            conn.execute("UPDATE users SET is_active=0, username=NULL, password=NULL WHERE chat_id=?", (chat_id,))

        # مسح الكاش
        if username and username in CACHE:
            del CACHE[username]

        bot.answer_callback_query(c.id, "✅ تم حذف بياناتك")
        bot.edit_message_text(
            "✅ *تم حذف بياناتك بنجاح.*\n\nيمكنك إعادة التسجيل في أي وقت باستخدام /check.",
            chat_id, c.message.message_id
        )
        log_action(chat_id, "logout", username)
    except Exception as e:
        log.error(f"cb_confirm_logout error: {e}")
        bot.answer_callback_query(c.id, "⚠️ حدث خطأ.")

@bot.callback_query_handler(func=lambda c: c.data == "cancel_action")
def cb_cancel(c):
    try:
        bot.answer_callback_query(c.id, "تم الإلغاء")
        bot.edit_message_text("❌ *تم إلغاء العملية.*", c.message.chat.id, c.message.message_id)
    except Exception as e:
        log.error(f"cb_cancel error: {e}")

# =====================================================
# لوحة الأدمن — Admin Panel
# =====================================================
@bot.message_handler(commands=["admin"])
def admin_panel(m):
    if not is_admin(m.chat.id):
        bot.send_message(m.chat.id, "⛔ هذا الأمر للمشرف فقط.")
        return
    try:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📊 إحصائيات المستخدمين", callback_data="admin_users"),
            types.InlineKeyboardButton("⚙️ إحصائيات النظام", callback_data="admin_sys"),
            types.InlineKeyboardButton("🗑️ حذف مستخدم", callback_data="admin_remove"),
            types.InlineKeyboardButton("📢 إرسال رسالة جماعية", callback_data="admin_broadcast"),
        )
        bot.send_message(
            m.chat.id,
            "🛠️ *لوحة تحكم المشرف*\n━━━━━━━━━━━━━━━━━━━━\n\nاختر الإجراء المطلوب:",
            reply_markup=markup
        )
        log_action(m.chat.id, "admin_panel")
    except Exception as e:
        log.error(f"admin_panel error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ.")

@bot.callback_query_handler(func=lambda c: c.data == "admin_users")
def cb_admin_users(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    try:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM users WHERE is_active=1 AND username IS NOT NULL").fetchone()[0]
            vip = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip=1").fetchone()[0]
            last_join = conn.execute(
                "SELECT username, joined_at FROM users WHERE username IS NOT NULL ORDER BY joined_at DESC LIMIT 1"
            ).fetchone()
            last_check = conn.execute(
                "SELECT MAX(last_check_time) FROM users"
            ).fetchone()[0] or "لم يتم بعد"

        last_join_txt = f"`{esc(last_join['username'])}` — {esc(str(last_join['joined_at'])[:10])}" if last_join else "لا يوجد"

        text = (
            f"📊 *إحصائيات المستخدمين*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 *إجمالي المستخدمين:* {total}\n"
            f"✅ *المستخدمون النشطون:* {active}\n"
            f"⭐ *مستخدمو VIP:* {vip}\n"
            f"🕐 *آخر فحص:* {esc(str(last_check)[:16])}\n"
            f"🆕 *آخر انضمام:* {last_join_txt}\n"
        )
        bot.answer_callback_query(c.id)
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=_back_to_admin_markup())
    except Exception as e:
        log.error(f"cb_admin_users error: {e}")
        bot.answer_callback_query(c.id, "⚠️ حدث خطأ.")

@bot.callback_query_handler(func=lambda c: c.data == "admin_sys")
def cb_admin_sys(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    try:
        cache_size = len(CACHE)
        db_size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        db_size_kb = round(db_size_bytes / 1024, 2)
        uptime = get_uptime()

        with get_db() as conn:
            log_count = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]

        text = (
            f"⚙️ *إحصائيات النظام*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄️ *حجم قاعدة البيانات:* {db_size_kb} KB\n"
            f"💾 *حجم الكاش:* {cache_size} مدخلات\n"
            f"📋 *إجمالي السجلات:* {log_count}\n"
            f"⏱️ *وقت التشغيل:* {uptime}\n"
            f"🕐 *الوقت الحالي:* {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )
        bot.answer_callback_query(c.id)
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                              reply_markup=_back_to_admin_markup())
    except Exception as e:
        log.error(f"cb_admin_sys error: {e}")
        bot.answer_callback_query(c.id, "⚠️ حدث خطأ.")

@bot.callback_query_handler(func=lambda c: c.data == "admin_remove")
def cb_admin_remove(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    try:
        bot.answer_callback_query(c.id)
        msg = bot.send_message(c.message.chat.id, "🗑️ أرسل *الرقم الجامعي* للمستخدم الذي تريد حذفه:")
        bot.register_next_step_handler(msg, admin_do_remove)
    except Exception as e:
        log.error(f"cb_admin_remove error: {e}")

def admin_do_remove(m):
    if not is_admin(m.chat.id):
        return
    try:
        target_username = m.text.strip()
        with get_db() as conn:
            user = conn.execute("SELECT chat_id FROM users WHERE username=?", (target_username,)).fetchone()
            if not user:
                bot.send_message(m.chat.id, f"❌ لم يُعثر على مستخدم بالرقم الجامعي: `{esc(target_username)}`")
                return
            conn.execute("UPDATE users SET is_active=0, username=NULL, password=NULL WHERE username=?", (target_username,))

        if target_username in CACHE:
            del CACHE[target_username]

        bot.send_message(m.chat.id, f"✅ تم حذف المستخدم `{esc(target_username)}` بنجاح.")
        log_action(m.chat.id, "admin_remove", target_username)
    except Exception as e:
        log.error(f"admin_do_remove error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ أثناء حذف المستخدم.")

@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def cb_admin_broadcast(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    try:
        bot.answer_callback_query(c.id)
        msg = bot.send_message(c.message.chat.id, "📢 أرسل الرسالة التي تريد إذاعتها لجميع المستخدمين النشطين:")
        bot.register_next_step_handler(msg, admin_do_broadcast)
    except Exception as e:
        log.error(f"cb_admin_broadcast error: {e}")

def admin_do_broadcast(m):
    if not is_admin(m.chat.id):
        return
    try:
        text = m.text.strip()
        if not text:
            bot.send_message(m.chat.id, "⚠️ الرسالة فارغة، تم الإلغاء.")
            return

        with get_db() as conn:
            users = conn.execute(
                "SELECT chat_id FROM users WHERE is_active=1 AND username IS NOT NULL"
            ).fetchall()

        sent, failed = 0, 0
        for row in users:
            try:
                bot.send_message(row["chat_id"], f"📢 *إعلان من الإدارة:*\n\n{text}")
                sent += 1
                time.sleep(0.05)  # تجنب حد الإرسال
            except Exception:
                failed += 1

        bot.send_message(
            m.chat.id,
            f"📢 *اكتملت الإذاعة*\n\n✅ أُرسلت إلى: {sent} مستخدم\n❌ فشل: {failed} مستخدم"
        )
        log_action(m.chat.id, "broadcast", f"sent={sent} failed={failed}")
    except Exception as e:
        log.error(f"admin_do_broadcast error: {e}")
        bot.send_message(m.chat.id, "⚠️ حدث خطأ أثناء الإذاعة.")

@bot.callback_query_handler(func=lambda c: c.data == "admin_back")
def cb_admin_back(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    try:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📊 إحصائيات المستخدمين", callback_data="admin_users"),
            types.InlineKeyboardButton("⚙️ إحصائيات النظام", callback_data="admin_sys"),
            types.InlineKeyboardButton("🗑️ حذف مستخدم", callback_data="admin_remove"),
            types.InlineKeyboardButton("📢 إرسال رسالة جماعية", callback_data="admin_broadcast"),
        )
        bot.answer_callback_query(c.id)
        bot.edit_message_text(
            "🛠️ *لوحة تحكم المشرف*\n━━━━━━━━━━━━━━━━━━━━\n\nاختر الإجراء المطلوب:",
            c.message.chat.id, c.message.message_id, reply_markup=markup
        )
    except Exception as e:
        log.error(f"cb_admin_back error: {e}")

def _back_to_admin_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_back"))
    return markup

# =====================================================
# نظام التنبيهات الدوري (محسن للأداء)
# =====================================================
def check_single_user(row):
    try:
        password = dec(row["password"])
        if not password:
            return

        res = run_moodle(row["username"], password)
        if res["status"] != "success":
            return

        new_hash = hashlib.md5(res["message"].encode()).hexdigest()
        if row["last_hash"] != new_hash:
            bot.send_message(row["chat_id"], "🔔 *تحديث جديد في المودل:*\n\n" + res["message"])
            with get_db() as conn:
                conn.execute(
                    "UPDATE users SET last_hash=?, last_report=?, last_check_time=? WHERE chat_id=?",
                    (new_hash, datetime.now().strftime("%Y-%m-%d %H:%M"),
                     datetime.now().strftime("%Y-%m-%d %H:%M"), row["chat_id"])
                )
            log_action(row["chat_id"], "auto_notify", "new_content")
    except Exception as e:
        log.warning(f"Error checking {row['chat_id']}: {e}")

def broadcast_reports():
    log.info("بدء جولة الفحص الدوري...")
    try:
        with get_db() as conn:
            users = conn.execute(
                "SELECT * FROM users WHERE username IS NOT NULL AND is_active=1"
            ).fetchall()

        with ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(check_single_user, users)

        log.info(f"اكتملت جولة الفحص — {len(users)} مستخدم")
    except Exception as e:
        log.error(f"broadcast_reports error: {e}")

def scheduler_thread():
    # الفحص الأول فور تشغيل البوت
    time.sleep(10)
    broadcast_reports()

    # جدولة ديناميكية بناءً على تفضيلات المستخدمين (الافتراضي 4 ساعات)
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
