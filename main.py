"""
بوت مودل الأقصى — النسخة المُصحَّحة والمُحسَّنة
=================================================
متغيرات البيئة المطلوبة:
  TOKEN              — Telegram Bot Token
  GROQ-KEY           — Groq API Key
  BINANCE_API_KEY    — Binance Pay Certificate SN  (اختياري)
  BINANCE_SECRET_KEY — Binance Pay Secret Key      (اختياري)
"""

import os, hmac, hashlib, json, uuid, time, threading, sqlite3, schedule, requests, logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from groq import Groq
import telebot
from telebot import types

# ══════════════════════════════════════════════════════
# 1. الإعدادات
# ══════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TOKEN")
GROQ_KEY   = os.getenv("GROQ-KEY")
BIN_CERT   = os.getenv("BINANCE_API_KEY")
BIN_SECRET = os.getenv("BINANCE_SECRET_KEY")

ADMIN_ID       = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)
PRICE_USD      = 2.0
ILS_PER_USD    = 3.7          # يُحدَّث تلقائياً
DB_PATH        = "/app/data/users.db"

bot    = telebot.TeleBot(TOKEN, parse_mode="Markdown")
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY = False

# ══════════════════════════════════════════════════════
# 2. قاعدة البيانات — context manager يضمن إغلاق الاتصال دائماً
# ══════════════════════════════════════════════════════
@contextmanager
def get_db():
    """يفتح الاتصال ويغلقه تلقائياً حتى عند الأخطاء."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row   # الوصول بالاسم: row["chat_id"]
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                username    TEXT,
                password    TEXT,
                expiry_date TEXT,
                is_vip      INTEGER DEFAULT 0,
                last_hash   TEXT,
                last_report TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                order_id   TEXT    PRIMARY KEY,
                chat_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'pending'
            );
            -- فهرس لتسريع استعلام poll_payments
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
        """)

# ══════════════════════════════════════════════════════
# 3. سعر الصرف
# ══════════════════════════════════════════════════════
def refresh_rate():
    global ILS_PER_USD
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=8).json()
        ILS_PER_USD = round(r["rates"]["ILS"], 2)
        log.info(f"سعر الصرف محدَّث: 1$ = {ILS_PER_USD} ₪")
    except Exception as e:
        log.warning(f"فشل تحديث سعر الصرف: {e}")

def price_str():
    return f"{PRICE_USD}$ USDT (≈ {round(PRICE_USD * ILS_PER_USD, 1)} ₪)"

# ══════════════════════════════════════════════════════
# 4. الاشتراك
# ══════════════════════════════════════════════════════
def check_access(chat_id: int) -> tuple[bool, str | None]:
    if datetime.now() < FREE_TRIAL_END:
        return True, "تجريبي"
    with get_db() as conn:
        row = conn.execute(
            "SELECT expiry_date, is_vip FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row:
        if row["is_vip"]:
            return True, "VIP"
        if row["expiry_date"]:
            exp = datetime.strptime(row["expiry_date"], "%Y-%m-%d %H:%M:%S")
            if exp > datetime.now():
                return True, f"مشترك ({(exp - datetime.now()).days} يوم)"
    return False, None

def activate(chat_id: int, plan: str):
    """plan = 'monthly' | 'VIP'"""
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        if plan == "VIP":
            conn.execute("UPDATE users SET is_vip=1 WHERE chat_id=?", (chat_id,))
        else:
            exp = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?", (exp, chat_id)
            )

# ══════════════════════════════════════════════════════
# 5. كشف المنجز — 3 خطوط دفاع
# ══════════════════════════════════════════════════════
_CAL_DONE_KW = [
    "تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال",
    "attempt already", "تم المحاولة", "no attempts allowed",
    "past due", "overdue",
]

def _quick_done(text: str) -> bool:
    """خط 1: فحص سريع بالكلمات المفتاحية من نص التقويم."""
    t = text.lower()
    return any(k.lower() in t for k in _CAL_DONE_KW)

