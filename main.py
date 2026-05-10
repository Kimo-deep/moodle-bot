# -*- coding: utf-8 -*-

import os
import re
import time
import queue
import hashlib
import logging
import sqlite3
import threading

from datetime import datetime, timedelta
from contextlib import contextmanager

import requests
import telebot
import schedule

from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from requests.adapters import HTTPAdapter
from telebot import types

# =====================================================
# الإعدادات
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
ENC_KEY = os.getenv("ENC_KEY")

if not TOKEN:
    raise ValueError("TOKEN missing")

if not ENC_KEY:
    raise ValueError("ENC_KEY missing")

ADMIN_ID = 7840931571

FREE_TRIAL_END = datetime(2026, 6, 1)

DB_PATH = "users.db"

CACHE = {}
CACHE_TTL = 300

fernet = Fernet(ENC_KEY.encode())

bot = telebot.TeleBot(TOKEN)

DB_LOCK = threading.Lock()

IS_HOLIDAY = False

# =====================================================
# قاعدة البيانات
# =====================================================

DB_CONN = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
    timeout=30
)

DB_CONN.row_factory = sqlite3.Row


@contextmanager
def get_db():

    with DB_LOCK:

        try:

            yield DB_CONN

            DB_CONN.commit()

        except:

            DB_CONN.rollback()

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

            last_report TEXT

        );

        """)

# =====================================================
# أدوات
# =====================================================


def enc(txt):

    return fernet.encrypt(txt.encode()).decode()


def dec(txt):

    return fernet.decrypt(txt.encode()).decode()


def esc(text):

    if not text:
        return ""

    chars = r"_*[]()~`>#+-=|{}.!"

    for c in chars:

        text = text.replace(c, f"\\{c}")

    return text


def get_cached_report(username):

    item = CACHE.get(username)

    if not item:
        return None

    ts, data = item

    if time.time() - ts > CACHE_TTL:

        del CACHE[username]

        return None

    return data


def set_cached_report(username, data):

    CACHE[username] = (time.time(), data)


# =====================================================
# الكلمات الدلالية
# =====================================================

_DONE_KW = [

    "تم التسليم",
    "submitted",
    "graded",
    "review attempt",
    "finished",
    "completed",
    "attempt",
    "تمت المحاولة",
    "تم الحل"

]

_EXAM_KW = [

    "اختبار",
    "امتحان",
    "quiz",
    "exam",
    "test"

]

_ASSIGN_KW = [

    "تكليف",
    "واجب",
    "assignment",
    "task",
    "report"

]

_MEET_KW = [

    "zoom",
    "meet",
    "bigbluebutton"

]

# =====================================================
# كشف المنجز
# =====================================================


def _quick_done(text):

    t = text.lower().strip()

    return any(k.lower() in t for k in _DONE_KW)


def _assign_done(session, url):

    try:

        soup = BeautifulSoup(

            session.get(url, timeout=15).text,

            "html.parser"

        )

        page = soup.get_text(" ", strip=True).lower()

        done_words = [

            "submitted",
            "edit submission",
            "تم التسليم",
            "graded"

        ]

        return any(w in page for w in done_words)

    except:

        return False


def _quiz_done(session, url):

    try:

        soup = BeautifulSoup(

            session.get(url, timeout=15).text,

            "html.parser"

        )

        page = soup.get_text(" ", strip=True).lower()

        done = [

            "review attempt",
            "finished",
            "your final grade",
            "تم الحل"

        ]

        return any(w in page for w in done)

    except:

        return False

# =====================================================
# استخراج الوقت
# =====================================================


def _extract_time_from_div(ev):

    time_tag = ev.find("time")

    if time_tag:

        dt = (

            time_tag.get("datetime")

            or time_tag.get("data-time")

            or ""

        ).strip()

        if dt:

            try:

                parsed = datetime.fromisoformat(

                    dt.replace("Z", "+00:00")

                )

                parsed += timedelta(hours=3)

                return parsed.strftime("%Y-%m-%d %I:%M %p")

            except:
                pass

    text = ev.get_text(" ", strip=True)

    m = re.search(r"\d{1,2}:\d{2}", text)

    if m:
        return m.group(0)

    return ""

# =====================================================
# استخراج النشاط
# =====================================================


def _extract_event(ev):

    h3 = ev.find("h3")

    atag = ev.find("a", href=True)

    name = h3.get_text(" ", strip=True) if h3 else "نشاط"

    url = atag["href"] if atag else ""

    course = "غير محدد"

    desc = ev.find(class_="description")

    if desc:

        course = desc.get_text(" ", strip=True)

    return {

        "name": esc(name),

        "course": esc(course),

        "url": url,

        "url_lower": url.lower(),

        "time": _extract_time_from_div(ev),

        "raw": ev.get_text(" ", strip=True),

    }

# =====================================================
# تنسيق الرسائل
# =====================================================


def _fmt_exam(ev):

    return (

        f"▪️ *{ev['name']}*\n"

        f"📌 {ev['course']}\n"

        f"🕐 {ev['time']}"

    )


def _fmt_task(ev):

    return (

        f"▪️ *{ev['name']}*\n"

        f"📌 {ev['course']}\n"

        f"📅 {ev['time']}"

    )

# =====================================================
# مودل
# =====================================================


def run_moodle(username, password):

    cached = get_cached_report(username)

    if cached:
        return cached

    session = requests.Session()

    adapter = HTTPAdapter(

        pool_connections=20,

        pool_maxsize=20,

        max_retries=2

    )

    session.mount("https://", adapter)

    session.headers.update({

        "User-Agent": "Mozilla/5.0"

    })

    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"

    try:

        soup = BeautifulSoup(

            session.get(login_url, timeout=20).text,

            "html.parser"

        )

        token = soup.find(

            "input",

            {"name": "logintoken"}

        )

        if not token:

            return {

                "status": "error",

                "message": "تعذر فتح صفحة تسجيل الدخول"

            }

        resp = session.post(

            login_url,

            data={

                "username": username,

                "password": password,

                "logintoken": token["value"]

            },

            timeout=20

        )

        if "login" in resp.url:

            return {

                "status": "fail",

                "message": "❌ بيانات الدخول غير صحيحة"

            }

        calendar = session.get(

            "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming",

            timeout=20

        )

        soup = BeautifulSoup(

            calendar.text,

            "html.parser"

        )

        exams = []

        assignments = []

        meetings = []

        lectures = []

        skipped = 0

        for ev_div in soup.find_all(

            "div",

            {"class": "event"}

        ):

            raw_txt = ev_div.get_text(

                " ",

                strip=True

            )

            if _quick_done(raw_txt):

                skipped += 1

                continue

            ev = _extract_event(ev_div)

            ll = ev["url_lower"]

            tl = ev["raw"].lower()

            is_quiz = (

                "quiz" in ll

                or any(w in tl for w in _EXAM_KW)

            )

            is_assign = (

                "assign" in ll

                or any(w in tl for w in _ASSIGN_KW)

            )

            is_meet = (

                any(x in ll for x in _MEET_KW)

                or "لقاء" in tl

            )

            if ev["url"]:

                if is_assign and not is_quiz:

                    if _assign_done(

                        session,

                        ev["url"]

                    ):

                        skipped += 1

                        continue

                elif is_quiz:

                    if _quiz_done(

                        session,

                        ev["url"]

                    ):

                        skipped += 1

                        continue

            if is_quiz:

                exams.append(

                    _fmt_exam(ev)

                )

            elif is_assign:

                assignments.append(

                    _fmt_task(ev)

                )

            elif is_meet:

                meetings.append(

                    _fmt_task(ev)

                )

            else:

                lectures.append(

                    _fmt_task(ev)

                )

        msg = []

        msg.append(

            f"🕐 *{datetime.now().strftime('%Y-%m-%d %H:%M')}*"

        )

        msg.append(

            f"\n📝 *الاختبارات غير المنجزة ({len(exams)}):*"

        )

        msg.append(

            "لا يوجد"

            if not exams

            else "\n\n".join(exams)

        )

        msg.append(

            f"\n⚠️ *التكاليف غير المنجزة ({len(assignments)}):*"

        )

        msg.append(

            "لا يوجد"

            if not assignments

            else "\n\n".join(assignments)

        )

        msg.append(

            f"\n🎥 *اللقاءات ({len(meetings)}):*"

        )

        msg.append(

            "لا يوجد"

            if not meetings

            else "\n\n".join(meetings)

        )

        msg.append(

            f"\n📚 *المحاضرات ({len(lectures)}):*"

        )

        msg.append(

            "لا يوجد"

            if not lectures

            else "\n\n".join(lectures)

        )

        if skipped:

            msg.append(

                f"\n_✅ تم إخفاء {skipped} عنصر منجز_"

            )

        result = {

            "status": "success",

            "message": "\n".join(msg)

        }

        set_cached_report(

            username,

            result

        )

        return result

    except Exception as e:

        log.error(e)

        return {

            "status": "error",

            "message": f"⚠️ خطأ: {str(e)[:100]}"

        }

# =====================================================
# الاشتراك
# =====================================================


def check_access(chat_id):

    if datetime.now() < FREE_TRIAL_END:
        return True

    with get_db() as conn:

        row = conn.execute(

            "SELECT expiry_date,is_vip FROM users WHERE chat_id=?",

            (chat_id,)

        ).fetchone()

    if not row:
        return False

    if row["is_vip"]:
        return True

    if row["expiry_date"]:

        exp = datetime.strptime(

            row["expiry_date"],

            "%Y-%m-%d %H:%M:%S"

        )

        return exp > datetime.now()

    return False

# =====================================================
# START
# =====================================================


@bot.message_handler(commands=["start"])
def start(m):

    kb = types.ReplyKeyboardMarkup(

        resize_keyboard=True

    )

    kb.row(

        "🔍 فحص الآن",

        "📊 حالتي"

    )

    bot.send_message(

        m.chat.id,

        "🎓 أهلاً بك في بوت مودل الأقصى",

        reply_markup=kb

    )

# =====================================================
# CHECK
# =====================================================


@bot.message_handler(commands=["check"])
def cmd_check(m):

    do_check(m.chat.id)


@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def btn_check(m):

    do_check(m.chat.id)


def do_check(chat_id):

    ok = check_access(chat_id)

    if not ok:

        bot.send_message(

            chat_id,

            "🚫 الاشتراك منتهي"

        )

        return

    with get_db() as conn:

        row = conn.execute(

            "SELECT username,password FROM users WHERE chat_id=?",

            (chat_id,)

        ).fetchone()

    if row and row["username"]:

        wm = bot.send_message(

            chat_id,

            "🔍 جاري الفحص..."

        )

        res = run_moodle(

            row["username"],

            dec(row["password"])

        )

        try:

            bot.edit_message_text(

                res["message"],

                chat_id,

                wm.message_id,

                parse_mode="Markdown"

            )

        except:

            bot.send_message(

                chat_id,

                res["message"],

                parse_mode="Markdown"

            )

    else:

        wm = bot.send_message(

            chat_id,

            "📋 أرسل الرقم الجامعي"

        )

        bot.register_next_step_handler(

            wm,

            step_user

        )

# =====================================================
# تسجيل الدخول
# =====================================================


def step_user(msg):

    user = msg.text.strip()

    wm = bot.send_message(

        msg.chat.id,

        "🔐 أرسل كلمة المرور"

    )

    bot.register_next_step_handler(

        wm,

        lambda m2: step_pwd(m2, user)

    )


def step_pwd(msg, user):

    pwd = msg.text.strip()

    wm = bot.send_message(

        msg.chat.id,

        "⏳ جاري التحقق"

    )

    res = run_moodle(user, pwd)

    if res["status"] == "success":

        with get_db() as conn:

            conn.execute(

                "INSERT OR REPLACE INTO users(chat_id,username,password) VALUES(?,?,?)",

                (

                    msg.chat.id,

                    user,

                    enc(pwd)

                )

            )

        bot.edit_message_text(

            "✅ تم الربط بنجاح\n\n" + res["message"],

            msg.chat.id,

            wm.message_id,

            parse_mode="Markdown"

        )

    else:

        bot.edit_message_text(

            res["message"],

            msg.chat.id,

            wm.message_id

        )

# =====================================================
# التقارير الدورية
# =====================================================


def broadcast_reports():

    if IS_HOLIDAY:
        return

    with get_db() as conn:

        rows = conn.execute(

            "SELECT chat_id,username,password,last_hash FROM users WHERE username IS NOT NULL"

        ).fetchall()

    for row in rows:

        uid = row["chat_id"]

        if not check_access(uid):
            continue

        try:

            res = run_moodle(

                row["username"],

                dec(row["password"])

            )

            if res["status"] != "success":
                continue

            h = hashlib.md5(

                res["message"].encode()

            ).hexdigest()

            if row["last_hash"] == h:
                continue

            bot.send_message(

                uid,

                "🔔 *تحديث جديد*\n\n" + res["message"],

                parse_mode="Markdown"

            )

            with get_db() as conn:

                conn.execute(

                    "UPDATE users SET last_hash=?,last_report=? WHERE chat_id=?",

                    (

                        h,

                        datetime.now().strftime("%Y-%m-%d %H:%M"),

                        uid

                    )

                )

        except Exception as e:

            log.warning(e)

# =====================================================
# المجدول
# =====================================================


def scheduler_thread():

    schedule.every(6).hours.do(

        broadcast_reports

    )

    while True:

        schedule.run_pending()

        time.sleep(30)

# =====================================================
# التشغيل
# =====================================================

if __name__ == "__main__":

    init_db()

    threading.Thread(

        target=scheduler_thread,

        daemon=True

    ).start()

    log.info("✅ Bot Running")

    bot.infinity_polling(

        timeout=30,

        long_polling_timeout=20

    )
