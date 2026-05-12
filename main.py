"""
بوت مودل الأقصى — النسخة النهائية (المحدثة)
=====================================
الميزات: تقويم شهري، كشف اللقاءات، إخفاء المنجز، إرسال دوري كل 6 ساعات.
"""

import os, hmac, hashlib, json, uuid, time, threading, sqlite3, schedule, requests, logging, re
from contextlib import contextmanager
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ══════════════════════════════════════════════════════════
# 1. الإعدادات
# ══════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TOKEN")
BIN_CERT   = os.getenv("BINANCE_API_KEY")
BIN_SECRET = os.getenv("BINANCE_SECRET_KEY")

ADMIN_ID       = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)
PRICE_USD      = 2.0
ILS_PER_USD    = 3.7          
DB_PATH        = "/app/data/users.db"

bot = telebot.TeleBot(TOKEN)
IS_HOLIDAY = False

# ══════════════════════════════════════════════════════════
# 2. قاعدة البيانات
# ══════════════════════════════════════════════════════════
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                username    TEXT,
                password    TEXT,
                expiry_date TEXT,
                is_vip      INTEGER DEFAULT 0,
                last_hash   TEXT,
                last_report TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                order_id   TEXT    PRIMARY KEY,
                chat_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'pending'
            );
        """)

# ══════════════════════════════════════════════════════════
# 3. سعر الصرف والاشتراكات (دوال المساعدة)
# ══════════════════════════════════════════════════════════
def refresh_rate():
    global ILS_PER_USD
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=8).json()
        ILS_PER_USD = round(r["rates"]["ILS"], 2)
    except: pass

def check_access(chat_id: int) -> tuple:
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    with get_db() as conn:
        row = conn.execute("SELECT expiry_date, is_vip FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        if row["is_vip"]: return True, "VIP"
        if row["expiry_date"]:
            exp = datetime.strptime(row["expiry_date"], "%Y-%m-%d %H:%M:%S")
            if exp > datetime.now(): return True, f"مشترك"
    return False, None

def activate(chat_id: int, plan: str):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        if plan == "VIP": conn.execute("UPDATE users SET is_vip=1 WHERE chat_id=?", (chat_id,))
        else:
            exp = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?", (exp, chat_id))

# ══════════════════════════════════════════════════════════
# 4. كشف المنجز — (النسخة المطورة)
# ══════════════════════════════════════════════════════════
_DONE_KW = ["تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال", "attempt already", "تم المحاولة", "انتهى", "closed", "finished"]

def _quick_done(text: str) -> bool:
    return any(k.lower() in text.lower() for k in _DONE_KW)

def _assign_done(session, url: str) -> bool:
    try:
        res = session.get(url, timeout=12).text
        soup = BeautifulSoup(res, "html.parser")
        page_text = soup.get_text(" ", strip=True).lower()
        if any(k in page_text for k in ["تعديل التسليم", "إزالة التسليم", "edit submission", "you have submitted"]):
            return True
        for tr in soup.find_all("tr"):
            th = tr.find("th"); td = tr.find("td")
            if th and td and any(k in th.get_text().lower() for k in ["حالة", "status"]):
                if any(k in td.get_text().lower() for k in ["submitted", "تم التسليم", "graded", "محملة"]):
                    return True
        return False
    except: return False

def _quiz_done(session, url: str) -> bool:
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        page = soup.get_text(" ", strip=True).lower()
        indicators = ["لقد أنهيت", "your last attempt", "آخر محاولة", "no more attempts", "review attempt", "درجتك", "grade:"]
        return any(k in page for k in indicators) or bool(soup.find("table", {"class": lambda x: x and "quizattemptsummary" in x}))
    except: return False

# ══════════════════════════════════════════════════════════
# 5. استخراج البيانات من HTML
# ══════════════════════════════════════════════════════════
_OPEN_KW, _CLOSE_KW = ["يُفتح", "يفتح", "open"], ["يُغلق", "يغلق", "close"]
_TIME_RE = re.compile(r"(?:الأحد|الاثنين|الثلاثاء|الأربعاء|الخميس|الجمعة|السبت|غدًا|اليوم).*\d{1,2}:\d{2}\s*(?:AM|PM|ص|م)", re.U)

def _extract_event(ev) -> dict:
    h3 = ev.find("h3") or ev.find(class_="name")
    atag = (h3.find("a", href=True) if h3 else None) or ev.find("a", href=True)
    url = atag["href"] if atag else ""
    raw_name = h3.get_text(strip=True) if h3 else "بدون اسم"
    
    # استخراج الوقت
    time_val = ""
    date_tag = ev.select_one(".date, .event-date")
    if date_tag: time_val = date_tag.get_text(strip=True)
    if not time_val:
        m = _TIME_RE.search(ev.get_text(" "))
        if m: time_val = m.group()

    # استخراج المادة
    course, doctor = "غير محدد", "غير محدد"
    for a in ev.find_all("a", href=True):
        if "/course/" in a["href"]:
            course = a.get_text(strip=True)
            break
            
    role = "open" if any(k in raw_name for k in _OPEN_KW) else "close" if any(k in raw_name for k in _CLOSE_KW) else "single"
    return {"name": raw_name, "course": course, "doctor": doctor, "url": url, "time": time_val, "role": role, "raw": ev.get_text(" ")}

def _merge_exams(events: list) -> list:
    merged, singles = {}, []
    for ev in events:
        key = (ev["name"].lower(), ev["course"].lower())
        if ev["role"] == "single": singles.append(ev); continue
        if key not in merged: merged[key] = ev.copy()
        if ev["role"] == "open": merged[key]["date_open"] = ev["time"]
        else: merged[key]["date_close"] = ev["time"]
    return list(merged.values()) + singles

# ══════════════════════════════════════════════════════════
# 6. محرك المودل (الرئيسي)
# ══════════════════════════════════════════════════════════
def run_moodle(username, password) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    
    try:
        # 1. تسجيل الدخول
        res_login = session.get(login_url, timeout=20).text
        soup_login = BeautifulSoup(res_login, "html.parser")
        token = soup_login.find("input", {"name": "logintoken"})
        if not token: 
            return {"status": "error", "message": "⚠️ المودل لا يستجيب حالياً."}
        
        resp = session.post(login_url, data={
            "username": username, 
            "password": password, 
            "logintoken": token["value"]
        }, timeout=20)
        
        if "login" in resp.url: 
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        # 2. جلب التقويم الشهري
        calendar_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=month"
        calendar_html = session.get(calendar_url, timeout=20).text
        soup = BeautifulSoup(calendar_html, "html.parser")

        # 3. استخراج الأحداث (البحث في كل الروابط التي تمثل أحداثاً)
        # في عرض الشهر، الأحداث غالباً ما تكون داخل data-event-id
        event_links = soup.find_all("a", {"data-action": "view-event"})
        
        exams_raw, assignments, meetings, others = [], [], [], []
        skipped = 0
        processed_ids = set() # لتجنب التكرار

        for link in event_links:
            ev_id = link.get("data-event-id")
            if ev_id in processed_ids: continue
            processed_ids.add(ev_id)

            # الحصول على الحاوية الأكبر للحدث (المربع الصغير)
            container = link.find_parent("div", class_="calendar-event-container") or link
            raw_text = container.get_text(" ", strip=True)
            
            # فحص إذا كان منجزاً (كويك دون)
            if _quick_done(raw_text):
                skipped += 1
                continue

            ev = _extract_event(container)
            # تعزيز استخراج الاسم إذا فشل المستخرج العادي
            if not ev["name"] or ev["name"] == "بدون اسم":
                ev["name"] = link.get_text(strip=True)
            
            url_l = ev["url"].lower()
            tl = raw_text.lower()

            # تصنيف الأحداث
            is_quiz = "quiz" in url_l or any(x in tl for x in ["اختبار", "كويز", "امتحان"])
            is_meet = any(x in url_l or x in tl for x in ["zoom", "meet", "لقاء", "محاضرة", "بث"])
            is_assign = "assign" in url_l or any(x in tl for x in ["تكليف", "واجب", "مهمة"])

            # فحص العمق (هل تم التسليم فعلياً؟)
            if ev["url"]:
                if is_assign and not is_quiz:
                    if _assign_done(session, ev["url"]):
                        skipped += 1; continue
                elif is_quiz:
                    if _quiz_done(session, ev["url"]):
                        skipped += 1; continue

            if is_quiz:     exams_raw.append(ev)
            elif is_meet:   meetings.append(ev)
            elif is_assign: assignments.append(ev)
            else:           others.append(ev)

        # 4. بناء التقرير
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        report = [f"🕐 *تقرير: {now}*\n"]
        
        def fmt(e):
            time_str = f"📅 {e['time']}" if e['time'] else "📅 موعد غير محدد"
            return f"▪️ *{e['name']}*\n   📌 {e['course']}\n   {time_str}"

        if meetings:
            report.append("🎥 *اللقاءات والمحاضرات:*\n" + "\n\n".join(fmt(e) for e in meetings))
        
        if exams_raw:
            merged = _merge_exams(exams_raw)
            report.append("📝 *الاختبارات:*\n" + "\n\n".join(fmt(e) for e in merged))
            
        if assignments:
            report.append("⚠️ *التكاليف والواجبات:*\n" + "\n\n".join(fmt(e) for e in assignments))

        if len(report) == 1:
            report.append("✅ لا توجد مهام أو لقاءات مسجلة في تقويم هذا الشهر.")
        
        if skipped:
            report.append(f"\n_✅ تم إخفاء {skipped} عنصر منجز_")
        
        return {"status": "success", "message": "\n\n".join(report)}

    except Exception as e:
        log.error(f"Error in run_moodle: {e}")
        return {"status": "error", "message": "⚠️ حدث خطأ فني أثناء جلب البيانات."}

# ══════════════════════════════════════════════════════════
# 7. أوامر البوت والتشغيل (نفس هيكلية كودك مع الإصلاحات)
# ══════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    kb.row("💳 اشتراك",   "❓ مساعدة")
    bot.send_message(m.chat.id, "🎓 *مرحباً بك في بوت مودل الأقصى المطور*", parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text in ["🔍 فحص الآن", "/check"])
def bot_check(m):
    ok, label = check_access(m.chat.id)
    if not ok: return bot.send_message(m.chat.id, "🚫 اشتراكك منتهٍ.")
    
    with get_db() as conn:
        row = conn.execute("SELECT username, password FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()
    
    if row and row["username"]:
        msg = bot.send_message(m.chat.id, "🔍 جاري فحص المودل...")
        res = run_moodle(row["username"], row["password"])
        bot.edit_message_text(res["message"], m.chat.id, msg.message_id, parse_mode="Markdown")
    else:
        bot.send_message(m.chat.id, "📋 أرسل رقمك الجامعي للربط:")
        bot.register_next_step_handler(m, _step_user)

def _step_user(m):
    user = m.text
    bot.send_message(m.chat.id, "🔐 الآن أرسل كلمة المرور:")
    bot.register_next_step_handler(m, lambda msg: _step_finish(msg, user))

def _step_finish(m, user):
    pwd = m.text
    res = run_moodle(user, pwd)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)", (m.chat.id, user, pwd))
        bot.send_message(m.chat.id, "✅ تم الربط بنجاح!\n\n" + res["message"], parse_mode="Markdown")
    else:
        bot.send_message(m.chat.id, res["message"])

# (بقية الدوال: payments, admin, broadcast تبقى كما هي في كودك الأصلي)
# ... [أكمل بقية كود Binance والأدمن من ملفك الأصلي] ...

def broadcast_reports():
    if IS_HOLIDAY: return
    with get_db() as conn:
        users = conn.execute("SELECT chat_id, username, password FROM users WHERE username IS NOT NULL").fetchall()
    for row in users:
        if not check_access(row["chat_id"])[0]: continue
        res = run_moodle(row["username"], row["password"])
        if res["status"] == "success":
            h = hashlib.md5(res["message"].encode()).hexdigest()
            with get_db() as conn:
                old = conn.execute("SELECT last_hash FROM users WHERE chat_id=?", (row["chat_id"],)).fetchone()
                if old and old["last_hash"] == h: continue
                try:
                    bot.send_message(row["chat_id"], f"🔔 *تحديث جديد:*\n\n{res['message']}", parse_mode="Markdown")
                    conn.execute("UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?", (h, datetime.now().strftime("%Y-%m-%d %H:%M"), row["chat_id"]))
                except: pass

def _scheduler():
    schedule.every(6).hours.do(broadcast_reports)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=_scheduler, daemon=True).start()
    bot.infinity_polling()