def _assign_done(session: requests.Session, url: str) -> bool:
    """
    خط 2: يفحص صفحة التكليف مباشرة.
    True  = سُلِّم → أخفِه
    False = لم يُسلَّم أو فشل الطلب → أبقه
    """
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        # جدول حالة التسليم الرسمي في مودل
        for row in soup.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not (th and td):
                continue
            label = th.get_text(strip=True).lower()
            value = td.get_text(strip=True).lower()
            if not any(k in label for k in ["حالة التسليم", "submission status", "status"]):
                continue
            # "لم يُسلَّم" → لم يسلم
            if any(k in value for k in ["لم يُسلَّم", "no submission", "not submitted", "لم يتم"]):
                return False
            return True   # أي حالة أخرى = سُلِّم
        # زر تعديل التسليم = سلّم مسبقاً
        page = soup.get_text(" ", strip=True).lower()
        return any(k in page for k in [
            "تعديل التسليم", "edit submission",
            "you have submitted", "already submitted",
        ])
    except Exception:
        return False   # فشل الطلب → أبقه ظاهراً (الأأمن)

def _quiz_done(session: requests.Session, url: str) -> bool:
    """
    خط 3: يفحص صفحة الكويز مباشرة.
    True  = حُلَّ أو لا محاولات → أخفِه
    False = لم يُحَل → أبقه
    """
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        page = soup.get_text(" ", strip=True).lower()
        if any(k in page for k in [
            "لقد أنهيت", "your last attempt", "آخر محاولة",
            "no more attempts", "لا محاولات متبقية",
            "مراجعة المحاولة", "review attempt",
            "your grade", "درجتك", "grade:",
            "attempt 1", "المحاولة 1",
        ]):
            return True
        # جدول المحاولات السابقة
        return bool(soup.find("table", {"class": lambda x: x and "quizattemptsummary" in x}))
    except Exception:
        return False

# ══════════════════════════════════════════════════════
# 6. محرك المودل
# ══════════════════════════════════════════════════════
_EXAM_KW   = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test", "midterm"]
_ASSIGN_KW = ["تكليف", "واجب", "مهمة", "تقرير", "تجربة", "رفع", "ملف",
              "assignment", "task", "experiment", "report", "upload", "submit"]
_MEET_KW   = ["zoom", "meet", "bigbluebutton"]

def run_moodle(username: str, password: str) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"

    try:
        # ── تسجيل الدخول ──────────────────────────────
        soup  = BeautifulSoup(session.get(login_url, timeout=20).text, "html.parser")
        token_input = soup.find("input", {"name": "logintoken"})
        if not token_input:
            return {"status": "error", "message": "⚠️ تعذّر الوصول إلى صفحة تسجيل الدخول."}
        token = token_input["value"]
        resp  = session.post(login_url,
                             data={"username": username, "password": password, "logintoken": token},
                             timeout=20)
        if "login" in resp.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        # ── صفحة التقويم ──────────────────────────────
        cal_html = session.get(
            "https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming", timeout=20
        ).text
        soup = BeautifulSoup(cal_html, "html.parser")

        lectures, meetings, exams, assignments = [], [], [], []
        skipped = 0

        for ev in soup.find_all("div", {"class": "event"}):
            txt  = ev.get_text(" ", strip=True)
            tl   = txt.lower()
            atag = ev.find("a", href=True)
            link = atag["href"] if atag else ""
            ll   = link.lower()

            # خط 1: كلمات سريعة
            if _quick_done(txt):
                skipped += 1
                continue

            is_quiz   = "quiz"   in ll or any(w in tl for w in _EXAM_KW)
            is_assign = "assign" in ll or any(w in tl for w in _ASSIGN_KW)
            is_meet   = any(x in ll for x in _MEET_KW) or "لقاء" in txt

            # خط 2 و 3: فحص الصفحة الفعلية
            if link:
                if is_assign and not is_quiz:
                    if _assign_done(session, link):
                        skipped += 1
                        continue
                elif is_quiz:
                    if _quiz_done(session, link):
                        skipped += 1
                        continue

            if is_quiz:        exams.append(txt)
            elif is_meet:      meetings.append(txt)
            elif is_assign:    assignments.append(txt)
            else:              lectures.append(txt)

        if not any([lectures, meetings, exams, assignments]):
            note = f"\n_(مخفي: {skipped} منجز)_" if skipped else ""
            return {"status": "success", "message": f"✅ لا يوجد تحديثات جديدة حالياً.{note}"}

        hidden = f"\n\n_(تم إخفاء {skipped} عنصر منجز)_" if skipped else ""

        prompt = (
            "أنت مساعد أكاديمي. نسّق البيانات التالية وفق القواعد الصارمة:\n\n"
            "=== القواعد ===\n"
            "1. احذف كل نص يحتوي على: حدث المساق، إذهب إلى النشاط، إضافة تسليم،\n"
            "   يرجى الالتزام، التسليم فقط، ولن يتم، WhatsApp، واتساب، PDF، Moodle،\n"
            "   أو أي تعليمات تسليم أو ملاحظات إدارية.\n"
            "2. شكل كل عنصر هكذا حرفياً:\n\n"
            "▪️ [اسم الاختبار أو التكليف أو التجربة]\n"
            "   📌 المادة: [اسم المادة]\n"
            "   👨‍🏫 الدكتور: [الاسم فقط بلا لقب]\n"
            "   🕐 يفتح: [يوم، تاريخ، وقت] | يغلق: [يوم، تاريخ، وقت]   ← للاختبارات\n"
            "   📅 آخر موعد: [يوم، تاريخ، وقت]                           ← للتكاليف\n\n"
            "3. سطر فارغ بين كل عنصر.\n"
            "4. لا مقدمة ولا خاتمة ولا شرح.\n"
            "5. إذا القسم فارغ: اكتب 'لا يوجد' بجانب عنوانه مباشرة.\n"
            "6. إذا لم يُذكر الدكتور: اكتب 'غير محدد'.\n\n"
            "=== البيانات ===\n\n"
            f"📚 المحاضرات:\n{chr(10).join(lectures) if lectures else 'لا يوجد'}\n\n"
            f"🎥 اللقاءات:\n{chr(10).join(meetings) if meetings else 'لا يوجد'}\n\n"
            f"📝 الاختبارات:\n{chr(10).join(exams) if exams else 'لا يوجد'}\n\n"
            f"⚠️ التكاليف والتجارب:\n{chr(10).join(assignments) if assignments else 'لا يوجد'}\n\n"
            "=== التقرير المنسق ==="
        )

        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1500,
        )
        return {"status": "success", "message": resp.choices[0].message.content + hidden}

    except requests.RequestException as e:
        log.error(f"خطأ شبكة run_moodle: {e}")
        return {"status": "error", "message": "⚠️ المودل لا يستجيب، حاول لاحقاً."}
    except Exception as e:
        log.error(f"خطأ run_moodle: {e}")
        return {"status": "error", "message": f"⚠️ خطأ غير متوقع: {str(e)[:60]}"}

