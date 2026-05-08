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
ADMIN_ID = 7840931571  # معرف الآدمن الخاص بك

# جلب مفاتيح بينانس من متغيرات البيئة
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. إدارة قاعدة البيانات ---
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

# --- 2. منطق فحص الصلاحية ---
def check_access(chat_id):
    free_until = datetime(2026, 6, 1)
    if datetime.now() < free_until:
        return True, "فترة تجريبية"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT expiry_date, is_vip FROM users WHERE chat_id = ?', (chat_id,))
    res = c.fetchone()
    conn.close()

    if res:
        expiry_date, is_vip = res
        if is_vip == 1: return True, "عضوية VIP"
        if expiry_date:
            expiry = datetime.strptime(expiry_date, '%Y-%m-%d %H:%M:%S')
            if expiry > datetime.now(): return True, "اشتراك فعال"
    return False, None

# --- 3. التحقق من بينانس ---
def verify_binance_payment(txid, amount_expected=1.5):
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return False, "API Binance غير مهيأ"
    base_url = "https://api.binance.com"
    endpoint = "/sapi/v1/capital/deposit/hisrec"
    params = f"timestamp={int(time.time() * 1000)}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), params.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{base_url}{endpoint}?{params}&signature={signature}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        deposits = response.json()
        for d in deposits:
            if d.get('txId') == txid and float(d.get('amount')) >= amount_expected and d.get('status') == 1:
                return True, "نجاح"
    except: pass
    return False, "فشل"

# --- 4. محرك مودل ---
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
        if BeautifulSoup(login_response.text, 'html.parser').find('input', {'name': 'logintoken'}):
            return {"status": "login_failed", "message": "❌ بيانات الدخول خاطئة."}
        res = session.get(calendar_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        if not events: return {"status": "success", "message": "✅ لا توجد واجبات."}
        data_list = [f"📌 {e.get_text(separator=' | ', strip=True)}" for e in events]
        prompt = f"نظم هذه الواجبات بالعربية: {' '.join(data_list)[:3500]}"
        completion = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": prompt}])
        return {"status": "success", "message": completion.choices[0].message.content}
    except: return {"status": "error", "message": "⚠️ خطأ في المودل."}

# --- 5. لوحة تحكم الإدارة (الجديدة) ---
@bot.message_handler(commands=['panel'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    conn = get_db_connection()
    c = conn.cursor()
    total = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    vips = c.execute('SELECT COUNT(*) FROM users WHERE is_vip = 1').fetchone()[0]
    conn.close()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ إضافة VIP يدوي", callback_data="admin_add_vip"))
    bot.send_message(ADMIN_ID, f"📊 **إحصائيات البوت**\n\n👥 المستخدمين: {total}\n🌟 أعضاء VIP: {vips}", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_vip")
def admin_add_vip_step(call):
    msg = bot.send_message(ADMIN_ID, "أرسل الآن الـ ID الخاص بالمستخدم لمنحه VIP مدى الحياة:")
    bot.register_next_step_handler(msg, process_manual_vip)

def process_manual_vip(message):
    uid = message.text
    if uid.isdigit():
        conn = get_db_connection()
        conn.cursor().execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        conn.commit()
        conn.close()
        bot.send_message(ADMIN_ID, f"✅ تم تفعيل VIP مدى الحياة للمعرف: {uid}")
        try: bot.send_message(uid, "🌟 مبروك! منحك المدير اشتراك VIP مدى الحياة.")
        except: pass
    else: bot.send_message(ADMIN_ID, "❌ رقم ID غير صحيح.")

# --- 6. التعامل مع الدفع والرسائل ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "مرحباً! البوت مجاني لغاية 1/6/2026.\n/check للفحص | /subscribe للاشتراك.")

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    text = "💳 **التفعيل (5 شيكل):**\n1- جوال باي: `0597599642` (ارسل الصورة)\n2- بينانس Pay ID: `983969145` (ارسل الـ TXID)"
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['check'])
def handle_check(message):
    allowed, reason = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. /subscribe")
        return
    user_data = get_db_connection().cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    if user_data:
        bot.send_message(message.chat.id, f"🔍 جاري الفحص... ({reason})")
        bot.send_message(message.chat.id, run_moodle_task(user_data[0], user_data[1])["message"])
    else:
        msg = bot.send_message(message.chat.id, "ارسل الرقم الجامعي:")
        bot.register_next_step_handler(msg, process_username)

def process_username(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "ارسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda m: process_password(m, user))

def process_password(message, user):
    pwd = message.text
    res = run_moodle_task(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', (message.chat.id, user, pwd))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, "✅ تم الحفظ!\n" + res["message"])
    else: bot.send_message(message.chat.id, res["message"])

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 VIP", callback_data=f"act_{message.chat.id}_VIP"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"إيصال من `{message.chat.id}`", reply_markup=markup, parse_mode="Markdown")
    bot.reply_to(message, "⏳ وصل الإيصال، انتظر التفعيل.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_confirm(call):
    _, uid, mode = call.data.split('_')
    conn = get_db_connection()
    if mode == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        msg = "تفعيل VIP مدى الحياة"
    else:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date = ?, is_vip = 0 WHERE chat_id = ?', (new_exp, uid))
        msg = "تفعيل لمدة 30 يوم"
    conn.commit()
    conn.close()
    bot.send_message(uid, f"🎉 {msg} بنجاح!")
    bot.answer_callback_query(call.id, "تم التنفيذ")

@bot.message_handler(func=lambda m: len(m.text) > 40)
def handle_txid(message):
    txid = message.text.strip()
    success, _ = verify_binance_payment(txid)
    if success:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection(); conn.cursor().execute('UPDATE users SET expiry_date = ? WHERE chat_id = ?', (new_exp, message.chat.id))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم التأكد تلقائياً! اشتراكك فعال 30 يوم.")
    else: bot.send_message(message.chat.id, "❌ لم نجد العملية.")

# --- 7. المجدل ---
def scheduler_loop():
    schedule.every(6).hours.do(lambda: [bot.send_message(u[0], run_moodle_task(u[1], u[2])["message"]) 
                                       for u in get_db_connection().cursor().execute('SELECT chat_id, username, password FROM users').fetchall() 
                                       if check_access(u[0])[0]])
    while True:
        schedule.run_pending(); time.sleep(60)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    bot.infinity_polling()
