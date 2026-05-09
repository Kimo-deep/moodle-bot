import os, telebot, requests, sqlite3, threading, schedule, time, hashlib, hmac, json, uuid
from bs4 import BeautifulSoup
from groq import Groq
from datetime import datetime, timedelta
from telebot import types

# ─────────────────────────────────────────────
# 1. الإعدادات
# ─────────────────────────────────────────────
TOKEN            = os.getenv("TOKEN")
GROQ_KEY         = os.getenv("GROQ-KEY")
BINANCE_API_KEY  = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET   = os.getenv("BINANCE_SECRET_KEY")
ADMIN_ID         = 7840931571
FREE_TRIAL_END   = datetime(2026, 6, 1)
PRICE_MONTHLY    = 2.0   # دولار USDT

bot    = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False

# ─────────────────────────────────────────────
# 2. قاعدة البيانات
# ─────────────────────────────────────────────
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    chat_id      INTEGER PRIMARY KEY,
                    username     TEXT,
                    password     TEXT,
                    expiry_date  TEXT,
                    is_vip       INTEGER DEFAULT 0,
                    last_hash    TEXT,
                    last_report  TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_payments (
                    order_id   TEXT PRIMARY KEY,
                    chat_id    INTEGER,
                    amount     REAL,
                    created_at TEXT,
                    status     TEXT DEFAULT "pending"
                 )''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect('/app/data/users.db', check_same_thread=False, timeout=20)

# ─────────────────────────────────────────────
# 3. فحص الاشتراك
# ─────────────────────────────────────────────
def check_access(chat_id):
    if datetime.now() < FREE_TRIAL_END:
        return True, "تجريبي"
    conn = get_db_connection()
    row = conn.cursor().execute(
        'SELECT expiry_date, is_vip FROM users WHERE chat_id=?', (chat_id,)
    ).fetchone()
    conn.close()
    if row:
        expiry_str, is_vip = row
        if is_vip == 1:
            return True, "VIP"
        if expiry_str:
            exp = datetime.strptime(expiry_str, '%Y-%m-%d %H:%M:%S')
            if exp > datetime.now():
                days = (exp - datetime.now()).days
                return True, f"مشترك ({days} يوم)"
    return False, None

# ─────────────────────────────────────────────
# 4. تفعيل الاشتراك
# ─────────────────────────────────────────────
def activate_subscription(chat_id, plan):
    conn = get_db_connection()
    if plan == "VIP":
        conn.cursor().execute('UPDATE users SET is_vip=1 WHERE chat_id=?', (chat_id,))
    else:
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute(
            'UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?', (exp, chat_id)
        )
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# 5. فلترة المنجز
# ─────────────────────────────────────────────
DONE_KEYWORDS = [
    "تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال", "finished",
    "completed", "مكتمل", "تم الحل", "answered", "graded", "تم التقييم",
    "درجتك", "your grade", "attempt already", "تم المحاولة",
    "no attempts allowed", "past due",
]

def is_done(text):
    t = text.lower()
    return any(k.lower() in t for k in DONE_KEYWORDS)

