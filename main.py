"""
بوت مودل الأقصى — النسخة النهائية
=====================================
متغيرات البيئة المطلوبة:
  TOKEN              — Telegram Bot Token
  GROQ-KEY           — Groq API Key (غير مستخدم حالياً، التنسيق مباشر)
  BINANCE_API_KEY    — Binance Pay Certificate SN  (اختياري)
  BINANCE_SECRET_KEY — Binance Pay Secret Key      (اختياري)
"""

import os, hmac, hashlib, json, uuid, time, threading, sqlite3, schedule, requests, logging, re
from contextlib import contextmanager
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ══════════════════════════════════════════════════════════
# 1. الإعدادات
# ══════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TOKEN")
BIN_CERT   = os.getenv("BINANCE_API_KEY")
BIN_SECRET = os.getenv("BINANCE_SECRET_KEY")

ADMIN_ID       = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)
PRICE_USD      = 2.0
ILS_PER_USD    = 3.7          # يُحدَّث تلقائياً
DB_PATH        = "/app/data/users.db"

bot = telebot.TeleBot(TOKEN)
IS_HOLIDAY = False

# ══════════════════════════════════════════════════════════
# 2. قاعدة البيانات
# ══════════════════════════════════════════════════════════
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
            CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
        """)

# ══════════════════════════════════════════════════════════
# 3. سعر الصرف
# ══════════════════════════════════════════════════════════
def refresh_rate():
    global ILS_PER_USD
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=8).json()
        ILS_PER_USD = round(r["rates"]["ILS"], 2)
        log.info(f"سعر الصرف: 1$ = {ILS_PER_USD} ₪")
    except Exception as e:
        log.warning(f"فشل تحديث سعر الصرف: {e}")

def price_str():
    return f"{PRICE_USD}$ USDT (≈ {round(PRICE_USD * ILS_PER_USD, 1)} ₪)"

# ══════════════════════════════════════════════════════════
# 4. الاشتراك
# ══════════════════════════════════════════════════════════
def check_access(chat_id: int) -> tuple:
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
                days = (exp - datetime.now()).days
                return True, f"مشترك ({days} يوم متبقي)"
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

# ══════════════════════════════════════════════════════════
# 5. استخراج بيانات الأحداث من HTML
#
#  مودل الأقصى ينشئ حدثين منفصلين لكل اختبار:
#    "اختبار قصير رقم (1) يُفتح"   → وقت الفتح
#    "اختبار قصير رقم (1) يُغلق"   → وقت الإغلاق
#
#  بنية HTML الفعلية:
#    <div class="event">
#      <h3 class="name"><a href="...">اسم النشاط يُفتح</a></h3>
#      <div class="description">
#        <a href="/course/view.php?id=...">اسم المادة أ.اسم الدكتور</a>
#        <span class="timeremaining">...</span>
#      </div>
#      <a class="card-link" ...>
#        <div class="date">الاثنين, 11 مايو, 2:00 pm</div>
#      </a>
#    </div>
# ══════════════════════════════════════════════════════════

# الكلمات الدالة على فتح/إغلاق في نهاية اسم الحدث
_OPEN_KW  = ["يُفتح", "يفتح", " opens", " open"]
_CLOSE_KW = ["يُغلق", "يغلق", " closes", " close"]

# الضجيج الذي يُضاف أحياناً للنصوص
_NOISE = [
    "إذهب إلى النشاط", "إضافة تسليم", "حدث المساق",
    "يرجى الالتزام", "التسليم فقط", "ولن يتم",
    "WhatsApp", "واتساب", "Moodle", "مودل",
]

# نمط الوقت الذي يستخدمه مودل الأقصى:
# "الاثنين, 11 مايو, 2:00 PM"  أو  "غدًا, 11:59 PM"
_TIME_RE = re.compile(
    r"(?:الأحد|الاثنين|الثلاثاء|الأربعاء|الخميس|الجمعة|السبت|غدًا|غداً|اليوم)"
    r"[,،\s]+"
    r"(?:\d{1,2}\s+\w+[,،\s]+)?"   # رقم + شهر (اختياري لـ "غدًا")
    r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm|ص|م)",
    re.UNICODE
)

def _clean_noise(text: str) -> str:
    for n in _NOISE:
        idx = text.find(n)
        if idx != -1:
            text = text[:idx]
    return text.strip()

def _event_role(name: str) -> str:
    nl = name.lower()
    for k in _OPEN_KW:
        if nl.endswith(k.lower()) or k.lower() in nl:
            return "open"
    for k in _CLOSE_KW:
        if nl.endswith(k.lower()) or k.lower() in nl:
            return "close"
    return "single"

def _strip_role_suffix(name: str) -> str:
    """يحذف لاحقة يُفتح/يُغلق من نهاية الاسم."""
    for kw in _OPEN_KW + _CLOSE_KW:
        pattern = re.compile(re.escape(kw.strip()) + r"\s*$", re.IGNORECASE)
        name = pattern.sub("", name)
    return name.strip()

def _extract_time_from_div(ev) -> str:
    """
    يقرأ الوقت من عنصر .date أو .col-1 أو div مباشر.
    مودل الأقصى يضع الوقت في div خاص منفصل عن الاسم.
    """
    # 1. عنصر .date
    for sel in [".date", ".event-date", ".col-1 .date", ".card-body .date"]:
        tag = ev.select_one(sel)
        if tag:
            t = tag.get_text(" ", strip=True)
            if _TIME_RE.search(t):
                return t.strip()

    # 2. عنصر <time datetime="...">
    for tag in ev.find_all("time"):
        dt = tag.get("datetime", "")
        if dt:
            try:
                parsed = datetime.fromisoformat(dt.replace("Z", "+00:00")).replace(tzinfo=None)
                # تحويل إلى نص عربي مقروء
                days_ar = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
                months_ar = ["","يناير","فبراير","مارس","أبريل","مايو","يونيو",
                             "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
                d = days_ar[parsed.weekday()]
                mo = months_ar[parsed.month]
                hr = parsed.strftime("%I:%M").lstrip("0") or "12"
                ampm = "AM" if parsed.hour < 12 else "PM"
                return f"{d}, {parsed.day} {mo}, {hr} {ampm}"
            except Exception:
                pass
        label = tag.get_text(" ", strip=True)
        if label and _TIME_RE.search(label):
            return label.strip()

    # 3. regex مباشر على النص الكامل للعنصر
    # لكن نتجنب أخذ الوقت من داخل الاسم أو المادة
    # نبحث فقط بعد كلمة "مستحق" أو "يُفتح" أو "يُغلق" أو في آخر النص
    raw = ev.get_text(" ", strip=True)
    matches = _TIME_RE.findall(raw)
    if matches:
        return matches[-1].strip()  # آخر تطابق = الوقت الفعلي دائماً

    return ""

def _extract_course_and_doctor(ev) -> tuple:
    """
    يستخرج اسم المادة واسم الدكتور من HTML الحدث.
    مودل الأقصى يضع المادة في رابط /course/ أو في div.description.
    """
    course = ""
    doctor = "غير محدد"

    # 1. رابط يحتوي على /course/
    for a in ev.find_all("a", href=True):
        href = a.get("href", "")
        if "/course/" in href or "course" in href:
            t = a.get_text(" ", strip=True)
            # تجاهل إذا كان نفس الاسم أو قصير جداً
            if t and 3 < len(t) < 150:
                course = t
                break

    # 2. div.description أو small كـ fallback
    if not course:
        for sel in ["div.description", ".description", "small"]:
            tag = ev.select_one(sel)
            if tag:
                t = tag.get_text(" ", strip=True)
                # تجنب الوقت أو النصوص الطويلة جداً
                if t and 3 < len(t) < 200 and not _TIME_RE.search(t):
                    course = t
                    break

    if not course:
        return "غير محدد", "غير محدد"

    # فصل الدكتور عن المادة: "خوارزميات متقدمة أ.فراس فؤاد العجلة"
    doc_m = re.search(r"\s+[أا]\.\s*([\u0600-\u06FF][^\n\u0600-\u06FF]{0,2}[\u0600-\u06FF\s]{2,25})", course)
    if doc_m:
        doctor = _clean_noise(doc_m.group(1)).strip()
        course = course[:doc_m.start()].strip()

    course = _clean_noise(course).strip() or "غير محدد"
    return course, doctor


def _extract_event(ev) -> dict:
    """يستخرج كل بيانات حدث مودل من عنصر HTML."""
    # ── الاسم ──
    h3   = ev.find("h3") or ev.find(class_="name")
    atag = (h3.find("a", href=True) if h3 else None) or ev.find("a", href=True)
    url  = atag["href"] if atag else ""
    raw_name = h3.get_text(" ", strip=True) if h3 else ""
    raw_name = _clean_noise(raw_name)

    role      = _event_role(raw_name)
    base_name = _strip_role_suffix(raw_name).strip() or raw_name[:70]

    # ── الوقت ──
    time_val = _extract_time_from_div(ev)

    # ── المادة والدكتور ──
    course, doctor = _extract_course_and_doctor(ev)

    return {
        "name":      base_name,
        "course":    course,
        "doctor":    doctor,
        "url":       url,
        "url_lower": url.lower(),
        "time":      time_val,
        "role":      role,       # "open" | "close" | "single"
        "raw":       ev.get_text(" ", strip=True),
    }


def _merge_exams(events: list) -> list:
    """
    يدمج حدثَي الفتح والإغلاق لنفس الاختبار في سجل واحد.
    المطابقة بـ (الاسم الأساسي + المادة).
    """
    merged  = {}   # key → dict
    singles = []

    for ev in events:
        key  = (ev["name"].strip().lower(), ev["course"].lower())
        role = ev["role"]

        if role == "single":
            singles.append({
                **ev,
                "date_open":  "",
                "date_close": ev["time"],
            })
            continue

        if key not in merged:
            merged[key] = {
                "name":       ev["name"],
                "course":     ev["course"],
                "doctor":     ev["doctor"],
                "url":        ev["url"],
                "url_lower":  ev["url_lower"],
                "date_open":  "",
                "date_close": "",
            }
        if role == "open":
            merged[key]["date_open"]  = ev["time"]
        else:
            merged[key]["date_close"] = ev["time"]

    return list(merged.values()) + singles


def _fmt_exam(ev: dict) -> str:
    lines = [
        f"▪️ *{ev['name']}*",
        f"   📌 {ev['course']}",
        f"   👨‍🏫 {ev['doctor']}",
    ]
    d_open  = ev.get("date_open",  "")
    d_close = ev.get("date_close", "")
    if d_open and d_close:
        lines.append(f"   🕐 يفتح: {d_open}")
        lines.append(f"   🔒 يغلق: {d_close}")
    elif d_open:
        lines.append(f"   🕐 يفتح: {d_open}")
    elif d_close:
        lines.append(f"   🔒 يغلق: {d_close}")
    return "\n".join(lines)


def _fmt_task(ev: dict) -> str:
    t = ev.get("date_close") or ev.get("time", "")
    lines = [
        f"▪️ *{ev['name']}*",
        f"   📌 {ev['course']}",
        f"   👨‍🏫 {ev['doctor']}",
    ]
    if t:
        lines.append(f"   📅 آخر موعد: {t}")
    return "\n".join(lines)


def _fmt_other(ev: dict) -> str:
    t = ev.get("time", "")
    lines = [
        f"▪️ *{ev['name']}*",
        f"   📌 {ev['course']}",
        f"   👨‍🏫 {ev['doctor']}",
    ]
    if t:
        lines.append(f"   📅 {t}")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════
# 6. كشف المنجز — 3 خطوط
# ══════════════════════════════════════════════════════════
_DONE_KW = [
    "تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال",
    "attempt already", "تم المحاولة", "no attempts allowed", "past due", "overdue",
]

def _quick_done(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in _DONE_KW)

def _assign_done(session, url: str) -> bool:
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        for tr in soup.find_all("tr"):
            th = tr.find("th"); td = tr.find("td")
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

# ══════════════════════════════════════════════════════════
# 7. محرك المودل
# ══════════════════════════════════════════════════════════
_EXAM_KW   = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test", "midterm"]
_ASSIGN_KW = ["تكليف", "واجب", "مهمة", "تقرير", "تجربة", "رفع", "ملف",
              "assignment", "task", "experiment", "report", "upload", "submit"]
_MEET_KW   = ["zoom", "meet", "bigbluebutton"]

def run_moodle(username: str, password: str) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    login_url = "https://moodle.alaqsa.edu.ps/login/index.php"
    try:
        # ── تسجيل الدخول ──
        soup = BeautifulSoup(session.get(login_url, timeout=20).text, "html.parser")
        ti   = soup.find("input", {"name": "logintoken"})
        if not ti:
            return {"status": "error", "message": "⚠️ تعذّر الوصول إلى صفحة تسجيل الدخول."}
        resp = session.post(login_url,
                            data={"username": username, "password": password,
                                  "logintoken": ti["value"]}, timeout=20)
        if "login" in resp.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        # ── صفحة التقويم ──
        soup = BeautifulSoup(
            session.get("https://moodle.alaqsa.edu.ps/calendar/view.php?view=upcoming",
                        timeout=20).text, "html.parser"
        )

        lectures, meetings, exams_raw, assignments = [], [], [], []
        skipped = 0

        for ev_div in soup.find_all("div", {"class": "event"}):
            raw_txt = ev_div.get_text(" ", strip=True)
            if _quick_done(raw_txt):
                skipped += 1; continue

            ev = _extract_event(ev_div)
            ll = ev["url_lower"]
            tl = ev["raw"].lower()

            is_quiz   = "quiz"   in ll or any(w in tl for w in _EXAM_KW)
            is_assign = "assign" in ll or any(w in tl for w in _ASSIGN_KW)
            is_meet   = any(x in ll for x in _MEET_KW) or "لقاء" in tl

            # فحص صفحة النشاط
            if ev["url"]:
                if is_assign and not is_quiz:
                    if _assign_done(session, ev["url"]):
                        skipped += 1; continue
                elif is_quiz:
                    if _quiz_done(session, ev["url"]):
                        skipped += 1; continue

            if is_quiz:     exams_raw.append(ev)
            elif is_meet:   meetings.append(_fmt_other(ev))
            elif is_assign: assignments.append(_fmt_task(ev))
            else:           lectures.append(_fmt_other(ev))

        # دمج أحداث الفتح/الإغلاق
        exams = [_fmt_exam(e) for e in _merge_exams(exams_raw)]

        # ── بناء التقرير ──
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        parts   = [f"🕐 *{now_str}*\n"]

        parts.append("📚 *المحاضرات:* " + ("لا يوجد" if not lectures else ""))
        if lectures:    parts[-1] += "\n\n" + "\n\n".join(lectures)

        parts.append("🎥 *اللقاءات:* " + ("لا يوجد" if not meetings else ""))
        if meetings:    parts[-1] += "\n\n" + "\n\n".join(meetings)

        parts.append("📝 *الاختبارات:* " + ("لا يوجد" if not exams else ""))
        if exams:       parts[-1] += "\n\n" + "\n\n".join(exams)

        parts.append("⚠️ *التكاليف والتجارب:* " + ("لا يوجد" if not assignments else ""))
        if assignments: parts[-1] += "\n\n" + "\n\n".join(assignments)

        hidden = f"\n\n_✅ تم إخفاء {skipped} عنصر منجز_" if skipped else ""
        return {"status": "success", "message": "\n\n".join(parts) + hidden}

    except requests.RequestException as e:
        log.error(f"خطأ شبكة: {e}")
        return {"status": "error", "message": "⚠️ المودل لا يستجيب، حاول لاحقاً."}
    except Exception as e:
        log.error(f"خطأ run_moodle: {e}")
        return {"status": "error", "message": f"⚠️ خطأ: {str(e)[:80]}"}

# ══════════════════════════════════════════════════════════
# 8. Binance Pay
# ══════════════════════════════════════════════════════════
def _bin_headers(body: str) -> dict:
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time() * 1000))
    sig   = hmac.new(
                BIN_SECRET.encode(),
                f"{ts}\n{nonce}\n{body}\n".encode(),
                hashlib.sha512
            ).hexdigest().upper()
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
        return None, f"Binance: {r.get('errorMessage', 'خطأ غير معروف')}"
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
            return r["data"]["status"]   # PAID / UNPAID / CANCELLED / EXPIRED
    except Exception as e:
        log.warning(f"Binance query: {e}")
    return None

def poll_payments():
    """يفحص الطلبات المعلقة ويحذف ما هو أقدم من 24 ساعة."""
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT order_id, chat_id FROM payments "
            "WHERE status='pending' AND created_at >= ?", (cutoff,)
        ).fetchall()
        conn.execute(
            "DELETE FROM payments WHERE status='pending' AND created_at < ?", (cutoff,)
        )
    for row in rows:
        st = binance_query(row["order_id"])
        if st == "PAID":
            activate(row["chat_id"], "monthly")
            with get_db() as conn:
                conn.execute("UPDATE payments SET status='paid' WHERE order_id=?",
                             (row["order_id"],))
            try: bot.send_message(row["chat_id"], "🎉 تم استلام دفعتك وتفعيل اشتراكك تلقائياً!")
            except: pass
        elif st in ("CANCELLED", "EXPIRED"):
            with get_db() as conn:
                conn.execute("UPDATE payments SET status=? WHERE order_id=?",
                             (st.lower(), row["order_id"]))

# ══════════════════════════════════════════════════════════
# 9. أوامر البوت
# ══════════════════════════════════════════════════════════

# ── /start ─────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    kb.row("💳 اشتراك",   "❓ مساعدة")
    bot.send_message(m.chat.id,
        "🎓 *مرحباً في بوت مودل الأقصى*\n\n"
        "• تقرير تلقائي كل 6 ساعات\n"
        "• يُخفي التكاليف المسلّمة والاختبارات المحلولة\n"
        "• يدمج حدثَي الفتح والإغلاق في اختبار واحد",
        parse_mode="Markdown", reply_markup=kb)

# ── فحص ────────────────────────────────────────────────
def _do_check(chat_id: int):
    ok, label = check_access(chat_id)
    if not ok:
        bot.send_message(chat_id,
            "🚫 اشتراكك منتهٍ.\nاستخدم /subscribe للتجديد.")
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

# ── حالتي ───────────────────────────────────────────────
def _do_status(chat_id: int):
    ok, label = check_access(chat_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, last_report FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    linked = (f"✅ مرتبط: `{row['username']}`"
              if row and row["username"] else "❌ غير مرتبط — /check")
    last   = (f"\n📅 آخر تقرير: {row['last_report']}"
              if row and row["last_report"] else "\n📅 لم يُرسل تقرير بعد")
    sub    = f"✅ {label}" if ok else "❌ منتهٍ — /subscribe"
    trial  = (f"\n⏳ تنتهي التجربة: {FREE_TRIAL_END:%Y-%m-%d}"
              if datetime.now() < FREE_TRIAL_END else "")
    bot.send_message(chat_id,
        f"👤 *حالة حسابك:*\n\n🔗 {linked}\n🎫 {sub}{trial}{last}",
        parse_mode="Markdown")

@bot.message_handler(commands=["status"])
def cmd_status(m): _do_status(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 حالتي")
def btn_status(m): _do_status(m.chat.id)

# ── اشتراك ──────────────────────────────────────────────
def _do_subscribe(chat_id: int):
    kb = types.InlineKeyboardMarkup()
    if BIN_CERT and BIN_SECRET:
        kb.add(types.InlineKeyboardButton(
            f"💳 ادفع عبر Binance Pay ({PRICE_USD}$)",
            callback_data="sub_binance"))
    kb.add(types.InlineKeyboardButton("📷 إرسال إيصال يدوي", callback_data="sub_manual"))
    bot.send_message(chat_id,
        f"💳 *تفعيل الاشتراك الشهري*\n\n"
        f"💵 السعر: *{price_str()}* / شهر\n\n"
        f"• Binance Pay ID: `983969145`\n"
        f"• جوال باي: `0597599642`\n\n"
        f"ادفع ثم أرسل صورة الإيصال أو استخدم زر الدفع المباشر.",
        parse_mode="Markdown", reply_markup=kb)

@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(m): _do_subscribe(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def btn_subscribe(m): _do_subscribe(m.chat.id)

# ── مساعدة ──────────────────────────────────────────────
def _do_help(chat_id: int):
    bot.send_message(chat_id,
        "📖 *الأوامر المتاحة:*\n\n"
        "/check — فحص المودل الآن\n"
        "/status — حالة حسابك\n"
        "/subscribe — تفعيل اشتراك\n"
        "/unlink — إلغاء ربط حسابك\n\n"
        "💡 *البوت يُخفي تلقائياً:*\n"
        "• التكاليف المسلّمة\n"
        "• الاختبارات المحلولة أو المنتهية المحاولات",
        parse_mode="Markdown")

@bot.message_handler(commands=["help"])
def cmd_help(m): _do_help(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "❓ مساعدة")
def btn_help(m): _do_help(m.chat.id)

# ── إلغاء الربط ─────────────────────────────────────────
@bot.message_handler(commands=["unlink"])
def cmd_unlink(m):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET username=NULL, password=NULL WHERE chat_id=?", (m.chat.id,)
        )
    bot.send_message(m.chat.id, "🔓 تم إلغاء الربط.\nاستخدم /check للربط من جديد.")

# ── ربط الحساب (خطوات) ─────────────────────────────────
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
    wm  = bot.send_message(msg.chat.id, "⏳ جاري التحقق من بياناتك...")
    res = run_moodle(user, pwd)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)",
                (msg.chat.id, user, pwd)
            )
        text = f"✅ تم الربط بنجاح!\nستصلك تقارير كل 6 ساعات.\n\n{res['message']}"
        try:    bot.edit_message_text(text, msg.chat.id, wm.message_id, parse_mode="Markdown")
        except: bot.send_message(msg.chat.id, text, parse_mode="Markdown")
    else:
        try:    bot.edit_message_text(res["message"], msg.chat.id, wm.message_id)
        except: bot.send_message(msg.chat.id, res["message"])

# ── استلام الإيصالات اليدوية ────────────────────────────
@bot.message_handler(content_types=["photo"])
def handle_photo(m):
    try:
        # 1. الحصول على أعلى دقة للصورة
        file_id = m.photo[-1].file_id
        
        # 2. تنظيف البيانات لتجنب أخطاء الـ Markdown
        user_id = m.chat.id
        first_name = m.from_user.first_name.replace('_', '\\_').replace('*', '\\*')
        username = f"@{m.from_user.username}".replace('_', '\\_') if m.from_user.username else "بدون يوزرنيم"
        
        # 3. تجهيز نص الرسالة
        caption = (
            f"📩 *طلب تفعيل يدوي*\n\n"
            f"👤 الاسم: {first_name}\n"
            f"🆔 الآيدي: `{user_id}`\n"
            f"🔗 اليوزر: {username}\n"
            f"📅 التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        
        # 4. أزرار التحكم
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ تفعيل شهر", callback_data=f"pay_{user_id}"),
            types.InlineKeyboardButton("❌ رفض", callback_data=f"rej_{user_id}")
        )
        
        # 5. محاولة الإرسال للأدمن
        bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=kb, parse_mode="Markdown")
        
        # 6. تأكيد للمستخدم
        bot.reply_to(m, "⏳ تم إرسال الإيصال للإدارة بنجاح، سيتم الرد عليك قريباً.")

    except telebot.apihelper.ApiTelegramException as e:
        log.error(f"Telegram API Error: {e}")
        # إذا فشل الماركدوان، نحاول الإرسال بدون تنسيق كخطة بديلة
        try:
            bot.send_photo(ADMIN_ID, file_id, caption=f"طلب يدوي من: {user_id}")
            bot.reply_to(m, "✅ تم الإرسال (بدون تنسيق).")
        except:
            bot.reply_to(m, f"❌ فشل الإرسال للأدمن. السبب: {e.description}")
    except Exception as e:
        log.error(f"Error: {e}")
        bot.reply_to(m, "⚠️ حدث خطأ تقني داخلي.")

# ══════════════════════════════════════════════════════════
# 10. Callbacks
# ══════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data == "sub_binance")
def cb_binance(call):
    bot.answer_callback_query(call.id, "⏳ جاري إنشاء رابط الدفع...")
    url, result = binance_create(call.message.chat.id)
    if url:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💳 ادفع الآن", url=url))
        kb.add(types.InlineKeyboardButton("✅ تحقق من الدفع",
                                          callback_data=f"verify_{result}"))
        bot.send_message(call.message.chat.id,
            f"💵 المبلغ: *{price_str()}*\n\n"
            "① اضغط *ادفع الآن*\n"
            "② بعد الدفع اضغط *تحقق من الدفع*",
            parse_mode="Markdown", reply_markup=kb)
    else:
        bot.send_message(call.message.chat.id,
            f"❌ {result}\nأرسل صورة الإيصال يدوياً.")

@bot.callback_query_handler(func=lambda c: c.data == "sub_manual")
def cb_manual(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "📷 أرسل صورة الإيصال وسيراجعها الأدمن.")

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
                conn.execute("UPDATE payments SET status='paid' WHERE order_id=?",
                             (order_id,))
            bot.send_message(call.message.chat.id, "🎉 تم تفعيل اشتراكك بنجاح!")
        else:
            bot.send_message(call.message.chat.id, "⚠️ لم يُعثر على الطلب.")
    elif st == "UNPAID":
        bot.send_message(call.message.chat.id,
            "⏳ لم تصل الدفعة بعد.\nانتظر دقيقة وأعد المحاولة.")
    elif st in ("CANCELLED", "EXPIRED"):
        bot.send_message(call.message.chat.id,
            "❌ انتهت صلاحية الطلب.\nأنشئ طلباً جديداً من /subscribe")
    else:
        bot.send_message(call.message.chat.id, "⚠️ لم يرد Binance. حاول لاحقاً.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_") or c.data.startswith("rej_"))
def cb_admin_pay(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية."); return
    action, uid_str = call.data.split("_", 1)
    uid = int(uid_str)
    if action == "pay":
        activate(uid, "monthly")
        bot.send_message(uid, "✅ تم تفعيل اشتراكك لمدة شهر!")
        try:
            bot.edit_message_caption(f"✅ تم تفعيل `{uid}`",
                                     call.message.chat.id, call.message.message_id,
                                     parse_mode="Markdown")
        except: pass
    elif action == "rej":
        bot.send_message(uid,
            "❌ تم رفض طلبك.\nتأكد من إرسال إيصال صحيح أو تواصل مع الدعم.")
        try:
            bot.edit_message_caption(f"❌ رُفض `{uid}`",
                                     call.message.chat.id, call.message.message_id,
                                     parse_mode="Markdown")
        except: pass
    bot.answer_callback_query(call.id)

# ══════════════════════════════════════════════════════════
# 11. أوامر الأدمن
# ══════════════════════════════════════════════════════════
def _admin(m): return m.chat.id == ADMIN_ID

@bot.message_handler(commands=["vip"])
def cmd_vip(m):
    """/vip [chat_id] — تفعيل VIP"""
    if not _admin(m): return
    parts = m.text.split()
    if len(parts) < 2:
        bot.send_message(m.chat.id, "الاستخدام: /vip [chat\\_id]", parse_mode="Markdown")
        return
    try: uid = int(parts[1])
    except:
        bot.send_message(m.chat.id, "❌ ID غير صحيح."); return
    activate(uid, "VIP")
    try: bot.send_message(uid, "🌟 تم تفعيل اشتراك VIP من قِبل الإدارة!")
    except: pass
    bot.send_message(m.chat.id, f"✅ VIP فعّال للمستخدم `{uid}`.", parse_mode="Markdown")

@bot.message_handler(commands=["revoke"])
def cmd_revoke(m):
    """/revoke [chat_id] — إلغاء اشتراك"""
    if not _admin(m): return
    parts = m.text.split()
    if len(parts) < 2:
        bot.send_message(m.chat.id, "الاستخدام: /revoke [chat\\_id]", parse_mode="Markdown")
        return
    try: uid = int(parts[1])
    except:
        bot.send_message(m.chat.id, "❌ ID غير صحيح."); return
    with get_db() as conn:
        conn.execute("UPDATE users SET is_vip=0, expiry_date=NULL WHERE chat_id=?", (uid,))
    bot.send_message(m.chat.id, f"✅ تم إلغاء اشتراك `{uid}`.", parse_mode="Markdown")

@bot.message_handler(commands=["holiday"])
def cmd_holiday(m):
    global IS_HOLIDAY
    if not _admin(m): return
    IS_HOLIDAY = not IS_HOLIDAY
    bot.send_message(m.chat.id,
        "🏖️ وضع العطلة *مفعّل* — لن تُرسل تقارير." if IS_HOLIDAY
        else "✅ وضع العطلة *ملغى* — التقارير ستُستأنف.",
        parse_mode="Markdown")

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not _admin(m): return
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
        f"📊 *إحصائيات البوت:*\n\n"
        f"👥 المستخدمون: {total}\n"
        f"🔗 مرتبطون: {linked}\n"
        f"🌟 VIP: {vip}\n"
        f"✅ اشتراك نشط: {active}\n"
        f"⏳ طلبات معلقة: {pending}\n"
        f"💵 السعر: {price_str()}\n"
        f"🏖️ العطلة: {'مفعّل' if IS_HOLIDAY else 'ملغى'}",
        parse_mode="Markdown")

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not _admin(m): return
    text = m.text.replace("/broadcast", "", 1).strip()
    if not text:
        bot.send_message(m.chat.id, "الاستخدام: /broadcast [الرسالة]"); return
    with get_db() as conn:
        uids = [r[0] for r in conn.execute("SELECT chat_id FROM users").fetchall()]
    ok = 0
    for uid in uids:
        try: bot.send_message(uid, f"📢 *إشعار:*\n\n{text}", parse_mode="Markdown"); ok += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ أُرسلت لـ {ok}/{len(uids)} مستخدم.")

@bot.message_handler(commands=["report_all"])
def cmd_report_all(m):
    if not _admin(m): return
    
    bot.send_message(m.chat.id, "🚀 جاري فحص وإرسال التقارير للجميع، قد يستغرق الأمر وقتاً...")
    
    with get_db() as conn:
        users = conn.execute(
            "SELECT chat_id, username, password FROM users WHERE username IS NOT NULL"
        ).fetchall()

    success_count = 0
    fail_count = 0

    for row in users:
        uid, user, pwd = row["chat_id"], row["username"], row["password"]
        
        # التأكد من صلاحية الاشتراك
        if not check_access(uid)[0]: continue

        res = run_moodle(user, pwd)
        if res["status"] == "success":
            try:
                bot.send_message(uid, f"🔔 *تحديث فوري من الإدارة:*\n\n{res['message']}", 
                                 parse_mode="Markdown")
                success_count += 1
                
                # تحديث قاعدة البيانات
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET last_report=? WHERE chat_id=?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M"), uid)
                    )
            except Exception:
                fail_count += 1
        else:
            fail_count += 1

    bot.send_message(m.chat.id, 
                     f"✅ اكتمل الإرسال الجماعي:\n"
                     f"🟢 تم بنجاح: {success_count}\n"
                     f"🔴 فشل/حظر: {fail_count}")

# ══════════════════════════════════════════════════════════
# 12. التقارير الدورية
# ══════════════════════════════════════════════════════════
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
        if res["status"] != "success": continue

        msg = res["message"]
        h   = hashlib.md5(msg.encode()).hexdigest()

        with get_db() as conn:
            old = conn.execute(
                "SELECT last_hash FROM users WHERE chat_id=?", (uid,)
            ).fetchone()
            # لا ترسل إذا نفس المحتوى تماماً (تجنب الإزعاج بدون سبب)
            if old and old["last_hash"] == h:
                continue
            try:
                bot.send_message(uid, f"🔔 *تقرير المودل:*\n\n{msg}",
                                 parse_mode="Markdown")
                conn.execute(
                    "UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?",
                    (h, datetime.now().strftime("%Y-%m-%d %H:%M"), uid)
                )
            except Exception as e:
                log.warning(f"فشل إرسال تقرير لـ {uid}: {e}")

# ══════════════════════════════════════════════════════════
# 13. المُجدوِل الخلفي
# ══════════════════════════════════════════════════════════
def _scheduler():
    schedule.every(6).hours.do(broadcast_reports)
    schedule.every(2).minutes.do(poll_payments)
    schedule.every(12).hours.do(refresh_rate)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ══════════════════════════════════════════════════════════
# 14. التشغيل
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    refresh_rate()
    threading.Thread(target=_scheduler, daemon=True).start()
    log.info("✅ البوت يعمل...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
