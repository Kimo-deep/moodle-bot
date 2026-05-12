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

TOKEN = os.getenv("TOKEN")
ADMIN_ID = 7840931571
DB_PATH = "/app/data/users.db"
FREE_TRIAL_END = datetime(2026, 6, 1)

bot = telebot.TeleBot(TOKEN)

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
    if not os.path.exists("/app/data"): os.makedirs("/app/data")
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT, password TEXT,
                expiry_date TEXT, is_vip INTEGER DEFAULT 0,
                last_hash TEXT, last_report TEXT
            );
        """)

def check_access(chat_id: int):
    if datetime.now() < FREE_TRIAL_END: return True
    with get_db() as conn:
        row = conn.execute("SELECT expiry_date, is_vip FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        if row["is_vip"]: return True
        if row["expiry_date"] and datetime.strptime(row["expiry_date"], "%Y-%m-%d %H:%M:%S") > datetime.now():
            return True
    return False

# ══════════════════════════════════════════════════════════
# 3. منطق الفحص الدقيق (Surgical Check)
# ══════════════════════════════════════════════════════════
def _assign_done(session, url: str) -> bool:
    """فحص هل التكليف تم تسليمه فعلاً أم لا"""
    try:
        res_html = session.get(url, timeout=10).text
        soup = BeautifulSoup(res_html, "html.parser")
        # البحث في جدول حالة التسليم
        status_table = soup.find("table", class_="generaltable")
        if status_table:
            text = status_table.get_text().lower()
            # الكلمات القاطعة للتسليم
            if any(k in text for k in ["submitted for grading", "تم تقديمها للتقييم", "محملة للتقييم", "submitted", "سلمت"]):
                return True
        return False
    except: return False

def _quiz_done(session, url: str) -> bool:
    """فحص هل تم إنهاء الاختبار"""
    try:
        res_html = session.get(url, timeout=10).text
        # إذا وجد زر "مراجعة" أو نص "لا توجد محاولات أخرى"
        if any(k in res_html.lower() for k in ["review", "مراجعة", "no more attempts", "درجتك", "grade"]):
            return True
        return False
    except: return False

def _extract_event(ev) -> dict:
    try:
        atag = ev.find("a", {"data-action": "view-event"}) or ev.find("a", href=True)
        if not atag: return None
        
        raw_name = atag.get("title") or atag.get_text(strip=True)
        url = atag.get("href", "")
        
        # استخراج اسم المساق من الـ Breadcrumbs أو العنوان
        course = "غير محدد"
        title_attr = atag.get("title", "")
        if "مساق" in title_attr:
            course = title_attr.split("مساق")[-1].strip()
        elif "course" in title_attr.lower():
            course = title_attr.lower().split("course")[-1].replace("is due for the", "").strip().upper()

        # استخراج اليوم
        time_val = ""
        cell = ev.find_parent("td", class_="day")
        if cell:
            day_num = cell.find(class_="day-number")
            if day_num: time_val = f"يوم {day_num.get_text(strip=True)}"

        clean_name = re.sub(r"(يُفتح|يفتح|يُغلق|يغلق|مستحق|opens|closes|is due).*", "", raw_name, flags=re.I).strip()
        return {"name": clean_name, "course": course, "url": url, "time": time_val}
    except: return None

def _merge_events(events: list) -> list:
    unique = {}
    for ev in events:
        key = (ev["name"].strip().lower(), ev["course"].strip().lower())
        if key not in unique:
            unique[key] = ev
        else:
            if not unique[key]["time"] and ev["time"]:
                unique[key]["time"] = ev["time"]
    return list(unique.values())

# ══════════════════════════════════════════════════════════
# 4. المحرك الرئيسي
# ══════════════════════════════════════════════════════════
def run_moodle(username, password) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    try:
        login_pg = session.get("https://moodle.alaqsa.edu.ps/login/index.php", timeout=20).text
        token = BeautifulSoup(login_pg, "html.parser").find("input", {"name": "logintoken"})
        if not token: return {"status": "error", "message": "⚠️ المودل لا يستجيب."}
        
        session.post("https://moodle.alaqsa.edu.ps/login/index.php", 
                     data={"username": username, "password": password, "logintoken": token["value"]}, timeout=20)
        
        cal_html = session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=month", timeout=20).text
        soup = BeautifulSoup(cal_html, "html.parser")
        
        event_links = soup.find_all("a", {"data-action": "view-event"})
        exams, assigns, meets = [], [], []
        skipped, processed_ids = 0, set()

        for link in event_links:
            ev_id = link.get("data-event-id")
            if ev_id in processed_ids: continue
            processed_ids.add(ev_id)
            
            ev = _extract_event(link.find_parent("div") or link)
            if not ev: continue
            
            u, n = ev["url"].lower(), ev["name"].lower()
            
            # التصنيف
            if "quiz" in u or any(x in n for x in ["امتحان", "اختبار", "كويز"]):
                if _quiz_done(session, ev["url"]): skipped += 1
                else: exams.append(ev)
            elif "assign" in u or any(x in n for x in ["تكليف", "واجب", "تجربة", "experiment"]):
                if _assign_done(session, ev["url"]): skipped += 1
                else: assigns.append(ev)
            elif any(x in u for x in ["zoom", "meet", "bigbluebutton"]):
                meets.append(ev)

        # التقرير
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        report = [f"🕐 *تقرير: {now}*\n"]

        def fmt(e):
            return f"▪️ *{e['name']}*\n   📌 {e['course']}\n   📅 {e['time'] or 'راجع الرابط'}"

        if meets: report.append("🎥 *اللقاءات:*\n" + "\n\n".join(fmt(e) for e in _merge_events(meets)))
        if exams: report.append("📝 *الاختبارات:*\n" + "\n\n".join(fmt(e) for e in _merge_events(exams)))
        if assigns: report.append("⚠️ *التكاليف:*\n" + "\n\n".join(fmt(e) for e in _merge_events(assigns)))
        
        if len(report) == 1: report.append("✅ لا يوجد مهام قادمة.")
        if skipped: report.append(f"\n_✅ تم إخفاء {skipped} عنصر منجز_")
        
        return {"status": "success", "message": "\n\n".join(report)}
    except:
        return {"status": "error", "message": "⚠️ فشل في الاتصال."}

# ══════════════════════════════════════════════════════════
# 5. دوال البوت (نفس الهيكل السابق)
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    bot.send_message(m.chat.id, "🎓 بوت مودل الأقصى المطور.", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def bot_check(m):
    with get_db() as conn:
        row = conn.execute("SELECT username, password FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()
    if row:
        wait = bot.send_message(m.chat.id, "🔍 جاري الفحص الدقيق...")
        res = run_moodle(row["username"], row["password"])
        bot.edit_message_text(res["message"], m.chat.id, wait.message_id, parse_mode="Markdown", disable_web_page_preview=True)
    else:
        bot.send_message(m.chat.id, "أرسل الرقم الجامعي للربط:")
        bot.register_next_step_handler(m, _reg_user)

def _reg_user(m):
    u = m.text
    bot.send_message(m.chat.id, "أرسل كلمة المرور:")
    bot.register_next_step_handler(m, lambda msg: _reg_fin(msg, u))

def _reg_fin(m, u):
    p = m.text
    res = run_moodle(u, p)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)", (m.chat.id, u, p))
        bot.send_message(m.chat.id, "✅ تم الربط!\n\n" + res["message"], parse_mode="Markdown")
    else: bot.send_message(m.chat.id, "❌ خطأ في البيانات.")

if __name__ == "__main__":
    init_db()
    bot.infinity_polling()
