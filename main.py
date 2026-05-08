import telebot
from telebot import types
import requests
import sqlite3
import os
import hashlib
import hmac
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from groq import Groq

# --- الإعدادات (تستدعى من Variables السيرفر للأمان) ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  # !!! ضع الـ ID الخاص بك هنا !!!
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. قاعدة البيانات ---
def init_db():
    db_path = '/app/data/users.db'
    if not os.path.exists('/app/data'): os.makedirs('/app/data')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

# --- 2. وظيفة التحقق من بينانس (تلقائي) ---
def verify_binance_payment(txid, amount_expected=1.5):
    """التحقق من وصول حوالة USDT عبر Binance API"""
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return False, "API غير مهيأ"
    
    base_url = "https://api.binance.com"
    endpoint = "/sapi/v1/capital/deposit/hisrec"
    params = f"timestamp={int(time.time() * 1000)}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), params.encode(), hashlib.sha256).hexdigest()
    
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{base_url}{endpoint}?{params}&signature={signature}"
    
    try:
        response = requests.get(url, headers=headers)
        deposits = response.json()
        for d in deposits:
            # التحقق من رقم العملية والمبلغ والحالة (1 تعني نجاح)
            if d.get('txId') == txid and float(d.get('amount')) >= amount_expected and d.get('status') == 1:
                return True, "تم التأكد"
    except Exception as e:
        print(f"Error Binance: {e}")
    return False, "لم يتم العثور على العملية"

# --- 3. فحص الصلاحية ---
def check_access(chat_id):
    if datetime.now() < datetime(2026, 6, 1): return True, "مجاني"
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('SELECT expiry_date, is_vip FROM users WHERE chat_id = ?', (chat_id,))
    res = c.fetchone()
    conn.close()
    if res and (res[1] == 1 or (res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now())):
        return True, "مشترك"
    return False, None

# --- 4. استقبال الصور والـ TXID ---
@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    bot.reply_to(message, "⏳ جارٍ إرسال الإيصال للمسؤول للمراجعة (جوال باي).")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"act_{message.chat.id}_30"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"إيصال من {message.chat.id}", reply_markup=markup)

@bot.message_handler(func=lambda m: len(m.text) > 50) # بافتراض أن الـ TXID طويل
def handle_txid(message):
    txid = message.text.strip()
    bot.reply_to(message, "🔍 جارٍ التحقق تلقائياً من Binance...")
    success, reason = verify_binance_payment(txid)
    
    if success:
        new_expiry = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect('/app/data/users.db')
        c = conn.cursor()
        c.execute('UPDATE users SET expiry_date = ? WHERE chat_id = ?', (new_expiry, message.chat.id))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, "✅ تم تأكيد الدفع تلقائياً! اشتراكك فعال لمدة 30 يوم.")
    else:
        bot.send_message(message.chat.id, f"❌ فشل التحقق: {reason}. تأكد من رقم العملية.")

# --- 5. التحكم بالآدمن ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_activate(call):
    _, uid, days = call.data.split('_')
    new_expiry = (datetime.now() + timedelta(days=int(days))).strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect('/app/data/users.db')
    c.cursor().execute('UPDATE users SET expiry_date = ? WHERE chat_id = ?', (new_expiry, uid))
    conn.commit()
    conn.close()
    bot.send_message(uid, "🎉 تم تفعيل حسابك من قبل الإدارة!")
    bot.answer_callback_query(call.id, "تم التفعيل")

@bot.message_handler(commands=['subscribe'])
def sub(message):
    text = "💳 **طرق الدفع:**\n1- جوال باي: حول لـ `0597599642` وارسل الصورة.\n2- بينانس: حول لـ Pay ID `983969145` وارسل **رقم العملية (TXID)** هنا للتحقق التلقائي."
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['check'])
def run_check(message):
    allowed, _ = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهت الفترة المجانية، اشترك الآن /subscribe")
        return
    bot.send_message(message.chat.id, "🚀 جاري فحص الواجبات...")
    # (تكملة كود المودل الخاص بك هنا)

if __name__ == "__main__":
    init_db()
    bot.infinity_polling()
