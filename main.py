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
ADMIN_ID = 7840931571  # !!! استبدله بـ ID حسابك الشخصي !!!

# جلب مفاتيح بينانس من متغيرات البيئة (للحماية)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. إدارة قاعدة البيانات ---
def init_db():
    os.makedirs('/app/data', exist_ok=True)
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    # تحديث الجدول ليشمل تاريخ الانتهاء وحالة الـ VIP
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False)

# --- 2. منطق فحص الصلاحية والاشتراك ---
def check_access(chat_id):
    # مجاني لغاية 1/6/2026
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

# --- 3. محرك بينانس للتحقق التلقائي ---
def verify_binance_payment(txid, amount_expected=1.5):
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return False, "API Binance غير مهيأ في السيرفر"
    
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
    return False, "لم يتم العثور على العملية"

# --- 4. محرك سحب البيانات (Moodle) ---
def run_moodle_task(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    calendar_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"

    try:
        r = session.get(login_url, timeout=20)
        soup_login = BeautifulSoup(r.text, 'html.parser')
        token_input = soup_login.find('input', {'name': 'logintoken'})
        if not token_input: return {"status": "error", "message": "⚠️ تعذر الاتصال بالمودل."}

        login_data = {'username': user, 'password': pwd, 'logintoken': token_input['value']}
        login_response = session.post(login_url, data=login_data, timeout=20)
        
        if BeautifulSoup(login_response.text, 'html.parser').find('input', {'name': 'logintoken'}):
            return {"status": "login_failed", "message": "❌ بيانات الدخول خاطئة."}

        res = session.get(calendar_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})

        if not events:
            return {"status": "success", "message": "✅ لا توجد واجبات حالياً."}

        data_list = [f"📌 {e.get_text(separator=' | ', strip=True)}" for e in events]
        final_text = "\n\n".join(data_list)

        prompt = f"نظم الواجبات التالية (اسم المادة، الواجب، الموعد) بالعربية: {final_text[:4000]}"
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except:
        return {"status": "error", "message": "⚠️ حدث خطأ فني في المودل."}

# --- 5. التعامل مع الرسائل والأوامر ---

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "مرحباً بك! البوت مجاني حتى 1/6/2026.\nاستخدم /check لفحص الواجبات أو /subscribe للاشتراك.")

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    text = "💳 **طرق التفعيل (5 شيكل شهرياً):**\n\n"
    text += "1️⃣ **جوال باي:** حول لـ `0597599642` وارسل صورة الإيصال هنا.\n"
    text += "2️⃣ **بينانس (تلقائي):** حول `1.5 USDT` لـ Pay ID `8702727538` ثم ارسل **رقم العملية (TXID)** هنا مباشرة."
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['check'])
def handle_check(message):
    allowed, reason = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. يرجى التجديد عبر /subscribe")
        return

    user_data = sqlite3.connect('/app/data/users.db').cursor().execute(
        'SELECT username, password FROM users WHERE chat_id = ?', (message.chat.id,)
    ).fetchone()

    if user_data:
        bot.send_message(message.chat.id, f"🔍 جاري الفحص... ({reason})")
        result = run_moodle_task(user_data[0], user_data[1])
        bot.send_message(message.chat.id, result["message"])
    else:
        msg = bot.send_message(message.chat.id, "يرجى إرسال **الرقم الجامعي** أولاً:")
        bot.register_next_step_handler(msg, process_username)

def process_username(message):
    username = message.text
    msg = bot.send_message(message.chat.id, "أرسل **كلمة المرور**:")
    bot.register_next_step_handler(msg, lambda m: process_password(m, username))

def process_password(message, username):
    password = message.text
    bot.send_message(message.chat.id, "⏳ جاري التحقق...")
    result = run_moodle_task(username, password)
    if result["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', 
                             (message.chat.id, username, password))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, "✅ تم الحفظ! " + result["message"])
    else:
        bot.send_message(message.chat.id, result["message"])

# --- 6. استقبال الإيصالات (جوال باي) والتحقق التلقائي (بينانس) ---

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    bot.reply_to(message, "⏳ تم استلام الصورة، بانتظار تفعيل المسؤول...")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ تفعيل 30 يوم", callback_data=f"act_{message.chat.id}_30"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"إيصال من: `{message.chat.id}`", 
                   reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: len(m.text) > 40) # استقبال الـ TXID
def handle_txid(message):
    txid = message.text.strip()
    bot.reply_to(message, "🔍 جاري التحقق من عملية بينانس...")
    success, _ = verify_binance_payment(txid)
    if success:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection()
        conn.cursor().execute('UPDATE users SET expiry_date = ? WHERE chat_id = ?', (new_exp, message.chat.id))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, "✅ تم التأكد! تفعيلك فعال لـ 30 يوم.")
    else:
        bot.send_message(message.chat.id, "❌ لم نجد عملية بهذا الرقم، تأكد من الـ TXID.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_confirm(call):
    _, uid, days = call.data.split('_')
    new_exp = (datetime.now() + timedelta(days=int(days))).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    conn.cursor().execute('UPDATE users SET expiry_date = ? WHERE chat_id = ?', (new_exp, uid))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"🎉 تم تفعيل حسابك لمدة {days} يوم!")
    bot.answer_callback_query(call.id, "تم التفعيل")

# --- 7. المجدل (Scheduler) ---
def auto_check():
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users').fetchall()
    conn.close()
    for u in users:
        allowed, _ = check_access(u[0])
        if allowed:
            res = run_moodle_task(u[1], u[2])
            if res["status"] == "success" and "لا توجد واجبات" not in res["message"]:
                bot.send_message(u[0], "🔔 واجبات جديدة:\n\n" + res["message"])

def scheduler_loop():
    schedule.every(6).hours.do(auto_check)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("🚀 البوت يعمل بكفاءة...")
    bot.infinity_polling()
