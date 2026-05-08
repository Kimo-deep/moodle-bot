import os
import telebot
import requests
from bs4 import BeautifulSoup
from groq import Groq
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from telebot import types

# --- الإعدادات الأساسية ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
BINANCE_PAY_ID = "983969145"

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. إدارة قاعدة البيانات مع الإصلاح التلقائي ---
def init_db():
    os.makedirs('/app/data', exist_ok=True)
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    
    # إنشاء الجدول الأساسي
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT)''')
    
    # إصلاح ذكي: إضافة عمود is_vip إذا لم يكن موجوداً (حل مشكلة no such column)
    try:
        c.execute('ALTER TABLE users ADD COLUMN is_vip INTEGER DEFAULT 0')
        print("✅ تم تحديث قاعدة البيانات بنجاح.")
    except sqlite3.OperationalError:
        pass # العمود موجود مسبقاً
        
    conn.commit()
    conn.close()

def get_db_connection():
    # حل مشكلة database is locked عبر إضافة timeout
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

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

# --- 4. نظام التعامل مع الطلبات (الآدمن) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('act_') or call.data.startswith('cancel_'))
def handle_admin_requests(call):
    if int(call.from_user.id) != int(ADMIN_ID): return
    
    data = call.data.split('_')
    action = data[0] 
    uid = data[1]
    
    # خيار الإلغاء
    if action == "cancel":
        bot.answer_callback_query(call.id, "تم إلغاء الطلب")
        bot.edit_message_caption(call.message.caption + "\n\n❌ تم إلغاء الطلب من قبل المسؤول.", call.message.chat.id, call.message.message_id)
        return

    # خيارات التفعيل
    mode = data[2]
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # إنشاء المستخدم إذا لم يكن موجوداً
        c.execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (uid,))
        
        if mode == "VIP":
            c.execute('UPDATE users SET is_vip = 1, expiry_date = NULL WHERE chat_id = ?', (uid,))
            res_text = "VIP مدى الحياة"
        else:
            new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            c.execute('UPDATE users SET expiry_date = ?, is_vip = 0 WHERE chat_id = ?', (new_exp, uid))
            res_text = "اشتراك لمدة شهر"
        
        conn.commit()
        conn.close()
        
        # رسائل النجاح
        bot.send_message(ADMIN_ID, f"✅ نجح التفعيل بنجاح للمستخدم: `{uid}`\nنوع الاشتراك: {res_text}", parse_mode="Markdown")
        try: bot.send_message(uid, f"🎉 مبروك! قام المسؤول بتفعيل {res_text} لحسابك.")
        except: pass
            
        bot.answer_callback_query(call.id, "تم التفعيل")
        bot.edit_message_caption(call.message.caption + f"\n\n✅ الحالة: تم التفعيل ({res_text})", call.message.chat.id, call.message.message_id)

    except Exception as e:
        if conn: conn.close()
        bot.send_message(ADMIN_ID, f"❌ فشل التفعيل للمستخدم {uid}. الخطأ: {e}")

# --- 5. أوامر المستخدم ---
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
    
    conn = get_db_connection()
    user_data = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()

    if user_data and user_data[0]:
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
    markup.add(
        types.InlineKeyboardButton("✅ شهر", callback_data=f"act_{message.chat.id}_30"),
        types.InlineKeyboardButton("🌟 VIP", callback_data=f"act_{message.chat.id}_VIP")
    )
    markup.add(types.InlineKeyboardButton("❌ إلغاء الطلب", callback_data=f"cancel_{message.chat.id}"))
    
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال من `{message.chat.id}`", reply_markup=markup, parse_mode="Markdown")
    bot.reply_to(message, "⏳ تم استلام الصورة، سيتم تفعيلك قريباً.")

if __name__ == "__main__":
    init_db()
    bot.infinity_polling()
