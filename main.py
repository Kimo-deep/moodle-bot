import os, telebot, requests, sqlite3, threading, schedule, time
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- 1. الإعدادات الأساسية ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"
ADMIN_ID = 7840931571  
BINANCE_PAY_ID = "983969145"
FREE_TRIAL_END = datetime(2026, 6, 1) # فترة تجريبية مجانية للجميع

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False 

# --- 2. إدارة قاعدة البيانات ---
def init_db():
    # المسار المخصص لـ Railway لضمان بقاء البيانات
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

# --- 3. محرك المودل المطور (وضع الصياد + التصنيف الراداري) ---
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
        seen_items = set() # لمنع التكرار بين التقويم واللوحة

        # --- المسار 1: فحص التقويم (Upcoming Events) ---
        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res_cal = session.get(cal_url, timeout=20)
        soup_cal = BeautifulSoup(res_cal.text, 'html.parser')
        events = soup_cal.find_all('div', {'class': 'event'})

        for e in events:
            txt = e.get_text(separator=' ', strip=True).lower()
            link_tag = e.find('a', href=True)
            link = link_tag['href'].lower() if link_tag else ""

            if any(word in txt for word in ["تم التسليم", "محلول", "submitted", "تخطى", "سلمت"]):
                continue

            # تصنيف شامل
            assign_keywords = ["تكليف", "واجب", "نشاط", "مهمة", "تقرير", "تجربة", "سلم", "رفع", "ملف", "assignment", "task", "experiment", "report", "upload", "submit"]
            exam_keywords = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test"]

            item_display = txt.capitalize()

            if "quiz" in link or any(word in txt for word in exam_keywords):
                exams.append(item_display)
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in txt:
                meetings.append(item_display)
            elif "assign" in link or any(word in txt for word in assign_keywords):
                assignments.append(item_display)
            else:
                lectures.append(item_display)
            
            seen_items.add(item_display.lower())

        # --- المسار 2: فحص لوحة التحكم (صياد المحاضرات المخفية) ---
        dash_url = "https://moodle.alaqsa.edu.ps/my/"
        res_dash = session.get(dash_url, timeout=20)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        
        all_links = soup_dash.find_all('a', href=True)
        for link in all_links:
            href = link['href'].lower()
            title = link.get_text(strip=True)
            
            # رصد الروابط التي تشير لمواد تعليمية مباشرة
            if any(x in href for x in ['resource/view.php', 'folder/view.php', 'url/view.php']):
                if len(title) > 6 and title.lower() not in seen_items:
                    lectures.append(f"📄 {title} (مادة مرفوعة حديثاً)")
                    seen_items.add(title.lower())

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا يوجد تحديثات جديدة حالياً."}

        # طلب التنسيق من Groq مع الحفاظ على المسافات واسم المادة
        prompt = f"""
        رتب هذا التقرير الأكاديمي. لا تغير التصنيفات واجعل مسافة (سطر فارغ) بين كل عنصر والآخر داخل نفس القسم لتسهيل القراءة. 
        تأكد من كتابة اسم المادة بجانب كل عنصر إذا كان متاحاً.
        
        📚 المحاضرات والملفات الجديدة: {lectures if lectures else 'لا يوجد'}
        🎥 اللقاءات المباشرة: {meetings if meetings else 'لا يوجد'}
        📝 الاختبارات والكويزات: {exams if exams else 'لا يوجد'}
        ⚠️ التكاليف والتجارب: {assignments if assignments else 'لا يوجد'}
        """

        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except:
        return {"status": "error", "message": "⚠️ المودل لا يستجيب أو تحت الضغط."}

# --- 4. فحص الاشتراك والصلاحية ---
def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END:
        return True, "تجريبي"
    conn = get_db_connection()
    user_data = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if user_data:
        expiry_date_str, is_vip = user_data
        if is_vip == 1: return True, "VIP"
        if expiry_date_str:
            expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d %H:%M:%S')
            if expiry_date > datetime.now(): return True, "مشترك"
    return False, None

