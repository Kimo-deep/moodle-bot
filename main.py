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

# ══════════════════════════════════════════════════════════
# 3. محرك الـ Upcoming الأصلي
# ══════════════════════════════════════════════════════════
def run_moodle(username, password) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    try:
        # تسجيل الدخول
        login_pg = session.get("https://moodle.alaqsa.edu.ps/login/index.php", timeout=20).text
        soup_login = BeautifulSoup(login_pg, "html.parser")
        token = soup_login.find("input", {"name": "logintoken"})
        if not token: return {"status": "error", "message": "⚠️ المودل لا يستجيب."}
        
        resp = session.post("https://moodle.alaqsa.edu.ps/login/index.php", 
                            data={"username": username, "password": password, "logintoken": token["value"]}, timeout=20)
        
        if "login" in resp.url: return {"status": "fail", "message": "❌ بياناتك خاطئة."}

        # جلب المهام القادمة (Upcoming)
        up_html = session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming", timeout=20).text
        soup = BeautifulSoup(up_html, "html.parser")
        
        event_items = soup.find_all("div", class_="event")
        if not event_items:
            return {"status": "success", "message": "✅ لا توجد مهام قادمة حالياً."}

        report = [f"🕐 *تقرير المهام القادمة: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n"]

        for item in event_items:
            # اسم المهمة
            name = item.find("h3").get_text(strip=True) if item.find("h3") else "مهمة غير معروفة"
            
            # استخراج اسم المادة والوقت
            details = item.find_all("div", class_="row")
            course = "غير محدد"
            due_time = "غير محدد"
            
            for row in details:
                text = row.get_text().strip()
                if "المساق" in text or "Course" in text:
                    course = text.split("المساق")[-1].strip() if "المساق" in text else text.split("Course")[-1].strip()
                if "متى" in text or "When" in text:
                    due_time = text.split("متى")[-1].strip() if "متى" in text else text.split("When")[-1].strip()

            report.append(f"▪️ *{name}*\n   📌 {course}\n   📅 {due_time}")

        return {"status": "success", "message": "\n\n".join(report)}
    except Exception as e:
        log.error(f"Moodle Error: {e}")
        return {"status": "error", "message": "⚠️ حدث خطأ أثناء الاتصال بالمودل."}

# ══════════════════════════════════════════════════════════
# 4. دوال البوت الأساسية
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص المهام", "📊 حالتي")
    bot.send_message(m.chat.id, "🎓 أهلاً بك في بوت مودل الأقصى (النسخة الأصلية).", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص المهام")
def bot_check(m):
    with get_db() as conn:
        row = conn.execute("SELECT username, password FROM users WHERE chat_id=?", (m.chat.id,)).fetchone()
    
    if row:
        wait = bot.send_message(m.chat.id, "🔄 جاري جلب المهام القادمة...")
        res = run_moodle(row["username"], row["password"])
        bot.edit_message_text(res["message"], m.chat.id, wait.message_id, parse_mode="Markdown")
    else:
        bot.send_message(m.chat.id, "📧 يرجى إرسال رقمك الجامعي للربط:")
        bot.register_next_step_handler(m, _reg_user)

def _reg_user(m):
    u = m.text
    bot.send_message(m.chat.id, "🔐 الآن أرسل كلمة المرور:")
    bot.register_next_step_handler(m, lambda msg: _reg_fin(msg, u))

def _reg_fin(m, u):
    p = m.text
    wait = bot.send_message(m.chat.id, "⚙️ يتم التحقق...")
    res = run_moodle(u, p)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)", (m.chat.id, u, p))
        bot.send_message(m.chat.id, "✅ تم الربط بنجاح!\n\n" + res["message"], parse_mode="Markdown")
    else:
        bot.send_message(m.chat.id, res["message"])

if __name__ == "__main__":
    init_db()
    log.info("Bot is running...")
    bot.infinity_polling()
