import os, telebot, requests, sqlite3, threading, schedule, time
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- الإعدادات الأساسية ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
BINANCE_PAY_ID = "983969145"
FREE_TRIAL_END = datetime(2026, 6, 1)

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False  # وضع الإجازة الافتراضي

# --- 1. إدارة قاعدة البيانات ---
def init_db():
    os.makedirs('/app/data', exist_ok=True)
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)')
    columns = [
        ('username', 'TEXT'), ('password', 'TEXT'), 
        ('expiry_date', 'TEXT'), ('is_vip', 'INTEGER DEFAULT 0')
    ]
    for col, ctype in columns:
        try: c.execute(f'ALTER TABLE users ADD COLUMN {col} {ctype}')
        except: pass
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# --- 2. محرك المودل المتقدم (Scraper) ---
def run_moodle_engine(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    
    try:
        # تسجيل الدخول
        r = session.get(login_url, timeout=20)
        soup_login = BeautifulSoup(r.text, 'html.parser')
        token = soup_login.find('input', {'name': 'logintoken'})['value']
        login_res = session.post(login_url, data={'username': user, 'password': pwd, 'logintoken': token}, timeout=20)
        
        if "login" in login_res.url:
            return {"status": "fail", "message": "❌ بيانات الدخول للمودل خاطئة."}

        # جلب الفعاليات
        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res = session.get(cal_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        
        relevant_info = []
        for e in events:
            txt = e.get_text(separator=' ', strip=True)
            # فلترة: تخطي المهام المحلولة أو التي تم تسليمها
            if any(word in txt for word in ["تم التسليم", "محلول", "Submitted", "تخطى"]):
                continue
            relevant_info.append(txt)

        if not relevant_info:
            return {"status": "success", "message": "✅ لا توجد محاضرات جديدة أو تكاليف غير منجزة حالياً."}

        # تنظيم التقرير عبر الذكاء الاصطناعي بالترتيب المطلوب
        prompt = f"""
        أنت مساعد طالب في جامعة الأقصى. رتب البيانات التالية في تقرير عربي منظم جداً:
        1. المحاضرات الجديدة (إن وجدت).
        2. اللقاءات المباشرة (Zoom/BigBlueButton) - احذف أي لقاء فات موعده.
        3. الامتحانات القريبة (غير المحلولة).
        4. التكاليف والواجبات (غير المسلمة).
        
        البيانات: {' | '.join(relevant_info)}
        """
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}]
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except:
        return {"status": "error", "message": "⚠️ موقع المودل لا يستجيب."}

# --- 3. نظام التذكيرات ووضع الإجازة ---
def send_auto_reports():
    if IS_HOLIDAY: return
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    
    for uid, user, pwd in users:
        # فحص الصلاحية أولاً
        allowed, _ = check_access(uid)
        if allowed:
            res = run_moodle_engine(user, pwd)
            if res["status"] == "success":
                try: bot.send_message(uid, f"🔔 **تقريرك التلقائي اليومي:**\n\n{res['message']}", parse_mode="Markdown")
                except: pass

def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res:
        if res[1] == 1: return True, "VIP"
        if res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

# --- 4. معالجة الرسائل والأوامر ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🎓 **بوت مودل الأقصى المطور**\n\n- فحص تلقائي للمحاضرات والواجبات.\n- تنبيهات باللقاءات المباشرة.\n- تقارير ذكية للمهام غير المحلولة.\n\nاستخدم /check للفحص الآن.")

@bot.message_handler(commands=['holiday'])
def holiday_mode(message):
    global IS_HOLIDAY
    if message.from_user.id != ADMIN_ID: return
    if "on" in message.text:
        IS_HOLIDAY = True
        bot.reply_to(message, "🏝️ تم تفعيل وضع الإجازة (توقف الإشعارات).")
    else:
        IS_HOLIDAY = False
        bot.reply_to(message, "🚀 تم إيقاف وضع الإجازة (عادت الإشعارات).")

@bot.message_handler(commands=['check'])
def manual_check(message):
    allowed, reason = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. للتفعيل: /subscribe")
        return
    
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    
    if u:
        bot.send_message(message.chat.id, f"🔍 جاري فحص المحاضرات والمهام... ({reason})")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])
    else:
        msg = bot.send_message(message.chat.id, "أرسل الرقم الجامعي:")
        bot.register_next_step_handler(msg, get_user)

def get_user(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "أرسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda m: save_user(m, user))

def save_user(message, user):
    pwd = message.text
    bot.send_message(message.chat.id, "⏳ جاري التحقق من بيانات المودل...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الحفظ! إليك تقريرك:\n\n" + res["message"])
    else:
        bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 **تفعيل الاشتراك (5 شيكل):**\n\n1️⃣ جوال باي: `0597599642`\n2️⃣ بينانس ID: `{BINANCE_PAY_ID}`\n\nأرسل صورة الإيصال هنا بعد الدفع.")

@bot.message_handler(content_types=['photo'])
def handle_payment(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 VIP", callback_data=f"act_{message.chat.id}_VIP"))
    markup.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel_{message.chat.id}"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال من `{message.chat.id}`", reply_markup=markup)
    bot.reply_to(message, "⏳ تم استلام إيصالك، سيتم التفعيل قريباً.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('act_') or call.data.startswith('cancel_'))
def admin_action(call):
    if call.from_user.id != ADMIN_ID: return
    data = call.data.split('_')
    if data[0] == "cancel":
        bot.edit_message_caption("❌ تم رفض الطلب.", call.message.chat.id, call.message.message_id)
        return
    
    uid, mode = data[1], data[2]
    conn = get_db_connection()
    if mode == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip=1 WHERE chat_id=?', (uid,))
        txt = "VIP مدى الحياة"
    else:
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?', (exp, uid))
        txt = "شهر كامل"
    conn.commit(); conn.close()
    bot.send_message(uid, f"🎉 مبروك! تم تفعيل اشتراكك ({txt}).")
    bot.edit_message_caption(f"✅ تم التفعيل بنجاح ({txt}).", call.message.chat.id, call.message.message_id)

# --- 5. التشغيل المجدول ---
def scheduler_loop():
    # إرسال التقرير اليومي الساعة 8 صباحاً
    schedule.every().day.at("08:00").do(send_auto_reports)
    # فحص انتهاء الاشتراكات (لإرسال تنبيهات 5 أيام، يومين، يوم) - يمكنك إضافتها هنا
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    bot.infinity_polling()