# --- 5. أوامر البوت ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "🎓 **مرحباً بك في بوت مودل الأقصى (النسخة المتكاملة)**\n\nأنا الآن أفحص التقويم ولوحة التحكم معاً لضمان عدم ضياع أي محاضرة.\nيتم إرسال تقارير تلقائية كل 6 ساعات.\nاستخدم /check للفحص الفوري.")

@bot.message_handler(commands=['check'])
def manual_check(message):
    allowed, status = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 اشتراكك منتهي. لتجديد الاشتراك: /subscribe")
        return
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()
    if u:
        bot.send_message(message.chat.id, f"🔍 جاري الفحص الشامل (حساب {status})...")
        res = run_moodle_engine(u[0], u[1])
        bot.send_message(message.chat.id, res["message"])
    else:
        msg = bot.send_message(message.chat.id, "أرسل الرقم الجامعي للربط:")
        bot.register_next_step_handler(msg, get_user_id)

def get_user_id(message):
    user = message.text
    msg = bot.send_message(message.chat.id, "أرسل كلمة مرور المودل:")
    bot.register_next_step_handler(msg, lambda m: save_user_data(m, user))

def save_user_data(message, user):
    pwd = message.text
    bot.send_message(message.chat.id, "⏳ جاري التحقق والربط...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?, ?, ?)', (message.chat.id, user, pwd))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط بنجاح! سأقوم بمراقبة محاضراتك وتكاليفك.\n\n" + res["message"])
    else:
        bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 **تفعيل الاشتراك:**\nجوال باي: `0597599642`\nبينانس: `{BINANCE_PAY_ID}`\nارسل صورة الإيصال هنا.")

@bot.message_handler(content_types=['photo'])
def handle_payment(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"pay_{message.chat.id}_30"),
        types.InlineKeyboardButton("🌟 تفعيل VIP", callback_data=f"pay_{message.chat.id}_VIP")
    )
    markup.add(types.InlineKeyboardButton("❌ رفض الطلب", callback_data=f"rej_{message.chat.id}"))
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, 
                   caption=f"📩 **طلب تفعيل جديد**\n👤 المستخدم: `{message.chat.id}`", 
                   reply_markup=markup)
    bot.reply_to(message, "⏳ تم إرسال الإيصال للآدمن. سيصلك إشعار فور التفعيل.")

@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    if call.from_user.id != ADMIN_ID: return
    data = call.data.split('_')
    action, uid = data[0], data[1]
    if action == "pay":
        mode = data[2]
        conn = get_db_connection()
        if mode == "VIP":
            conn.cursor().execute('UPDATE users SET is_vip=1 WHERE chat_id=?', (uid,))
            msg_text = "🌟 تم تفعيل اشتراكك VIP مدى الحياة!"
        else:
            new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            conn.cursor().execute('UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?', (new_exp, uid))
            msg_text = "✅ تم تفعيل اشتراكك لمدة شهر بنجاح!"
        conn.commit(); conn.close()
        bot.send_message(uid, msg_text)
        bot.edit_message_caption(f"✅ تم التفعيل بنجاح للمستخدم `{uid}`", call.message.chat.id, call.message.message_id)
    elif action == "rej":
        bot.send_message(uid, "❌ نعتذر، تم رفض طلبك. تأكد من صحة الإيصال.")
        bot.edit_message_caption(f"❌ تم رفض المستخدم `{uid}`", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

# --- 6. الجدولة ---
def auto_reports():
    schedule.every(6).hours.do(broadcast_reports)
    while True:
        schedule.run_pending()
        time.sleep(60)

def broadcast_reports():
    if IS_HOLIDAY: return
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id, username, password FROM users WHERE username IS NOT NULL').fetchall()
    conn.close()
    for uid, user, pwd in users:
        allowed, _ = check_access(uid)
        if allowed:
            res = run_moodle_engine(user, pwd)
            if res["status"] == "success":
                try: bot.send_message(uid, f"🔔 **تقرير المودل الدوري (كل 6 ساعات):**\n\n{res['message']}")
                except: pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_reports, daemon=True).start()
    bot.infinity_polling()
