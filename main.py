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

# --- 2. قاعدة البيانات ---
def init_db():
    conn = sqlite3.connect('users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit(); conn.close()

def get_db_connection():
    return sqlite3.connect('users.db', check_same_thread=False, timeout=20)

# --- 3. محرك المودل الذكي (منطق فرز جديد وصارم) ---
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
        
        # قوائم التصنيف
        lectures, meetings, exams, assignments = [], [], [], []

        for e in events:
            txt = e.get_text(separator=' ', strip=True).lower()
            link_tag = e.find('a', href=True)
            link = link_tag['href'].lower() if link_tag else ""
            
            # 1. تخطي المهام المحلولة
            if any(word in txt for word in ["تم التسليم", "محلول", "submitted", "تخطى", "سلمت"]):
                continue

            # 2. الفرز الصارم (بناءً على الكلمات والروابط معاً)
            
            # قسم الامتحانات والكويزات
            if "quiz" in link or any(word in txt for word in ["اختبار", "امتحان", "كويز", "quiz", "test"]):
                exams.append(txt.capitalize())
            
            # قسم اللقاءات المباشرة
            elif any(x in link for x in ["bigbluebutton", "zoom", "meet"]) or "لقاء" in txt:
                meetings.append(txt.capitalize())
            
            # قسم التكاليف والواجبات (الأولوية هنا للكلمات المفتاحية الصريحة)
            elif "assign" in link or any(word in txt for word in ["تكليف", "واجب", "نشرط", "مهمة", "assignment", "task"]):
                assignments.append(txt.capitalize())
            
            # قسم المحاضرات (أي شيء متبقي)
            else:
                lectures.append(txt.capitalize())

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا يوجد مهام معلقة؛ استمتع بوقتك!"}

        # إرسال البيانات المفرزة للذكاء الاصطناعي للصياغة الجمالية فقط
        prompt = f"""
        رتب هذا التقرير الأكاديمي للطالب. اتبع التقسيمات المعطاة لك بدقة ولا تغير تصنيف أي عنصر:
        
        1️⃣ المحاضرات الجديدة: {lectures if lectures else 'لا يوجد حالياً'}
        2️⃣ اللقاءات المباشرة: {meetings if meetings else 'لا يوجد حالياً'}
        3️⃣ الاختبارات والكويزات: {exams if exams else 'لا يوجد حالياً'}
        4️⃣ التكاليف والواجبات: {assignments if assignments else 'لا يوجد حالياً'}
        
        ملاحظة: إذا كان هناك موعد لقاء فات وقته، لا تذكره. استخدم ايموجيات مناسبة.
        """
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": "أنت منسق بيانات أكاديمي تنظم المعلومات دون تغيير تصنيفها."},
                      {"role": "user", "content": prompt}],
            temperature=0.0
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except Exception as e:
        return {"status": "error", "message": f"⚠️ حدث خطأ أثناء الفحص: {str(e)}"}

# --- باقي كود البوت (start, check, subscribe, etc) ---
# (نفس الكود السابق تماماً)
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🎓 بوت مودل الأقصى المطور جاهز للعمل!\nاستخدم /check للفحص والترتيب.")

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
        bot.send_message(message.chat.id, "🔍 جاري فحص وتصنيف بياناتك...")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])
    else:
        msg = bot.send_message(message.chat.id, "أرسل الرقم الجامعي:")
        bot.register_next_step_handler(msg, lambda m: bot.register_next_step_handler(bot.send_message(m.chat.id, "أرسل كلمة المرور:"), lambda p: save_user(p, m.text)))

def save_user(message, user):
    pwd = message.text
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط! تقريرك:\n\n" + res["message"])
    else: bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 التفعيل (5 شيكل):\nجوال باي: `0597599642`\nبينانس: `{BINANCE_PAY_ID}`\nارسل الإيصال هنا.")

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
    exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S') if mode != "VIP" else None
    conn.cursor().execute('UPDATE users SET expiry_date=?, is_vip=? WHERE chat_id=?', (exp, 1 if mode=="VIP" else 0, uid))
    conn.commit(); conn.close()
    bot.send_message(uid, "🎉 تم تفعيل اشتراكك بنجاح!")
    bot.answer_callback_query(call.id, "تم")

def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res and (res[1] == 1 or (res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now())): return True, "مشترك"
    return False, None

def scheduler_loop():
    schedule.every().day.at("08:00").do(daily_broadcast)
    while True: schedule.run_pending(); time.sleep(60)

def daily_broadcast():
    if IS_HOLIDAY: return
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    for uid, user, pwd in users:
        if check_access(uid)[0]:
            res = run_moodle_engine(user, pwd)
            if res["status"] == "success": bot.send_message(uid, "🔔 تقريرك اليومي:\n\n" + res["message"])

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    bot.infinity_polling()