# ══════════════════════════════════════════════════════
# 7. Binance Pay
# ══════════════════════════════════════════════════════
def _bin_headers(body: str) -> dict:
    """
    يولّد headers التوقيع لـ Binance Pay API.
    الصيغة الرسمية: TIMESTAMP\nNONCE\nBODY\n
    """
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time() * 1000))
    raw   = f"{ts}\n{nonce}\n{body}\n"
    sig   = hmac.new(                     # hmac.new(key, msg, digestmod)
                BIN_SECRET.encode(),
                raw.encode(),
                hashlib.sha512
            ).hexdigest().upper()
    return {
        "Content-Type":              "application/json",
        "BinancePay-Timestamp":      ts,
        "BinancePay-Nonce":          nonce,
        "BinancePay-Certificate-SN": BIN_CERT,
        "BinancePay-Signature":      sig,
    }

def binance_create(chat_id: int) -> tuple[str | None, str]:
    """يرجع (checkout_url, order_id) أو (None, رسالة_خطأ)."""
    if not (BIN_CERT and BIN_SECRET):
        return None, "Binance Pay غير مفعّل."
    order_id = f"MDB_{chat_id}_{int(time.time())}"   # أقصر من MOODLE_
    body = json.dumps({
        "env":             {"terminalType": "WEB"},
        "merchantTradeNo": order_id,
        "orderAmount":     f"{PRICE_USD:.2f}",    # "2.00" — Binance تتوقع string بخانتين
        "currency":        "USDT",
        "description":     "Moodle Bot Monthly",
        "goodsDetails": [{
            "goodsType":        "02",
            "goodsCategory":    "Z000",
            "referenceGoodsId": "monthly",
            "goodsName":        "Moodle Bot Subscription",
            "goodsUnitAmount":  {"currency": "USDT", "amount": f"{PRICE_USD:.2f}"},
        }],
    }, separators=(",", ":"))
    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v2/order",
            headers=_bin_headers(body), data=body, timeout=15
        ).json()
        if r.get("status") == "SUCCESS":
            with get_db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO payments VALUES (?,?,?,?)",
                    (order_id, chat_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending")
                )
            return r["data"]["checkoutUrl"], order_id
        err = r.get("errorMessage", "خطأ غير معروف")
        log.warning(f"Binance create failed: {err}")
        return None, f"Binance: {err}"
    except Exception as e:
        log.error(f"Binance create exception: {e}")
        return None, str(e)[:60]