# ─────────────────────────────────────────────
# 6. محرك المودل
# ─────────────────────────────────────────────
def run_moodle_engine(user, pwd):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    try:
        r     = session.get(login_url, timeout=20)
        token = BeautifulSoup(r.text, 'html.parser').find('input', {'name': 'logintoken'})['value']
        lr    = session.post(login_url,
                             data={'username': user, 'password': pwd, 'logintoken': token},
                             timeout=20)
        if "login" in lr.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        soup   = BeautifulSoup(
            session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming", timeout=20).text,
            'html.parser'
        )
        events = soup.find_all('div', {'class': 'event'})

        lectures, meetings, exams, assignments = [], [], [], []
        skipped = 0

        for e in events:
            txt     = e.get_text(separator=' ', strip=True)
            txt_low = txt.lower()
            link_tag = e.find('a', href=True)
            link     = link_tag['href'].lower() if link_tag else ""

            if is_done(txt):
                skipped += 1
                continue

            expired = False
            for tt in e.find_all(['time', 'span'], {'class': lambda x: x and 'date' in x}):
                ds = tt.get('datetime', '')
                if ds:
                    try:
                        et = datetime.fromisoformat(ds.replace('Z', '+00:00')).replace(tzinfo=None)
                        if et < datetime.now() - timedelta(hours=1):
                            skipped += 1
                            expired = True
                            break
                    except:
                        pass
            if expired:
                continue

            exam_kw   = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test", "midterm"]
            assign_kw = ["تكليف", "واجب", "مهمة", "تقرير", "تجربة", "رفع", "ملف",
                         "assignment", "task", "experiment", "report", "upload", "submit"]

            if "quiz" in link or any(w in txt_low for w in exam_kw):
                exams.append(txt)
            elif any(x in link for x in ["zoom", "meet", "bigbluebutton"]) or "لقاء" in txt:
                meetings.append(txt)
            elif "assign" in link or any(w in txt_low for w in assign_kw):
                assignments.append(txt)
            else:
                lectures.append(txt)

        if not any([lectures, meetings, exams, assignments]):
            note = f"\n_(مخفي: {skipped} منجز)_" if skipped else ""
            return {"status": "success", "message": f"✅ لا يوجد تحديثات جديدة حالياً.{note}"}

        hidden_note = f"\n\n_(مخفي: {skipped} منجز)_" if skipped else ""

        prompt = f"""أنت مساعد أكاديمي. نسّق البيانات التالية بالضبط وفق القواعد:

=== القواعد الصارمة ===
1. احذف كل نص يحتوي على: "حدث المساق", "إذهب إلى النشاط", "إضافة تسليم", "يرجى الالتزام", "التسليم فقط", "ولن يتم", "WhatsApp", "واتساب", "PDF", "Moodle", أو أي تعليمات تسليم.
2. شكل كل عنصر حرفياً هكذا:

▪️ [اسم الاختبار أو التكليف]
   📌 المادة: [اسم المادة]
   👨‍🏫 الدكتور: [الاسم فقط بدون لقب]
   🕐 يفتح: [اليوم التاريخ الوقت] | يغلق: [اليوم التاريخ الوقت]   ← للاختبارات فقط
   📅 آخر موعد: [اليوم التاريخ الوقت]                              ← للتكاليف فقط

3. سطر فارغ بين كل عنصر.
4. لا تكتب مقدمة ولا خاتمة ولا شرحاً.
5. إذا القسم فارغ: اكتب "لا يوجد" بجانب عنوانه مباشرة.
6. إذا لم يُذكر الدكتور: اكتب "غير محدد".

=== البيانات ===

📚 المحاضرات:
{chr(10).join(lectures) if lectures else 'لا يوجد'}

🎥 اللقاءات:
{chr(10).join(meetings) if meetings else 'لا يوجد'}

📝 الاختبارات:
{chr(10).join(exams) if exams else 'لا يوجد'}

⚠️ التكاليف والتجارب:
{chr(10).join(assignments) if assignments else 'لا يوجد'}

=== التقرير ==="""

        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1500
        )
        return {"status": "success", "message": resp.choices[0].message.content + hidden_note}

    except Exception as ex:
        return {"status": "error", "message": f"⚠️ المودل لا يستجيب. ({str(ex)[:60]})"}

# ─────────────────────────────────────────────
# 7. Binance Pay
# ─────────────────────────────────────────────
def _binance_headers(body_str):
    nonce     = uuid.uuid4().hex
    ts        = str(int(time.time() * 1000))
    payload   = f"{ts}\n{nonce}\n{body_str}\n"
    signature = hmac.new(BINANCE_SECRET.encode(), payload.encode(), hashlib.sha512).hexdigest().upper()
    return {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": ts,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": BINANCE_API_KEY,
        "BinancePay-Signature": signature,
    }

def create_binance_order(chat_id):
    if not (BINANCE_API_KEY and BINANCE_SECRET):
        return None, "Binance API غير مفعّل"
    order_id = f"MOODLE_{chat_id}_{int(time.time())}"
    body = json.dumps({
        "env": {"terminalType": "APP"},
        "merchantTradeNo": order_id,
        "orderAmount": PRICE_MONTHLY,
        "currency": "USDT",
        "description": "Moodle Bot - شهر",
        "goodsDetails": [{"goodsType": "02", "goodsCategory": "Z000",
                          "referenceGoodsId": "monthly", "goodsName": "Moodle Bot",
                          "goodsUnitAmount": {"currency": "USDT", "amount": str(PRICE_MONTHLY)}}]
    }, separators=(',', ':'))
    try:
        r = requests.post("https://bpay.binanceapi.com/binancepay/openapi/v2/order",
                          headers=_binance_headers(body), data=body, timeout=15).json()
        if r.get("status") == "SUCCESS":
            conn = get_db_connection()
            conn.cursor().execute(
                'INSERT INTO pending_payments VALUES (?,?,?,?,?)',
                (order_id, chat_id, PRICE_MONTHLY, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "pending")
            )
            conn.commit(); conn.close()
            return r["data"]["checkoutUrl"], order_id
        return None, r.get("errorMessage", "خطأ غير معروف")
    except Exception as ex:
        return None, str(ex)[:60]

