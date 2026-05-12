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
_DONE_KW = ["تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال", "attempt already", "انتهى", "closed", "finished"]

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
# 3. منطق معالجة الأحداث (Moodle Processing)
# ══════════════════════════════════════════════════════════
def _quick_done(text: str) -> bool:
    return any(k.lower() in text.lower() for k in _DONE_KW)

def _assign_done(session, url: str) -> bool:
    try:
        res = session.get(url, timeout=10).text.lower()
        return any(k in res for k in ["تعديل التسليم", "إزالة التسليم", "edit submission", "submitted for grading", "محملة للتقييم"])
    except: return False

def _quiz_done(session, url: str) -> bool:
    try:
        res = session.get(url, timeout=10).text.lower()
        return any(k in res for k in ["review", "مراجعة", "درجتك", "grade", "لقد أنهيت", "no more attempts"])
    except: return False

def _extract_event(ev) -> dict:
    try:
        atag = ev.find("a", {"data-action": "view-event"}) or ev.find("a", href=True)
        if not atag: return None
        
        raw_name = atag.get("title") or atag.get_text(strip=True)
        url = atag.get("href", "")
        
        # استخراج المادة
        course = "غير محدد"
        if "من مساق" in raw_name:
            course = raw_name.split("من مساق")[-1].strip()
        elif atag.get("title") and "course" in atag["title"].lower():
            course = atag["title"].split("course")[-1].replace("is due for the", "").strip()

        # استخراج الوقت من "يوم" التقويم
        time_val = ""
        cell = ev.find_parent("td", class_="day")
        if cell:
            day_num = cell.find(class_="day-number")
            if day_num: time_val = f"يوم {day_num.get_text(strip=True)}"

        # تنظيف الاسم من الكلمات الدليلية
        clean_name = re.sub(r"(يُفتح|يفتح|يُغلق|يغلق|مستحق|opens|closes|is due).*", "", raw_name, flags=re.I).strip()
        
        return {"name": clean_name, "course": course, "url": url, "time": time_val}
    except: return None

def _merge_events(events: list) -> list:
    """دمج الأحداث المتكررة بناءً على الاسم والمادة"""
    unique = {}
    for ev in events:
        key = (ev["name"].strip().lower(), ev["course"].strip().lower())
        if key not in unique:
            unique[key] = ev
        else:
            # تحديث الوقت إذا كان متاحاً في نسخة وغير متاح في أخرى
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
        # تسجيل الدخول
        login_pg = session.get("https://moodle.alaqsa.edu.ps/login/index.php", timeout=20).text
        soup_login = BeautifulSoup(login_pg, "html.parser")
        token = soup_login.find("input", {"name": "logintoken"})
        if not token: return {"status": "error", "message": "⚠️ المودل لا يستجيب حالياً."}
        
        resp = session.post("https://moodle.alaqsa.edu.ps/login/index.php", 
                            data={"username": username, "password": password, "logintoken": token["value"]}, timeout=20)
        if "login" in resp.url: return {"status": "fail", "message": "❌ بياناتك خاطئة."}

        # جلب التقويم
        cal_html = session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=month", timeout=20).text
        soup = BeautifulSoup(cal_html, "html.parser")
        
        event_links = soup.find_all("a", {"data-action": "view-event"})
        exams_raw, assignments, meetings = [], [], []
        skipped, processed_ids = 0, set()

        for link in event_links:
            ev_id = link.get("data-event-id")
            if ev_id in processed_ids: continue
            processed_ids.add(ev_id)
            
            container = link.find_parent("div")
            ev = _extract_event(container or link)
            if not ev: continue
            
            if _quick_done(ev["name"]): skipped += 1; continue
            
            url_l = ev["url"].lower()
            if "assign" in url_l:
                if _assign_done(session, ev["url"]): skipped += 1; continue
                assignments.append(ev)
            elif "quiz" in url_l:
                if _quiz_done(session, ev["url"]): skipped += 1; continue
                exams_raw.append(ev)
            elif any(x in url_l for x in ["zoom", "meet", "bigbluebutton"]):
                meetings.append(ev)

        # بناء التقرير
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        report = [f"🕐 *تقرير: {now}*\n"]

        def fmt(e):
            c = e['course']
            if c == "غير محدد" and "id=" in e['url']:
                cid = re.search(r"id=(\d+)", e['url'])
                c = f"مساق ({cid.group(1)})" if cid else c
            t = e['time'] if e['time'] else "راجع الرابط"
            return f"▪️ *{e['name']}*\n   📌 {c}\n   📅 {t}"

        if meetings:
            report.append("🎥 *اللقاءات والمحاضرات:*\n" + "\n\n".join(fmt(e) for e in _merge_events(meetings)))
        if exams_raw:
            report.append("📝 *الاختبارات:*\n" + "\n\n".join(fmt(e) for e in _merge_events(exams_raw)))
        if assignments:
            report.append("⚠️ *التكاليف والواجبات:*\n" + "\n\n".join(fmt(e) for e in _merge_events(assignments)))
        
        if len(report) == 1: report.append("✅ لا توجد مهام حالياً.")
        if skipped: report.append(f"\n_✅ تم إخفاء {skipped} عنصر منجز_")
        
        return {"status": "success", "message": "\n\n".join(report)}
    except Exception as e:
        log.error(f"Global Error: {e}")
        return {"status": "error", "message": "⚠️ حدث خطأ فني أثناء الفحص."}