def binance_query(order_id: str) -> str | None:
    """يرجع 'PAID' | 'UNPAID' | 'CANCELLED' | 'EXPIRED' | None."""
    if not (BIN_CERT and BIN_SECRET):
        return None
    body = json.dumps({"merchantTradeNo": order_id}, separators=(",", ":"))
    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v1/order/query",
            headers=_bin_headers(body), data=body, timeout=10
        ).json()
        if r.get("status") == "SUCCESS":
            return r["data"]["status"]
    except Exception as e:
        log.warning(f"Binance query exception: {e}")
    return None

def poll_payments():
    """يتحقق من الطلبات المعلقة — يُهمل الطلبات الأقدم من 24 ساعة تلقائياً."""
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT order_id, chat_id FROM payments WHERE status='pending' AND created_at >= ?",
            (cutoff,)
        ).fetchall()
        # تنظيف تلقائي: احذف الطلبات المعلقة الأقدم من 24 ساعة لتوفير المساحة
        conn.execute(
            "DELETE FROM payments WHERE status='pending' AND created_at < ?", (cutoff,)
        )

    for row in rows:
        order_id, chat_id = row["order_id"], row["chat_id"]
        st = binance_query(order_id)
        if st == "PAID":
            activate(chat_id, "monthly")
            with get_db() as conn:
                conn.execute("UPDATE payments SET status='paid' WHERE order_id=?", (order_id,))
            try:
                bot.send_message(chat_id, "🎉 تم استلام دفعتك وتفعيل اشتراكك تلقائياً\\!")
            except Exception:
                pass
        elif st in ("CANCELLED", "EXPIRED"):
            with get_db() as conn:
                conn.execute(
                    "UPDATE payments SET status=? WHERE order_id=?", (st.lower(), order_id)
                )

# ══════════════════════════════════════════════════════
# 8. أوامر البوت
# ══════════════════════════════════════════════════════

# ── /start ──────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    kb.row("💳 اشتراك",   "❓ مساعدة")
    bot.send_message(m.chat.id,
        "🎓 *مرحباً في بوت مودل الأقصى*\n\n"
        "• فحص تلقائي كل 6 ساعات\n"
        "• يُخفي التكاليف المسلّمة والكويزات المحلولة\n"
        "• لا يُرسل إذا لا يوجد جديد",
        reply_markup=kb)

# ── /check + زر ─────────────────────────────────────
def _do_check(chat_id: int):
    ok, label = check_access(chat_id)
    if not ok:
        bot.send_message(chat_id, "🚫 اشتراكك منتهٍ\\. استخدم /subscribe للتجديد\\.")
        return
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, password FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row and row["username"]:
        wm  = bot.send_message(chat_id, f"🔍 جاري الفحص \\({label}\\)\\.\\.\\.")
        res = run_moodle(row["username"], row["password"])
        try:
            bot.edit_message_text(res["message"], chat_id, wm.message_id)
        except Exception:
            bot.send_message(chat_id, res["message"])
    else:
        wm = bot.send_message(chat_id, "📋 أرسل رقمك الجامعي:")
        bot.register_next_step_handler(wm, _step_user)