def check_binance_order(order_id):
    if not (BINANCE_API_KEY and BINANCE_SECRET):
        return None
    body = json.dumps({"merchantTradeNo": order_id}, separators=(',', ':'))
    try:
        r = requests.post("https://bpay.binanceapi.com/binancepay/openapi/v1/order/query",
                          headers=_binance_headers(body), data=body, timeout=10).json()
        if r.get("status") == "SUCCESS":
            return r["data"]["status"]
    except:
        pass
    return None

def poll_pending_payments():
    conn = get_db_connection()
    rows = conn.cursor().execute(
        "SELECT order_id, chat_id FROM pending_payments WHERE status='pending'"
    ).fetchall()
    conn.close()
    for order_id, chat_id in rows:
        status = check_binance_order(order_id)
        if status == "PAID":
            activate_subscription(chat_id, "monthly")
            conn = get_db_connection()
            conn.cursor().execute("UPDATE pending_payments SET status='paid' WHERE order_id=?", (order_id,))
            conn.commit(); conn.close()
            try:
                bot.send_message(chat_id, "🎉 تم استلام دفعتك وتفعيل اشتراكك تلقائياً!")
            except:
                pass
        elif status == "CANCELLED":
            conn = get_db_connection()
            conn.cursor().execute("UPDATE pending_payments SET status='cancelled' WHERE order_id=?", (order_id,))
            conn.commit(); conn.close()

# ─────────────────────────────────────────────
# 8. /start
# ─────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def cmd_start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔍 فحص الآن", "📊 حالتي")
    markup.row("💳 اشتراك",   "❓ مساعدة")
    bot.send_message(
        message.chat.id,
        "🎓 *مرحباً في بوت مودل الأقصى*\n\n"
        "• يفحص المودل كل 6 ساعات تلقائياً\n"
        "• يُخفي التكاليف المسلّمة والامتحانات المنتهية\n"
        "• لا يُرسل إذا لم يكن هناك جديد\n\n"
        "استخدم /check للفحص الآن.",
        parse_mode="Markdown",
        reply_markup=markup
    )

# ─────────────────────────────────────────────
# 9. فحص الآن
# ─────────────────────────────────────────────
def _do_check(chat_id):
    allowed, status = check_access(chat_id)
    if not allowed:
        bot.send_message(chat_id, "🚫 اشتراكك منتهي. استخدم /subscribe للتجديد.")
        return
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, password FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()
    if u and u[0]:
        m   = bot.send_message(chat_id, f"🔍 جاري الفحص ({status})...")
        res = run_moodle_engine(u[0], u[1])
        bot.edit_message_text(res["message"], chat_id, m.message_id)
    else:
        m = bot.send_message(chat_id, "📋 أرسل رقمك الجامعي للربط:")
        bot.register_next_step_handler(m, _step_get_user)

