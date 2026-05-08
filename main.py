import os, telebot, requests, sqlite3, threading, schedule, time
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- 1. الإعدادات ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
BINANCE_PAY_ID = "983969145"
FREE_TRIAL_END = datetime(2026, 6, 1)

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False 

# --- 2. قاعدة البيانات (تعديل المسار ليعمل في Railway بسلاسة) ---
def init_db():
    conn = sqlite3.connect('users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit(); conn.close()

def get_db_connection():
    return sqlite3.connect('users.db', check_same_thread=False, timeout=20)

# --- 3. محرك المودل الذكي (تم تصحيح خطأ السطر 82) ---
def run_moodle_engine(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    
    try:
        r = session.get(login_url, timeout=20)
        token = BeautifulSoup(r.text, 'html.parser').find('input', {'name': 'logintoken'})['value']
        login_res = session.post(login_url, data={'username': user, 'password': pwd, 'logintoken': token}, timeout=20)
        
        if "login" in login_res.url:
            return {"status": "fail", "message": "❌ بيانات الدخول للمودل خاطئة."}

        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res = session.get(cal_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        
        lectures, meetings, exams, assignments = [], [], [], []

        for e in events:
            txt = e.get_text(separator=' ', strip=True)
            link_tag = e.find('a', href=True)
            link = link_tag['href'] if link_tag else ""
            
            if any(word in txt for word in ["تم التسليم", "محلول", "Submitted", "تخطى"]):
                continue

            if "assign" in link:
                assignments.append(txt)
            elif "quiz" in link or "اختبار" in txt:
                exams.append(txt)
            elif any(x in link for x in ["bigbluebutton", "zoom", "meet"]):
                meetings.append(txt)
            else:
                lectures.append(txt)

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا توجد تحديثات جديدة؛ كل شيء مكتمل!"}

        # تم تصحيح الجملة هنا (assignments if assignments else ...)
        prompt = f"""
        رتب التقرير التالي بأسلوب احترافي. التزم بالأقسام ولا تنقل أي عنصر من قسمه:
        
        📚 المحاضرات الجديدة: {lectures if lectures else 'لا يوجد'}
        🎥 اللقاءات المباشرة: {meetings if meetings else 'لا يوجد'}
        📝 الامتحانات القادمة: {exams if exams else 'لا يوجد'}
        ⚠️ التكاليف والواجبات المطلوبة: {assignments if assignments else 'لا يوجد'}
        """
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": "أنت منسق تقارير ملتزم بالتصنيفات المعطاة لك حرفياً."},
                      {"role": "user", "content": prompt}],
            temperature=0.0
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except Exception as e:
        return {"status": "error", "message": f"⚠️ خطأ في الاتصال بالمودل: {str(e)}"}

# --- 4. الصلاحيات ---
def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res:
        if res[1] == 1: return True, "VIP"
        if res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

# --- 5. التعامل مع الرسائل والآدمن ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🎓 بوت مودل الأقصى المطور جاهز للعمل!\n\nاستخدم /check للفحص.")

@bot.message_handler(commands=['holiday'])
def toggle_holiday(message):
    global IS_HOLIDAY
    if message.from_user.id != ADMIN_ID: return
    IS_HOLIDAY = "on" in message.text
    bot.reply_to(message, f"🏝️ وضع الإجازة: {'مفعل' if IS_HOLIDAY else 'معطل'}")

@bot.message_handler(commands=['check'])
def manual_check(message):
    allowed, _ = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 انتهى اشتراكك. /subscribe")
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
    bot.send_message(message.chat.id, "⏳ جاري التحقق...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط! \n\n" + res["message"])
    else: bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 التفعيل (5 شيكل):\nجوال باي: `0597599642`\nبينانس ID: `{BINANCE_PAY_ID}`\nارسل الإيصال هنا.")

@bot.message_handler(content_types=['photo'])
def handle_receipt(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ شهر", callback_data=f"act_{message.chat.id}_30"),
               types.InlineKeyboardButton("🌟 VIP", callback_data=f"act_{message.chat.id}_VIP"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"📩 إيصال: `{message.chat.id}`", reply_markup=markup)
    bot.reply_to(message, "⏳ جارٍ التفعيل...")

@bot.callback_query_handler(func=lambda call: call.data.startswith('act_'))
def admin_confirm(call):
    if call.from_user.id != ADMIN_ID: return
    _, uid, mode = call.data.split('_')
    conn = get_db_connection()
    if mode == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip=1 WHERE chat_id=?', (uid,))
        info = "VIP"
    else:
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?', (exp, uid))
        info = "شهر"
    conn.commit(); conn.close()
    bot.send_message(uid, f"🎉 تم تفعيل اشتراكك ({info}).")
    bot.answer_callback_query(call.id, "تم")

# --- 6. الجدولة ---
def auto_scheduler():
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
                try: bot.send_message(uid, f"🔔 تقريرك اليومي:\n\n{res['message']}", parse_mode="Markdown")
                except: pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_scheduler, daemon=True).start()
    bot.infinity_polling()