@bot.message_handler(commands=["check"])
def cmd_check(m): _do_check(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def btn_check(m): _do_check(m.chat.id)

# ── /status + زر ────────────────────────────────────
def _do_status(chat_id: int):
    ok, label = check_access(chat_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, last_report FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    linked = f"✅ مرتبط برقم `{row['username']}`" if row and row["username"] else "❌ غير مرتبط — /check"
    last   = f"\n📅 آخر تقرير: {row['last_report']}" if row and row["last_report"] else "\n📅 لم يُرسل بعد"
    sub    = f"✅ {label}" if ok else "❌ منتهٍ — /subscribe"
    trial  = (f"\n⏳ التجربة تنتهي: {FREE_TRIAL_END:%Y\\-%-m\\-%-d}"
              if datetime.now() < FREE_TRIAL_END else "")
    bot.send_message(chat_id, f"👤 *حالة حسابك:*\n\n🔗 {linked}\n🎫 {sub}{trial}{last}")

@bot.message_handler(commands=["status"])
def cmd_status(m): _do_status(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 حالتي")
def btn_status(m): _do_status(m.chat.id)

# ── /subscribe + زر ─────────────────────────────────
def _do_subscribe(chat_id: int):
    kb = types.InlineKeyboardMarkup()
    if BIN_CERT and BIN_SECRET:
        kb.add(types.InlineKeyboardButton("💳 ادفع عبر Binance Pay", callback_data="sub_binance"))
    kb.add(types.InlineKeyboardButton("📷 إرسال إيصال يدوي", callback_data="sub_manual"))
    bot.send_message(chat_id,
        f"💳 *تفعيل الاشتراك الشهري*\n\n"
        f"💵 السعر: *{price_str()}* / شهر\n\n"
        f"• Binance Pay ID: `983969145`\n"
        f"• جوال باي: `0597599642`\n\n"
        "ادفع ثم أرسل صورة الإيصال أو استخدم زر الدفع المباشر\\.",
        reply_markup=kb)

@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(m): _do_subscribe(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def btn_subscribe(m): _do_subscribe(m.chat.id)

# ── /help + زر ──────────────────────────────────────
def _do_help(chat_id: int):
    bot.send_message(chat_id,
        "📖 *قائمة الأوامر:*\n\n"
        "/check — فحص المودل الآن\n"
        "/status — حالة حسابك\n"
        "/subscribe — تفعيل اشتراك\n"
        "/unlink — إلغاء ربط حسابك\n\n"
        "💡 *البوت يُخفي تلقائياً:*\n"
        "• التكاليف المسلّمة \\(يفحص الصفحة مباشرة\\)\n"
        "• الكويزات المحلولة أو المنتهية المحاولات\n"
        "• الأحداث التي مضى وقتها")

@bot.message_handler(commands=["help"])
def cmd_help(m): _do_help(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "❓ مساعدة")
def btn_help(m): _do_help(m.chat.id)

# ── /unlink ──────────────────────────────────────────
@bot.message_handler(commands=["unlink"])
def cmd_unlink(m):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET username=NULL, password=NULL WHERE chat_id=?", (m.chat.id,)
        )
    bot.send_message(m.chat.id, "🔓 تم إلغاء الربط\\. استخدم /check للربط من جديد\\.")

# ── ربط الحساب (خطوات) ──────────────────────────────
def _step_user(msg):
    # تحقق أن الرسالة نص
    if not msg.text:
        wm = bot.send_message(msg.chat.id, "❌ أرسل الرقم الجامعي كنص:")
        bot.register_next_step_handler(wm, _step_user)
        return
    user = msg.text.strip()
    wm   = bot.send_message(msg.chat.id, "🔐 أرسل كلمة المرور:")
    bot.register_next_step_handler(wm, lambda m2: _step_pwd(m2, user))

def _step_pwd(msg, user):
    if not msg.text:
        wm = bot.send_message(msg.chat.id, "❌ أرسل كلمة المرور كنص:")
        bot.register_next_step_handler(wm, lambda m2: _step_pwd(m2, user))
        return
    pwd = msg.text.strip()
    wm  = bot.send_message(msg.chat.id, "⏳ جاري التحقق\\.\\.\\.")
    res = run_moodle(user, pwd)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)",
                (msg.chat.id, user, pwd)
            )
        try:
            bot.edit_message_text(
                f"✅ تم الربط\\! ستصلك تقارير كل 6 ساعات\\.\n\n{res['message']}",
                msg.chat.id, wm.message_id
            )
        except Exception:
            bot.send_message(msg.chat.id, f"✅ تم الربط\\!\n\n{res['message']}")
    else:
        try:
            bot.edit_message_text(res["message"], msg.chat.id, wm.message_id)
        except Exception:
            bot.send_message(msg.chat.id, res["message"])

# ── استلام الإيصالات ────────────────────────────────
@bot.message_handler(content_types=["photo"])
def handle_photo(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"pay_{m.chat.id}"),
        types.InlineKeyboardButton("❌ رفض",        callback_data=f"rej_{m.chat.id}")
    )
    try:
        bot.send_photo(ADMIN_ID, m.photo[-1].file_id,
            caption=f"📩 *طلب تفعيل يدوي*\n👤 ID: `{m.chat.id}`\n📅 {datetime.now():%Y-%m-%d %H:%M}",
            reply_markup=kb)
        bot.reply_to(m, "⏳ تم إرسال الإيصال\\. سيُشعرك الأدمن فور المراجعة\\.")
    except Exception as e:
        log.error(f"handle_photo error: {e}")
        bot.reply_to(m, "⚠️ حدث خطأ أثناء الإرسال\\. حاول مجدداً\\.")