@bot.message_handler(commands=['check'])
def cmd_check(message):
    _do_check(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def btn_check(message):
    _do_check(message.chat.id)

# ─────────────────────────────────────────────
# 10. حالتي
# ─────────────────────────────────────────────
def _do_status(chat_id):
    allowed, status = check_access(chat_id)
    conn = get_db_connection()
    u = conn.cursor().execute('SELECT username, last_report FROM users WHERE chat_id=?', (chat_id,)).fetchone()
    conn.close()

    linked     = f"✅ مرتبط برقم `{u[0]}`" if u and u[0] else "❌ غير مرتبط — استخدم /check"
    last_rep   = f"\n📅 آخر تقرير: {u[1]}"  if u and u[1] else "\n📅 لم يُرسل تقرير بعد"
    sub_stat   = f"✅ {status}"              if allowed    else "❌ منتهي — /subscribe"
    trial_note = (f"\n⏳ تنتهي التجربة: {FREE_TRIAL_END.strftime('%Y-%m-%d')}"
                  if datetime.now() < FREE_TRIAL_END else "")

    bot.send_message(
        chat_id,
        f"👤 *حالة حسابك:*\n\n"
        f"🔗 الربط: {linked}\n"
        f"🎫 الاشتراك: {sub_stat}{trial_note}{last_rep}",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['status'])
def cmd_status(message):
    _do_status(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 حالتي")
def btn_status(message):
    _do_status(message.chat.id)

# ─────────────────────────────────────────────
# 11. اشتراك
# ─────────────────────────────────────────────
def _do_subscribe(chat_id):
    markup = types.InlineKeyboardMarkup()
    if BINANCE_API_KEY and BINANCE_SECRET:
        markup.add(types.InlineKeyboardButton(
            f"💳 ادفع عبر Binance Pay ({PRICE_MONTHLY}$)", callback_data="sub_binance"
        ))
    markup.add(types.InlineKeyboardButton("📷 إرسال إيصال يدوي", callback_data="sub_manual"))
    bot.send_message(
        chat_id,
        f"💳 *تفعيل الاشتراك الشهري:*\n\n"
        f"💵 السعر: {PRICE_MONTHLY}$ USDT / شهر\n\n"
        f"• Binance Pay ID: `983969145`\n"
        f"• جوال باي: `0597599642`\n\n"
        f"ادفع ثم أرسل صورة الإيصال، أو استخدم زر الدفع المباشر.",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.message_handler(commands=['subscribe'])
def cmd_subscribe(message):
    _do_subscribe(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def btn_subscribe(message):
    _do_subscribe(message.chat.id)

# ─────────────────────────────────────────────
# 12. مساعدة
# ─────────────────────────────────────────────
def _do_help(chat_id):
    bot.send_message(
        chat_id,
        "📖 *قائمة الأوامر:*\n\n"
        "/check — فحص المودل الآن\n"
        "/status — حالة حسابك\n"
        "/subscribe — تفعيل اشتراك\n"
        "/unlink — إلغاء ربط حسابك\n\n"
        "💡 *البوت يُخفي تلقائياً:*\n"
        "• التكاليف المسلّمة\n"
        "• الاختبارات المنتهية أو المحلولة\n"
        "• الأحداث التي مضى وقتها",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    _do_help(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "❓ مساعدة")
def btn_help(message):
    _do_help(message.chat.id)

# ─────────────────────────────────────────────
# 13. إلغاء الربط
# ─────────────────────────────────────────────
@bot.message_handler(commands=['unlink'])
def cmd_unlink(message):
    conn = get_db_connection()
    conn.cursor().execute(
        'UPDATE users SET username=NULL, password=NULL WHERE chat_id=?', (message.chat.id,)
    )
    conn.commit(); conn.close()
    bot.send_message(message.chat.id, "🔓 تم إلغاء الربط. استخدم /check للربط من جديد.")

# ─────────────────────────────────────────────
# 14. ربط الحساب (خطوات)
# ─────────────────────────────────────────────
def _step_get_user(message):
    user = message.text.strip()
    m    = bot.send_message(message.chat.id, "🔐 أرسل كلمة مرور المودل:")
    bot.register_next_step_handler(m, lambda msg: _step_save(msg, user))

def _step_save(message, user):
    pwd = message.text.strip()
    wm  = bot.send_message(message.chat.id, "⏳ جاري التحقق من بياناتك...")
    res = run_moodle_engine(user, pwd)
    if res["status"] == "success":
        conn = get_db_connection()
        conn.cursor().execute(
            'INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)',
            (message.chat.id, user, pwd)
        )
        conn.commit(); conn.close()
        bot.edit_message_text(
            f"✅ تم الربط بنجاح! ستصلك تقارير كل 6 ساعات.\n\n{res['message']}",
            message.chat.id, wm.message_id
        )
    else:
        bot.edit_message_text(res["message"], message.chat.id, wm.message_id)

# ─────────────────────────────────────────────
# 15. استلام الإيصالات اليدوية
# ─────────────────────────────────────────────
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"pay_{message.chat.id}_monthly"),
        types.InlineKeyboardButton("❌ رفض",        callback_data=f"rej_{message.chat.id}")
    )
    bot.send_photo(
        ADMIN_ID, message.photo[-1].file_id,
        caption=(f"📩 *طلب تفعيل يدوي*\n"
                 f"👤 ID: `{message.chat.id}`\n"
                 f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"),
        reply_markup=markup, parse_mode="Markdown"
    )
    bot.reply_to(message, "⏳ تم إرسال الإيصال. سيُشعرك الأدمن فور المراجعة.")

# ─────────────────────────────────────────────
# 16. Callbacks
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "sub_binance")
def cb_sub_binance(call):
    bot.answer_callback_query(call.id, "⏳ جاري إنشاء رابط الدفع...")
    pay_url, result = create_binance_order(call.message.chat.id)
    if pay_url:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 ادفع الآن", url=pay_url))
        markup.add(types.InlineKeyboardButton("✅ تحقق من الدفع", callback_data=f"verify_{result}"))
        bot.send_message(
            call.message.chat.id,
            "🔗 اضغط *ادفع الآن* للدفع.\nبعد الدفع اضغط *تحقق من الدفع* للتفعيل الفوري.",
            parse_mode="Markdown", reply_markup=markup
        )
    else:
        bot.send_message(call.message.chat.id, f"❌ {result}\nأرسل الإيصال يدوياً.")

@bot.callback_query_handler(func=lambda c: c.data == "sub_manual")
def cb_sub_manual(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📷 أرسل صورة الإيصال وسيراجعها الأدمن.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("verify_"))
def cb_verify(call):
    order_id = call.data.replace("verify_", "")
    bot.answer_callback_query(call.id, "⏳ جاري التحقق...")
    status = check_binance_order(order_id)
    if status == "PAID":
        conn = get_db_connection()
        row  = conn.cursor().execute(
            "SELECT chat_id FROM pending_payments WHERE order_id=?", (order_id,)
        ).fetchone()
        conn.close()
        if row:
            activate_subscription(row[0], "monthly")
            conn = get_db_connection()
            conn.cursor().execute("UPDATE pending_payments SET status='paid' WHERE order_id=?", (order_id,))
            conn.commit(); conn.close()
            bot.send_message(call.message.chat.id, "🎉 تم تفعيل اشتراكك بنجاح!")
        else:
            bot.send_message(call.message.chat.id, "⚠️ لم يُعثر على الطلب.")
    elif status == "UNPAID":
        bot.send_message(call.message.chat.id, "⚠️ لم تصل الدفعة بعد. انتظر وأعد المحاولة.")
    else:
        bot.send_message(call.message.chat.id, "❌ انتهت صلاحية الطلب. أنشئ طلباً جديداً من /subscribe")

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_") or c.data.startswith("rej_"))
def cb_admin(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية.")
        return
    parts  = call.data.split("_")
    action = parts[0]
    uid    = int(parts[1])

    if action == "pay":
        activate_subscription(uid, "monthly")
        bot.send_message(uid, "✅ تم تفعيل اشتراكك لمدة شهر!")
        bot.edit_message_caption(
            f"✅ تم تفعيل `{uid}`",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown"
        )
    elif action == "rej":
        bot.send_message(uid, "❌ تم رفض طلبك. تأكد من إرسال إيصال صحيح أو تواصل مع الدعم.")
        bot.edit_message_caption(
            f"❌ رُفض طلب `{uid}`",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown"
        )
    bot.answer_callback_query(call.id)

# ─────────────────────────────────────────────
# 17. أوامر الأدمن
# ─────────────────────────────────────────────
@bot.message_handler(commands=['vip'])
def cmd_vip(message):
    """تفعيل VIP لأي مستخدم — للأدمن فقط. /vip [ID]"""
    if message.chat.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "الاستخدام: /vip [chat_id]")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID غير صحيح.")
        return
    conn = get_db_connection()
    conn.cursor().execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (uid,))
    conn.commit(); conn.close()
    activate_subscription(uid, "VIP")
    try:
        bot.send_message(uid, "🌟 تم تفعيل اشتراك VIP الخاص بك من قِبل الإدارة!")
    except:
        pass
    bot.send_message(message.chat.id, f"✅ تم تفعيل VIP للمستخدم `{uid}`.", parse_mode="Markdown")

@bot.message_handler(commands=['revoke'])
def cmd_revoke(message):
    """إلغاء اشتراك مستخدم. /revoke [ID]"""
    if message.chat.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "الاستخدام: /revoke [chat_id]")
        return
    uid = int(parts[1])
    conn = get_db_connection()
    conn.cursor().execute('UPDATE users SET is_vip=0, expiry_date=NULL WHERE chat_id=?', (uid,))
    conn.commit(); conn.close()
    bot.send_message(message.chat.id, f"✅ تم إلغاء اشتراك `{uid}`.", parse_mode="Markdown")

@bot.message_handler(commands=['holiday'])
def cmd_holiday(message):
    global IS_HOLIDAY
    if message.chat.id != ADMIN_ID:
        return
    IS_HOLIDAY = not IS_HOLIDAY
    bot.send_message(message.chat.id, "🏖️ وضع العطلة مفعّل" if IS_HOLIDAY else "✅ وضع العطلة ملغى")

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if message.chat.id != ADMIN_ID:
        return
    conn   = get_db_connection()
    total  = conn.cursor().execute('SELECT COUNT(*) FROM users').fetchone()[0]
    linked = conn.cursor().execute('SELECT COUNT(*) FROM users WHERE username IS NOT NULL').fetchone()[0]
    vip    = conn.cursor().execute('SELECT COUNT(*) FROM users WHERE is_vip=1').fetchone()[0]
    active = conn.cursor().execute(
        "SELECT COUNT(*) FROM users WHERE expiry_date > datetime('now')"
    ).fetchone()[0]
    conn.close()
    bot.send_message(
        message.chat.id,
        f"📊 *إحصائيات:*\n\n"
        f"👥 المستخدمون: {total}\n"
        f"🔗 مرتبطون: {linked}\n"
        f"🌟 VIP: {vip}\n"
        f"✅ اشتراك نشط: {active}\n"
        f"🏖️ وضع العطلة: {'مفعّل' if IS_HOLIDAY else 'ملغى'}",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    if message.chat.id != ADMIN_ID:
        return
    text = message.text.replace('/broadcast', '', 1).strip()
    if not text:
        bot.send_message(message.chat.id, "الاستخدام: /broadcast [الرسالة]")
        return
    conn  = get_db_connection()
    users = conn.cursor().execute('SELECT chat_id FROM users').fetchall()
    conn.close()
    ok = 0
    for (uid,) in users:
        try:
            bot.send_message(uid, f"📢 *إشعار من الإدارة:*\n\n{text}", parse_mode="Markdown")
            ok += 1
        except:
            pass
    bot.send_message(message.chat.id, f"✅ أُرسلت لـ {ok}/{len(users)} مستخدم.")

# ─────────────────────────────────────────────
# 18. التقارير الدورية (كل 6 ساعات)
# ─────────────────────────────────────────────
def broadcast_reports():
    if IS_HOLIDAY:
        return
    conn  = get_db_connection()
    users = conn.cursor().execute(
        'SELECT chat_id, username, password FROM users WHERE username IS NOT NULL'
    ).fetchall()
    conn.close()

    for uid, user, pwd in users:
        allowed, _ = check_access(uid)
        if not allowed:
            continue

        res = run_moodle_engine(user, pwd)
        if res["status"] != "success":
            continue

        msg = res["message"]

        # لا ترسل إذا لا يوجد تحديثات
        if "لا يوجد تحديثات" in msg:
            continue

        # لا ترسل إذا نفس المحتوى السابق
        content_hash = hashlib.md5(msg.encode()).hexdigest()
        conn = get_db_connection()
        row  = conn.cursor().execute('SELECT last_hash FROM users WHERE chat_id=?', (uid,)).fetchone()
        if row and row[0] == content_hash:
            conn.close()
            continue

        try:
            bot.send_message(uid, f"🔔 *تقرير المودل:*\n\n{msg}", parse_mode="Markdown")
            conn.cursor().execute(
                'UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?',
                (content_hash, datetime.now().strftime('%Y-%m-%d %H:%M'), uid)
            )
            conn.commit()
        except:
            pass
        finally:
            conn.close()

def auto_runner():
    schedule.every(6).hours.do(broadcast_reports)
    schedule.every(2).minutes.do(poll_pending_payments)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ─────────────────────────────────────────────
# 19. التشغيل
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=auto_runner, daemon=True).start()
    bot.infinity_polling()