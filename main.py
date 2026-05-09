import os, telebot, requests, sqlite3, threading, schedule, time, hashlib, hmac, json
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# --- 1. الإعدادات الأساسية ---
TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ-KEY")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")       # مفتاح Binance Pay API
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY") # Secret Key لـ Binance Pay
ADMIN_ID = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)

# أسعار الاشتراك
PRICE_MONTHLY_USD = 2.0  # سعر الشهر بالدولار
PRICE_VIP_USD = 10.0     # سعر VIP بالدولار

bot = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False

# --- 2. قاعدة البيانات ---
def init_db():
    conn = sqlite3.connect('/app/data/users.db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id INTEGER PRIMARY KEY, 
                  username TEXT, 
                  password TEXT, 
                  expiry_date TEXT, 
                  is_vip INTEGER DEFAULT 0,
                  last_report TEXT)''')  # حقل جديد: آخر تقرير لتجنب التكرار
    # جدول الدفع المعلق عبر Binance
    c.execute('''CREATE TABLE IF NOT EXISTS pending_payments
                 (order_id TEXT PRIMARY KEY,
                  chat_id INTEGER,
                  plan TEXT,
                  amount REAL,
                  created_at TEXT,
                  status TEXT DEFAULT "pending")''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# --- 3. فلترة المحتوى المنجز (محسّنة) ---
COMPLETED_KEYWORDS = [
    # عربي
    "تم التسليم", "محلول", "submitted", "تخطى", "سلمت", "تم الإرسال",
    "finished", "completed", "مكتمل", "أنهيت", "تم الحل", "تم الإجابة",
    "answered", "graded", "تم التقييم", "درجتك", "your grade",
    "attempt already", "لديك محاولة", "تم المحاولة", "مغلق", "closed",
    "overdue", "past due",  # منتهي الصلاحية
    "no attempts allowed",
]

def is_completed(text):
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in COMPLETED_KEYWORDS)

