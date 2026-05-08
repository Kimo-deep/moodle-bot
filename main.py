import os, telebot, requests, sqlite3, threading, schedule, time
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- 1. الإعدادات ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
FREE_TRIAL_END = datetime(2026, 6, 1)

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 2. قاعدة البيانات ---
def init_db():
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit(); conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# --- 3. محرك المودل المطور (إصلاح الخلل في الفرز) ---
def run_moodle_engine(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"

    try:
        r = session.get(login_url, timeout=20)
        token = BeautifulSoup(r.text, 'html.parser').find('input', {'name': 'logintoken'})['value']
        login_res = session.post(login_url, data={'username': user, 'password': pwd, 'logintoken': token}, timeout=20)

        if "login" in login_res.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        lectures, meetings, exams, assignments = [], [], [], []
        seen_items = set()

        # الفحص الأول: التقويم (الأحداث القادمة)
        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res_cal = session.get(cal_url, timeout=20)
        soup_cal = BeautifulSoup(res_cal.text, 'html.parser')
        events = soup_cal.find_all('div', {'class': 'event'})

        for e in events:
            # تنظيف النص وفصل اسم المادة عن نوع النشاط
            txt = e.get_text(separator=' | ', strip=True).lower()
            link_tag = e.find('a', href=True)
            link = link_tag['href'].lower() if link_tag else ""
            
            if any(word in txt for word in ["تم التسليم", "محلول", "تخطى"]): continue

            # تحديد العنوان الفعلي للحدث
            display_title = e.find('h3').get_text(strip=True) if e.find('h3') else txt.capitalize()

            # منطق الفرز الصارم (ترتيب الأولوية مهم جداً هنا)
            if "quiz" in link or any(w in txt for w in ["اختبار", "كويز", "امتحان", "quiz"]):
                exams.append(display_title)
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in txt:
                meetings.append(display_title)
            elif "assign" in link or any(w in txt for w in ["تكليف", "واجب", "نشاط", "مهمة", "تقرير", "تجربة"]):
                assignments.append(display_title)
            else:
                lectures.append(display_title)
            seen_items.add(display_title.lower())

        # الفحص الثاني: لوحة التحكم (صيد المحاضرات المرفوعة كملفات)
        dash_url = "https://moodle.alaqsa.edu.ps/my/"
        res_dash = session.get(dash_url, timeout=20)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        all_links = soup_dash.find_all('a', href=True)
        
        for link in all_links:
            href = link['href'].lower()
            title = link.get_text(strip=True)
            
            # رصد المحاضرات (Resources/Folders)
            if any(x in href for x in ['resource/view.php', 'folder/view.php', 'url/view.php']):
                if len(title) > 5 and title.lower() not in seen_items:
                    lectures.append(f"📄 {title} (محاضرة/ملف جديد)")
                    seen_items.add(title.lower())

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا يوجد أي تحديثات حالياً."}

        prompt = f"""
        رتب التقرير بوضوح تام. ضع كل عنصر في قسمه الصحيح بناءً على القوائم:
        📚 المحاضرات والملفات: {lectures}
        🎥 اللقاءات: {meetings}
        📝 الاختبارات: {exams}
        ⚠️ التكاليف: {assignments}
        اجعل هناك سطر فارغ بين كل عنصر والآخر واذكر اسم المادة إن وجد.
        """
        comp = client.chat.completions.create(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": prompt}], temperature=0.0)
        return {"status": "success", "message": comp.choices[0].message.content}
    except:
        return {"status": "error", "message": "⚠️ المودل لا يستجيب حالياً."}

# --- 4. بقية الأوامر والجدولة (كما هي دون تغيير) ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    allowed, status = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    if u and allowed:
        bot.send_message(message.chat.id, "📊 جاري جلب التقرير الشامل...")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])
    else:
        msg = bot.send_message(message.chat.id, "🎓 مرحباً! أرسل الرقم الجامعي للربط:")
        bot.register_next_step_handler(msg, get_user_id)

@bot.message_handler(commands=['check'])
def check_cmd(message):
    allowed, _ = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    if u and allowed:
        bot.send_message(message.chat.id, "🔍 جاري فحص التحديثات الأخيرة...")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])

# (أكمل بقية الدوال: check_access, save_user_data, subscribe, handle_payment, admin_actions, auto_reports)
# ملاحظة: تم اختصار الكود هنا لبيان موضع التصليح، تأكد من وجود الدوال المذكورة أعلاه في ملفك.

def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    user_data = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if user_data:
        if user_data[1] == 1: return True, "VIP"
        if user_data[0] and datetime.strptime(user_data[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

def get_user_id(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "أرسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda m: save_user_data(m, user))

def save_user_data(message, user):
    pwd = message.text
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?, ?, ?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط بنجاح!\n\n" + res["message"])
    else: bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, "💳 ارسل صورة الإيصال هنا للتفعيل.")

@bot.message_handler(content_types=['photo'])
def handle_payment(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ تفعيل", callback_data=f"pay_{message.chat.id}_30"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"طلب تفعيل: `{message.chat.id}`", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    if call.from_user.id != ADMIN_ID: return
    act, uid, *rest = call.data.split('_')
    if act == "pay":
        conn = get_db_connection()
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date=? WHERE chat_id=?', (exp, uid))
        conn.commit(); conn.close()
        bot.send_message(uid, "🎉 تم تفعيل اشتراكك!")
    bot.answer_callback_query(call.id)

def auto_reports():
    schedule.every(6).hours.do(broadcast_reports)
    while True: schedule.run_pending(); time.sleep(60)

def broadcast_reports():
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    for uid, user, pwd in users:
        if check_access(uid)[0]:
            try:
                res = run_moodle_engine(user, pwd)
                if res["status"] == "success": bot.send_message(uid, "🔔 تقرير المودل الدوري:\n\n" + res["message"])
            except: pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_reports, daemon=True).start()
    bot.infinity_polling()
