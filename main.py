import os, telebot, requests, sqlite3, threading, schedule, time, re
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- 1. الإعدادات الأساسية ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
FREE_TRIAL_END = datetime(2026, 6, 1)

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 2. إدارة قاعدة البيانات ---
def init_db():
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                  expiry_date TEXT, is_vip INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# --- 3. محرك المودل المطور (نسخة الفرز النهائي) ---
def run_moodle_engine(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"

    try:
        # تسجيل الدخول
        r = session.get(login_url, timeout=20)
        token = BeautifulSoup(r.text, 'html.parser').find('input', {'name': 'logintoken'})['value']
        login_res = session.post(login_url, data={'username': user, 'password': pwd, 'logintoken': token}, timeout=20)

        if "login" in login_res.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        lectures, meetings, exams, assignments = [], [], [], []
        seen_titles = set()

        # الفحص الأول: التقويم (للتكاليف والاختبارات واللقاءات)
        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res_cal = session.get(cal_url, timeout=20)
        soup_cal = BeautifulSoup(res_cal.text, 'html.parser')
        
        for e in soup_cal.find_all('div', {'class': 'event'}):
            title_tag = e.find('h3')
            if not title_tag: continue
            
            # تنظيف النص من الرموز المخفية مثل \u200f
            raw_title = title_tag.get_text(strip=True).replace('\u200f', '').strip()
            link = title_tag.find('a', href=True)['href'].lower() if title_tag.find('a', href=True) else ""
            t_low = raw_title.lower()

            if any(word in t_low for word in ["تم التسليم", "محلول", "تخطى"]): continue

            # فرز صارم بناءً على الرابط والكلمات المفتاحية
            if "quiz" in link or any(x in t_low for x in ["اختبار", "كويز", "امتحان", "quiz"]):
                exams.append(raw_title)
            elif "assign" in link or any(x in t_low for x in ["مستحق", "تكليف", "واجب", "assignment", "task"]):
                assignments.append(raw_title)
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in t_low:
                meetings.append(raw_title)
            
            seen_titles.add(t_low)

        # الفحص الثاني: لوحة التحكم (للمحاضرات والملفات الصافية فقط)
        dash_url = "https://moodle.alaqsa.edu.ps/my/"
        res_dash = session.get(dash_url, timeout=20)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        
        for link_tag in soup_dash.find_all('a', href=True):
            href = link_tag['href'].lower()
            title = link_tag.get_text(strip=True).replace('\u200f', '').strip()
            t_low = title.lower()

            # استهداف روابط الملفات والمجلدات
            if any(x in href for x in ['resource/view.php', 'folder/view.php', 'url/view.php']):
                # استبعاد أي شيء ظهر في التقويم (مثل التكاليف) لضمان عدم التكرار
                if len(title) > 5 and t_low not in seen_titles:
                    if not any(x in t_low for x in ["مستحق", "يُفتح", "يُغلق", "quiz", "assignment", "تكليف"]):
                        lectures.append(title)
                        seen_titles.add(t_low)

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا توجد تحديثات جديدة حالياً."}

        # طلب التنسيق النهائي من AI مع تعليمات فرز حادة
        prompt = f"""
        رتب التقرير التالي بأسلوب احترافي:
        - [📚 المحاضرات]: {lectures}
        - [🎥 اللقاءات]: {meetings}
        - [📝 الاختبارات]: {exams}
        - [⚠️ التكاليف]: {assignments}
        
        قواعد:
        1. لا تكرر أي عنصر في أكثر من قسم.
        2. أي عنصر يحتوي على "مستحق" أو "تكليف" ضعه في [التكاليف] فقط.
        3. اذكر اسم المادة بجانب كل عنصر إن وجد.
        """
        
        comp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        return {"status": "success", "message": comp.choices[0].message.content}
    except Exception as e:
        return {"status": "error", "message": f"⚠️ المودل لا يستجيب حالياً."}

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

# --- 5. منطق الأوامر (تلبية طلبك بدقة) ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    allowed, status = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()

    if u: # إذا كان الحساب مربوط مسبقاً
        if allowed:
            bot.send_message(message.chat.id, "📊 **جاري جلب التقرير الشامل (كل شيء)...**")
            res = run_moodle_engine(u[0], u[1])
            bot.send_message(message.chat.id, res["message"])
        else:
            bot.send_message(message.chat.id, "🚫 اشتراكك منتهي. للتجديد: /subscribe")
    else: # مستخدم جديد
        welcome = "🎓 **مرحباً بك في بوت مودل الأقصى الشامل**\n\nأرسل الرقم الجامعي لربط حسابك وبدء الفحص الفوري:"
        msg = bot.send_message(message.chat.id, welcome)
        bot.register_next_step_handler(msg, get_user_id)

@bot.message_handler(commands=['check'])
def handle_check(message):
    allowed, _ = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()

    if u and allowed:
        bot.send_message(message.chat.id, "🔍 **جاري فحص التحديثات الأخيرة والشغلات الحديثة...**")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])
    elif not u:
        bot.send_message(message.chat.id, "⚠️ يرجى استخدام /start للربط أولاً.")

def get_user_id(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "أرسل كلمة المرور:")
    bot.register_next_step_handler(msg, lambda m: save_user_data(m, user))

def save_user_data(message, user):
    pwd = message.text
    bot.send_message(message.chat.id, "⏳ جارِ التحقق والربط الشامل...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?, ?, ?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط بنجاح! إليك تقريرك الشامل:\n\n" + res["message"])
    else:
        bot.send_message(message.chat.id, res["message"])

# --- 6. الاشتراك والإدارة ---
@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 **تفعيل الاشتراك:**\nجوال باي: `0597599642`\nبينانس: `983969145`\nارسل الإيصال كصورة هنا.")

@bot.message_handler(content_types=['photo'])
def handle_payment(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ تفعيل", callback_data=f"pay_{message.chat.id}"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"طلب تفعيل: `{message.chat.id}`", reply_markup=markup)
    bot.reply_to(message, "⏳ جارِ المراجعة...")

@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    if call.from_user.id != ADMIN_ID: return
    if call.data.startswith("pay_"):
        uid = call.data.split('_')[1]
        conn = get_db_connection()
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date=? WHERE chat_id=?', (exp, uid))
        conn.commit(); conn.close()
        bot.send_message(uid, "🎉 تم تفعيل اشتراكك لمدة شهر!")
    bot.answer_callback_query(call.id)

# --- 7. التقارير التلقائية (كل 6 ساعات) ---
def auto_reports():
    schedule.every(6).hours.do(broadcast_reports)
    while True:
        schedule.run_pending()
        time.sleep(60)

def broadcast_reports():
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    for uid, user, pwd in users:
        allowed, _ = check_access(uid)
        if allowed:
            try:
                res = run_moodle_engine(user, pwd)
                if res["status"] == "success":
                    bot.send_message(uid, "🔔 **تقرير المودل الدوري (كل 6 ساعات):**\n\n" + res["message"])
            except:
                pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_reports, daemon=True).start()
    bot.infinity_polling()
