import os
import telebot
import requests
from bs4 import BeautifulSoup
from groq import Groq
import sqlite3
import time
import threading
import schedule
import hmac
import hashlib
from datetime import datetime, timedelta
from telebot import types

# --- الإعدادات الأساسية ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
BINANCE_PAY_ID = "983969145"

# جلب مفاتيح بينانس من Variables السيرفر
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. قاعدة البيانات ---
def init_db():
    os.makedirs('/app/data', exist_ok=True)
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False)

# --- 2. محرك المودل (Moodle Engine) ---
def run_moodle_task(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    calendar_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
    try:
        r = session.get(login_url, timeout=20)
        soup_login = BeautifulSoup(r.text, 'html.parser')
        token = soup_login.find('input', {'name': 'logintoken'})['value']
        login_data = {'username': user, 'password': pwd, 'logintoken': token}
        login_response = session.post(login_url, data=login_data, timeout=20)
        
        if "login" in login_response.url or BeautifulSoup(login_response.text, 'html.parser').find('input', {'name': 'logintoken'}):
            return {"status": "login_failed", "message": "❌ الرقم الجامعي أو كلمة المرور غير صحيحة."}
            
        res = session.get(calendar_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        if not events: return {"status": "success", "message": "✅ لا يوجد واجبات قادمة حالياً."}
        
        data_list = [f"📌 {e.get_text(separator=' | ', strip=True)}" for e in events]
        prompt = f"نظم هذه الواجبات بالعربية (المادة، الواجب، الموعد): {' '.join(data_list)[:3500]}"
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": prompt}])
        return {"status": "success", "message": completion.choices[0].message.content}
    except: return {"status": "error", "message": "⚠️ عذراً، موقع المودل لا يستجيب حالياً."}

# --- 3. فحص الصلاحية ---
def check_access(chat_id):
    if datetime.now() < datetime(2026, 6, 1): return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res:
        if res[1] == 1: return True, "VIP"
        if res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

# --- 4. لوحة التحكم (Admin Panel) ---
@bot.message_handler(commands=['panel'])
def admin_panel(message):
    if int(message.from_user.id) != int(ADMIN_ID): return
    conn = get_db_connection()
    total = conn.cursor().execute('SELECT COUNT(*) FROM users').fetchone()[0]
    vips = conn.cursor().execute('SELECT COUNT(*) FROM users WHERE is_vip = 1').fetchone()[0]
    conn.close()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ إضافة VIP يدوي", callback_data="admin_add_vip"))
    bot.send_message(ADMIN_ID, f"📊 **إحصائيات النظام**\n\n👥 الطلاب المسجلين: {total}\n🌟 أعضاء VIP: {vips}", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_vip")
def admin_add_vip_step(call):
    msg = bot.send_message(ADMIN_ID, "أرسل ID الطالب الآن:")
    bot.register_next_step_handler(msg, process_manual_vip)

def process_manual_vip(message):
    uid = message.text.strip()
    if uid.isdigit():
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (uid,))
        conn.cursor().execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        conn.commit(); conn.close()
        bot.send_message(ADMIN_ID, f"✅ تم تفعيل VIP للمعرف {uid}")
        try: bot.send_message(uid, "🌟 مبروك! منحك المدير اشتراك VIP مدى الحياة.")
        except: pass
    else: bot.send_message(ADMIN_ID, "❌ ID غير صالح.")

# --- 5. أوامر المستخدم والدفع ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "مرحباً بك في بوت مودل الأقصى! 🎓\nالبوت مجاني تماماً حتى شهر 6/2026.\nاستخدم /check لبدء فحص واجباتك.")

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    text = f"💳 **طرق التفعيل (5 شيكل):**\n\n1️⃣ جوال باي: `0597599642` (ارسل الصورة)\n2️⃣ بينانس Pay ID: `{BINANCE_PAY_ID}` (ارسل الـ TXID)"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['check'])
def handle_check(message):
    allowed, reason = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. للتجديد استخدم /subscribe")
        return
    user_data = get_db_connection().cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    if user_data:
        bot.send_message(message.chat.id, f"🔍 جاري فحص المودل... ({reason})")
        res = run_moodle_task(user_data[0], user_data[1])
        bot.send_message(message.chat.id, res["message"])
    else:
        msg = bot.send_message(message.chat.id, "يرجى إرسال **الرقم الجامعي**:")
        bot.register_next_step_handler(msg, process_username)

def process_username(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "يرجى إرسال **كلمة المرور**:")
    bot.register_next_step_handler(msg, lambda m: process_password(m, user))

def process_password(message, user):
    pwd = message.text
    bot.send_message(message.chat.id, "⏳ جاري التحقق من بياناتك...")
    res = run_moodle_task(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الحفظ بنجاح!\n\n" + res["message"])
    else: bot.send_message(message.chat.id, res["message"])

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 VIP", callback_data=f"act_{message.chat.id}_VIP"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال من `{message.chat.id}`", reply_markup=markup, parse_mode="Markdown")
    bot.reply_to(message, "⏳ تم استلام الصورة، سيتم تفعيلك قريباً.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_confirm(call):
    if int(call.from_user.id) != int(ADMIN_ID): return
    _, uid, mode = call.data.split('_')
    conn = get_db_connection()
    if mode == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        msg = "VIP مدى الحياة"
    else:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date = ?, is_vip = 0 WHERE chat_id = ?', (new_exp, uid))
        msg = "اشتراك شهر"
    conn.commit(); conn.close()
    try: bot.send_message(uid, f"🎉 مبروك! تم تفعيل {msg} بنجاح.")
    except: pass
    bot.answer_callback_query(call.id, "تم التفعيل")
    bot.edit_message_caption(call.message.caption + f"\n✅ تم التفعيل: {msg}", call.message.chat.id, call.message.message_id)

# --- 6. الجدولة ---
def auto_check_all():
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users').fetchall()
    conn.close()
    for u in users:
        if check_access(u[0])[0]:
            res = run_moodle_task(u[1], u[2])
            if res["status"] == "success" and "لا توجد واجبات" not in res["message"]:
                bot.send_message(u[0], "🔔 **تذكير بالواجبات:**\n\n" + res["message"])

def scheduler_loop():
    schedule.every(6).hours.do(auto_check_all)
    while True:
        schedule.run_pending(); time.sleep(60)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    bot.infinity_polling()
    
