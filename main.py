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

# --- 3. محرك المودل (الفرز الطبقي الدقيق) ---
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
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        lectures, meetings, exams, assignments = [], [], [], []
        seen_titles = set()

        # الفحص الأول: التقويم (Upcoming Events)
        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res_cal = session.get(cal_url, timeout=20)
        soup_cal = BeautifulSoup(res_cal.text, 'html.parser')
        events = soup_cal.find_all('div', {'class': 'event'})

        for e in events:
            # استخراج العنوان الصافي من وسم h3
            title_tag = e.find('h3')
            if not title_tag: continue
            
            raw_title = title_tag.get_text(strip=True)
            link_tag = title_tag.find('a', href=True)
            link = link_tag['href'].lower() if link_tag else ""
            txt_lower = raw_title.lower()

            if any(word in txt_lower for word in ["تم التسليم", "محلول", "تخطى"]): continue

            # نظام الفرز الطبقي: الأولوية للرابط ثم الكلمات المفتاحية
            if "quiz" in link or any(w in txt_lower for w in ["اختبار", "كويز", "امتحان", "quiz"]):
                exams.append(raw_title)
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in txt_lower:
                meetings.append(raw_title)
            elif "assign" in link or any(w in txt_lower for w in ["تكليف", "واجب", "نشاط", "مهمة", "تقرير"]):
                assignments.append(raw_title)
            else:
                lectures.append(raw_title)
            
            seen_titles.add(raw_title.lower())

        # الفحص الثاني: لوحة التحكم (لجلب المحاضرات والملفات)
        dash_url = "https://moodle.alaqsa.edu.ps/my/"
        res_dash = session.get(dash_url, timeout=20)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        
        # البحث عن كافة الروابط التي تمثل مصادر تعليمية
        for link in soup_dash.find_all('a', href=True):
            href = link['href'].lower()
            title = link.get_text(strip=True)
            
            # رصد المحاضرات والملفات (Resources)
            if any(x in href for x in ['resource/view.php', 'folder/view.php', 'url/view.php']):
                if len(title) > 5 and title.lower() not in seen_titles:
                    lectures.append(f"📄 {title} (مادة تعليمية)")
                    seen_titles.add(title.lower())

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا يوجد تحديثات حالياً."}

        # طلب التنسيق النهائي من AI
        prompt = f"""
        رتب هذا التقرير الأكاديمي بوضوح فائق:
        📚 المحاضرات والملفات: {lectures}
        🎥 اللقاءات المباشرة: {meetings}
        📝 الاختبارات: {exams}
        ⚠️ التكاليف والواجبات: {assignments}
        - افصل بين كل عنصر والآخر بسطر فارغ.
        - اذكر اسم المادة لكل عنصر بوضوح.
        """
        
        comp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        return {"status": "success", "message": comp.choices[0].message.content}
    except Exception as e:
        return {"status": "error", "message": "⚠️ المودل لا يستجيب، حاول مجدداً لاحقاً."}

# --- 4. الأوامر والجدولة ---

def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END: return True, "تجريبي"
    conn = get_db_connection()
    res = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if res:
        if res[1] == 1: return True, "VIP"
        if res[0] and datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S') > datetime.now(): return True, "مشترك"
    return False, None

@bot.message_handler(commands=['start'])
def start_handler(message):
    allowed, status = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    
    if u and allowed:
        bot.send_message(message.chat.id, "📊 **التقرير الشامل للمساقات:**")
        bot.send_message(message.chat.id, run_moodle_engine(u[0], u[1])["message"])
    else:
        bot.send_message(message.chat.id, "🎓 مرحباً! أرسل الرقم الجامعي للربط:")
        bot.register_next_step_handler(message, lambda m: bot.register_next_step_handler(bot.send_message(m.chat.id, "أرسل كلمة المرور:"), lambda p: save_user(p, m.text)))

def save_user(message, user):
    pwd = message.text
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?, ?, ?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط! إليك تقريرك الأولي:\n\n" + res["message"])
    else: bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['check'])
def check_handler(message):
    allowed, _ = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    if u and allowed:
        bot.send_message(message.chat.id, "🔍 **فحص التحديثات الجديدة فقط...**")
        bot.send_message(message.chat.id, run_moodle_engine(u[0], u[1])["message"])

# (أكمل باقي الدوال: subscribe, handle_payment, admin_actions, auto_reports) كالمعتاد...

def auto_reports():
    schedule.every(6).hours.do(broadcast)
    while True: schedule.run_pending(); time.sleep(60)

def broadcast():
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    for uid, user, pwd in users:
        if check_access(uid)[0]:
            try: bot.send_message(uid, "🔔 **تحديث المودل الدوري:**\n\n" + run_moodle_engine(user, pwd)["message"])
            except: pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_reports, daemon=True).start()
    bot.infinity_polling()