# ══════════════════════════════════════════════════════════
# 5. دوال البوت
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    bot.send_message(m.chat.id, "🎓 بوت مودل الأقصى المطور جاهز لخدمتك.", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def bot_check(m):
    if not check_access(m.chat.id): return bot.send_message(m.chat.id, "🚫 انتهى اشتراكك.")
    with get_db() as conn:
        row = conn.execute("SELECT username, password FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()
    
    if row and row["username"]:
        wait = bot.send_message(m.chat.id, "🔍 جاري فحص التقويم الشهري...")
        res = run_moodle(row["username"], row["password"])
        bot.edit_message_text(res["message"], m.chat.id, wait.message_id, parse_mode="Markdown", disable_web_page_preview=True)
    else:
        bot.send_message(m.chat.id, "📧 أرسل رقمك الجامعي للربط:")
        bot.register_next_step_handler(m, _reg_user)

def _reg_user(m):
    u = m.text
    bot.send_message(m.chat.id, "🔐 أرسل كلمة مرور المودل:")
    bot.register_next_step_handler(m, lambda msg: _reg_fin(msg, u))

def _reg_fin(m, u):
    p = m.text
    wait = bot.send_message(m.chat.id, "⚙️ جاري التحقق من البيانات...")
    res = run_moodle(u, p)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)", (m.chat.id, u, p))
        bot.edit_message_text("✅ تم الربط بنجاح!\n\n" + res["message"], m.chat.id, wait.message_id, parse_mode="Markdown")
    else:
        bot.edit_message_text(res["message"], m.chat.id, wait.message_id)

def broadcast_loop():
    while True:
        try:
            time.sleep(3600 * 6)
            with get_db() as conn:
                users = conn.execute("SELECT * FROM users WHERE username IS NOT NULL").fetchall()
            for u in users:
                if not check_access(u["chat_id"]): continue
                res = run_moodle(u["username"], u["password"])
                if res["status"] == "success":
                    h = hashlib.md5(res["message"].encode()).hexdigest()
                    if u["last_hash"] != h:
                        bot.send_message(u["chat_id"], "🔔 *تحديث جديد من المودل:*\n\n" + res["message"], parse_mode="Markdown")
                        with get_db() as conn:
                            conn.execute("UPDATE users SET last_hash=? WHERE chat_id=?", (h, u["chat_id"]))
        except: pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=broadcast_loop, daemon=True).start()
    bot.infinity_polling()
