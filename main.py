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
ADMIN_ID = 7840931571  # تأكد من أن هذا هو ID حسابك الصحيح
BINANCE_PAY_ID = "983969145"

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

# --- 2. لوحة تحكم الإدارة (إصلاح شامل) ---
@bot.message_handler(commands=['panel'])
def admin_panel(message):
    # طباعة المعرف للتأكد في لوحة تحكم السيرفر
    print(f"User {message.from_user.id} tried to access panel. Admin is {ADMIN_ID}")
    
    if int(message.from_user.id) != int(ADMIN_ID):
        bot.reply_to(message, "❌ عذراً، هذا الأمر مخصص لمدير البوت فقط.")
        return 

    conn = get_db_connection()
    c = conn.cursor()
    total = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    vips = c.execute('SELECT COUNT(*) FROM users WHERE is_vip = 1').fetchone()[0]
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ إضافة VIP يدوي", callback_data="admin_add_vip"))
    
    bot.send_message(ADMIN_ID, f"📊 **لوحة تحكم المدير**\n\n👥 إجمالي المستخدمين: {total}\n🌟 أعضاء VIP: {vips}", 
                     reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_vip")
def admin_add_vip_step(call):
    msg = bot.send_message(ADMIN_ID, "أرسل الآن الـ ID الخاص بالمستخدم (رقم فقط):")
    bot.register_next_step_handler(msg, process_manual_vip)

def process_manual_vip(message):
    uid = message.text.strip()
    if uid.isdigit():
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (uid,))
        c.execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        conn.commit()
        conn.close()
        bot.send_message(ADMIN_ID, f"✅ تم تفعيل VIP مدى الحياة للمعرف: {uid}")
        try:
            bot.send_message(uid, "🌟 مبروك! لقد منحك المدير عضوية VIP مجانية مدى الحياة.")
        except Exception as e:
            bot.send_message(ADMIN_ID, f"⚠️ تم التفعيل لكن تعذر إرسال رسالة التهنئة للمستخدم (ربما قام بحظر البوت). الخطأ: {e}")
    else:
        bot.send_message(ADMIN_ID, "❌ رقم ID غير صحيح، أعد المحاولة من /panel")

# --- 3. إصلاح أزرار التفعيل التلقائي ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_confirm(call):
    if int(call.from_user.id) != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "ليس لديك صلاحية.")
        return

    _, uid, mode = call.data.split('_')
    conn = get_db_connection()
    c = conn.cursor()
    
    success_msg = ""
    if mode == "VIP":
        c.execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        success_msg = "اشتراك VIP مدى الحياة"
    else:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute('UPDATE users SET expiry_date = ?, is_vip = 0 WHERE chat_id = ?', (new_exp, uid))
        success_msg = "اشتراك لمدة شهر (30 يوم)"
    
    conn.commit()
    conn.close()

    # محاولة إرسال رسالة التهنئة
    try:
        bot.send_message(uid, f"🎉 مبروك! تم تفعيل {success_msg} في البوت بنجاح. يمكنك الآن استخدام /check")
        bot.answer_callback_query(call.id, "✅ تم التفعيل وإرسال رسالة للمستخدم")
    except Exception as e:
        bot.answer_callback_query(call.id, "⚠️ تم التفعيل ولكن فشل إرسال الرسالة للمستخدم.")
        print(f"Error sending congrats to {uid}: {e}")

    # تحديث رسالة الآدمن لكي تعرف أنك ضغطت الزر
    bot.edit_message_caption(call.message.caption + f"\n\n✅ الحالة: تم تفعيل {mode}", call.message.chat.id, call.message.message_id)

# --- 4. بقية الأوامر (Moodle, Check, Start) ---
# ... (ضع بقية الأوامر التي أرسلتها لك في الكود السابق هنا كما هي) ...

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "مرحباً بك! البوت مجاني لغاية 1/6/2026.\nاستخدم /check لفحص الواجبات.")

@bot.message_handler(commands=['check'])
def handle_check(message):
    # ... (دالة الفحص كما هي) ...
    pass

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 تفعيل VIP", callback_data=f"act_{message.chat.id}_VIP"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال جديد من `{message.chat.id}`", reply_markup=markup, parse_mode="Markdown")
    bot.reply_to(message, "⏳ تم استلام الصورة، سيتم التفعيل فور مراجعته.")

if __name__ == "__main__":
    init_db()
    print(f"🚀 البوت يعمل. الآدمن المسجل هو: {ADMIN_ID}")
    bot.infinity_polling()
    
