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

# --- 2. إدارة قاعدة البيانات (ضمان بقاء البيانات) ---
def init_db():
    # إنشاء اتصال بقاعدة البيانات (سيتم إنشاؤها إذا لم توجد)
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

# --- 3. محرك المودل (تصنيف راداري دقيق) ---
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

        cal_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"
        res = session.get(cal_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        
        lectures, meetings, exams, assignments = [], [], [], []

        for e in events:
            txt = e.get_text(separator=' ', strip=True).lower()
            link_tag = e.find('a', href=True)
            link = link_tag['href'].lower() if link_tag else ""
            
            if any(word in txt for word in ["تم التسليم", "محلول", "submitted", "تخطى", "سلمت"]):
                continue

            # تصنيف شامل (تجربة، رفع ملفات، كويزات، لقاءات)
            assign_keywords = ["تكليف", "واجب", "نشاط", "مهمة", "تقرير", "تجربة", "سلم", "رفع", "ملف", "assignment", "task", "experiment", "report", "upload", "submit"]
            exam_keywords = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test"]

            if "quiz" in link or any(word in txt for word in exam_keywords):
                exams.append(txt.capitalize())
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in txt:
                meetings.append(txt.capitalize())
            elif "assign" in link or any(word in txt for word in assign_keywords):
                assignments.append(txt.capitalize())
            else:
                lectures.append(txt.capitalize())

        if not (lectures or meetings or exams or assignments):
            return {"status": "success", "message": "✅ لا يوجد تحديثات جديدة حالياً."}

        prompt = f"""
        رتب هذا التقرير الأكاديمي. لا تغير التصنيفات واجعل مسافة بين المعلومة والاخرى مثلا لا تجعل التكاليف ملتصقة في بعضها البعض اجعل بينهم space لمعرفة ان هذا تكليف يختلف عن الذي قبله وطبق نفس الشيء على الاختبارات واللقاءات والمحاضرات:
        📚 المحاضرات: {lectures if lectures else 'لا يوجد'}
        🎥 اللقاءات: {meetings if meetings else 'لا يوجد'}
        📝 الاختبارات: {exams if exams else 'لا يوجد'}
        ⚠️ التكاليف والتجارب: {assignments if assignments else 'لا يوجد'}
        """
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        return {"status": "success", "message": completion.choices[0].message.content}
    except:
        return {"status": "error", "message": "⚠️ المودل لا يستجيب."}

# --- 4. فحص الاشتراك والصلاحية ---
def check_access(chat_id):
    # 1. فحص الفترة التجريبية العامة
    if datetime.now() < FREE_TRIAL_END:
        return True, "تجريبي"
    
    conn = get_db_connection()
    user_data = conn.cursor().execute('SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    
    if user_data:
        expiry_date_str, is_vip = user_data
        # 2. فحص الـ VIP
        if is_vip == 1:
            return True, "VIP"
        # 3. فحص تاريخ انتهاء الاشتراك العادي
        if expiry_date_str:
            expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d %H:%M:%S')
            if expiry_date > datetime.now():
                return True, "مشترك"
    
    return False, None

# --- 5. أوامر البوت ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "🎓 **مرحباً بك في بوت مودل الأقصى المطور**\n\nيتم حفظ بياناتك تلقائياً لتقديم تقارير دورية كل 6 ساعات.\nاستخدم /check للفحص الآن.")

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
        bot.send_message(message.chat.id, f"🔍 جاري الفحص (حساب {status})...")
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
        # INSERT OR REPLACE تضمن تحديث البيانات لنفس المستخدم دون حذف القديم
        conn.cursor().execute('INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?, ?, ?)', (message.chat.id, user, pwd))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, "✅ تم الربط بنجاح! سيصلك تقرير كل 6 ساعات.\n\n" + res["message"])
    else:
        bot.send_message(message.chat.id, res["message"])

@bot.message_handler(commands=['subscribe'])
def subscribe(message):
    bot.send_message(message.chat.id, f"💳 **تفعيل الاشتراك:**\nجوال باي: `0597599642`\nبينانس: `{BINANCE_PAY_ID}`\nارسل الإيصال هنا.")

# --- تعديل نظام استلام الإيصالات (إضافة زر الرفض) ---
@bot.message_handler(content_types=['photo'])
def handle_payment(message):
    markup = types.InlineKeyboardMarkup()
    # إضافة زر التفعيل وزر الرفض
    markup.add(
        types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"pay_{message.chat.id}_30"),
        types.InlineKeyboardButton("🌟 تفعيل VIP", callback_data=f"pay_{message.chat.id}_VIP")
    )
    markup.add(types.InlineKeyboardButton("❌ رفض الطلب (وهمي)", callback_data=f"rej_{message.chat.id}"))
    
    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, 
                   caption=f"📩 **طلب تفعيل جديد**\n👤 المستخدم: `{message.chat.id}`\n📅 التاريخ: {datetime.now().strftime('%Y-%m-%d')}", 
                   reply_markup=markup)
    bot.reply_to(message, "⏳ تم إرسال إيصالك للآدمن. سيتم إشعارك فور مراجعته.")

# --- تعديل معالج الأزرار للآدمن (التفعيل والرفض) ---
@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    if call.from_user.id != ADMIN_ID: return
    
    data = call.data.split('_')
    action = data[0] # pay أو rej
    uid = data[1] # ID المستخدم
    
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
        conn.commit()
        conn.close()
        
        bot.send_message(uid, msg_text)
        bot.edit_message_caption(f"✅ تم التفعيل للمستخدم `{uid}`", call.message.chat.id, call.message.message_id)
        
    elif action == "rej":
        # في حال الرفض
        bot.send_message(uid, "❌ نعتذر، تم رفض طلب التفعيل الخاص بك. يرجى التأكد من إرسال إيصال صحيح أو التواصل مع الدعم.")
        bot.edit_message_caption(f"❌ تم رفض طلب المستخدم `{uid}` (طلب وهمي)", call.message.chat.id, call.message.message_id)
    
    bot.answer_callback_query(call.id)


# --- 6. التوقيت المجدول (كل 6 ساعات) ---
def auto_reports():
    # فحص دوري كل 6 ساعات
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
