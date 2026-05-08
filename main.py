import os, telebot, requests, sqlite3, threading, schedule, time
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- 1. الإعدادات والروابط (تأكد من صحتها) ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
BINANCE_PAY_ID = "983969145"
FREE_TRIAL_END = datetime(2026, 6, 1)

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False  # وضع الإجازة الافتراضي

# --- 2. إدارة قاعدة البيانات ---
def init_db():
    # إنشاء مجلد البيانات إذا لم يكن موجوداً (مهم للسيرفر)
    if not os.path.exists('/app/data'):
        os.makedirs('/app/data', exist_ok=True)
        
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, 
                  username TEXT, 
                  password TEXT, 
                  expiry_date TEXT, 
                  is_vip INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# --- 3. محرك المودل والذكاء الاصطناعي (الفرز والترتيب) ---
def run_moodle_engine(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    
    try:
        # تسجيل الدخول للمودل
        r = session.get(login_url, timeout=20)
        token = BeautifulSoup(r.text, 'html.parser').find('input', {'name': 'logintoken'})['value']
        login_res = session.post(login_url, data={'username': user, 'password': pwd, 'logintoken': token}, timeout=20)
        
        if "login" in login_res.url:
            return {"status": "fail", "message": "❌ بيانات الدخول للمودل خاطئة."}

        # جلب الفعاليات القادمة
        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res = session.get(cal_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        
        relevant_info = []
        for e in events:
            txt = e.get_text(separator=' ', strip=True)
            # فلترة: تخطي أي مهمة مكتوب بجانبها أنها محلولة أو مسلمة
            if any(word in txt for word in ["تم التسليم", "محلول", "Submitted", "تخطى"]):
                continue
            relevant_info.append(txt)

        if not relevant_info:
            return {"status": "success", "message": "✅ لا توجد تحديثات جديدة؛ جميع مهامك منجزة!"}

        # إرسال البيانات للذكاء الاصطناعي مع تعليمات الترتيب الصارمة
        prompt = f"""
        رتب هذه البيانات الأكاديمية لطالب جامعة الأقصى في تقرير منظم جداً:
        
        الترتيب الإجباري للتقرير:
        1. المحاضرات الجديدة (فقط الدروس المرفوعة).
        2. اللقاءات المباشرة (روابط زووم/ميت القادمة - احذف أي موعد فات).
        3. الامتحانات (الاختبارات والـ Quizzes القادمة).
        4. التكاليف والواجبات (التي لم تسلم بعد).
        
        ملاحظة: لا تخلط بين الأقسام. إذا كان القسم فارغاً لا تذكره.
        
        البيانات الخام:
        {' | '.join(relevant_info)}
        """
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "أنت سكرتير أكاديمي دقيق جداً لا يخلط بين أنواع المهام."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1 # لضمان عدم الهلوسة أو الخلط
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except:
        return {"status": "error", "message": "⚠️ عذراً، موقع المودل لا يستجيب حالياً."}

# --- 4. فحص الصلاحية ووضع الإجازة ---
def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res:
        if res[1] == 1: return True, "VIP"
        if res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

@bot.message_handler(commands=['holiday'])
def toggle_holiday(message):
    global IS_HOLIDAY
    if message.from_user.id != ADMIN_ID: return
    IS_HOLIDAY = "on" in message.text
    bot.reply_to(message, f"🏝️ تم {'تفعيل' if IS_HOLIDAY else 'إيقاف'} وضع الإجازة.")

# --- 5. أوامر البوت الأساسية ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "🎓 **مرحباً بك في بوت مودل الأقصى الذكي**\n\nأنا هنا لأرتب لك محاضراتك، لقاءاتك، وواجباتك غير المحلولة.\n\nاستخدم /check للبدء.")

@bot.message_handler(commands=['check'])
def manual_check(message):
    allowed, _ = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. للتفعيل: /subscribe")
        return
    
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    
    if u:
        bot.send_message(message.chat.id, "🔍 جاري الفحص والترتيب...")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])
    else:
        msg = bot.send_message(message.chat.id, "أرسل الرقم الجامعي:")
        bot.register_next_step_handler(msg, process_user_step)

def process_user_step(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "أرسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda m: finish_registration(m, user))

def finish_registration(message, user):
    pwd = message.text
    bot.send_message(message.chat.id, "⏳ جاري التحقق من البيانات...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط بنجاح! إليك تقريرك:\n\n" + res["message"])
    else:
        bot.send_message(message.chat.id, res["message"])

# --- 6. نظام الدفع والاشتراك ---
@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 **لتفعيل الاشتراك (5 شيكل):**\n\n1️⃣ جوال باي: `0597599642`\n2️⃣ بينانس ID: `{BINANCE_PAY_ID}`\n\nارسل صورة الإيصال هنا.")

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 تفعيل VIP", callback_data=f"act_{message.chat.id}_VIP"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال من: `{message.chat.id}`", reply_markup=markup)
    bot.reply_to(message, "⏳ تم استلام إيصالك، سيتم التفعيل من قبل الإدارة فوراً.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_confirm(call):
    if call.from_user.id != ADMIN_ID: return
    _, uid, mode = call.data.split('_')
    conn = get_db_connection()
    if mode == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip=1 WHERE chat_id=?', (uid,))
        info = "VIP مدى الحياة"
    else:
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?', (exp, uid))
        info = "شهر واحد"
    conn.commit(); conn.close()
    bot.send_message(uid, f"🎉 مبروك! تم تفعيل اشتراكك ({info}).")
    bot.answer_callback_query(call.id, "تم التفعيل")

# --- 7. الجدولة التلقائية ---
def auto_scheduler():
    # إرسال التقرير اليومي الساعة 8:00 صباحاً
    schedule.every().day.at("08:00").do(daily_broadcast)
    while True:
        schedule.run_pending()
        time.sleep(60)

def daily_broadcast():
    if IS_HOLIDAY: return
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    for uid, user, pwd in users:
        allowed, _ = check_access(uid)
        if allowed:
            res = run_moodle_engine(user, pwd)
            if res["status"] == "success":
                try: bot.send_message(uid, f"🔔 **تقرير الصباح المنظم:**\n\n{res['message']}", parse_mode="Markdown")
                except: pass

# --- التشغيل ---
if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_scheduler, daemon=True).start()
    bot.infinity_polling()
