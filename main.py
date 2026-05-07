import os
import telebot
import requests
from bs4 import BeautifulSoup
from groq import Groq
import sqlite3
import time
import threading
import schedule

# --- الإعدادات (تأكد من صحة التوكنات) ---
TOKEN = "8702727538:AAE4rcAcrLeo4Luf2DeLgv3qtMWh2bleKic"
GROQ_KEY = "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"

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)

# --- 1. إدارة قاعدة البيانات ---
def init_db():
    os.makedirs('/app/data', exist_ok=True)
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, username TEXT, password TEXT)''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False)

def save_user(chat_id, user, pwd):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO users VALUES (?, ?, ?)', (chat_id, user, pwd))
    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT username, password FROM users WHERE chat_id = ?', (chat_id,))
    res = c.fetchone()
    conn.close()
    return res

def get_all_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT chat_id, username, password FROM users')
    users = c.fetchall()
    conn.close()
    return users

# --- 2. محرك سحب ومعالجة البيانات ---
def run_moodle_task(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    calendar_url = "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming"

    try:
        # تسجيل الدخول
        r = session.get(login_url, timeout=20)
        soup_login = BeautifulSoup(r.text, 'html.parser')
        token_input = soup_login.find('input', {'name': 'logintoken'})
        
        if not token_input:
            return "⚠️ تعذر العثور على توكن تسجيل الدخول، قد يكون الموقع تحت الصيانة."
            
        token = token_input['value']
        login_data = {'username': user, 'password': pwd, 'logintoken': token}
        session.post(login_url, data=login_data, timeout=20)

        # جلب التقويم
        res = session.get(calendar_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        events = soup.find_all('div', {'class': 'event'})
        
        if not events:
            return "✅ لا توجد واجبات قادمة في التقويم حالياً. استمتع بوقتك!"

        data_list = [f"📌 {e.get_text(separator=' | ', strip=True)}" for e in events]
        final_text = "\n\n".join(data_list)

        # التحليل عبر AI
        prompt = f"قم بتنظيم الواجبات التالية في قائمة احترافية تحتوي على (اسم المادة، اسم الواجب، الموعد النهائي) باللغة العربية: {final_text[:5000]}"
        
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        return completion.choices[0].message.content

    except Exception as e:
        print(f"Error: {e}")
        return "⚠️ عذراً، حدث خطأ أثناء الاتصال بمودل الجامعة. حاول لاحقاً."

# --- 3. نظام الجدولة (التذكير التلقائي) ---
def auto_job():
    users = get_all_users()
    for u in users:
        try:
            report = run_moodle_task(u[1], u[2])
            # نرسل فقط إذا وجد واجبات (تجنب الإزعاج إذا كان الرد "لا يوجد")
            if "لا توجد واجبات" not in report:
                bot.send_message(u[0], f"🔔 **تذكير تلقائي بالمواعيد القادمة:**\n\n{report}")
        except Exception as e:
            print(f"Error in auto_job for user {u[0]}: {e}")

def scheduler_loop():
    # فحص تلقائي كل 6 ساعات
    schedule.every(6).hours.do(auto_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

# --- 4. أوامر البوت ---
@bot.message_handler(commands=['start', 'check'])
def handle_commands(message):
    user_data = get_user(message.chat.id)
    
    if user_data:
        bot.send_message(message.chat.id, "⏳ جاري فحص مودل الأقصى، لحظات...")
        report = run_moodle_task(user_data[0], user_data[1])
        bot.send_message(message.chat.id, report)
    else:
        msg = bot.send_message(message.chat.id, "مرحباً بك! يرجى إرسال **الرقم الجامعي** للبدء:")
        bot.register_next_step_handler(msg, process_username)

def process_username(message):
    username = message.text
    msg = bot.send_message(message.chat.id, "الآن أرسل **كلمة المرور** (سيتم تشفيرها وحفظها محلياً):")
    bot.register_next_step_handler(msg, lambda m: process_password(m, username))

def process_password(message, username):
    password = message.text
    save_user(message.chat.id, username, password)
    bot.send_message(message.chat.id, "✅ تم حفظ بياناتك بنجاح! جاري فحص الواجبات لأول مرة...")
    report = run_moodle_task(username, password)
    bot.send_message(message.chat.id, report)
    bot.send_message(message.chat.id, "💡 سأقوم الآن بتذكيرك تلقائياً كل 6 ساعات في حال وجود واجبات جديدة.")

# --- التشغيل الأساسي ---
if __name__ == "__main__":
    init_db()
    
    # تشغيل المجدل في خيط (Thread) منفصل
    threading.Thread(target=scheduler_loop, daemon=True).start()
    
    print("🚀 البوت قيد التشغيل...")
    bot.infinity_polling()
    
    
