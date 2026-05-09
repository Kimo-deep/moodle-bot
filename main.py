"""
بوت مودل الأقصى
================
متغيرات البيئة:
  TOKEN, GROQ-KEY, BINANCE_API_KEY (اختياري), BINANCE_SECRET_KEY (اختياري)
"""

import os, hmac, hashlib, json, uuid, time, threading, sqlite3, schedule, requests, logging, re
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
ILS_PER_USD    = 3.7
DB_PATH        = "/app/data/users.db"

bot    = telebot.TeleBot(TOKEN)
client = Groq(api_key=GROQ_KEY)
IS_HOLIDAY    = False
FEEDBACK_MODE = {}   # chat_id → True إذا المستخدم في وضع إرسال ملاحظة

# ══════════════════════════════════════════════════════
# 2. قاعدة البيانات
# ══════════════════════════════════════════════════════
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    conn.row_factory = sqlite3.Row
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
            CREATE TABLE IF NOT EXISTS feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                username   TEXT,
                message    TEXT    NOT NULL,
                sent_at    TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'new'
            );
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);
        """)

# ══════════════════════════════════════════════════════
# 3. سعر الصرف
# ══════════════════════════════════════════════════════
def refresh_rate():
    global ILS_PER_USD
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=8).json()
        ILS_PER_USD = round(r["rates"]["ILS"], 2)
    except Exception as e:
        log.warning(f"فشل تحديث سعر الصرف: {e}")

def price_str():
    return f"{PRICE_USD}$ USDT (≈ {round(PRICE_USD * ILS_PER_USD, 1)} ₪)"

# ══════════════════════════════════════════════════════
# 4. الاشتراك
# ══════════════════════════════════════════════════════
def check_access(chat_id: int):
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
# 5. استخراج بيانات الحدث من HTML (الحل الصحيح للوقت)
# ══════════════════════════════════════════════════════
# تقويم مودل يضع الوقت في: <span class="date"> أو <a class="..."> أو data-*
# البنية الفعلية:
#   <div class="event">
#     <div class="referer">اسم المادة</div>
#     <h3><a href="...">اسم النشاط</a></h3>
#     <div class="date">الاثنين، 11 مايو، 2:00 م</div>   ← وقت البداية
#     <div class="date">... يُغلق ...</div>               ← وقت النهاية (إذا وُجد)
#     <p>وصف اختياري</p>
#   </div>

def _extract_event(ev) -> dict:
    """
    يستخرج من div.event:
      name, course, url, date_open, date_close, description, type_hint
    """
    # ── اسم النشاط ───────────────────────────────────
    title_tag = ev.find("h3") or ev.find(class_="name")
    name = title_tag.get_text(" ", strip=True) if title_tag else ""

    # ── رابط النشاط ──────────────────────────────────
    a_tag = (title_tag.find("a", href=True) if title_tag else None) or ev.find("a", href=True)
    url   = a_tag["href"] if a_tag else ""

    # ── اسم المادة ───────────────────────────────────
    course = ""
    for sel in ["div.referer", "div.course-name", "small", "div.description a"]:
        tag = ev.select_one(sel)
        if tag:
            t = tag.get_text(" ", strip=True)
            # استبعد النص الطويل جداً (ليس اسم مادة)
            if t and len(t) < 80:
                course = t
                break

    # ── التواريخ ─────────────────────────────────────
    # مودل يضع الوقت في عدة أشكال، نجمعها كلها
    dates = []

    # 1. عناصر <time datetime="...">
    for t in ev.find_all("time"):
        dt_attr = t.get("datetime", "")
        label   = t.get_text(" ", strip=True)
        if dt_attr:
            try:
                dt = datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).replace(tzinfo=None)
                dates.append((dt, label or dt_attr))
            except Exception:
                pass
        elif label:
            dates.append((None, label))

    # 2. عناصر .date أو .event-date
    if not dates:
        for sel in [".date", ".event-date", ".col-11"]:
            for tag in ev.select(sel):
                txt = tag.get_text(" ", strip=True)
                if txt and len(txt) > 3:
                    dates.append((None, txt))

    # 3. نص عام إذا لم يُجد شيء
    raw_text = ev.get_text(" ", strip=True)

    # ── تحديد date_open و date_close ─────────────────
    date_open = date_close = ""
    if len(dates) == 1:
        # حدث واحد = موعد التسليم
        date_close = dates[0][1]
    elif len(dates) >= 2:
        date_open  = dates[0][1]
        date_close = dates[1][1]
    elif dates:
        date_close = dates[0][1]

    # ── الوصف (بدون روابط وعناوين) ───────────────────
    # نأخذ النص الخام كاملاً ونُنظفه
    desc = raw_text

    return {
        "name":       name or desc[:60],
        "course":     course,
        "url":        url,
        "url_lower":  url.lower(),
        "date_open":  date_open,
        "date_close": date_close,
        "raw":        raw_text,
    }

def _clean_name(name: str) -> str:
    """يزيل فقرات التعليمات من الاسم."""
    noise = [
        "حدث المساق", "إذهب إلى النشاط", "إضافة تسليم", "يرجى الالتزام",
        "التسليم فقط", "ولن يتم", "WhatsApp", "واتساب", "PDF", "Moodle",
        "مودل", "يمكنك", "يجب", "تقديم الواجب", "رابط",
    ]
    for n in noise:
        # احذف الجملة التي تحتوي على الكلمة
        name = re.sub(rf"[^.!؟]*{re.escape(n)}[^.!؟]*[.!؟]?", "", name, flags=re.IGNORECASE)
    return name.strip()

def _format_event_block(ev: dict, kind: str) -> str:
    """يبني نص الحدث المنسق مباشرةً بدون AI."""
    name   = _clean_name(ev["name"]) or "—"
    course = ev["course"] or "غير محدد"

    # استخراج اسم الدكتور من اسم المادة إذا كان مدمجاً (شائع في مودل الأقصى)
    # مثال: "خوارزميات متقدمة أ.فراس فؤاد" → course="خوارزميات متقدمة", doctor="فراس فؤاد"
    doctor = "غير محدد"
    doc_match = re.search(r"[أا]\.\s*([\w\s]{4,30})", course)
    if doc_match:
        doctor = doc_match.group(1).strip()
        course = course[:doc_match.start()].strip()

    lines = [f"▪️ {name}", f"   📌 المادة: {course}", f"   👨‍🏫 الدكتور: {doctor}"]

    if kind == "exam":
        if ev["date_open"] and ev["date_close"]:
            lines.append(f"   🕐 يفتح: {ev['date_open']} | يغلق: {ev['date_close']}")
        elif ev["date_close"]:
            lines.append(f"   🕐 يغلق: {ev['date_close']}")
        elif ev["date_open"]:
            lines.append(f"   🕐 يفتح: {ev['date_open']}")
    else:  # assign / lecture / meeting
        if ev["date_close"]:
            lines.append(f"   📅 آخر موعد: {ev['date_close']}")
        elif ev["date_open"]:
            lines.append(f"   📅 الموعد: {ev['date_open']}")

    return "\n".join(lines)

# ══════════════════════════════════════════════════════
# 6. كشف المنجز
# ══════════════════════════════════════════════════════
_CAL_DONE_KW = [
    "تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال",
    "attempt already", "تم المحاولة", "no attempts allowed", "past due", "overdue",
]

def _quick_done(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in _CAL_DONE_KW)

def _assign_done(session, url: str) -> bool:
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        for row in soup.find_all("tr"):
            th = row.find("th"); td = row.find("td")
            if not (th and td): continue
            label = th.get_text(strip=True).lower()
            value = td.get_text(strip=True).lower()
            if not any(k in label for k in ["حالة التسليم", "submission status", "status"]):
                continue
            if any(k in value for k in ["لم يُسلَّم", "no submission", "not submitted", "لم يتم"]):
                return False
            return True
        page = soup.get_text(" ", strip=True).lower()
        return any(k in page for k in [
            "تعديل التسليم", "edit submission", "you have submitted", "already submitted",
        ])
    except Exception:
        return False

def _quiz_done(session, url: str) -> bool:
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        page = soup.get_text(" ", strip=True).lower()
        if any(k in page for k in [
            "لقد أنهيت", "your last attempt", "آخر محاولة", "no more attempts",
            "لا محاولات متبقية", "مراجعة المحاولة", "review attempt",
            "your grade", "درجتك", "grade:", "attempt 1", "المحاولة 1",
        ]):
            return True
        return bool(soup.find("table", {"class": lambda x: x and "quizattemptsummary" in x}))
    except Exception:
        return False

# ══════════════════════════════════════════════════════
# 7. محرك المودل (استخراج منظم + تنسيق مباشر)
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
        soup = BeautifulSoup(session.get(login_url, timeout=20).text, "html.parser")
        ti   = soup.find("input", {"name": "logintoken"})
        if not ti:
            return {"status": "error", "message": "⚠️ تعذّر الوصول إلى صفحة تسجيل الدخول."}
        resp = session.post(login_url,
                            data={"username": username, "password": password, "logintoken": ti["value"]},
                            timeout=20)
        if "login" in resp.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        # ── صفحة التقويم ──────────────────────────────
        soup = BeautifulSoup(
            session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming",
                        timeout=20).text, "html.parser"
        )

        lectures, meetings, exams, assignments = [], [], [], []
        skipped = 0

        for ev_div in soup.find_all("div", {"class": "event"}):
            raw_txt = ev_div.get_text(" ", strip=True)

            # خط 1: كلمات سريعة
            if _quick_done(raw_txt):
                skipped += 1
                continue

            ev = _extract_event(ev_div)
            ll = ev["url_lower"]
            tl = ev["raw"].lower()

            is_quiz   = "quiz"   in ll or any(w in tl for w in _EXAM_KW)
            is_assign = "assign" in ll or any(w in tl for w in _ASSIGN_KW)
            is_meet   = any(x in ll for x in _MEET_KW) or "لقاء" in tl

            # خط 2 و 3: فحص الصفحة الفعلية
            if ev["url"]:
                if is_assign and not is_quiz:
                    if _assign_done(session, ev["url"]):
                        skipped += 1
                        continue
                elif is_quiz:
                    if _quiz_done(session, ev["url"]):
                        skipped += 1
                        continue

            if is_quiz:
                exams.append(_format_event_block(ev, "exam"))
            elif is_meet:
                meetings.append(_format_event_block(ev, "meeting"))
            elif is_assign:
                assignments.append(_format_event_block(ev, "assign"))
            else:
                lectures.append(_format_event_block(ev, "lecture"))

        if not any([lectures, meetings, exams, assignments]):
            note = f"\n_(مخفي: {skipped} منجز)_" if skipped else ""
            return {"status": "success", "message": f"✅ لا يوجد تحديثات جديدة حالياً.{note}"}

        hidden = f"\n\n_(تم إخفاء {skipped} عنصر منجز)_" if skipped else ""
        parts  = []

        if lectures:
            parts.append("📚 *المحاضرات:*\n" + "\n\n".join(lectures))
        if meetings:
            parts.append("🎥 *اللقاءات:*\n" + "\n\n".join(meetings))
        if exams:
            parts.append("📝 *الاختبارات:*\n" + "\n\n".join(exams))
        if assignments:
            parts.append("⚠️ *التكاليف والتجارب:*\n" + "\n\n".join(assignments))

        return {"status": "success", "message": "\n\n".join(parts) + hidden}

    except requests.RequestException as e:
        log.error(f"خطأ شبكة: {e}")
        return {"status": "error", "message": "⚠️ المودل لا يستجيب، حاول لاحقاً."}
    except Exception as e:
        log.error(f"خطأ run_moodle: {e}")
        return {"status": "error", "message": f"⚠️ خطأ: {str(e)[:60]}"}

# ══════════════════════════════════════════════════════
# 8. Binance Pay
# ══════════════════════════════════════════════════════
def _bin_headers(body: str) -> dict:
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time() * 1000))
    raw   = f"{ts}\n{nonce}\n{body}\n"
    sig   = hmac.new(BIN_SECRET.encode(), raw.encode(), hashlib.sha512).hexdigest().upper()
    return {
        "Content-Type":              "application/json",
        "BinancePay-Timestamp":      ts,
        "BinancePay-Nonce":          nonce,
        "BinancePay-Certificate-SN": BIN_CERT,
        "BinancePay-Signature":      sig,
    }

def binance_create(chat_id: int):
    if not (BIN_CERT and BIN_SECRET):
        return None, "Binance Pay غير مفعّل."
    order_id = f"MDB_{chat_id}_{int(time.time())}"
    body = json.dumps({
        "env":             {"terminalType": "WEB"},
        "merchantTradeNo": order_id,
        "orderAmount":     f"{PRICE_USD:.2f}",
        "currency":        "USDT",
        "description":     "Moodle Bot Monthly",
        "goodsDetails": [{
            "goodsType": "02", "goodsCategory": "Z000",
            "referenceGoodsId": "monthly", "goodsName": "Moodle Bot",
            "goodsUnitAmount": {"currency": "USDT", "amount": f"{PRICE_USD:.2f}"},
        }],
    }, separators=(",", ":"))
    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v2/order",
            headers=_bin_headers(body), data=body, timeout=15
        ).json()
        if r.get("status") == "SUCCESS":
            with get_db() as conn:
                conn.execute("INSERT OR IGNORE INTO payments VALUES (?,?,?,?)",
                             (order_id, chat_id,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending"))
            return r["data"]["checkoutUrl"], order_id
        return None, f"Binance: {r.get('errorMessage','خطأ غير معروف')}"
    except Exception as e:
        return None, str(e)[:60]

def binance_query(order_id: str):
    if not (BIN_CERT and BIN_SECRET): return None
    body = json.dumps({"merchantTradeNo": order_id}, separators=(",", ":"))
    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v1/order/query",
            headers=_bin_headers(body), data=body, timeout=10
        ).json()
        if r.get("status") == "SUCCESS":
            return r["data"]["status"]
    except Exception as e:
        log.warning(f"Binance query: {e}")
    return None

def poll_payments():
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT order_id, chat_id FROM payments WHERE status='pending' AND created_at >= ?",
            (cutoff,)
        ).fetchall()
        conn.execute("DELETE FROM payments WHERE status='pending' AND created_at < ?", (cutoff,))

    for row in rows:
        st = binance_query(row["order_id"])
        if st == "PAID":
            activate(row["chat_id"], "monthly")
            with get_db() as conn:
                conn.execute("UPDATE payments SET status='paid' WHERE order_id=?", (row["order_id"],))
            try: bot.send_message(row["chat_id"], "🎉 تم استلام دفعتك وتفعيل اشتراكك تلقائياً!")
            except: pass
        elif st in ("CANCELLED", "EXPIRED"):
            with get_db() as conn:
                conn.execute("UPDATE payments SET status=? WHERE order_id=?",
                             (st.lower(), row["order_id"]))

# ══════════════════════════════════════════════════════
# 9. أوامر البوت
# ══════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    kb.row("💳 اشتراك",   "📝 ملاحظة أو شكوى")
    kb.row("❓ مساعدة")
    bot.send_message(m.chat.id,
        "🎓 *مرحباً في بوت مودل الأقصى*\n\n"
        "• فحص تلقائي كل 6 ساعات\n"
        "• يُخفي التكاليف المسلّمة والكويزات المحلولة\n"
        "• لا يُرسل إذا لا يوجد جديد\n"
        "• يمكنك إرسال ملاحظات أو شكاوى مباشرة",
        parse_mode="Markdown", reply_markup=kb)

# ── فحص ─────────────────────────────────────────────
def _do_check(chat_id: int):
    ok, label = check_access(chat_id)
    if not ok:
        bot.send_message(chat_id,
            "🚫 اشتراكك منتهٍ. استخدم /subscribe للتجديد.")
        return
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, password FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row and row["username"]:
        wm  = bot.send_message(chat_id, f"🔍 جاري الفحص ({label})...")
        res = run_moodle(row["username"], row["password"])
        try:
            bot.edit_message_text(res["message"], chat_id, wm.message_id,
                                  parse_mode="Markdown")
        except Exception:
            bot.send_message(chat_id, res["message"], parse_mode="Markdown")
    else:
        wm = bot.send_message(chat_id, "📋 أرسل رقمك الجامعي:")
        bot.register_next_step_handler(wm, _step_user)

@bot.message_handler(commands=["check"])
def cmd_check(m): _do_check(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def btn_check(m): _do_check(m.chat.id)

# ── حالتي ────────────────────────────────────────────
def _do_status(chat_id: int):
    ok, label = check_access(chat_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, last_report FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    linked = (f"✅ مرتبط برقم `{row['username']}`"
              if row and row["username"] else "❌ غير مرتبط — /check")
    last   = (f"\n📅 آخر تقرير: {row['last_report']}"
              if row and row["last_report"] else "\n📅 لم يُرسل بعد")
    sub    = f"✅ {label}" if ok else "❌ منتهٍ — /subscribe"
    trial  = (f"\n⏳ التجربة تنتهي: {FREE_TRIAL_END:%Y-%m-%d}"
              if datetime.now() < FREE_TRIAL_END else "")
    bot.send_message(chat_id,
        f"👤 *حالة حسابك:*\n\n🔗 {linked}\n🎫 {sub}{trial}{last}",
        parse_mode="Markdown")

@bot.message_handler(commands=["status"])
def cmd_status(m): _do_status(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 حالتي")
def btn_status(m): _do_status(m.chat.id)

# ── اشتراك ───────────────────────────────────────────
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
        "ادفع ثم أرسل صورة الإيصال أو استخدم زر الدفع المباشر.",
        parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(m): _do_subscribe(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def btn_subscribe(m): _do_subscribe(m.chat.id)

# ── مساعدة ───────────────────────────────────────────
def _do_help(chat_id: int):
    bot.send_message(chat_id,
        "📖 *قائمة الأوامر:*\n\n"
        "/check — فحص المودل الآن\n"
        "/status — حالة حسابك\n"
        "/subscribe — تفعيل اشتراك\n"
        "/feedback — إرسال ملاحظة أو شكوى\n"
        "/unlink — إلغاء ربط حسابك\n\n"
        "💡 *البوت يُخفي تلقائياً:*\n"
        "• التكاليف المسلّمة\n"
        "• الكويزات المحلولة أو المنتهية المحاولات",
        parse_mode="Markdown")

@bot.message_handler(commands=["help"])
def cmd_help(m): _do_help(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "❓ مساعدة")
def btn_help(m): _do_help(m.chat.id)

# ── إلغاء الربط ──────────────────────────────────────
@bot.message_handler(commands=["unlink"])
def cmd_unlink(m):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET username=NULL, password=NULL WHERE chat_id=?", (m.chat.id,)
        )
    bot.send_message(m.chat.id, "🔓 تم إلغاء الربط. استخدم /check للربط من جديد.")

# ── ربط الحساب (خطوات) ──────────────────────────────
def _step_user(msg):
    if not msg.text:
        wm = bot.send_message(msg.chat.id, "❌ أرسل الرقم الجامعي كنص:")
        bot.register_next_step_handler(wm, _step_user); return
    user = msg.text.strip()
    wm   = bot.send_message(msg.chat.id, "🔐 أرسل كلمة المرور:")
    bot.register_next_step_handler(wm, lambda m2: _step_pwd(m2, user))

def _step_pwd(msg, user):
    if not msg.text:
        wm = bot.send_message(msg.chat.id, "❌ أرسل كلمة المرور كنص:")
        bot.register_next_step_handler(wm, lambda m2: _step_pwd(m2, user)); return
    pwd = msg.text.strip()
    wm  = bot.send_message(msg.chat.id, "⏳ جاري التحقق...")
    res = run_moodle(user, pwd)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)",
                (msg.chat.id, user, pwd)
            )
        text = f"✅ تم الربط! ستصلك تقارير كل 6 ساعات.\n\n{res['message']}"
        try:
            bot.edit_message_text(text, msg.chat.id, wm.message_id, parse_mode="Markdown")
        except Exception:
            bot.send_message(msg.chat.id, text, parse_mode="Markdown")
    else:
        try:
            bot.edit_message_text(res["message"], msg.chat.id, wm.message_id)
        except Exception:
            bot.send_message(msg.chat.id, res["message"])

# ══════════════════════════════════════════════════════
# 10. نظام الملاحظات والشكاوى
# ══════════════════════════════════════════════════════

@bot.message_handler(commands=["feedback"])
def cmd_feedback(m):
    _start_feedback(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📝 ملاحظة أو شكوى")
def btn_feedback(m):
    _start_feedback(m.chat.id)

def _start_feedback(chat_id: int):
    FEEDBACK_MODE[chat_id] = True
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="feedback_cancel"))
    bot.send_message(chat_id,
        "📝 *إرسال ملاحظة أو شكوى*\n\n"
        "اكتب ملاحظتك أو شكواك وسيطّلع عليها الأدمن.\n"
        "يمكنك ذكر أي مشكلة تقنية أو اقتراح تحسين.",
        parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(func=lambda m: FEEDBACK_MODE.get(m.chat.id))
def receive_feedback(m):
    if not m.text:
        bot.send_message(m.chat.id, "❌ أرسل ملاحظتك كنص.")
        return

    FEEDBACK_MODE.pop(m.chat.id, None)
    uname = f"@{m.from_user.username}" if m.from_user.username else "بدون يوزرنيم"


    # إرسال للأدمن
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("↩️ رد", callback_data=f"fb_reply_{m.chat.id}"),
        types.InlineKeyboardButton("✅ تم", callback_data=f"fb_done_{m.chat.id}")
    )
    try:
        bot.send_message(ADMIN_ID,
            f"📝 *ملاحظة جديدة*\n\n"
            f"👤 المستخدم: {uname} (`{m.chat.id}`)\n"
            f"📅 {datetime.now():%Y-%m-%d %H:%M}\n\n"
            f"💬 *الرسالة:*\n{m.text}",
            parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        log.error(f"فشل إرسال الملاحظة للأدمن: {e}")

    bot.send_message(m.chat.id,
        "✅ تم إرسال ملاحظتك! سيطّلع عليها الأدمن قريباً.\n"
        "شكراً على تواصلك معنا. 🙏")

@bot.callback_query_handler(func=lambda c: c.data == "feedback_cancel")
def cb_feedback_cancel(call):
    FEEDBACK_MODE.pop(call.message.chat.id, None)
    bot.answer_callback_query(call.id, "تم الإلغاء.")
    bot.edit_message_text("❌ تم إلغاء إرسال الملاحظة.",
                          call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("fb_reply_"))
def cb_fb_reply(call):
    if call.from_user.id != ADMIN_ID: return
    uid = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    wm = bot.send_message(ADMIN_ID,
        f"✍️ اكتب ردك على المستخدم `{uid}`:", parse_mode="Markdown")
    bot.register_next_step_handler(wm, lambda m: _send_admin_reply(m, uid))

def _send_admin_reply(msg, uid: int):
    if not msg.text:
        bot.send_message(ADMIN_ID, "❌ أرسل الرد كنص."); return
    try:
        bot.send_message(uid,
            f"📨 *رد من الإدارة:*\n\n{msg.text}",
            parse_mode="Markdown")
        bot.send_message(ADMIN_ID, f"✅ تم إرسال الرد للمستخدم `{uid}`.")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ فشل الإرسال: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("fb_done_"))
def cb_fb_done(call):
    if call.from_user.id != ADMIN_ID: return
    uid = int(call.data.split("_")[2])
    with get_db() as conn:
        conn.execute(
            "UPDATE feedback SET status='resolved' WHERE chat_id=? AND status='new'", (uid,)
        )
    bot.answer_callback_query(call.id, "✅ تم وضع علامة مُعالَج.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=None)
    except Exception:
        pass

# أمر الأدمن لعرض الملاحظات المعلقة
@bot.message_handler(commands=["feedbacks"])
def cmd_feedbacks(m):
    if m.chat.id != ADMIN_ID: return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, username, message, sent_at FROM feedback "
            "WHERE status='new' ORDER BY sent_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        bot.send_message(m.chat.id, "✅ لا توجد ملاحظات معلقة."); return
    for row in rows:
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("↩️ رد", callback_data=f"fb_reply_{row['chat_id']}"),
            types.InlineKeyboardButton("✅ تم", callback_data=f"fb_done_{row['chat_id']}")
        )
        bot.send_message(m.chat.id,
            f"📝 *#{row['id']}* — {row['username']} (`{row['chat_id']}`)\n"
            f"📅 {row['sent_at']}\n\n{row['message']}",
            parse_mode="Markdown", reply_markup=kb)

# ══════════════════════════════════════════════════════
# 11. استلام الإيصالات اليدوية
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=["photo"])
def handle_photo(m):
    # تجاهل إذا كان في وضع الملاحظة
    if FEEDBACK_MODE.get(m.chat.id):
        bot.send_message(m.chat.id, "📝 أرسل ملاحظتك كنص."); return
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"pay_{m.chat.id}"),
        types.InlineKeyboardButton("❌ رفض",        callback_data=f"rej_{m.chat.id}")
    )
    try:
        bot.send_photo(ADMIN_ID, m.photo[-1].file_id,
            caption=(f"📩 *طلب تفعيل يدوي*\n"
                     f"👤 {m.from_user.username or 'بدون يوزرنيم'} (`{m.chat.id}`)\n"
                     f"📅 {datetime.now():%Y-%m-%d %H:%M}"),
            reply_markup=kb, parse_mode="Markdown")
        bot.reply_to(m, "⏳ تم إرسال الإيصال. سيُشعرك الأدمن فور المراجعة.")
    except Exception as e:
        log.error(f"handle_photo: {e}")
        bot.reply_to(m, "⚠️ حدث خطأ. حاول مجدداً.")

# ══════════════════════════════════════════════════════
# 12. Callbacks الدفع
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
            "اضغط *ادفع الآن* ثم عد واضغط *تحقق من الدفع* للتفعيل الفوري.",
            parse_mode="Markdown", reply_markup=kb)
    else:
        bot.send_message(call.message.chat.id, f"❌ {result}\nأرسل الإيصال يدوياً.")

@bot.callback_query_handler(func=lambda c: c.data == "sub_manual")
def cb_manual(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📷 أرسل صورة الإيصال وسيراجعها الأدمن.")

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
            bot.send_message(call.message.chat.id, "🎉 تم تفعيل اشتراكك بنجاح!")
        else:
            bot.send_message(call.message.chat.id, "⚠️ لم يُعثر على الطلب.")
    elif st == "UNPAID":
        bot.send_message(call.message.chat.id,
            "⏳ لم تصل الدفعة بعد. انتظر دقيقة وأعد المحاولة.")
    elif st in ("CANCELLED", "EXPIRED"):
        bot.send_message(call.message.chat.id,
            "❌ انتهت صلاحية الطلب. أنشئ طلباً جديداً من /subscribe")
    else:
        bot.send_message(call.message.chat.id, "⚠️ لم يرد Binance. حاول لاحقاً.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_") or c.data.startswith("rej_"))
def cb_admin(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية."); return
    action, uid_str = call.data.split("_", 1)
    uid = int(uid_str)
    if action == "pay":
        activate(uid, "monthly")
        bot.send_message(uid, "✅ تم تفعيل اشتراكك لمدة شهر!")
        try: bot.edit_message_caption(f"✅ تم تفعيل `{uid}`",
                                      call.message.chat.id, call.message.message_id,
                                      parse_mode="Markdown")
        except: pass
    elif action == "rej":
        bot.send_message(uid, "❌ تم رفض طلبك. أرسل إيصالاً صحيحاً أو تواصل مع الدعم.")
        try: bot.edit_message_caption(f"❌ رُفض `{uid}`",
                                      call.message.chat.id, call.message.message_id,
                                      parse_mode="Markdown")
        except: pass
    bot.answer_callback_query(call.id)

# ══════════════════════════════════════════════════════
# 13. أوامر الأدمن
# ══════════════════════════════════════════════════════
def _admin(m): return m.chat.id == ADMIN_ID

@bot.message_handler(commands=["vip"])
def cmd_vip(m):
    if not _admin(m): return
    parts = m.text.split()
    if len(parts) < 2: bot.send_message(m.chat.id, "الاستخدام: /vip [chat_id]"); return
    try: uid = int(parts[1])
    except: bot.send_message(m.chat.id, "❌ ID غير صحيح."); return
    activate(uid, "VIP")
    try: bot.send_message(uid, "🌟 تم تفعيل اشتراك VIP من قِبل الإدارة!")
    except: pass
    bot.send_message(m.chat.id, f"✅ VIP فعّال للمستخدم `{uid}`.", parse_mode="Markdown")

@bot.message_handler(commands=["revoke"])
def cmd_revoke(m):
    if not _admin(m): return
    parts = m.text.split()
    if len(parts) < 2: bot.send_message(m.chat.id, "الاستخدام: /revoke [chat_id]"); return
    try: uid = int(parts[1])
    except: bot.send_message(m.chat.id, "❌ ID غير صحيح."); return
    with get_db() as conn:
        conn.execute("UPDATE users SET is_vip=0, expiry_date=NULL WHERE chat_id=?", (uid,))
    bot.send_message(m.chat.id, f"✅ تم إلغاء اشتراك `{uid}`.", parse_mode="Markdown")

@bot.message_handler(commands=["holiday"])
def cmd_holiday(m):
    global IS_HOLIDAY
    if not _admin(m): return
    IS_HOLIDAY = not IS_HOLIDAY
    bot.send_message(m.chat.id, "🏖️ وضع العطلة مفعّل" if IS_HOLIDAY else "✅ وضع العطلة ملغى")

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not _admin(m): return
    with get_db() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        linked   = conn.execute("SELECT COUNT(*) FROM users WHERE username IS NOT NULL").fetchone()[0]
        vip      = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip=1").fetchone()[0]
        active   = conn.execute("SELECT COUNT(*) FROM users WHERE expiry_date > datetime('now')").fetchone()[0]
        pending  = conn.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
        new_fb   = conn.execute("SELECT COUNT(*) FROM feedback WHERE status='new'").fetchone()[0]
    bot.send_message(m.chat.id,
        f"📊 *إحصائيات:*\n\n"
        f"👥 المستخدمون: {total}\n"
        f"🔗 مرتبطون: {linked}\n"
        f"🌟 VIP: {vip}\n"
        f"✅ اشتراك نشط: {active}\n"
        f"⏳ طلبات دفع معلقة: {pending}\n"
        f"📝 ملاحظات جديدة: {new_fb}\n"
        f"💵 سعر الاشتراك: {price_str()}\n"
        f"🏖️ وضع العطلة: {'مفعّل' if IS_HOLIDAY else 'ملغى'}",
        parse_mode="Markdown")

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not _admin(m): return
    text = m.text.replace("/broadcast", "", 1).strip()
    if not text: bot.send_message(m.chat.id, "الاستخدام: /broadcast [الرسالة]"); return
    with get_db() as conn:
        uids = [r[0] for r in conn.execute("SELECT chat_id FROM users").fetchall()]
    ok = 0
    for uid in uids:
        try: bot.send_message(uid, f"📢 *إشعار:*\n\n{text}", parse_mode="Markdown"); ok += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ أُرسلت لـ {ok}/{len(uids)} مستخدم.")

# ══════════════════════════════════════════════════════
# 14. التقارير الدورية
# ══════════════════════════════════════════════════════
def broadcast_reports():
    if IS_HOLIDAY: return
    with get_db() as conn:
        users = conn.execute(
            "SELECT chat_id, username, password FROM users WHERE username IS NOT NULL"
        ).fetchall()

    for row in users:
        uid, user, pwd = row["chat_id"], row["username"], row["password"]
        if not check_access(uid)[0]: continue
        res = run_moodle(user, pwd)
        if res["status"] != "success" or "لا يوجد تحديثات" in res["message"]: continue
        msg = res["message"]
        h   = hashlib.md5(msg.encode()).hexdigest()
        with get_db() as conn:
            old = conn.execute("SELECT last_hash FROM users WHERE chat_id=?", (uid,)).fetchone()
            if old and old["last_hash"] == h: continue
            try:
                bot.send_message(uid, f"🔔 *تقرير المودل:*\n\n{msg}", parse_mode="Markdown")
                conn.execute(
                    "UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?",
                    (h, datetime.now().strftime("%Y-%m-%d %H:%M"), uid)
                )
            except Exception as e:
                log.warning(f"فشل إرسال تقرير لـ {uid}: {e}")

# ══════════════════════════════════════════════════════
# 15. المُجدوِل
# ══════════════════════════════════════════════════════
def _scheduler():
    schedule.every(6).hours.do(broadcast_reports)
    schedule.every(2).minutes.do(poll_payments)
    schedule.every(12).hours.do(refresh_rate)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ══════════════════════════════════════════════════════
# 16. التشغيل
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    refresh_rate()
    threading.Thread(target=_scheduler, daemon=True).start()
    log.info("البوت يعمل...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
