import telebot
from telebot import types
import requests
from bs4 import BeautifulSoup
import sqlite3
import schedule
import time
import threading
from groq import Groq
from datetime import datetime, timedelta
import os

# --- الإعدادات ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  # !!! ضع الـ ID الخاص بك هنا لكي تصلك الإيصالات وتتحكم بالبوت !!!

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. قاعدة البيانات (نسخة مطورة) ---
def init_db():
    # تأكد من استخدام المسار الصحيح للـ Volume في Railway
    db_path = '/app/data/users.db'
    if not os.path.exists('/app/data'):
        os.makedirs('/app/data')
        
    conn = sqlite3.connect(db_path, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

# --- 2. منطق فحص الصلاحية ---
def check_access(chat_id):
    # الفترة المجانية لغاية 1/6/2026
    free_until = datetime(2026, 6, 1)
    if datetime.now() < free_until:
        return True, "تجريبي مجاني"

    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('SELECT expiry_date, is_vip FROM users WHERE chat_id = ?', (chat_id,))
    res = c.fetchone()
    conn.close()

    if res:
        expiry_date, is_vip = res
        if is_vip == 1: return True, "عضوية VIP"
        if expiry_date:
            expiry = datetime.strptime(expiry_date, '%Y-%m-%d %H:%M:%S')
            if expiry > datetime.now(): return True, "اشتراك مدفوع"
    
    return False, None

# --- 3. التعامل مع إيصالات الدفع (للمسؤول) ---
@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    user_id = message.chat.id
    user_name = message.from_user.first_name
    
    bot.reply_to(message, "⏳ وصل الإيصال! سيتم تفعيل حسابك فور مراجعته من قبل الإدارة.")
    
    markup = types.InlineKeyboardMarkup()
    btn_month = types.InlineKeyboardButton("✅ تفعيل 30 يوم", callback_data=f"act_{user_id}_30")
    btn_free = types.InlineKeyboardButton("🎁 VIP مجاني", callback_data=f"vip_{user_id}")
    btn_rej = types.InlineKeyboardButton("❌ رفض", callback_data=f"rej_{user_id}")
    markup.add(btn_month, btn_free, btn_rej)
    
    caption = f"📩 **إيصال جديد!**\n👤: {user_name}\n🆔: `{user_id}`"
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith(('act_', 'vip_', 'rej_')))
def admin_callback(call):
    data = call.data.split('_')
    action, uid = data[0], data[1]
    conn = sqlite3.connect('/app/data/users.db')
    c = conn.cursor()
    
    if action == "act":
        days = int(data[2])
        exp = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute('UPDATE users SET expiry_date = ? WHERE chat_id = ?', (exp, uid))
        bot.send_message(uid, f"🎉 تم تفعيل اشتراكك لمدة {days} يوم!")
    elif action == "vip":
        c.execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        bot.send_message(uid, "🌟 تم منحك العضوية الدائمة مجاناً!")
    elif action == "rej":
        bot.send_message(uid, "❌ تم رفض الإيصال، تأكد من الدفع مجدداً.")
        
    conn.commit()
    conn.close()
    bot.answer_callback_query(call.id, "تم التنفيذ")

# --- 4. أوامر البوت (start, subscribe, check) ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "مرحباً بك في بوت المودل! 🎓\nالبوت مجاني تماماً حتى 1/6/2026.\nاستخدم /subscribe لمعرفة طرق الدفع للمستقبل.")

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    text = "💳 **طرق الاشتراك (5 شيكل شهرياً):**\n\n"
    text += "1️⃣ **جوال باي:** حول لـ `059xxxxxxx` وارسل الصورة.\n"
    text += "2️⃣ **بينانس:** حول لـ Pay ID `12345678` وارسل الصورة.\n\n"
    text += "🎁 البوت مجاني حالياً لجميع الطلاب."
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# --- 5. وظيفة سحب البيانات (المعدلة) ---
def run_moodle_task(chat_id, username, password):
    # (هنا تضع كود BeautifulSoup و Groq الخاص بك من الملف الأصلي)
    # تأكد من استخدامه داخل دالة check_access
    pass

@bot.message_handler(commands=['check'])
def handle_check(message):
    allowed, reason = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. يرجى التجديد عبر /subscribe")
        return
    
    bot.send_message(message.chat.id, f"🔍 جاري الفحص... ({reason})")
    # استدعاء دالة الفحص هنا...

# --- تشغيل البوت ---
if __name__ == "__main__":
    init_db()
    bot.infinity_polling()
