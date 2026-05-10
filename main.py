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

TOKEN    = os.getenv("TOKEN")
ADMIN_ID = 7840931571
WALLET   = "0597599642"          # رقم المحفظة للدفع
FREE_TRIAL_DAYS = 30             # مدة التجربة المجانية بالأيام

# ── مسار قاعدة البيانات على الـ volume المُثبَّت ──────────────────────────
DATA_DIR = "/app/data"
DB_PATH  = os.path.join(DATA_DIR, "users.db")

# ── مفتاح التشفير: يُقرأ من البيئة أو يُولَّد ويُحفظ تلقائياً ───────────
_KEY_FILE = os.path.join(DATA_DIR, ".enc_key")

def _load_or_create_enc_key() -> str:
    """
    يحاول قراءة المفتاح من متغير البيئة ENC_KEY أولاً.
    إن لم يوجد، يبحث عن ملف .enc_key داخل /app/data/.
    إن لم يوجد الملف، يولّد مفتاحاً جديداً ويحفظه.
    """
    env_key = os.getenv("ENC_KEY", "").strip()
    if env_key:
        return env_key

    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "r") as f:
            stored = f.read().strip()
        if stored:
            log.info("🔑 تم تحميل مفتاح التشفير من الملف.")
            return stored

    new_key = Fernet.generate_key().decode()
    with open(_KEY_FILE, "w") as f:
        f.write(new_key)
    log.warning(
        "🔑 تم توليد مفتاح تشفير جديد وحفظه في %s — "
        "يُنصح بنسخه وضبطه كمتغير بيئة ENC_KEY لمزيد من الأمان.",
        _KEY_FILE
    )
    return new_key

if not TOKEN:
    raise ValueError("برجاء ضبط TOKEN في متغيرات البيئة")

ENC_KEY = _load_or_create_enc_key()

try:
    fernet = Fernet(ENC_KEY.encode())
except Exception as e:
    log.error(f"خطأ في مفتاح التشفير: {e}")
    raise

bot      = telebot.TeleBot(TOKEN, parse_mode="Markdown")
DB_LOCK  = threading.Lock()
CACHE    = {}
CACHE_TTL = 600  # 10 دقائق

# =====================================================
# قاعدة البيانات المطورة
# =====================================================
def _new_connection() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