# --- 4. محرك المودل ---
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
        skipped_count = 0  # عداد العناصر المخفية

        for e in events:
            txt = e.get_text(separator=' ', strip=True)
            txt_lower = txt.lower()
            link_tag = e.find('a', href=True)
            link = link_tag['href'].lower() if link_tag else ""

            # --- تصفية المنجز (محسّنة) ---
            if is_completed(txt):
                skipped_count += 1
                continue

            # فحص إضافي: هل الحدث منتهي الوقت؟
            time_tags = e.find_all(['time', 'span'], {'class': lambda x: x and 'date' in x})
            for tt in time_tags:
                date_str = tt.get('datetime', '')
                if date_str:
                    try:
                        event_time = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        # إذا انتهى الحدث منذ أكثر من ساعة اعتبره منجزاً
                        if event_time.replace(tzinfo=None) < datetime.now() - timedelta(hours=1):
                            skipped_count += 1
                            txt = None
                            break
                    except:
                        pass
            if txt is None:
                continue

            # تصنيف الأحداث
            assign_keywords = ["تكليف", "واجب", "نشاط", "مهمة", "تقرير", "تجربة", "سلم", "رفع", "ملف",
                                "assignment", "task", "experiment", "report", "upload", "submit"]
            exam_keywords = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test"]

            if "quiz" in link or any(word in txt_lower for word in exam_keywords):
                exams.append(txt.capitalize())
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in txt:
                meetings.append(txt.capitalize())
            elif "assign" in link or any(word in txt_lower for word in assign_keywords):
                assignments.append(txt.capitalize())
            else:
                lectures.append(txt.capitalize())

        if not (lectures or meetings or exams or assignments):
            hidden_note = f"\n\n_(تم إخفاء {skipped_count} عنصر منجز)_" if skipped_count else ""
            return {"status": "success", "message": f"✅ لا يوجد تحديثات جديدة حالياً.{hidden_note}"}

        hidden_note = f"\n_(مخفي: {skipped_count} منجز)_" if skipped_count else ""

        prompt = f"""
أنت مساعد أكاديمي متخصص في تنسيق التقارير الجامعية.

قواعد التنسيق:
- حافظ على التصنيفات الأصلية كما هي دون تعديل.
- اجعل اسم المادة أو العنصر بارزاً وواضحاً.
- اذكر اسم المدرس مرة واحدة فقط لكل عنصر.
- في الاختبارات: اعرض وقت الفتح ووقت الإغلاق إذا توفرا.
- في الواجبات: اعرض موعد التسليم فقط.
- لا تكرر الوقت أو التاريخ داخل نفس العنصر.
- احذف أي نص متعلق بـ: ملاحظات التسليم، التعليمات، طريقة الرفع، PDF، Moodle، WhatsApp.
- أضف مسافة فارغة بين كل عنصر.
- إذا لم يوجد محتوى داخل تصنيف اكتب: "لا يوجد".
- لا تكتب أي نص خارج التقرير النهائي.

البيانات:
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
        return {"status": "success", "message": completion.choices[0].message.content + hidden_note}

    except Exception as ex:
        return {"status": "error", "message": f"⚠️ المودل لا يستجيب. ({str(ex)[:50]})"}

# --- 5. Binance Pay API ---
def generate_binance_signature(nonce, timestamp, body_str):
    payload = f"{timestamp}\n{nonce}\n{body_str}\n"
    signature = hmac.new(
        BINANCE_SECRET_KEY.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha512
    ).hexdigest().upper()
    return signature

def create_binance_order(chat_id, plan):
    """إنشاء طلب دفع على Binance Pay"""
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return None, "⚠️ Binance API غير مفعّل."

    import uuid
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    order_id = f"MOODLE_{chat_id}_{int(time.time())}"
    amount = PRICE_MONTHLY_USD if plan == "monthly" else PRICE_VIP_USD

    body = {
        "env": {"terminalType": "APP"},
        "merchantTradeNo": order_id,
        "orderAmount": amount,
        "currency": "USDT",
        "description": f"Moodle Bot - {'شهر' if plan == 'monthly' else 'VIP'}",
        "goodsDetails": [{
            "goodsType": "02",
            "goodsCategory": "Z000",
            "referenceGoodsId": plan,
            "goodsName": "Moodle Bot Subscription",
            "goodsUnitAmount": {"currency": "USDT", "amount": str(amount)}
        }]
    }
    body_str = json.dumps(body, separators=(',', ':'))
    signature = generate_binance_signature(nonce, timestamp, body_str)

    headers = {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": BINANCE_API_KEY,
        "BinancePay-Signature": signature,
    }

    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v2/order",
            headers=headers, data=body_str, timeout=15
        )
        data = r.json()
        if data.get("status") == "SUCCESS":
            pay_url = data["data"]["checkoutUrl"]
            # حفظ الطلب في قاعدة البيانات
            conn = get_db_connection()
            conn.cursor().execute(
                'INSERT INTO pending_payments VALUES (?, ?, ?, ?, ?, ?)',
                (order_id, chat_id, plan, amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "pending")
            )
            conn.commit()
            conn.close()
            return pay_url, order_id
        else:
            return None, f"خطأ Binance: {data.get('errorMessage', 'غير معروف')}"
    except Exception as ex:
        return None, f"⚠️ فشل الاتصال بـ Binance: {str(ex)[:60]}"

def check_binance_order(order_id):
    """فحص حالة طلب Binance Pay"""
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return None

    import uuid
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    body = json.dumps({"merchantTradeNo": order_id}, separators=(',', ':'))
    signature = generate_binance_signature(nonce, timestamp, body)

    headers = {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": BINANCE_API_KEY,
        "BinancePay-Signature": signature,
    }

    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v1/order/query",
            headers=headers, data=body, timeout=10
        )
        data = r.json()
        if data.get("status") == "SUCCESS":
            return data["data"]["status"]  # PAID / UNPAID / CANCELLED
        return None
    except:
        return None

def poll_pending_payments():
    """فحص الطلبات المعلقة كل دقيقتين تلقائياً"""
    conn = get_db_connection()
    pending = conn.cursor().execute(
        "SELECT order_id, chat_id, plan FROM pending_payments WHERE status='pending'"
    ).fetchall()
    conn.close()

    for order_id, chat_id, plan in pending:
        status = check_binance_order(order_id)
        if status == "PAID":
            activate_subscription(chat_id, plan)
            conn = get_db_connection()
            conn.cursor().execute(
                "UPDATE pending_payments SET status='paid' WHERE order_id=?", (order_id,)
            )
            conn.commit()
            conn.close()
            msg = "✅ تم استلام دفعتك عبر Binance وتفعيل اشتراكك تلقائياً!" if plan == "monthly" \
                  else "🌟 تم استلام دفعتك وتفعيل اشتراك VIP تلقائياً!"
            try:
                bot.send_message(chat_id, msg)
            except:
                pass
        elif status == "CANCELLED":
            conn = get_db_connection()
            conn.cursor().execute(
                "UPDATE pending_payments SET status='cancelled' WHERE order_id=?", (order_id,)
            )
            conn.commit()
            conn.close()

def activate_subscription(chat_id, plan):
    conn = get_db_connection()
    if plan == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip=1 WHERE chat_id=?', (chat_id,))
    else:
        new_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute('UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?', (new_exp, chat_id))
    conn.commit()
    conn.close()

# --- 6. فحص الاشتراك ---
def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END:
        return True, "تجريبي"

    conn = get_db_connection()
    user_data = conn.cursor().execute(
        'SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)
    ).fetchone()
    conn.close()

    if user_data:
        expiry_date_str, is_vip = user_data
        if is_vip == 1:
            return True, "VIP"
        if expiry_date_str:
            expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d %H:%M:%S')
            if expiry_date > datetime.now():
                days_left = (expiry_date - datetime.now()).days
                return True, f"مشترك ({days_left} يوم متبقي)"

    return False, None

# --- 7. أوامر البوت ---
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔍 فحص الآن", "📊 حالتي")
    markup.row("💳 اشتراك", "❓ مساعدة")
    bot.send_message(
        message.chat.id,
        "🎓 **مرحباً بك في بوت مودل الأقصى**\n\n"
        "• يفحص المودل كل 6 ساعات تلقائياً\n"
        "• يخفي التكاليف المسلمة والامتحانات المنتهية\n"
        "• يدعم الدفع التلقائي عبر Binance Pay\n\n"
        "استخدم /check للفحص الآن أو اضغط الأزرار أدناه.",
        reply_markup=markup
    )

@bot.message_handler(commands=['check'])
@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def manual_check(message):
    allowed, status = check_access(message.chat.id)
    if not allowed:
        bot.send_message(message.chat.id, "🚫 اشتراكك منتهي. لتجديد الاشتراك اضغط /subscribe")
        return

    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()

    if u:
        msg = bot.send_message(message.chat.id, f"🔍 جاري الفحص ({status})...")
        res = run_moodle_engine(u[0], u[1])
        bot.edit_message_text(res["message"], message.chat.id, msg.message_id)
    else:
        msg = bot.send_message(message.chat.id, "أرسل رقمك الجامعي:")
        bot.register_next_step_handler(msg, get_user_id)

@bot.message_handler(commands=['status'])
@bot.message_handler(func=lambda m: m.text == "📊 حالتي")
def user_status(message):
    allowed, status = check_access(message.chat.id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, last_report FROM users WHERE chat_id=?', (message.chat.id,)).fetchone()
    conn.close()

    linked = f"✅ مرتبط برقم `{u[0]}`" if u and u[0] else "❌ غير مرتبط بعد"
    last_rep = f"\n📅 آخر تقرير: {u[1]}" if u and u[1] else ""
    sub_status = f"✅ {status}" if allowed else "❌ منتهي"
    trial_note = f"\n⏳ تنتهي الفترة التجريبية: {FREE_TRIAL_END.strftime('%Y-%m-%d')}" if datetime.now() < FREE_TRIAL_END else ""

    bot.send_message(
        message.chat.id,
        f"👤 **حالة حسابك:**\n\n"
        f"🔗 الربط: {linked}\n"
        f"🎫 الاشتراك: {sub_status}{trial_note}{last_rep}"
    )

@bot.message_handler(commands=['help'])
@bot.message_handler(func=lambda m: m.text == "❓ مساعدة")
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "📖 **قائمة الأوامر:**\n\n"
        "/check - فحص المودل الآن\n"
        "/status - حالة حسابك\n"
        "/subscribe - تفعيل اشتراك\n"
        "/unlink - إلغاء ربط حسابك\n"
        "/holiday - (أدمن) تفعيل/إلغاء وضع العطلة\n\n"
        "💡 **ملاحظة:** البوت يخفي تلقائياً:\n"
        "• التكاليف التي تم تسليمها\n"
        "• الاختبارات المنتهية أو المحلولة\n"
        "• الأحداث المنتهية منذ أكثر من ساعة"
    )

@bot.message_handler(commands=['unlink'])
def unlink(message):
    conn = get_db_connection()
    conn.cursor().execute('UPDATE users SET username=NULL, password=NULL WHERE chat_id=?', (message.chat.id,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, "🔓 تم إلغاء ربط حسابك. استخدم /check للربط من جديد.")

@bot.message_handler(commands=['holiday'])
def toggle_holiday(message):
    global IS_HOLIDAY
    if message.chat.id != ADMIN_ID:
        return
    IS_HOLIDAY = not IS_HOLIDAY
    state = "🏖️ وضع العطلة مفعّل" if IS_HOLIDAY else "✅ وضع العطلة ملغى"
    bot.send_message(message.chat.id, state)

# --- 8. ربط الحساب ---
def get_user_id(message):
    user = message.text.strip()
    msg = bot.send_message(message.chat.id, "🔐 أرسل كلمة مرور المودل:")
    bot.register_next_step_handler(msg, lambda m: save_user_data(m, user))

def save_user_data(message, user):
    pwd = message.text.strip()
    wait_msg = bot.send_message(message.chat.id, "⏳ جاري التحقق من بياناتك...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute(
            'INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?, ?, ?)',
            (message.chat.id, user, pwd)
        )
        conn.commit()
        conn.close()
        bot.edit_message_text(
            f"✅ تم الربط بنجاح! ستصلك تقارير كل 6 ساعات.\n\n{res['message']}",
            message.chat.id, wait_msg.message_id
        )
    else:
        bot.edit_message_text(res["message"], message.chat.id, wait_msg.message_id)

# --- 9. الاشتراك ---
@bot.message_handler(commands=['subscribe'])
@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def subscribe(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(f"📅 شهر ({PRICE_MONTHLY_USD}$)", callback_data="sub_monthly"),
        types.InlineKeyboardButton(f"🌟 VIP ({PRICE_VIP_USD}$)", callback_data="sub_VIP")
    )
    markup.add(types.InlineKeyboardButton("📷 إرسال إيصال يدوي", callback_data="sub_manual"))

    bot.send_message(
        message.chat.id,
        f"💳 **خيارات الاشتراك:**\n\n"
        f"📅 شهر واحد: {PRICE_MONTHLY_USD}$ USDT\n"
        f"🌟 VIP مدى الحياة: {PRICE_VIP_USD}$ USDT\n\n"
        f"• Binance Pay: `983969145`\n"
        f"• جوال باي: `0597599642`",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("sub_"))
def handle_sub_choice(call):
    plan = call.data.replace("sub_", "")
    if plan == "manual":
        bot.send_message(call.message.chat.id, "📷 أرسل صورة الإيصال وسيراجعها الأدمن.")
        bot.answer_callback_query(call.id)
        return

    if BINANCE_API_KEY and BINANCE_SECRET_KEY:
        bot.answer_callback_query(call.id, "⏳ جاري إنشاء رابط الدفع...")
        pay_url, result = create_binance_order(call.message.chat.id, plan)
        if pay_url:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💳 ادفع الآن", url=pay_url))
            markup.add(types.InlineKeyboardButton("✅ تحقق من الدفع", callback_data=f"verify_{result}"))
            bot.send_message(
                call.message.chat.id,
                "🔗 اضغط على الزر أدناه للدفع عبر Binance Pay.\n"
                "بعد الدفع اضغط **تحقق من الدفع** لتفعيل اشتراكك فوراً.",
                reply_markup=markup
            )
        else:
            bot.send_message(call.message.chat.id, f"❌ {result}\nأرسل الإيصال يدوياً.")
    else:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "📷 أرسل صورة الإيصال وسيراجعها الأدمن.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_"))
def verify_payment(call):
    order_id = call.data.replace("verify_", "")
    bot.answer_callback_query(call.id, "⏳ جاري التحقق...")
    status = check_binance_order(order_id)
    if status == "PAID":
        conn = get_db_connection()
        row = conn.cursor().execute(
            "SELECT chat_id, plan FROM pending_payments WHERE order_id=?", (order_id,)
        ).fetchone()
        conn.close()
        if row:
            activate_subscription(row[0], row[1])
            conn = get_db_connection()
            conn.cursor().execute("UPDATE pending_payments SET status='paid' WHERE order_id=?", (order_id,))
            conn.commit()
            conn.close()
            bot.send_message(call.message.chat.id, "🎉 تم تفعيل اشتراكك بنجاح!")
    elif status == "UNPAID":
        bot.send_message(call.message.chat.id, "⚠️ لم يتم استلام الدفعة بعد. انتظر قليلاً وحاول مجدداً.")
    else:
        bot.send_message(call.message.chat.id, "❌ انتهت صلاحية الطلب أو تم إلغاؤه. أنشئ طلباً جديداً.")

# --- 10. استلام الإيصالات اليدوية ---
@bot.message_handler(content_types=['photo'])
def handle_payment(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ شهر", callback_data=f"pay_{message.chat.id}_30"),
        types.InlineKeyboardButton("🌟 VIP", callback_data=f"pay_{message.chat.id}_VIP")
    )
    markup.add(types.InlineKeyboardButton("❌ رفض", callback_data=f"rej_{message.chat.id}"))
    bot.send_photo(
        ADMIN_ID, message.photo[-1].file_id,
        caption=f"📩 **طلب تفعيل يدوي**\n👤 ID: `{message.chat.id}`\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        reply_markup=markup
    )
    bot.reply_to(message, "⏳ تم إرسال الإيصال. سيُشعرك الأدمن فور المراجعة.")

# --- 11. أوامر الأدمن ---
@bot.callback_query_handler(func=lambda call: True)
def admin_actions(call):
    if call.from_user.id != ADMIN_ID:
        return

    data = call.data.split('_')
    action = data[0]
    uid = int(data[1])

    if action == "pay":
        mode = data[2]
        activate_subscription(uid, "VIP" if mode == "VIP" else "monthly")
        msg_text = "🌟 تم تفعيل VIP!" if mode == "VIP" else "✅ تم تفعيل اشتراكك لمدة شهر!"
        bot.send_message(uid, msg_text)
        bot.edit_message_caption(f"✅ تم التفعيل للمستخدم `{uid}`", call.message.chat.id, call.message.message_id)

    elif action == "rej":
        bot.send_message(uid, "❌ تم رفض طلب التفعيل. تأكد من إرسال إيصال صحيح أو تواصل مع الدعم.")
        bot.edit_message_caption(f"❌ رُفض طلب `{uid}`", call.message.chat.id, call.message.message_id)

    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    text = message.text.replace('/broadcast', '').strip()
    if not text:
        bot.send_message(message.chat.id, "استخدم: /broadcast [الرسالة]")
        return
    conn = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id FROM users').fetchall()
    conn.close()
    success = 0
    for (uid,) in users:
        try:
            bot.send_message(uid, f"📢 **إشعار من الإدارة:**\n\n{text}")
            success += 1
        except:
            pass
    bot.send_message(message.chat.id, f"✅ تم الإرسال لـ {success}/{len(users)} مستخدم.")

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if message.chat.id != ADMIN_ID:
        return
    conn = get_db_connection()
    total = conn.cursor().execute('SELECT COUNT(*) FROM users').fetchone()[0]
    linked = conn.cursor().execute('SELECT COUNT(*) FROM users WHERE username IS NOT NULL').fetchone()[0]
    vip = conn.cursor().execute('SELECT COUNT(*) FROM users WHERE is_vip=1').fetchone()[0]
    active_subs = conn.cursor().execute(
        "SELECT COUNT(*) FROM users WHERE expiry_date > datetime('now')"
    ).fetchone()[0]
    conn.close()
    bot.send_message(
        message.chat.id,
        f"📊 **إحصائيات البوت:**\n\n"
        f"👥 إجمالي المستخدمين: {total}\n"
        f"🔗 مرتبطون بالمودل: {linked}\n"
        f"🌟 VIP: {vip}\n"
        f"✅ اشتراك نشط: {active_subs}\n"
        f"🏖️ وضع العطلة: {'مفعّل' if IS_HOLIDAY else 'ملغى'}"
    )

# --- 12. التقارير الدورية ---
def auto_reports():
    schedule.every(6).hours.do(broadcast_reports)
    schedule.every(2).minutes.do(poll_pending_payments)  # فحص Binance كل دقيقتين
    while True:
        schedule.run_pending()
        time.sleep(30)

def broadcast_reports():
    if IS_HOLIDAY:
        return
    conn = get_db_connection()
    users = conn.cursor().execute(
        'SELECT chat_id, username, password FROM users WHERE username IS NOT NULL'
    ).fetchall()
    conn.close()

    for uid, user, pwd in users:
        allowed, _ = check_access(uid)
        if allowed:
            res = run_moodle_engine(user, pwd)
            if res["status"] == "success" and "لا يوجد تحديثات" not in res["message"]:
                try:
                    bot.send_message(uid, f"🔔 **تقرير المودل (كل 6 ساعات):**\n\n{res['message']}")
                    # تحديث وقت آخر تقرير
                    conn = get_db_connection()
                    conn.cursor().execute(
                        'UPDATE users SET last_report=? WHERE chat_id=?',
                        (datetime.now().strftime('%Y-%m-%d %H:%M'), uid)
                    )
                    conn.commit()
                    conn.close()
                except:
                    pass

if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_reports, daemon=True).start()
    bot.infinity_polling()