"""
بوت مودل الأقصى — النسخة المحسّنة
=====================================
متغيرات البيئة المطلوبة:
  TOKEN              — Telegram Bot Token
  BINANCE_API_KEY    — Binance Pay Certificate SN  (اختياري)
  BINANCE_SECRET_KEY — Binance Pay Secret Key      (اختياري)
"""

import os, hmac, hashlib, json, uuid, time, threading, sqlite3, schedule
import requests, logging, re
from contextlib import contextmanager
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ══════════════════════════════════════════════════════════
# 1. الإعدادات
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TOKEN")
BIN_CERT   = os.getenv("BINANCE_API_KEY")
BIN_SECRET = os.getenv("BINANCE_SECRET_KEY")

if not TOKEN:
    log.critical("❌ متغير TOKEN غير موجود. عيّنه في متغيرات البيئة ثم أعد التشغيل.")
    raise SystemExit(1)

ADMIN_ID       = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)
PRICE_USD      = 2.0
ILS_PER_USD    = 3.5
DB_PATH        = "/app/data/users.db"
REPORT_INTERVAL_HOURS = 6

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

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
                chat_id        INTEGER PRIMARY KEY,
                username       TEXT,
                moodle_user    TEXT,
                moodle_pass    TEXT,
                expiry_date    TEXT,
                is_vip         INTEGER DEFAULT 0,
                last_hash      TEXT,
                last_report    TEXT,
                auto_report    INTEGER DEFAULT 1,
                created_at     TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS payments (
                order_id   TEXT    PRIMARY KEY,
                chat_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
        """)
    log.info("قاعدة البيانات جاهزة")


# ══════════════════════════════════════════════════════════
# 3. سعر الصرف
# ══════════════════════════════════════════════════════════
def refresh_rate():
    global ILS_PER_USD
    try:
        r = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD", timeout=8
        ).json()
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
    """يعيد (True/False, نص_الحالة)"""
    if datetime.now() < FREE_TRIAL_END:
        return True, "تجريبي مجاني"
    with get_db() as conn:
        row = conn.execute(
            "SELECT expiry_date, is_vip FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row:
        if row["is_vip"]:
            return True, "VIP"
        if row["expiry_date"]:
            try:
                exp = datetime.strptime(row["expiry_date"], "%Y-%m-%d %H:%M:%S")
                if exp > datetime.now():
                    days = (exp - datetime.now()).days
                    return True, f"مشترك ({days} يوم متبقٍ)"
            except ValueError:
                pass
    return False, None


def activate(chat_id: int, plan: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,)
        )
        if plan == "VIP":
            conn.execute(
                "UPDATE users SET is_vip=1 WHERE chat_id=?", (chat_id,)
            )
        else:
            exp = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?",
                (exp, chat_id),
            )


# ══════════════════════════════════════════════════════════
# 5. استخراج بيانات الأحداث من HTML — مودل الأقصى
# ══════════════════════════════════════════════════════════
_OPEN_KW  = ["يُفتح", "يفتح", " opens", " open"]
_CLOSE_KW = ["يُغلق", "يغلق", " closes", " close"]

_NOISE = [
    "إذهب إلى النشاط", "إضافة تسليم", "حدث المساق",
    "يرجى الالتزام", "التسليم فقط", "ولن يتم",
    "WhatsApp", "واتساب", "Moodle", "مودل",
]

_TIME_RE = re.compile(
    r"(?:الأحد|الاثنين|الثلاثاء|الأربعاء|الخميس|الجمعة|السبت|غدًا|غداً|اليوم)"
    r"[,،\s]+"
    r"(?:\d{1,2}\s+\w+[,،\s]+)?"
    r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm|ص|م)",
    re.UNICODE,
)

_DONE_KW = [
    "تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال",
    "attempt already", "تم المحاولة", "no attempts allowed",
    "past due", "overdue", "لقد أنهيت محاولتك",
]

_EXAM_KW   = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test", "midterm"]
_ASSIGN_KW = ["تكليف", "واجب", "مهمة", "تقرير", "تجربة", "رفع", "ملف",
              "assignment", "task", "experiment", "report", "upload", "submit"]
_MEET_KW   = ["zoom", "meet", "bigbluebutton", "لقاء"]


def _clean_noise(text: str) -> str:
    for n in _NOISE:
        idx = text.find(n)
        if idx != -1:
            text = text[:idx]
    return text.strip()


def _event_role(name: str) -> str:
    nl = name.lower()
    for k in _OPEN_KW:
        if k.lower() in nl:
            return "open"
    for k in _CLOSE_KW:
        if k.lower() in nl:
            return "close"
    return "single"


def _strip_role_suffix(name: str) -> str:
    for kw in _OPEN_KW + _CLOSE_KW:
        name = re.sub(re.escape(kw.strip()) + r"\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def _extract_time_from_div(ev) -> str:
    for sel in [".date", ".event-date", ".col-1 .date", ".card-body .date"]:
        tag = ev.select_one(sel)
        if tag:
            t = tag.get_text(" ", strip=True)
            if _TIME_RE.search(t):
                return t.strip()

    for tag in ev.find_all("time"):
        dt_attr = tag.get("datetime", "")
        if dt_attr:
            try:
                parsed = datetime.fromisoformat(
                    dt_attr.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                days_ar   = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
                months_ar = ["","يناير","فبراير","مارس","أبريل","مايو","يونيو",
                             "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
                d    = days_ar[parsed.weekday()]
                mo   = months_ar[parsed.month]
                hr   = parsed.strftime("%I:%M").lstrip("0") or "12"
                ampm = "AM" if parsed.hour < 12 else "PM"
                return f"{d}, {parsed.day} {mo}, {hr} {ampm}"
            except Exception:
                pass
        label = tag.get_text(" ", strip=True)
        if label and _TIME_RE.search(label):
            return label.strip()

    raw     = ev.get_text(" ", strip=True)
    matches = _TIME_RE.findall(raw)
    return matches[-1].strip() if matches else ""


def _extract_course_and_doctor(ev) -> tuple:
    course = ""
    doctor = "غير محدد"

    for a in ev.find_all("a", href=True):
        href = a.get("href", "")
        if "/course/" in href:
            t = a.get_text(" ", strip=True)
            if t and 3 < len(t) < 150:
                course = t
                break

    if not course:
        for sel in ["div.description", ".description", "small"]:
            tag = ev.select_one(sel)
            if tag:
                t = tag.get_text(" ", strip=True)
                if t and 3 < len(t) < 200 and not _TIME_RE.search(t):
                    course = t
                    break

    if not course:
        return "غير محدد", "غير محدد"

    doc_m = re.search(
        r"\s+[أا]\.\s*([\u0600-\u06FF][^\n\u0600-\u06FF]{0,2}[\u0600-\u06FF\s]{2,25})",
        course,
    )
    if doc_m:
        doctor = _clean_noise(doc_m.group(1)).strip()
        course = course[: doc_m.start()].strip()

    course = _clean_noise(course).strip() or "غير محدد"
    return course, doctor


def _extract_event(ev) -> dict:
    h3   = ev.find("h3") or ev.find(class_="name")
    atag = (h3.find("a", href=True) if h3 else None) or ev.find("a", href=True)
    url  = atag["href"] if atag else ""
    raw_name = h3.get_text(" ", strip=True) if h3 else ""
    raw_name = _clean_noise(raw_name)

    role      = _event_role(raw_name)
    base_name = _strip_role_suffix(raw_name).strip() or raw_name[:70]
    time_val  = _extract_time_from_div(ev)
    course, doctor = _extract_course_and_doctor(ev)

    return {
        "name":      base_name,
        "course":    course,
        "doctor":    doctor,
        "url":       url,
        "url_lower": url.lower(),
        "time":      time_val,
        "role":      role,
        "raw":       ev.get_text(" ", strip=True),
    }


def _merge_exams(events: list) -> list:
    merged  = {}
    singles = []

    for ev in events:
        key  = (ev["name"].strip().lower(), ev["course"].lower())
        role = ev["role"]

        if role == "single":
            singles.append({**ev, "date_open": "", "date_close": ev["time"]})
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


# ══════════════════════════════════════════════════════════
# 6. التحقق من حالة التسليم
# ══════════════════════════════════════════════════════════
def _quick_done(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in _DONE_KW)


def _assign_done(session, url: str) -> bool:
    """يفحص صفحة التكليف للتأكد من التسليم."""
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return False
        soup = BeautifulSoup(resp.text, "lxml")

        # فحص جدول حالة التسليم
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not (th and td):
                continue
            label = th.get_text(strip=True).lower()
            value = td.get_text(strip=True).lower()
            if not any(k in label for k in ["حالة التسليم", "submission status", "status"]):
                continue
            # لم يُسلَّم بعد
            if any(k in value for k in [
                "لم يُسلَّم", "no submission", "not submitted", "لم يتم",
            ]):
                return False
            # تم التسليم
            if any(k in value for k in [
                "تم التسليم", "submitted for grading", "submitted",
            ]):
                return True

        # فحص نصي عام
        page = soup.get_text(" ", strip=True).lower()
        submitted_signals = [
            "تعديل التسليم", "edit submission", "you have submitted",
            "already submitted", "تم الإرسال", "تم التسليم",
        ]
        not_submitted_signals = [
            "إضافة تسليم", "add submission", "لم تُسلِّم بعد",
        ]
        if any(s in page for s in submitted_signals):
            return True
        if any(s in page for s in not_submitted_signals):
            return False
        return False
    except Exception as e:
        log.debug(f"_assign_done error for {url}: {e}")
        return False


def _quiz_done(session, url: str) -> bool:
    """يفحص صفحة الاختبار للتأكد من الإجابة."""
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return False
        soup = BeautifulSoup(resp.text, "lxml")

        # جدول ملخص المحاولات
        if soup.find("table", {"class": lambda x: x and "quizattemptsummary" in x}):
            return True

        page = soup.get_text(" ", strip=True).lower()
        done_signals = [
            "لقد أنهيت", "your last attempt", "آخر محاولة", "no more attempts",
            "لا محاولات متبقية", "مراجعة المحاولة", "review attempt",
            "your grade", "درجتك", "grade:", "attempt 1", "المحاولة 1",
            "you have already attempted", "نتائج الاختبار",
        ]
        open_signals = [
            "attempt quiz now", "ابدأ الاختبار", "ابدأ المحاولة",
            "start attempt",
        ]
        if any(s in page for s in done_signals):
            return True
        if any(s in page for s in open_signals):
            return False
        return False
    except Exception as e:
        log.debug(f"_quiz_done error for {url}: {e}")
        return False


# ══════════════════════════════════════════════════════════
# 7. تنسيق التقرير
# ══════════════════════════════════════════════════════════
def _fmt_exam(ev: dict) -> str:
    lines = [f"▪️ *{ev['name']}*",
             f"   📌 {ev['course']}",
             f"   👨‍🏫 {ev['doctor']}"]
    if ev.get("date_open") and ev.get("date_close"):
        lines += [f"   🕐 يفتح: {ev['date_open']}", f"   🔒 يغلق: {ev['date_close']}"]
    elif ev.get("date_open"):
        lines.append(f"   🕐 يفتح: {ev['date_open']}")
    elif ev.get("date_close"):
        lines.append(f"   🔒 يغلق: {ev['date_close']}")
    return "\n".join(lines)


def _fmt_task(ev: dict) -> str:
    t = ev.get("date_close") or ev.get("time", "")
    lines = [f"▪️ *{ev['name']}*",
             f"   📌 {ev['course']}",
             f"   👨‍🏫 {ev['doctor']}"]
    if t:
        lines.append(f"   📅 آخر موعد: {t}")
    return "\n".join(lines)


def _fmt_other(ev: dict) -> str:
    lines = [f"▪️ *{ev['name']}*",
             f"   📌 {ev['course']}",
             f"   👨‍🏫 {ev['doctor']}"]
    if ev.get("time"):
        lines.append(f"   📅 {ev['time']}")
    return "\n".join(lines)


def _send_long(chat_id, text: str):
    """يقسّم الرسائل الطويلة تلقائياً."""
    MAX = 4000
    chunks = [text[i : i + MAX] for i in range(0, len(text), MAX)]
    for chunk in chunks:
        try:
            bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except Exception as e:
            log.error(f"خطأ إرسال رسالة: {e}")


# ══════════════════════════════════════════════════════════
# 8. محرك المودل
# ══════════════════════════════════════════════════════════
MOODLE_BASE = "https://moodle.alaqsa.edu.ps"


def run_moodle(username: str, password: str, chat_id: int = None) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    login_url = f"{MOODLE_BASE}/login/index.php"
    try:
        # ── تسجيل الدخول ──
        resp0 = session.get(login_url, timeout=20)
        soup0 = BeautifulSoup(resp0.text, "lxml")
        ti    = soup0.find("input", {"name": "logintoken"})
        if not ti:
            return {"status": "error", "message": "⚠️ تعذّر الوصول إلى صفحة تسجيل الدخول."}

        resp = session.post(
            login_url,
            data={"username": username, "password": password,
                  "logintoken": ti["value"]},
            timeout=20,
            allow_redirects=True,
        )

        if "login" in resp.url and "logout" not in resp.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        # ── صفحة التقويم ──
        cal_url = f"{MOODLE_BASE}/calendar/view.php?view=upcoming"
        soup    = BeautifulSoup(session.get(cal_url, timeout=20).text, "lxml")

        lectures, meetings, exams_raw, assignments = [], [], [], []
        skipped = 0

        for ev_div in soup.find_all("div", {"class": "event"}):
            raw_txt = ev_div.get_text(" ", strip=True)

            ev       = _extract_event(ev_div)
            ll       = ev["url_lower"]
            tl       = ev["raw"].lower()

            is_quiz   = "quiz"   in ll or any(w in tl for w in _EXAM_KW)
            is_assign = "assign" in ll or any(w in tl for w in _ASSIGN_KW)
            is_meet   = any(x in ll for x in _MEET_KW) or "لقاء" in tl

            # ── التحقق الدقيق من حالة التسليم ──
            if ev["url"]:
                if is_quiz and not is_meet:
                    if _quiz_done(session, ev["url"]):
                        skipped += 1
                        continue
                elif is_assign and not is_quiz:
                    if _assign_done(session, ev["url"]):
                        skipped += 1
                        continue
                elif _quick_done(raw_txt):
                    skipped += 1
                    continue
            elif _quick_done(raw_txt):
                skipped += 1
                continue

            if is_quiz and not is_meet:
                exams_raw.append(ev)
            elif is_meet:
                meetings.append(_fmt_other(ev))
            elif is_assign:
                assignments.append(_fmt_task(ev))
            else:
                lectures.append(_fmt_other(ev))

        exams = [_fmt_exam(e) for e in _merge_exams(exams_raw)]

        # ── بناء التقرير ──
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        parts   = [f"🕐 *آخر تحديث: {now_str}*\n"]

        def section(icon, title, items):
            if items:
                return f"{icon} *{title}:*\n\n" + "\n\n".join(items)
            return f"{icon} *{title}:* لا يوجد"

        parts.append(section("📚", "المحاضرات", lectures))
        parts.append(section("🎥", "اللقاءات", meetings))
        parts.append(section("📝", "الاختبارات", exams))
        parts.append(section("⚠️", "التكاليف والتجارب", assignments))

        if skipped:
            parts.append(f"✅ _تم إخفاء {skipped} عنصر منجز أو منتهٍ_")

        return {"status": "success", "message": "\n\n".join(parts)}

    except requests.RequestException as e:
        log.error(f"خطأ شبكة: {e}")
        return {"status": "error", "message": "⚠️ المودل لا يستجيب، حاول لاحقاً."}
    except Exception as e:
        log.exception(f"خطأ run_moodle: {e}")
        return {"status": "error", "message": f"⚠️ خطأ غير متوقع: {str(e)[:100]}"}


# ══════════════════════════════════════════════════════════
# 9. الجدولة — تقرير كل 6 ساعات
# ══════════════════════════════════════════════════════════
def _auto_report_all():
    """يرسل تقريراً تلقائياً لجميع المستخدمين الذين لديهم بيانات مودل."""
    log.info("⏰ بدء إرسال التقارير التلقائية…")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT chat_id, moodle_user, moodle_pass FROM users "
            "WHERE moodle_user IS NOT NULL AND moodle_pass IS NOT NULL "
            "AND auto_report = 1"
        ).fetchall()

    for row in rows:
        chat_id = row["chat_id"]
        ok, _   = check_access(chat_id)
        if not ok:
            continue
        try:
            result = run_moodle(row["moodle_user"], row["moodle_pass"], chat_id)
            if result["status"] == "success":
                _send_long(chat_id, "🔔 *تقرير تلقائي*\n\n" + result["message"])
            else:
                log.warning(f"تقرير تلقائي فشل للمستخدم {chat_id}: {result['message']}")
        except Exception as e:
            log.error(f"خطأ تقرير تلقائي للمستخدم {chat_id}: {e}")
        time.sleep(1.5)

    log.info("✅ انتهى إرسال التقارير التلقائية")


def _run_scheduler():
    schedule.every(REPORT_INTERVAL_HOURS).hours.do(_auto_report_all)
    while True:
        schedule.run_pending()
        time.sleep(60)


# ══════════════════════════════════════════════════════════
# 10. Binance Pay
# ══════════════════════════════════════════════════════════
def _bin_headers(body: str) -> dict:
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time() * 1000))
    payload = f"{ts}\n{nonce}\n{body}\n"
    sig = hmac.new(
        BIN_SECRET.encode(),
        payload.encode(),
        hashlib.sha512,
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
            data = r["data"]
            with get_db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO payments VALUES (?,?,?,?)",
                    (order_id, chat_id,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending"),
                )
            return data.get("checkoutUrl"), None
        return None, f"رد Binance: {r.get('code')} — {r.get('errorMessage','')}"
    except Exception as e:
        return None, str(e)


def binance_check(order_id: str) -> str:
    if not (BIN_CERT and BIN_SECRET):
        return "unknown"
    body = json.dumps({"merchantTradeNo": order_id}, separators=(",", ":"))
    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v2/order/query",
            headers=_bin_headers(body), data=body, timeout=15
        ).json()
        return r.get("data", {}).get("status", "unknown").lower()
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════════
# 11. لوحة المفاتيح
# ══════════════════════════════════════════════════════════
def main_kb(chat_id: int = None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("📊 تقريري الآن"),
        types.KeyboardButton("⚙️ إعداداتي"),
        types.KeyboardButton("💳 اشتراك"),
        types.KeyboardButton("ℹ️ مساعدة"),
    )
    return kb


def admin_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("👥 المستخدمون"),
        types.KeyboardButton("📢 إذاعة"),
        types.KeyboardButton("✅ تفعيل اشتراك"),
        types.KeyboardButton("🏆 منح VIP"),
        types.KeyboardButton("📊 تقريري الآن"),
        types.KeyboardButton("🔙 رجوع"),
    )
    return kb


def settings_kb(auto_on: bool):
    kb = types.InlineKeyboardMarkup()
    lbl = "🟢 التقرير التلقائي: مفعّل" if auto_on else "🔴 التقرير التلقائي: معطّل"
    kb.add(
        types.InlineKeyboardButton(lbl, callback_data="toggle_auto"),
        types.InlineKeyboardButton("✏️ تحديث بيانات المودل", callback_data="update_creds"),
    )
    return kb


# ══════════════════════════════════════════════════════════
# 12. حالة الإدخال المؤقتة
# ══════════════════════════════════════════════════════════
_state: dict = {}


def set_state(chat_id, state, data=None):
    _state[chat_id] = {"s": state, "d": data or {}}


def get_state(chat_id):
    return _state.get(chat_id, {})


def clear_state(chat_id):
    _state.pop(chat_id, None)


# ══════════════════════════════════════════════════════════
# 13. هاندلرات Telegram
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    chat_id  = msg.chat.id
    username = msg.from_user.username or ""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, username) VALUES (?,?)",
            (chat_id, username),
        )
        conn.execute(
            "UPDATE users SET username=? WHERE chat_id=?",
            (username, chat_id),
        )
    ok, status = check_access(chat_id)
    trial_note = ""
    if ok and status == "تجريبي مجاني":
        trial_note = f"\n\n🎁 أنت الآن في الفترة التجريبية المجانية حتى {FREE_TRIAL_END.strftime('%Y/%m/%d')}."

    bot.send_message(
        chat_id,
        f"👋 مرحباً {msg.from_user.first_name}!\n\n"
        f"أنا *بوت مودل الأقصى* — أراقب جدولك وأرسل لك تقارير تلقائية كل *{REPORT_INTERVAL_HOURS} ساعات*."
        f"{trial_note}\n\n"
        "ابدأ بإدخال بيانات المودل عبر ⚙️ *إعداداتي*.",
        reply_markup=main_kb(chat_id),
    )


@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if msg.chat.id != ADMIN_ID:
        return
    bot.send_message(msg.chat.id, "👑 *لوحة الإدارة*", reply_markup=admin_kb())


# ── تقرير فوري ──
@bot.message_handler(func=lambda m: m.text == "📊 تقريري الآن")
def btn_report(msg):
    chat_id = msg.chat.id
    ok, status = check_access(chat_id)
    if not ok:
        bot.send_message(
            chat_id,
            "⛔ انتهى اشتراكك.\n\nاضغط *💳 اشتراك* للتجديد.",
            reply_markup=main_kb(chat_id),
        )
        return

    with get_db() as conn:
        row = conn.execute(
            "SELECT moodle_user, moodle_pass FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()

    if not row or not row["moodle_user"]:
        bot.send_message(
            chat_id,
            "⚙️ لم تُدخل بيانات المودل بعد.\n\nاضغط *⚙️ إعداداتي* لإضافتها.",
        )
        return

    wait = bot.send_message(chat_id, "⏳ جارٍ تحديث بياناتك من المودل…")
    result = run_moodle(row["moodle_user"], row["moodle_pass"], chat_id)
    try:
        bot.delete_message(chat_id, wait.message_id)
    except Exception:
        pass

    if result["status"] == "success":
        _send_long(chat_id, result["message"])
    else:
        bot.send_message(chat_id, result["message"])


# ── الإعدادات ──
@bot.message_handler(func=lambda m: m.text == "⚙️ إعداداتي")
def btn_settings(msg):
    chat_id = msg.chat.id
    with get_db() as conn:
        row = conn.execute(
            "SELECT moodle_user, auto_report FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()

    auto_on = bool(row["auto_report"]) if row else True
    user_set = row["moodle_user"] if row else None
    status_line = f"👤 مستخدم المودل: `{user_set}`" if user_set else "👤 لم تُحدَّد بيانات المودل بعد."

    bot.send_message(
        chat_id,
        f"⚙️ *إعداداتك*\n\n{status_line}\n\n"
        "اضغط الزر المناسب:",
        reply_markup=settings_kb(auto_on),
    )


@bot.callback_query_handler(func=lambda c: c.data == "toggle_auto")
def cb_toggle_auto(call):
    chat_id = call.message.chat.id
    with get_db() as conn:
        row = conn.execute(
            "SELECT auto_report FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        new_val = 0 if (row and row["auto_report"]) else 1
        conn.execute(
            "UPDATE users SET auto_report=? WHERE chat_id=?", (new_val, chat_id)
        )
    status = "مفعّل ✅" if new_val else "معطّل ⛔"
    bot.answer_callback_query(call.id, f"التقرير التلقائي {status}")
    bot.edit_message_reply_markup(
        chat_id, call.message.message_id,
        reply_markup=settings_kb(bool(new_val)),
    )


@bot.callback_query_handler(func=lambda c: c.data == "update_creds")
def cb_update_creds(call):
    chat_id = call.message.chat.id
    set_state(chat_id, "await_moodle_user")
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "✏️ أدخل *اسم المستخدم* في المودل (الرقم الجامعي):")


# ── الاشتراك ──
@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def btn_subscribe(msg):
    chat_id = msg.chat.id
    ok, status = check_access(chat_id)
    if ok:
        bot.send_message(
            chat_id,
            f"✅ اشتراكك فعّال — الحالة: *{status}*\n\nيمكنك التجديد في أي وقت.",
        )
        return

    if BIN_CERT and BIN_SECRET:
        wait = bot.send_message(chat_id, "⏳ جارٍ إنشاء طلب الدفع…")
        url, err = binance_create(chat_id)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        if url:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("💳 ادفع الآن", url=url))
            bot.send_message(
                chat_id,
                f"💳 *الاشتراك الشهري*\n\n💵 السعر: *{price_str()}*\n\n"
                "اضغط الزر لإتمام الدفع عبر Binance Pay:",
                reply_markup=kb,
            )
        else:
            bot.send_message(chat_id, f"⚠️ خطأ في إنشاء الدفع: {err}\n\nتواصل مع الإدارة.")
    else:
        bot.send_message(
            chat_id,
            f"💳 *الاشتراك الشهري*\n\n💵 السعر: *{price_str()}*\n\n"
            "للاشتراك تواصل مع المشرف مباشرة.",
        )


# ── مساعدة ──
@bot.message_handler(func=lambda m: m.text == "ℹ️ مساعدة")
def btn_help(msg):
    bot.send_message(
        msg.chat.id,
        "📖 *كيفية الاستخدام*\n\n"
        "1️⃣ اضغط *⚙️ إعداداتي* وأدخل بيانات تسجيل الدخول في المودل.\n"
        "2️⃣ اضغط *📊 تقريري الآن* للحصول على تقرير فوري.\n"
        "3️⃣ يصلك تقرير تلقائي كل *6 ساعات* يتضمن:\n"
        "   • المحاضرات القادمة\n"
        "   • اللقاءات والزوم\n"
        "   • الاختبارات مع أوقات الفتح والإغلاق\n"
        "   • التكاليف والتجارب\n\n"
        "✅ *العناصر المُنجزة تُخفى تلقائياً.*\n\n"
        "للدعم: تواصل مع المشرف.",
    )


# ══════════════════════════════════════════════════════════
# 14. أوامر الإدارة
# ══════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text == "👥 المستخدمون")
def admin_users(msg):
    with get_db() as conn:
        rows  = conn.execute("SELECT * FROM users").fetchall()
        total = len(rows)
        vip   = sum(1 for r in rows if r["is_vip"])
        sub   = sum(1 for r in rows if r["expiry_date"] and not r["is_vip"])
        creds = sum(1 for r in rows if r["moodle_user"])

    bot.send_message(
        ADMIN_ID,
        f"👥 *إحصاءات المستخدمين*\n\n"
        f"• الإجمالي: *{total}*\n"
        f"• VIP: *{vip}*\n"
        f"• مشتركون: *{sub}*\n"
        f"• ربطوا المودل: *{creds}*",
    )


@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text == "📢 إذاعة")
def admin_broadcast_start(msg):
    set_state(ADMIN_ID, "await_broadcast")
    bot.send_message(ADMIN_ID, "📢 أدخل نص الإذاعة (سيُرسل لجميع المستخدمين):")


@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text == "✅ تفعيل اشتراك")
def admin_activate_start(msg):
    set_state(ADMIN_ID, "await_activate_id")
    bot.send_message(ADMIN_ID, "✏️ أدخل chat_id المستخدم لتفعيل اشتراكه (شهر):")


@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text == "🏆 منح VIP")
def admin_vip_start(msg):
    set_state(ADMIN_ID, "await_vip_id")
    bot.send_message(ADMIN_ID, "✏️ أدخل chat_id المستخدم لمنحه VIP:")


@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text == "🔙 رجوع")
def admin_back(msg):
    clear_state(ADMIN_ID)
    bot.send_message(ADMIN_ID, "رجعت للقائمة الرئيسية.", reply_markup=main_kb(ADMIN_ID))


# ══════════════════════════════════════════════════════════
# 15. معالجة النصوص العامة (State Machine)
# ══════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(msg):
    chat_id = msg.chat.id
    st      = get_state(chat_id)
    s       = st.get("s")
    d       = st.get("d", {})
    text    = msg.text.strip()

    # ── إدخال اسم مستخدم مودل ──
    if s == "await_moodle_user":
        set_state(chat_id, "await_moodle_pass", {"user": text})
        bot.send_message(chat_id, "🔑 أدخل *كلمة المرور* في المودل:")
        return

    # ── إدخال كلمة مرور مودل ──
    if s == "await_moodle_pass":
        username = d.get("user", "")
        password = text
        wait = bot.send_message(chat_id, "⏳ جارٍ التحقق من بياناتك…")
        result = run_moodle(username, password, chat_id)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass

        if result["status"] == "fail":
            bot.send_message(chat_id, "❌ بيانات خاطئة، حاول مجدداً.\n\nأدخل *اسم المستخدم* مجدداً:")
            set_state(chat_id, "await_moodle_user")
            return

        if result["status"] == "error":
            bot.send_message(chat_id, result["message"] + "\n\nجرب مجدداً لاحقاً.")
            clear_state(chat_id)
            return

        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,)
            )
            conn.execute(
                "UPDATE users SET moodle_user=?, moodle_pass=? WHERE chat_id=?",
                (username, password, chat_id),
            )

        clear_state(chat_id)
        bot.send_message(
            chat_id,
            "✅ تم حفظ بياناتك بنجاح!\n\n"
            f"📊 إليك أول تقرير لك:",
            reply_markup=main_kb(chat_id),
        )
        _send_long(chat_id, result["message"])
        return

    # ── إذاعة ──
    if s == "await_broadcast" and chat_id == ADMIN_ID:
        with get_db() as conn:
            ids = [r["chat_id"] for r in conn.execute("SELECT chat_id FROM users").fetchall()]
        sent = failed = 0
        for uid in ids:
            try:
                bot.send_message(uid, f"📢 *إذاعة*\n\n{text}")
                sent += 1
            except Exception:
                failed += 1
            time.sleep(0.05)
        clear_state(chat_id)
        bot.send_message(ADMIN_ID, f"✅ الإذاعة انتهت — أُرسلت: {sent} | فشلت: {failed}")
        return

    # ── تفعيل اشتراك ──
    if s == "await_activate_id" and chat_id == ADMIN_ID:
        try:
            target = int(text)
            activate(target, "monthly")
            clear_state(chat_id)
            bot.send_message(ADMIN_ID, f"✅ تم تفعيل اشتراك شهري للمستخدم `{target}`")
            try:
                bot.send_message(target, "🎉 تم تفعيل اشتراكك الشهري! اضغط *📊 تقريري الآن* للبدء.")
            except Exception:
                pass
        except ValueError:
            bot.send_message(ADMIN_ID, "❌ ID غير صحيح، أدخل رقماً.")
        return

    # ── منح VIP ──
    if s == "await_vip_id" and chat_id == ADMIN_ID:
        try:
            target = int(text)
            activate(target, "VIP")
            clear_state(chat_id)
            bot.send_message(ADMIN_ID, f"🏆 تم منح VIP للمستخدم `{target}`")
            try:
                bot.send_message(target, "🏆 تم ترقيتك إلى VIP! اضغط *📊 تقريري الآن* للبدء.")
            except Exception:
                pass
        except ValueError:
            bot.send_message(ADMIN_ID, "❌ ID غير صحيح، أدخل رقماً.")
        return


# ══════════════════════════════════════════════════════════
# 16. التشغيل
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("🚀 تشغيل بوت مودل الأقصى…")
    init_db()
    refresh_rate()

    # خيط الجدولة
    threading.Thread(target=_run_scheduler, daemon=True).start()
    log.info(f"⏰ جدولة التقارير كل {REPORT_INTERVAL_HOURS} ساعات")

    bot.infinity_polling(timeout=30, long_polling_timeout=20)