# ══════════════════════════════════════════════════════
# 9. Callbacks
# ══════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data == "sub_binance")
def cb_binance(call):
    bot.answer_callback_query(call.id, "⏳ جاري إنشاء رابط الدفع...")
    url, result = binance_create(call.message.chat.id)
    if url:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💳 ادفع الآن", url=url))
        kb.add(types.InlineKeyboardButton("✅ تحقق من الدفع", callback_data=f"verify_{result}"))
        bot.send_message(call.message.chat.id,
            f"💵 المبلغ: *{price_str()}*\n\n"
            "اضغط *ادفع الآن* ثم عد واضغط *تحقق من الدفع* للتفعيل الفوري\\.",
            reply_markup=kb)
    else:
        bot.send_message(call.message.chat.id, f"❌ {result}\nأرسل الإيصال يدوياً\\.")

@bot.callback_query_handler(func=lambda c: c.data == "sub_manual")
def cb_manual(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📷 أرسل صورة الإيصال وسيراجعها الأدمن\\.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("verify_"))
def cb_verify(call):
    order_id = call.data[7:]
    bot.answer_callback_query(call.id, "⏳ جاري التحقق...")
    st = binance_query(order_id)
    if st == "PAID":
        with get_db() as conn:
            row = conn.execute(
                "SELECT chat_id FROM payments WHERE order_id=?", (order_id,)
            ).fetchone()
        if row:
            activate(row["chat_id"], "monthly")
            with get_db() as conn:
                conn.execute("UPDATE payments SET status='paid' WHERE order_id=?", (order_id,))
            bot.send_message(call.message.chat.id, "🎉 تم تفعيل اشتراكك بنجاح\\!")
        else:
            bot.send_message(call.message.chat.id, "⚠️ لم يُعثر على الطلب\\.")
    elif st == "UNPAID":
        bot.send_message(call.message.chat.id,
            "⏳ لم تصل الدفعة بعد\\. انتظر دقيقة وأعد المحاولة\\.")
    elif st in ("CANCELLED", "EXPIRED"):
        bot.send_message(call.message.chat.id,
            "❌ انتهت صلاحية الطلب\\. أنشئ طلباً جديداً من /subscribe")
    else:
        bot.send_message(call.message.chat.id,
            "⚠️ لم يرد Binance\\. حاول لاحقاً\\.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_") or c.data.startswith("rej_"))
