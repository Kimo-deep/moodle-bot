import os
import telebot
import requests
from bs4 import BeautifulSoup
from groq import Groq
import sqlite3
import time
import threading
import schedule
from datetime import datetime, timedelta
from telebot import types

# --- الإعدادات ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
FREE_TRIAL_END = datetime(2026, 6, 1) # تاريخ انتهاء الفترة التجريبية

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- إدارة قاعدة البيانات ---
def init_db():
    os.makedirs('/app/data', exist_ok=True)
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)')
    columns = [('username', 'TEXT'), ('password', 'TEXT'), ('expiry_date', 'TEXT'), ('is_vip', 'INTEGER DEFAULT 0'), ('last_reminded', 'TEXT')]
    for col, ctype in columns:
        try: c.execute(f'ALTER TABLE users ADD COLUMN {col} {ctype}')
        except: pass
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# --- نظام التذكيرات التلقائي ---
def send_reminders():
    print("🔍 جاري فحص الاشتراكات لإرسال التذكيرات...")
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, expiry_date, is_vip FROM users').fetchall()
    now = datetime.now()

    for uid, exp_str, is_vip in users:
        if is_vip: continue # الـ VIP لا يحتاج تذكير
        
        # 1. تذكير الفترة التجريبية (للجميع)
        days_to_trial = (FREE_TRIAL_END - now).days
        if days_to_trial in [7, 3, 1] and now < FREE_TRIAL_END:
            try: bot.send_message(uid, f"📢 تنبيه: متبقي {days_to_trial} أيام على انتهاء الفترة التجريبية المجانية. بادر بالاشتراك لضمان استمرار الخدمة! /subscribe")
            except: pass

        # 2. تذكير اشتراك الـ 30 يوم
        if exp_str:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S')
            days_left = (exp_date - now).days
            
            msg = ""
            if days_left == 5: msg = "⏳ متبقي 5 أيام على انتهاء اشتراكك. يمكنك التجديد الآن لإضافة المدة الجديدة لاشتراكك الحالي."
            elif days_left == 2: msg = "⚠️ متبقي يومان فقط على انتهاء اشتراكك. لا تنسَ التجديد لكي لا تتوقف الخدمة."
            elif days_left == 1: msg = "🚨 ينتهي اشتراكك غداً! يرجى إرسال إيصال الدفع لتجنب انقطاع فحص المودل."
            
            if msg:
                try: bot.send_message(uid, msg + "\nللتجديد استخدم الأمر: /subscribe")
                except: pass
    conn.close()

# تشغيل المجدل في خلفية البوت
def run_scheduler():
    schedule.every().day.at("10:00").do(send_reminders) # يفحص يومياً الساعة 10 صباحاً
    while True:
        schedule.run_pending()
        time.sleep(60)

# --- محرك المودل وفحص الصلاحية (نفس الكود السابق مع تحسينات بسيطة) ---
def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res:
        if res[1] == 1: return True, "VIP"
        if res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

# --- الأوامر والتعامل مع الآدمن ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('act_') or call.data.startswith('cancel_'))
def handle_admin_requests(call):
    if int(call.from_user.id) != int(ADMIN_ID): return
    data = call.data.split('_')
    action, uid = data[0], data[1]
    
    if action == "cancel":
        bot.edit_message_caption(call.message.caption + "\n\n❌ تم إلغاء الطلب.", call.message.chat.id, call.message.message_id)
        return

    mode = data[2]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (uid,))
    
    if mode == "VIP":
        c.execute('UPDATE users SET is_vip = 1 WHERE chat_id = ?', (uid,))
        res_text = "VIP مدى الحياة"
    else:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute('UPDATE users SET expiry_date = ?, is_vip = 0 WHERE chat_id = ?', (new_exp, uid))
        res_text = "اشتراك شهر"
    
    conn.commit(); conn.close()
    bot.send_message(ADMIN_ID, f"✅ تم تفعيل {res_text} لـ `{uid}`"); bot.answer_callback_query(call.id, "تم")
    try: bot.send_message(uid, f"🎉 مبروك! تم تفعيل {res_text} لحسابك.")
    except: pass
    bot.edit_message_caption(call.message.caption + f"\n\n✅ مفعل: {res_text}", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🎓 بوت مودل الأقصى المطور\nفحص تلقائي وإشعارات بالواجبات.\n\nاستخدم /check للبدء.")

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, "💳 للتفعيل أرسل 5 شيكل لـ:\n1️⃣ جوال باي: `0597599642`\n2️⃣ بينانس: `983969145`\nثم أرسل صورة الإيصال هنا.")

@bot.message_handler(commands=['check'])
def handle_check(message):
    allowed, _ = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 اشتراكك منتهي. اشترك الآن للاستمرار /subscribe")
        return
    # ... (بقية كود فحص المودل كما هو)

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 VIP", callback_data=f"act_{message.chat.id}_VIP"))
    markup.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel_{message.chat.id}"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال جديد من `{message.chat.id}`", reply_markup=markup)
    bot.reply_to(message, "⏳ جارٍ التحقق من إيصالك..")

# --- التشغيل النهائي ---
if __name__ == "__main__":
    init_db()
    # تشغيل نظام التذكيرات في "خيط" منفصل لكي لا يتوقف البوت
    threading.Thread(target=run_scheduler, daemon=True).start()
    bot.infinity_polling()