DB_CONN = _new_connection()

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
            chat_id      INTEGER PRIMARY KEY,
            username     TEXT,
            password     TEXT,
            is_vip       INTEGER DEFAULT 0,
            expiry_date  TEXT,
            last_hash    TEXT,
            last_report  TEXT,
            joined_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            amount       REAL    NOT NULL,
            months       INTEGER NOT NULL DEFAULT 1,
            submitted_at TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending'
        );
        """)
    log.info("✅ قاعدة البيانات جاهزة على %s", DB_PATH)

# =====================================================
# أدوات الحماية والتنسيق
# =====================================================
def enc(txt: str) -> str:
    return fernet.encrypt(txt.encode()).decode()

def dec(txt: str):
    try:
        return fernet.decrypt(txt.encode()).decode()
    except Exception:
        return None

def esc(text: str) -> str:
    if not text:
        return ""
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text

# =====================================================
# منطق الاشتراك والصلاحية
# =====================================================
SUBSCRIPTION_PRICE = 5.0   # دولار / شهر

def is_active(user: sqlite3.Row) -> bool:
    """يعيد True إذا كان المستخدم نشطاً (تجربة مجانية أو اشتراك مدفوع ساري)."""
    if user is None:
        return False
    # VIP دائم
    if user["is_vip"]:
        return True
    # تجربة مجانية: 30 يوماً من تاريخ التسجيل
    if user["joined_at"]:
        try:
            joined = datetime.fromisoformat(user["joined_at"])
            if datetime.now() < joined + timedelta(days=FREE_TRIAL_DAYS):
                return True
        except Exception:
            pass
    # اشتراك مدفوع
    if user["expiry_date"]:
        try:
            if datetime.now() < datetime.fromisoformat(user["expiry_date"]):
                return True
        except Exception:
            pass
    return False

def days_left(user: sqlite3.Row) -> int:
    """يعيد عدد الأيام المتبقية في الاشتراك (تجربة أو مدفوع)."""
    if user is None:
        return 0
    if user["is_vip"]:
        return 9999
    best = datetime.min
    if user["joined_at"]:
        try:
            trial_end = datetime.fromisoformat(user["joined_at"]) + timedelta(days=FREE_TRIAL_DAYS)
            if trial_end > best:
                best = trial_end
        except Exception:
            pass
    if user["expiry_date"]:
        try:
            paid_end = datetime.fromisoformat(user["expiry_date"])
            if paid_end > best:
                best = paid_end
        except Exception:
            pass
    remaining = (best - datetime.now()).days
    return max(remaining, 0)

# =====================================================
# منطق فحص المودل (محسن)
# =====================================================
def run_moodle(username: str, password: str) -> dict:
    if username in CACHE:
        ts, data = CACHE[username]
        if time.time() - ts < CACHE_TTL:
            return data

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=3))
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    try:
        login_page = session.get("https://moodle.alaqsa.edu.ps/login/index.php", timeout=15)
        soup  = BeautifulSoup(login_page.text, "html.parser")
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

        cal_resp = session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming", timeout=15)
        soup = BeautifulSoup(cal_resp.text, "html.parser")

        events  = {"exams": [], "tasks": [], "meets": [], "others": []}
        skipped = 0

        for ev_div in soup.find_all("div", {"class": "event"}):
            raw_txt = ev_div.get_text(" ", strip=True).lower()
            if any(k in raw_txt for k in ["تم التسليم", "submitted", "graded", "finished", "تم الحل"]):
                skipped += 1
                continue

            h3   = ev_div.find("h3")
            name = h3.get_text(strip=True) if h3 else "نشاط"
            atag = ev_div.find("a", href=True)
            url  = atag["href"] if atag else ""

            time_tag = ev_div.find("time")
            time_str = "غير محدد"
            if time_tag:
                try:
                    dt = datetime.fromisoformat((time_tag.get("datetime") or "").replace("Z", "+00:00"))
                    dt += timedelta(hours=3)
                    time_str = dt.strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    pass

            fmt_txt = f"▪️ *{esc(name)}*\n🕐 {esc(time_str)}"

            if   "quiz"   in url or "اختبار" in raw_txt: events["exams"].append(fmt_txt)
            elif "assign" in url or "واجب"   in raw_txt: events["tasks"].append(fmt_txt)
            elif "zoom"   in url or "meet"   in url:     events["meets"].append(fmt_txt)
            else:                                         events["others"].append(fmt_txt)

        res_msg = [f"📅 *تحديث المودل:* `{datetime.now().strftime('%H:%M')}`\n"]
        res_msg.append(f"📝 *الاختبارات:* {len(events['exams']) or 'لا يوجد'}")
        if events["exams"]: res_msg.append("\n".join(events["exams"]))
        res_msg.append(f"\n⚠️ *التكاليف:* {len(events['tasks']) or 'لا يوجد'}")
        if events["tasks"]: res_msg.append("\n".join(events["tasks"]))
        if skipped: res_msg.append(f"\n_✅ تم إخفاء {skipped} مهام مكتملة_")

        final_res = {"status": "success", "message": "\n".join(res_msg)}
        CACHE[username] = (time.time(), final_res)
        return final_res

    except Exception as e:
        log.error(f"Moodle Error: {e}")
        return {"status": "error", "message": "⚠️ حدث خطأ أثناء الاتصال بالمودل."}

# =====================================================
# أوامر البوت — /start
# =====================================================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص سريع", "📊 حالتي")
    kb.row("💳 اشتراك", "ℹ️ مساعدة")
    bot.send_message(
        m.chat.id,
        "👋 *أهلاً بك في بوت متابعة مودل الأقصى المطور!*\n\n"
        "سيقوم البوت بتنبيهك تلقائياً عند وجود واجبات أو اختبارات جديدة.\n"
        f"🎁 تمتع بتجربة مجانية لمدة *{FREE_TRIAL_DAYS} يوماً* من تاريخ التسجيل.",
        reply_markup=kb
    )

# =====================================================
# /check — فحص سريع
# =====================================================
@bot.message_handler(func=lambda m: m.text in ["🔍 فحص سريع", "/check"])
def handle_check(m):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()

    if not user or not user["username"]:
        msg = bot.send_message(m.chat.id, "📋 برجاء إرسال الرقم الجامعي:")
        bot.register_next_step_handler(msg, process_user)
        return

    if not is_active(user):
        bot.send_message(
            m.chat.id,
            "⛔ *انتهت صلاحية اشتراكك.*\n\n"
            "اضغط على /subscribe لتجديد الاشتراك والاستمرار في استخدام البوت."
        )
        return

    wait_msg = bot.send_message(m.chat.id, "⏳ جاري جلب البيانات...")
    password = dec(user["password"])
    if not password:
        bot.edit_message_text(
            "❌ خطأ في نظام التشفير، يرجى إعادة تسجيل الدخول.",
            m.chat.id, wait_msg.message_id
        )
        return

    res = run_moodle(user["username"], password)
    try:
        bot.edit_message_text(res["message"], m.chat.id, wait_msg.message_id)
    except Exception:
        bot.send_message(m.chat.id, res["message"])

def process_user(m):
    username = m.text.strip()
    msg = bot.send_message(m.chat.id, "🔐 الآن أرسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda ms: process_pass(ms, username))

def process_pass(m, username):
    password = m.text.strip()
    bot.send_message(m.chat.id, "⏳ يتم التحقق من بياناتك...")

    res = run_moodle(username, password)
    if res["status"] == "success":
        joined = datetime.now().isoformat()
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO users "
                    "(chat_id, username, password, joined_at) VALUES (?,?,?,?)",
                    (m.chat.id, username, enc(password), joined)
                )
        except Exception as e:
            log.error(f"DB insert error for {m.chat.id}: {e}")
            bot.send_message(m.chat.id, "⚠️ حدث خطأ أثناء حفظ بياناتك، حاول مجدداً.")
            return

        trial_end = (datetime.now() + timedelta(days=FREE_TRIAL_DAYS)).strftime("%Y-%m-%d")
        bot.send_message(
            m.chat.id,
            f"✅ *تم تفعيل الاشتراك التلقائي بنجاح!*\n\n"
            f"🎁 تجربتك المجانية سارية حتى: `{trial_end}`\n"
            f"بعدها استخدم /subscribe لتجديد الاشتراك."
        )
        try:
            bot.send_message(ADMIN_ID, f"🔔 مستخدم جديد: `{username}` — ID: `{m.chat.id}`")
        except Exception:
            pass
    else:
        bot.send_message(m.chat.id, res["message"])

# =====================================================
# /status — حالة المستخدم
# =====================================================
@bot.message_handler(func=lambda m: m.text in ["📊 حالتي", "/status"])
def handle_status(m):
    try:
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()
    except Exception as e:
        log.error(f"Status DB error: {e}")
        bot.send_message(m.chat.id, "⚠️ خطأ في قراءة البيانات، حاول لاحقاً.")
        return

    if not user or not user["username"]:
        bot.send_message(m.chat.id, "❌ لم تقم بتسجيل الدخول بعد.\nاستخدم /check للبدء.")
        return

    active  = is_active(user)
    d_left  = days_left(user)
    status_icon = "✅ نشط" if active else "⛔ منتهي"
    vip_tag     = " 👑 VIP" if user["is_vip"] else ""

    msg = (
        f"📊 *حالة حسابك*{vip_tag}\n\n"
        f"👤 الرقم الجامعي: `{esc(user['username'])}`\n"
        f"📌 الحالة: {status_icon}\n"
        f"⏳ الأيام المتبقية: `{d_left}`\n"
        f"📅 تاريخ الانضمام: `{(user['joined_at'] or '')[:10]}`\n"
    )
    if user["expiry_date"]:
        msg += f"🗓 انتهاء الاشتراك: `{user['expiry_date'][:10]}`\n"
    if not active:
        msg += "\n💳 اشترك الآن عبر /subscribe"

    bot.send_message(m.chat.id, msg)

# =====================================================
# /subscribe — نظام الاشتراك المدفوع
# =====================================================
@bot.message_handler(func=lambda m: m.text in ["💳 اشتراك", "/subscribe"])
def cmd_subscribe(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📲 دفعت — أرسل إشعار الدفع", callback_data="paid_notify"))
    bot.send_message(
        m.chat.id,
        f"💳 *الاشتراك في بوت مودل الأقصى*\n\n"
        f"💰 السعر: *{SUBSCRIPTION_PRICE}$ / شهر*\n\n"
        f"📲 *طريقة الدفع — محفظة جوال:*\n"
        f"رقم المحفظة: `{WALLET}`\n\n"
        f"📋 *خطوات الدفع:*\n"
        f"1️⃣ افتح تطبيق المحفظة الإلكترونية\n"
        f"2️⃣ أرسل المبلغ إلى الرقم: `{WALLET}`\n"
        f"3️⃣ اضغط الزر أدناه وأرسل صورة إيصال الدفع\n\n"
        f"⚡ سيتم تفعيل اشتراكك خلال دقائق بعد التحقق.",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "paid_notify")
def cb_paid_notify(c):
    bot.answer_callback_query(c.id)
    msg = bot.send_message(
        c.message.chat.id,
        "📸 أرسل صورة إيصال الدفع أو رقم العملية، وسيتم مراجعتها وتفعيل اشتراكك فوراً:"
    )
    bot.register_next_step_handler(msg, receive_payment_proof)

def receive_payment_proof(m):
    chat_id = m.chat.id
    submitted_at = datetime.now().isoformat()

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO pending_payments (chat_id, amount, months, submitted_at, status) "
                "VALUES (?,?,?,?,?)",
                (chat_id, SUBSCRIPTION_PRICE, 1, submitted_at, "pending")
            )
            payment_id = conn.execute(
                "SELECT id FROM pending_payments WHERE chat_id=? ORDER BY id DESC LIMIT 1",
                (chat_id,)
            ).fetchone()["id"]
    except Exception as e:
        log.error(f"Payment insert error: {e}")
        bot.send_message(chat_id, "⚠️ خطأ في تسجيل طلبك، حاول مجدداً.")
        return

    bot.send_message(
        chat_id,
        f"✅ *تم استلام طلب الدفع رقم #{payment_id}*\n\n"
        f"⏳ سيتم مراجعته وتفعيل اشتراكك خلال دقائق.\n"
        f"شكراً لثقتك! 🙏"
    )

    # إشعار الأدمن
    try:
        with get_db() as conn:
            user = conn.execute("SELECT username FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        uname = user["username"] if user else "غير مسجل"

        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✅ تفعيل شهر",   callback_data=f"approve_1_{chat_id}"),
            types.InlineKeyboardButton("✅ تفعيل 3 أشهر", callback_data=f"approve_3_{chat_id}")
        )
        kb.add(types.InlineKeyboardButton("❌ رفض", callback_data=f"reject_{chat_id}"))

        caption = (
            f"💳 *طلب دفع جديد #{payment_id}*\n\n"
            f"👤 المستخدم: `{uname}`\n"
            f"🆔 Chat ID: `{chat_id}`\n"
            f"💰 المبلغ: {SUBSCRIPTION_PRICE}$\n"
            f"🕐 الوقت: {submitted_at[:16]}"
        )

        if m.content_type == "photo":
            bot.send_photo(ADMIN_ID, m.photo[-1].file_id, caption=caption, reply_markup=kb, parse_mode="Markdown")
        elif m.content_type == "document":
            bot.send_document(ADMIN_ID, m.document.file_id, caption=caption, reply_markup=kb, parse_mode="Markdown")
        else:
            proof_text = m.text or "(لا يوجد نص)"
            bot.send_message(ADMIN_ID, caption + f"\n\n📝 الإيصال: {proof_text}", reply_markup=kb)
    except Exception as e:
        log.error(f"Admin notify error: {e}")

# =====================================================
# معالجة قرارات الأدمن (تفعيل / رفض)
# =====================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith(("approve_", "reject_")))
def cb_admin_decision(c):
    if c.from_user.id != ADMIN_ID:
        bot.answer_callback_query(c.id, "⛔ غير مصرح لك.")
        return

    parts = c.data.split("_")
    action = parts[0]

    try:
        if action == "approve":
            months  = int(parts[1])
            chat_id = int(parts[2])
            _activate_subscription(chat_id, months)
            bot.answer_callback_query(c.id, f"✅ تم تفعيل {months} شهر للمستخدم {chat_id}")
            try:
                bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception:
                pass
        elif action == "reject":
            chat_id = int(parts[1])
            bot.answer_callback_query(c.id, "❌ تم رفض الطلب.")
            bot.send_message(
                chat_id,
                "❌ *تم رفض طلب الدفع.*\n\n"
                "يرجى التواصل مع الدعم أو إعادة إرسال إيصال صحيح."
            )
            try:
                bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception:
                pass
    except Exception as e:
        log.error(f"Admin decision error: {e}")
        bot.answer_callback_query(c.id, "⚠️ حدث خطأ.")

def _activate_subscription(chat_id: int, months: int):
    """يمدد تاريخ انتهاء الاشتراك للمستخدم بعدد الأشهر المحددة."""
    try:
        with get_db() as conn:
            user = conn.execute("SELECT expiry_date FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            if user and user["expiry_date"]:
                try:
                    base = datetime.fromisoformat(user["expiry_date"])
                    if base < datetime.now():
                        base = datetime.now()
                except Exception:
                    base = datetime.now()
            else:
                base = datetime.now()

            new_expiry = (base + timedelta(days=30 * months)).isoformat()
            conn.execute(
                "UPDATE users SET expiry_date=?, is_vip=1 WHERE chat_id=?",
                (new_expiry, chat_id)
            )
            # تحديث حالة الطلب
            conn.execute(
                "UPDATE pending_payments SET status='approved' "
                "WHERE chat_id=? AND status='pending'",
                (chat_id,)
            )

        expiry_str = new_expiry[:10]
        bot.send_message(
            chat_id,
            f"🎉 *تم تفعيل اشتراكك بنجاح!*\n\n"
            f"✅ أنت الآن عضو VIP 👑\n"
            f"📅 اشتراكك ساري حتى: `{expiry_str}`\n\n"
            f"استمتع بالمتابعة التلقائية لمودل الأقصى! 🚀"
        )
        log.info(f"Subscription activated: chat_id={chat_id}, months={months}, expiry={expiry_str}")
    except Exception as e:
        log.error(f"Activate subscription error for {chat_id}: {e}")

# =====================================================
# /admin — لوحة تحكم الأدمن
# =====================================================
@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if m.chat.id != ADMIN_ID:
        bot.send_message(m.chat.id, "⛔ هذا الأمر للمشرف فقط.")
        return

    try:
        with get_db() as conn:
            total_users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            vip_users     = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip=1").fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_payments WHERE status='pending'"
            ).fetchone()[0]
            recent_users  = conn.execute(
                "SELECT chat_id, username, is_vip, expiry_date, joined_at "
                "FROM users ORDER BY joined_at DESC LIMIT 5"
            ).fetchall()
    except Exception as e:
        log.error(f"Admin panel DB error: {e}")
        bot.send_message(m.chat.id, "⚠️ خطأ في قراءة البيانات.")
        return

    lines = [
        "🛠 *لوحة تحكم الأدمن*\n",
        f"👥 إجمالي المستخدمين: `{total_users}`",
        f"👑 مستخدمو VIP: `{vip_users}`",
        f"💳 طلبات دفع معلقة: `{pending_count}`\n",
        "*آخر 5 مستخدمين:*"
    ]
    for u in recent_users:
        vip_tag = " 👑" if u["is_vip"] else ""
        expiry  = u["expiry_date"][:10] if u["expiry_date"] else "—"
        lines.append(
            f"• `{u['username'] or 'N/A'}`{vip_tag} — ID:`{u['chat_id']}` — حتى:`{expiry}`"
        )

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📋 طلبات الدفع المعلقة", callback_data="admin_pending"))
    kb.add(types.InlineKeyboardButton("📢 إرسال رسالة جماعية", callback_data="admin_broadcast"))

    bot.send_message(m.chat.id, "\n".join(lines), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "admin_pending")
def cb_admin_pending(c):
    if c.from_user.id != ADMIN_ID:
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    bot.answer_callback_query(c.id)

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT p.id, p.chat_id, p.amount, p.submitted_at, u.username "
                "FROM pending_payments p LEFT JOIN users u ON p.chat_id=u.chat_id "
                "WHERE p.status='pending' ORDER BY p.submitted_at DESC LIMIT 10"
            ).fetchall()
    except Exception as e:
        log.error(f"Pending payments query error: {e}")
        bot.send_message(ADMIN_ID, "⚠️ خطأ في قراءة الطلبات.")
        return

    if not rows:
        bot.send_message(ADMIN_ID, "✅ لا توجد طلبات دفع معلقة.")
        return

    for row in rows:
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✅ تفعيل شهر",   callback_data=f"approve_1_{row['chat_id']}"),
            types.InlineKeyboardButton("✅ تفعيل 3 أشهر", callback_data=f"approve_3_{row['chat_id']}")
        )
        kb.add(types.InlineKeyboardButton("❌ رفض", callback_data=f"reject_{row['chat_id']}"))
        bot.send_message(
            ADMIN_ID,
            f"💳 طلب #{row['id']}\n"
            f"👤 `{row['username'] or 'N/A'}` — ID: `{row['chat_id']}`\n"
            f"💰 {row['amount']}$ — 🕐 {row['submitted_at'][:16]}",
            reply_markup=kb
        )

@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def cb_admin_broadcast(c):
    if c.from_user.id != ADMIN_ID:
        bot.answer_callback_query(c.id, "⛔ غير مصرح.")
        return
    bot.answer_callback_query(c.id)
    msg = bot.send_message(ADMIN_ID, "📢 أرسل الرسالة التي تريد إذاعتها لجميع المستخدمين:")
    bot.register_next_step_handler(msg, do_broadcast)

def do_broadcast(m):
    if m.chat.id != ADMIN_ID:
        return
    text = m.text or ""
    try:
        with get_db() as conn:
            chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM users").fetchall()]
    except Exception as e:
        log.error(f"Broadcast DB error: {e}")
        bot.send_message(ADMIN_ID, "⚠️ خطأ في قراءة المستخدمين.")
        return

    sent = failed = 0
    for cid in chat_ids:
        try:
            bot.send_message(cid, f"📢 *إعلان من الإدارة:*\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)

    bot.send_message(ADMIN_ID, f"📢 تم الإرسال: ✅ {sent} | ❌ {failed}")

# =====================================================
# /vip — منح VIP يدوياً (أدمن فقط)
# =====================================================
@bot.message_handler(commands=["vip"])
def cmd_vip(m):
    if m.chat.id != ADMIN_ID:
        bot.send_message(m.chat.id, "⛔ هذا الأمر للمشرف فقط.")
        return
    parts = m.text.split()
    if len(parts) < 3:
        bot.send_message(m.chat.id, "الاستخدام: `/vip <chat_id> <months>`")
        return
    try:
        target_id = int(parts[1])
        months    = int(parts[2])
        _activate_subscription(target_id, months)
        bot.send_message(m.chat.id, f"✅ تم تفعيل {months} شهر للمستخدم `{target_id}`")
    except ValueError:
        bot.send_message(m.chat.id, "⚠️ تأكد من إدخال أرقام صحيحة.")
    except Exception as e:
        bot.send_message(m.chat.id, f"⚠️ خطأ: {e}")

# =====================================================
# نظام التنبيهات الدوري (محسن للأداء)
# =====================================================
def check_single_user(row):
    try:
        # تحقق من صلاحية الاشتراك قبل الفحص
        if not is_active(row):
            return

        password = dec(row["password"])
        if not password:
            return

        res = run_moodle(row["username"], password)
        if res["status"] != "success":
            return

        new_hash = hashlib.md5(res["message"].encode()).hexdigest()
        if row["last_hash"] != new_hash:
            bot.send_message(row["chat_id"], "🔔 *تحديث جديد في المودل:*\n\n" + res["message"])
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?",
                        (new_hash, datetime.now().strftime("%Y-%m-%d %H:%M"), row["chat_id"])
                    )
            except Exception as e:
                log.error(f"Update hash error for {row['chat_id']}: {e}")
    except Exception as e:
        log.warning(f"Error checking {row['chat_id']}: {e}")

def broadcast_reports():
    log.info("بدء جولة الفحص الدوري...")
    try:
        with get_db() as conn:
            users = conn.execute(
                "SELECT * FROM users WHERE username IS NOT NULL AND password IS NOT NULL"
            ).fetchall()
    except Exception as e:
        log.error(f"Broadcast fetch error: {e}")
        return

    with ThreadPoolExecutor(max_workers=5) as executor:
        executor.map(check_single_user, users)

def scheduler_thread():
    time.sleep(10)
    broadcast_reports()
    schedule.every(4).hours.do(broadcast_reports)
    while True:
        schedule.run_pending()
        time.sleep(60)

# =====================================================
# التشغيل النهائي
# =====================================================
if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    threading.Thread(target=scheduler_thread, daemon=True).start()
    log.info("✅ Bot is online | DB: %s | Wallet: %s", DB_PATH, WALLET)
    bot.infinity_polling()