def cb_admin(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية.")
        return
    action, uid_str = call.data.split("_", 1)
    uid = int(uid_str)
    if action == "pay":
        activate(uid, "monthly")
        bot.send_message(uid, "✅ تم تفعيل اشتراكك لمدة شهر\\!")
        try:
            bot.edit_message_caption(
                f"✅ تم تفعيل `{uid}`",
                call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    elif action == "rej":
        bot.send_message(uid, "❌ تم رفض طلبك\\. تأكد من إرسال إيصال صحيح أو تواصل مع الدعم\\.")
        try:
            bot.edit_message_caption(
                f"❌ رُفض `{uid}`",
                call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    bot.answer_callback_query(call.id)

# ══════════════════════════════════════════════════════
# 10. أوامر الأدمن
# ══════════════════════════════════════════════════════

def _admin_only(m) -> bool:
    return m.chat.id == ADMIN_ID

@bot.message_handler(commands=["vip"])
def cmd_vip(m):
    """/vip [chat_id] — تفعيل VIP"""
    if not _admin_only(m): return
    parts = m.text.split()
    if len(parts) < 2:
        bot.send_message(m.chat.id, "الاستخدام: `/vip [chat_id]`"); return
    try:
        uid = int(parts[1])
    except ValueError:
        bot.send_message(m.chat.id, "❌ ID غير صحيح\\."); return
    activate(uid, "VIP")
    try: bot.send_message(uid, "🌟 تم تفعيل اشتراك VIP من قِبل الإدارة\\!")
    except Exception: pass
    bot.send_message(m.chat.id, f"✅ VIP فعّال للمستخدم `{uid}`\\.")

@bot.message_handler(commands=["revoke"])
def cmd_revoke(m):
    """/revoke [chat_id] — إلغاء اشتراك"""
    if not _admin_only(m): return
    parts = m.text.split()
    if len(parts) < 2:
        bot.send_message(m.chat.id, "الاستخدام: `/revoke [chat_id]`"); return
    try:
        uid = int(parts[1])
    except ValueError:
        bot.send_message(m.chat.id, "❌ ID غير صحيح\\."); return
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_vip=0, expiry_date=NULL WHERE chat_id=?", (uid,)
        )
    bot.send_message(m.chat.id, f"✅ تم إلغاء اشتراك `{uid}`\\.")

@bot.message_handler(commands=["holiday"])
def cmd_holiday(m):
    global IS_HOLIDAY
    if not _admin_only(m): return
    IS_HOLIDAY = not IS_HOLIDAY
    bot.send_message(m.chat.id, "🏖️ وضع العطلة مفعّل" if IS_HOLIDAY else "✅ وضع العطلة ملغى")

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not _admin_only(m): return
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        linked  = conn.execute("SELECT COUNT(*) FROM users WHERE username IS NOT NULL").fetchone()[0]
        vip     = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip=1").fetchone()[0]
        active  = conn.execute(
            "SELECT COUNT(*) FROM users WHERE expiry_date > datetime('now')"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE status='pending'"
        ).fetchone()[0]
    bot.send_message(m.chat.id,
        f"📊 *إحصائيات:*\n\n"
        f"👥 المستخدمون: {total}\n"
        f"🔗 مرتبطون: {linked}\n"
        f"🌟 VIP: {vip}\n"
        f"✅ اشتراك نشط: {active}\n"
        f"⏳ طلبات دفع معلقة: {pending}\n"
        f"💵 سعر الاشتراك: {price_str()}\n"
        f"🏖️ وضع العطلة: {'مفعّل' if IS_HOLIDAY else 'ملغى'}")

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not _admin_only(m): return
    text = m.text.replace("/broadcast", "", 1).strip()
    if not text:
        bot.send_message(m.chat.id, "الاستخدام: `/broadcast [الرسالة]`"); return
    with get_db() as conn:
        uids = [r[0] for r in conn.execute("SELECT chat_id FROM users").fetchall()]
    ok = 0
    for uid in uids:
        try:
            bot.send_message(uid, f"📢 *إشعار:*\n\n{text}")
            ok += 1
        except Exception:
            pass
    bot.send_message(m.chat.id, f"✅ أُرسلت لـ {ok}/{len(uids)} مستخدم\\.")

# ══════════════════════════════════════════════════════
# 11. التقارير الدورية
# ══════════════════════════════════════════════════════
def broadcast_reports():
    if IS_HOLIDAY:
        return
    with get_db() as conn:
        users = conn.execute(
            "SELECT chat_id, username, password FROM users WHERE username IS NOT NULL"
        ).fetchall()

    for row in users:
        uid, user, pwd = row["chat_id"], row["username"], row["password"]
        if not check_access(uid)[0]:
            continue
        res = run_moodle(user, pwd)
        if res["status"] != "success" or "لا يوجد تحديثات" in res["message"]:
            continue
        msg = res["message"]

        # لا ترسل إذا نفس المحتوى السابق
        h = hashlib.md5(msg.encode()).hexdigest()
        with get_db() as conn:
            old = conn.execute("SELECT last_hash FROM users WHERE chat_id=?", (uid,)).fetchone()
            if old and old["last_hash"] == h:
                continue
            try:
                bot.send_message(uid, f"🔔 *تقرير المودل:*\n\n{msg}")
                conn.execute(
                    "UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?",
                    (h, datetime.now().strftime("%Y-%m-%d %H:%M"), uid)
                )
            except Exception as e:
                log.warning(f"فشل إرسال تقرير لـ {uid}: {e}")

# ══════════════════════════════════════════════════════
# 12. المُجدوِل الخلفي
# ══════════════════════════════════════════════════════
def _scheduler():
    schedule.every(6).hours.do(broadcast_reports)
    schedule.every(2).minutes.do(poll_payments)
    schedule.every(12).hours.do(refresh_rate)   # كل 12 ساعة يكفي لسعر الصرف
    while True:
        schedule.run_pending()
        time.sleep(30)

# ══════════════════════════════════════════════════════
# 13. التشغيل
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    refresh_rate()
    threading.Thread(target=_scheduler, daemon=True).start()
    log.info("البوت يعمل...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
