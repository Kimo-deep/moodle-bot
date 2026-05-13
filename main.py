"""
بوت مودل الأقصى — النسخة النهائية
=====================================
متغيرات البيئة:
  TOKEN, BINANCE_API_KEY (اختياري), BINANCE_SECRET_KEY (اختياري)
"""

import os, hmac, hashlib, json, uuid, time, threading, sqlite3
import schedule, requests, logging, re
from contextlib import contextmanager
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import telebot
from telebot import types

# ══════════════════════════════════════════════════════════
# 1. الإعدادات
# ══════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TOKEN")
BIN_CERT   = os.getenv("BINANCE_API_KEY")
BIN_SECRET = os.getenv("BINANCE_SECRET_KEY")

ADMIN_ID       = 7840931571
FREE_TRIAL_END = datetime(2026, 6, 1)
PRICE_USD      = 2.0
ILS_PER_USD    = 3.7
DB_PATH        = "/app/data/users.db"

bot        = telebot.TeleBot(TOKEN)
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
        r = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD", timeout=8
        ).json()
        ILS_PER_USD = round(r["rates"]["ILS"], 2)
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
                return True, f"مشترك ({days} يوم)"
    return False, None

def activate(chat_id: int, plan: str):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        if plan == "VIP":
            conn.execute("UPDATE users SET is_vip=1 WHERE chat_id=?", (chat_id,))
        else:
            exp = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?",
                (exp, chat_id)
            )

# ══════════════════════════════════════════════════════════
# 5. ثوابت التصنيف والتنظيف
# ══════════════════════════════════════════════════════════
_OPEN_KW  = ["يُفتح", "يفتح", "opens", "open"]
_CLOSE_KW = ["يُغلق", "يغلق", "closes", "close"]

_NOISE = [
    "إذهب إلى النشاط", "إضافة تسليم", "حدث المساق",
    "يرجى الالتزام", "التسليم فقط", "ولن يتم",
    "WhatsApp", "واتساب", "Moodle", "مودل",
    "Go to activity", "Add submission",
]

# نمط الوقت المستخدم في مودل الأقصى
_TIME_RE = re.compile(
    r"(?:الأحد|الاثنين|الثلاثاء|الأربعاء|الخميس|الجمعة|السبت"
    r"|غدًا|غداً|اليوم|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)"
    r"[,،\s]+"
    r"(?:\d{1,2}\s+\w+[,،\s]+)?"
    r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm|ص|م)",
    re.UNICODE | re.IGNORECASE,
)

_EXAM_KW   = ["اختبار", "امتحان", "كويز", "quiz", "exam", "test", "midterm"]
_ASSIGN_KW = ["تكليف", "واجب", "مهمة", "تقرير", "تجربة", "رفع", "ملف",
              "assignment", "task", "experiment", "report", "upload", "submit",
              "homework", "hw", "project"]
_MEET_KW   = ["zoom", "meet", "bigbluebutton", "webex", "teams"]

_DONE_KW = [
    "تم التسليم", "submitted", "تخطى", "سلمت", "تم الإرسال",
    "attempt already", "تم المحاولة", "no attempts allowed",
    "past due", "overdue",
]

def _clean_noise(text: str) -> str:
    for n in _NOISE:
        idx = text.find(n)
        if idx != -1:
            text = text[:idx]
    return text.strip()

def _event_role(name: str) -> str:
    nl = name.lower()
    if any(k.lower() in nl for k in _OPEN_KW):  return "open"
    if any(k.lower() in nl for k in _CLOSE_KW): return "close"
    return "single"

def _strip_role_suffix(name: str) -> str:
    for kw in _OPEN_KW + _CLOSE_KW:
        name = re.sub(re.escape(kw) + r"\s*$", "", name, flags=re.IGNORECASE)
    # حذف "مستحق" من نهاية الاسم
    name = re.sub(r"\s*مستحق\s*$", "", name)
    return name.strip()

# ══════════════════════════════════════════════════════════
# 6. استخراج الوقت — يتجنب div.description كلياً
# ══════════════════════════════════════════════════════════
_DAYS_AR   = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
_MONTHS_AR = ["","يناير","فبراير","مارس","أبريل","مايو","يونيو",
              "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]

def _dt_to_arabic(dt: datetime) -> str:
    d    = _DAYS_AR[dt.weekday()]
    mo   = _MONTHS_AR[dt.month]
    hr   = dt.strftime("%I:%M").lstrip("0") or "12"
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{d}, {dt.day} {mo}, {hr} {ampm}"

def _extract_time(ev) -> str:
    """يستخرج الوقت من عنصر الحدث، متجاهلاً div.description."""
    # نسخة مؤقتة بدون description لمنع الخلط مع المادة
    ev_clone = BeautifulSoup(str(ev), "html.parser")
    for tag in ev_clone.select("div.description, .description, small"):
        tag.decompose()

    # 1. عنصر .date المخصص
    for sel in [".date", ".event-date", ".col-1", "[class*='date']"]:
        tag = ev_clone.select_one(sel)
        if tag:
            t = tag.get_text(" ", strip=True)
            if _TIME_RE.search(t):
                m = _TIME_RE.search(t)
                return m.group(0).strip()

    # 2. عنصر <time datetime="...">
    for tag in ev_clone.find_all("time"):
        dt_str = tag.get("datetime", "")
        if dt_str:
            try:
                dt = datetime.fromisoformat(
                    dt_str.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                return _dt_to_arabic(dt)
            except Exception:
                pass
        label = tag.get_text(" ", strip=True)
        if _TIME_RE.search(label):
            return _TIME_RE.search(label).group(0).strip()

    # 3. آخر تطابق في باقي النص (بعد حذف description)
    raw = ev_clone.get_text(" ", strip=True)
    matches = _TIME_RE.findall(raw)
    return matches[-1].strip() if matches else ""

# ══════════════════════════════════════════════════════════
# 7. استخراج المادة والدكتور
# ══════════════════════════════════════════════════════════
def _extract_course_doctor(ev) -> tuple:
    """
    يستخرج المادة والدكتور.
    مودل الأقصى يضع المادة في:
      <a href="...course/view.php?id=...">اسم المادة أ.اسم الدكتور</a>
    """
    raw_course = ""

    # 1. رابط href يحتوي "course" و"view" = رابط المادة الرسمي
    for a in ev.find_all("a", href=True):
        href = a.get("href", "")
        if "course" in href and ("view" in href or "id=" in href):
            t = a.get_text(" ", strip=True)
            if t and len(t) > 4 and not _TIME_RE.search(t):
                raw_course = t
                break

    # 2. text nodes مباشرة في description (بدون روابط الضجيج)
    if not raw_course:
        desc = (ev.select_one("div.description")
                or ev.select_one(".description"))
        if desc:
            for node in desc.children:
                if not hasattr(node, "name"):  # text node
                    t = str(node).strip()
                    if (t and len(t) > 4
                            and not _TIME_RE.search(t)
                            and not any(n in t for n in _NOISE)):
                        raw_course = t
                        break

    if not raw_course:
        return "غير محدد", "غير محدد"

    # فصل الدكتور: "خوارزميات متقدمة أ.فراس فؤاد العجلة"
    doc_m = re.search(
        r"\s*[أا]\.\s*([\u0600-\u06FF][\u0600-\u06FF\s]{2,30}?)\s*$",
        raw_course,
    )
    if doc_m:
        doctor = _clean_noise(doc_m.group(1)).strip()
        course = _clean_noise(raw_course[:doc_m.start()]).strip()
    else:
        doctor = "غير محدد"
        course = _clean_noise(raw_course).strip()

    return course or "غير محدد", doctor

# ══════════════════════════════════════════════════════════
# 8. استخراج الحدث الكامل
# ══════════════════════════════════════════════════════════
def _extract_event(ev) -> dict:
    h3   = ev.find("h3") or ev.find(class_="name")
    atag = (h3.find("a", href=True) if h3 else None) or ev.find("a", href=True)
    url  = atag["href"] if atag else ""

    raw_name  = _clean_noise(h3.get_text(" ", strip=True) if h3 else "")
    role      = _event_role(raw_name)
    base_name = _strip_role_suffix(raw_name) or raw_name[:70]

    course, doctor = _extract_course_doctor(ev)
    time_val       = _extract_time(ev)

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

# ══════════════════════════════════════════════════════════
# 9. دمج الاختبارات (فتح + إغلاق → حدث واحد)
# ══════════════════════════════════════════════════════════
def _merge_exams(events: list) -> list:
    merged  = {}
    singles = []

    for ev in events:
        # المطابقة بالاسم فقط (لأن المادة قد تكون مختلفة بين الحدثين)
        key  = re.sub(r"\s+", " ", ev["name"].strip().lower())
        role = ev["role"]

        if role == "single":
            singles.append({**ev, "date_open": "", "date_close": ev["time"]})
            continue

        if key not in merged:
            merged[key] = {
                "name": ev["name"], "course": ev["course"],
                "doctor": ev["doctor"], "url": ev["url"],
                "url_lower": ev["url_lower"],
                "date_open": "", "date_close": "",
            }
        else:
            # تحديث المادة والدكتور إن كانا أفضل في الحدث الثاني
            if ev["course"] != "غير محدد":
                merged[key]["course"] = ev["course"]
            if ev["doctor"] != "غير محدد":
                merged[key]["doctor"] = ev["doctor"]

        if role == "open":
            merged[key]["date_open"]  = ev["time"]
        else:
            merged[key]["date_close"] = ev["time"]

    return list(merged.values()) + singles

# ══════════════════════════════════════════════════════════
# 10. تنسيق الأحداث
# ══════════════════════════════════════════════════════════
def _fmt_exam(ev: dict) -> str:
    lines = [f"▪️ *{ev['name']}*",
             f"   📌 {ev['course']}",
             f"   👨‍🏫 {ev['doctor']}"]
    if ev.get("date_open") and ev.get("date_close"):
        lines += [f"   🕐 يفتح: {ev['date_open']}",
                  f"   🔒 يغلق: {ev['date_close']}"]
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
    t = ev.get("time", "")
    lines = [f"▪️ *{ev['name']}*",
             f"   📌 {ev['course']}",
             f"   👨‍🏫 {ev['doctor']}"]
    if t:
        lines.append(f"   📅 {t}")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════
# 11. كشف المنجز
# ══════════════════════════════════════════════════════════
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
            if not any(k in label for k in
                       ["حالة التسليم", "submission status", "status"]):
                continue
            if any(k in value for k in
                   ["لم يُسلَّم", "no submission", "not submitted", "لم يتم"]):
                return False
            return True
        page = soup.get_text(" ", strip=True).lower()
        return any(k in page for k in [
            "تعديل التسليم", "edit submission",
            "you have submitted", "already submitted",
        ])
    except Exception:
        return False

def _quiz_done(session, url: str) -> bool:
    try:
        soup = BeautifulSoup(session.get(url, timeout=12).text, "html.parser")
        page = soup.get_text(" ", strip=True).lower()
        if any(k in page for k in [
            "لقد أنهيت", "your last attempt", "آخر محاولة",
            "no more attempts", "لا محاولات متبقية",
            "مراجعة المحاولة", "review attempt",
            "your grade", "درجتك", "grade:", "attempt 1", "المحاولة 1",
        ]):
            return True
        return bool(
            soup.find("table",
                      {"class": lambda x: x and "quizattemptsummary" in x})
        )
    except Exception:
        return False

# ══════════════════════════════════════════════════════════
# 12. محرك المودل — يفحص upcoming + dashboard
# ══════════════════════════════════════════════════════════
_BASE = "https://moodle.alaqsa.edu.ps"

def _collect_events(session, soup) -> tuple:
    """يصنّف أحداث صفحة واحدة ويرجع (lectures, meetings, exams_raw, assignments, skipped)."""
    lectures, meetings, exams_raw, assignments = [], [], [], []
    skipped = 0

    for ev_div in soup.find_all("div", {"class": "event"}):
        raw_txt = ev_div.get_text(" ", strip=True)
        if _quick_done(raw_txt):
            skipped += 1
            continue

        ev = _extract_event(ev_div)
        ll = ev["url_lower"]
        tl = ev["raw"].lower()

        is_quiz   = "quiz"   in ll or any(w in tl for w in _EXAM_KW)
        is_assign = "assign" in ll or any(w in tl for w in _ASSIGN_KW)
        is_meet   = any(x in ll for x in _MEET_KW) or "لقاء" in tl

        # فحص صفحة النشاط مباشرة
        if ev["url"]:
            if is_assign and not is_quiz:
                if _assign_done(session, ev["url"]):
                    skipped += 1
                    continue
            elif is_quiz:
                if _quiz_done(session, ev["url"]):
                    skipped += 1
                    continue

        if is_quiz:     exams_raw.append(ev)
        elif is_meet:   meetings.append(_fmt_other(ev))
        elif is_assign: assignments.append(_fmt_task(ev))
        else:           lectures.append(_fmt_other(ev))

    return lectures, meetings, exams_raw, assignments, skipped


def run_moodle(username: str, password: str) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    login_url = f"{_BASE}/login/index.php"
    try:
        # ── تسجيل الدخول ──
        soup = BeautifulSoup(session.get(login_url, timeout=20).text, "html.parser")
        ti   = soup.find("input", {"name": "logintoken"})
        if not ti:
            return {"status": "error",
                    "message": "⚠️ تعذّر الوصول لصفحة تسجيل الدخول."}
        resp = session.post(login_url,
                            data={"username": username, "password": password,
                                  "logintoken": ti["value"]}, timeout=20)
        if "login" in resp.url:
            return {"status": "fail", "message": "❌ بيانات المودل غير صحيحة."}

        # ── الصفحة 1: upcoming (الأحداث القادمة) ──
        soup1 = BeautifulSoup(
            session.get(f"{_BASE}/calendar/view.php?view=upcoming",
                        timeout=20).text, "html.parser"
        )
        l1, m1, e1, a1, s1 = _collect_events(session, soup1)

        # ── الصفحة 2: month (الأحداث الشهرية — تشمل اللقاءات والتكاليف خارج upcoming) ──
        soup2 = BeautifulSoup(
            session.get(f"{_BASE}/calendar/view.php?view=month",
                        timeout=20).text, "html.parser"
        )
        l2, m2, e2, a2, s2 = _collect_events(session, soup2)

        # ── الصفحة 3: dashboard (واجهة المستخدم — تعرض التكاليف الحالية) ──
        soup3 = BeautifulSoup(
            session.get(f"{_BASE}/my/", timeout=20).text, "html.parser"
        )
        l3, m3, e3, a3, s3 = _collect_events(session, soup3)

        # ── دمج النتائج (إزالة التكرار بالاسم) ──
        def _dedup(lst: list) -> list:
            seen, out = set(), []
            for item in lst:
                # أول سطر هو الاسم (▪️ ...)
                key = item.split("\n")[0]
                if key not in seen:
                    seen.add(key); out.append(item)
            return out

        def _dedup_ev(lst: list) -> list:
            seen, out = set(), []
            for ev in lst:
                key = ev["name"].strip().lower()
                if key not in seen:
                    seen.add(key); out.append(ev)
            return out

        lectures   = _dedup(l1 + l2 + l3)
        meetings   = _dedup(m1 + m2 + m3)
        exams_raw  = _dedup_ev(e1 + e2 + e3)
        assignments = _dedup(a1 + a2 + a3)
        skipped    = s1 + s2 + s3

        exams = [_fmt_exam(e) for e in _merge_exams(exams_raw)]

        # ── بناء التقرير ──
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        parts   = [f"🕐 *{now_str}*\n"]

        parts.append("📚 *المحاضرات:* " + ("لا يوجد" if not lectures else ""))
        if lectures:     parts[-1] += "\n\n" + "\n\n".join(lectures)

        parts.append("🎥 *اللقاءات:* " + ("لا يوجد" if not meetings else ""))
        if meetings:     parts[-1] += "\n\n" + "\n\n".join(meetings)

        parts.append("📝 *الاختبارات:* " + ("لا يوجد" if not exams else ""))
        if exams:        parts[-1] += "\n\n" + "\n\n".join(exams)

        parts.append("⚠️ *التكاليف والتجارب:* " + ("لا يوجد" if not assignments else ""))
        if assignments:  parts[-1] += "\n\n" + "\n\n".join(assignments)

        hidden = f"\n\n_✅ تم إخفاء {skipped} عنصر منجز_" if skipped else ""
        return {"status": "success", "message": "\n\n".join(parts) + hidden,
                "has_content": bool(lectures or meetings or exams or assignments)}

    except requests.RequestException as e:
        log.error(f"خطأ شبكة: {e}")
        return {"status": "error", "message": "⚠️ المودل لا يستجيب، حاول لاحقاً.",
                "has_content": False}
    except Exception as e:
        log.error(f"خطأ run_moodle: {e}")
        return {"status": "error",
                "message": f"⚠️ خطأ: {str(e)[:80]}", "has_content": False}

# ══════════════════════════════════════════════════════════
# 13. Binance Pay
# ══════════════════════════════════════════════════════════
def _bin_headers(body: str) -> dict:
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time() * 1000))
    sig   = hmac.new(
                BIN_SECRET.encode(),
                f"{ts}\n{nonce}\n{body}\n".encode(),
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
        "env": {"terminalType": "WEB"},
        "merchantTradeNo": order_id,
        "orderAmount": f"{PRICE_USD:.2f}",
        "currency": "USDT",
        "description": "Moodle Bot Monthly",
        "goodsDetails": [{
            "goodsType": "02", "goodsCategory": "Z000",
            "referenceGoodsId": "monthly", "goodsName": "Moodle Bot",
            "goodsUnitAmount": {"currency": "USDT", "amount": f"{PRICE_USD:.2f}"},
        }],
    }, separators=(",", ":"))
    try:
        r = requests.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v2/order",
            headers=_bin_headers(body), data=body, timeout=15,
        ).json()
        if r.get("status") == "SUCCESS":
            with get_db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO payments VALUES (?,?,?,?)",
                    (order_id, chat_id,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending"),
                )
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
            headers=_bin_headers(body), data=body, timeout=10,
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
            "SELECT order_id, chat_id FROM payments "
            "WHERE status='pending' AND created_at >= ?", (cutoff,)
        ).fetchall()
        conn.execute(
            "DELETE FROM payments WHERE status='pending' AND created_at < ?",
            (cutoff,),
        )
    for row in rows:
        st = binance_query(row["order_id"])
        if st == "PAID":
            activate(row["chat_id"], "monthly")
            with get_db() as conn:
                conn.execute(
                    "UPDATE payments SET status='paid' WHERE order_id=?",
                    (row["order_id"],),
                )
            try:
                bot.send_message(row["chat_id"],
                    "🎉 تم استلام دفعتك وتفعيل اشتراكك تلقائياً!")
            except Exception:
                pass
        elif st in ("CANCELLED", "EXPIRED"):
            with get_db() as conn:
                conn.execute(
                    "UPDATE payments SET status=? WHERE order_id=?",
                    (st.lower(), row["order_id"]),
                )

# ══════════════════════════════════════════════════════════
# 14. لوحة مفاتيح المستخدم
# ══════════════════════════════════════════════════════════
def _user_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 فحص الآن", "📊 حالتي")
    kb.row("💳 اشتراك",   "❓ مساعدة")
    return kb

# ══════════════════════════════════════════════════════════
# 15. /start
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(
        m.chat.id,
        "🎓 *مرحباً في بوت مودل الأقصى*\n\n"
        "• تقرير تلقائي كل 6 ساعات\n"
        "• يفحص upcoming + month + dashboard\n"
        "• يُخفي المسلَّم والمحلول تلقائياً",
        parse_mode="Markdown",
        reply_markup=_user_kb(),
    )

# ══════════════════════════════════════════════════════════
# 16. فحص الآن
# ══════════════════════════════════════════════════════════
def _do_check(chat_id: int):
    ok, label = check_access(chat_id)
    if not ok:
        bot.send_message(
            chat_id,
            "🚫 *اشتراكك منتهٍ*\n\nاستخدم /subscribe للتجديد.",
            parse_mode="Markdown",
        )
        return
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, password FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row and row["username"]:
        wm  = bot.send_message(chat_id, f"🔍 جاري الفحص ({label})…")
        res = run_moodle(row["username"], row["password"])
        try:
            bot.edit_message_text(
                res["message"], chat_id, wm.message_id, parse_mode="Markdown"
            )
        except Exception:
            bot.send_message(chat_id, res["message"], parse_mode="Markdown")
    else:
        wm = bot.send_message(chat_id, "📋 أرسل رقمك الجامعي:")
        bot.register_next_step_handler(wm, _step_user)

@bot.message_handler(commands=["check"])
def cmd_check(m): _do_check(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "🔍 فحص الآن")
def btn_check(m): _do_check(m.chat.id)

# ══════════════════════════════════════════════════════════
# 17. حالتي
# ══════════════════════════════════════════════════════════
def _do_status(chat_id: int):
    ok, label = check_access(chat_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, last_report FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    linked  = (f"✅ `{row['username']}`"
               if row and row["username"] else "❌ غير مرتبط — اضغط 🔍 فحص الآن")
    last    = (f"\n📅 آخر تقرير: {row['last_report']}"
               if row and row["last_report"] else "\n📅 لم يُرسل بعد")
    sub     = f"✅ {label}" if ok else "❌ منتهٍ"
    trial   = (f"\n⏳ التجربة تنتهي: {FREE_TRIAL_END:%Y-%m-%d}"
               if datetime.now() < FREE_TRIAL_END else "")
    bot.send_message(
        chat_id,
        f"👤 *حالة حسابك*\n\n"
        f"🔗 الربط: {linked}\n"
        f"🎫 الاشتراك: {sub}{trial}{last}",
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["status"])
def cmd_status(m): _do_status(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 حالتي")
def btn_status(m): _do_status(m.chat.id)

# ══════════════════════════════════════════════════════════
# 18. اشتراك
# ══════════════════════════════════════════════════════════
def _do_subscribe(chat_id: int):
    kb = types.InlineKeyboardMarkup()
    if BIN_CERT and BIN_SECRET:
        kb.add(types.InlineKeyboardButton(
            f"💳 ادفع عبر Binance Pay ({PRICE_USD}$)",
            callback_data="sub_binance",
        ))
    kb.add(types.InlineKeyboardButton(
        "📷 إرسال إيصال يدوي", callback_data="sub_manual"
    ))
    bot.send_message(
        chat_id,
        f"💳 *تفعيل الاشتراك الشهري*\n\n"
        f"💵 السعر: *{price_str()}* / شهر\n\n"
        f"• Binance Pay ID: `983969145`\n"
        f"• جوال باي: `0597599642`",
        parse_mode="Markdown",
        reply_markup=kb,
    )

@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(m): _do_subscribe(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "💳 اشتراك")
def btn_subscribe(m): _do_subscribe(m.chat.id)

# ══════════════════════════════════════════════════════════
# 19. مساعدة
# ══════════════════════════════════════════════════════════
def _do_help(chat_id: int):
    bot.send_message(
        chat_id,
        "📖 *الأوامر المتاحة:*\n\n"
        "/check — فحص المودل الآن\n"
        "/status — حالة حسابك\n"
        "/subscribe — تفعيل اشتراك\n"
        "/unlink — إلغاء ربط حسابك\n\n"
        "💡 *البوت يفحص 3 صفحات:*\n"
        "• upcoming — الأحداث القادمة\n"
        "• month — التقويم الشهري\n"
        "• dashboard — واجهة المستخدم",
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["help"])
def cmd_help(m): _do_help(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "❓ مساعدة")
def btn_help(m): _do_help(m.chat.id)

# ══════════════════════════════════════════════════════════
# 20. إلغاء الربط
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["unlink"])
def cmd_unlink(m):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET username=NULL, password=NULL WHERE chat_id=?",
            (m.chat.id,),
        )
    bot.send_message(
        m.chat.id,
        "🔓 تم إلغاء الربط.\nاستخدم /check للربط من جديد.",
    )

# ══════════════════════════════════════════════════════════
# 21. ربط الحساب (خطوات)
# ══════════════════════════════════════════════════════════
def _step_user(msg):
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
    wm  = bot.send_message(msg.chat.id, "⏳ جاري التحقق من بياناتك…")
    res = run_moodle(user, pwd)
    if res["status"] == "success":
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (chat_id, username, password) VALUES (?,?,?)",
                (msg.chat.id, user, pwd),
            )
        text = f"✅ تم الربط!\n\n{res['message']}"
        try:
            bot.edit_message_text(
                text, msg.chat.id, wm.message_id, parse_mode="Markdown"
            )
        except Exception:
            bot.send_message(msg.chat.id, text, parse_mode="Markdown")
    else:
        try:
            bot.edit_message_text(res["message"], msg.chat.id, wm.message_id)
        except Exception:
            bot.send_message(msg.chat.id, res["message"])

# ══════════════════════════════════════════════════════════
# 22. استلام الإيصالات
# ══════════════════════════════════════════════════════════
@bot.message_handler(content_types=["photo"])
def handle_photo(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "✅ تفعيل شهر", callback_data=f"pay_{m.chat.id}"
        ),
        types.InlineKeyboardButton(
            "❌ رفض", callback_data=f"rej_{m.chat.id}"
        ),
    )
    try:
        uname = (f"@{m.from_user.username}"
                 if m.from_user.username else "بدون يوزرنيم")
        bot.send_photo(
            ADMIN_ID,
            m.photo[-1].file_id,
            caption=(f"📩 *طلب تفعيل يدوي*\n"
                     f"👤 {uname} (`{m.chat.id}`)\n"
                     f"📅 {datetime.now():%Y-%m-%d %H:%M}"),
            reply_markup=kb,
            parse_mode="Markdown",
        )
        bot.reply_to(m, "⏳ تم إرسال الإيصال. سيُشعرك الأدمن فور المراجعة.")
    except Exception as e:
        log.error(f"handle_photo: {e}")
        bot.reply_to(m, "⚠️ حدث خطأ. حاول مجدداً.")

# ══════════════════════════════════════════════════════════
# 23. Callbacks الدفع
# ══════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "sub_binance")
def cb_binance(call):
    bot.answer_callback_query(call.id, "⏳ جاري إنشاء رابط الدفع…")
    url, result = binance_create(call.message.chat.id)
    if url:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💳 ادفع الآن", url=url))
        kb.add(types.InlineKeyboardButton(
            "✅ تحقق من الدفع", callback_data=f"verify_{result}"
        ))
        bot.send_message(
            call.message.chat.id,
            f"💵 المبلغ: *{price_str()}*\n\n"
            "① اضغط *ادفع الآن*\n"
            "② بعد الدفع اضغط *تحقق من الدفع*",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        bot.send_message(
            call.message.chat.id,
            f"❌ {result}\nأرسل صورة الإيصال يدوياً.",
        )

@bot.callback_query_handler(func=lambda c: c.data == "sub_manual")
def cb_manual(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     "📷 أرسل صورة الإيصال وسيراجعها الأدمن.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("verify_"))
def cb_verify(call):
    order_id = call.data[7:]
    bot.answer_callback_query(call.id, "⏳ جاري التحقق…")
    st = binance_query(order_id)
    if st == "PAID":
        with get_db() as conn:
            row = conn.execute(
                "SELECT chat_id FROM payments WHERE order_id=?", (order_id,)
            ).fetchone()
        if row:
            activate(row["chat_id"], "monthly")
            with get_db() as conn:
                conn.execute(
                    "UPDATE payments SET status='paid' WHERE order_id=?",
                    (order_id,),
                )
            bot.send_message(call.message.chat.id, "🎉 تم تفعيل اشتراكك!")
        else:
            bot.send_message(call.message.chat.id, "⚠️ لم يُعثر على الطلب.")
    elif st == "UNPAID":
        bot.send_message(call.message.chat.id,
            "⏳ لم تصل الدفعة بعد. انتظر دقيقة وأعد المحاولة.")
    elif st in ("CANCELLED", "EXPIRED"):
        bot.send_message(call.message.chat.id,
            "❌ انتهت صلاحية الطلب. أنشئ طلباً جديداً من /subscribe")
    else:
        bot.send_message(call.message.chat.id,
            "⚠️ لم يرد Binance. حاول لاحقاً.")

@bot.callback_query_handler(
    func=lambda c: c.data.startswith("pay_") or c.data.startswith("rej_")
)
def cb_admin_pay(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية.")
        return
    action, uid_str = call.data.split("_", 1)
    uid = int(uid_str)
    if action == "pay":
        activate(uid, "monthly")
        bot.send_message(uid, "✅ تم تفعيل اشتراكك لمدة شهر!")
        try:
            bot.edit_message_caption(
                f"✅ فُعِّل `{uid}`",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
    elif action == "rej":
        bot.send_message(uid,
            "❌ تم رفض طلبك. أرسل إيصالاً صحيحاً أو تواصل مع الدعم.")
        try:
            bot.edit_message_caption(
                f"❌ رُفض `{uid}`",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass
    bot.answer_callback_query(call.id)

# ══════════════════════════════════════════════════════════
# 24. أوامر الأدمن
# ══════════════════════════════════════════════════════════
def _admin(m):
    return m.chat.id == ADMIN_ID

def _parse_uid(m, usage: str):
    parts = m.text.split()
    if len(parts) < 2:
        bot.send_message(m.chat.id, f"الاستخدام: {usage}")
        return None
    try:
        return int(parts[1])
    except ValueError:
        bot.send_message(m.chat.id, "❌ ID غير صحيح.")
        return None

@bot.message_handler(commands=["vip"])
def cmd_vip(m):
    if not _admin(m): return
    uid = _parse_uid(m, "/vip [chat_id]")
    if uid is None: return
    activate(uid, "VIP")
    try:
        bot.send_message(uid, "🌟 تم تفعيل اشتراك VIP من قِبل الإدارة!")
    except Exception:
        pass
    bot.send_message(m.chat.id, f"✅ VIP فعّال للمستخدم `{uid}`.",
                     parse_mode="Markdown")

@bot.message_handler(commands=["addmonth"])
def cmd_addmonth(m):
    if not _admin(m): return
    uid = _parse_uid(m, "/addmonth [chat_id]")
    if uid is None: return
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (uid,))
        row = conn.execute(
            "SELECT expiry_date, is_vip FROM users WHERE chat_id=?", (uid,)
        ).fetchone()
        if row and row["is_vip"]:
            bot.send_message(m.chat.id,
                f"ℹ️ المستخدم `{uid}` لديه VIP مدى الحياة.",
                parse_mode="Markdown")
            return
        base = datetime.now()
        if row and row["expiry_date"]:
            try:
                ex = datetime.strptime(row["expiry_date"], "%Y-%m-%d %H:%M:%S")
                if ex > datetime.now():
                    base = ex
            except Exception:
                pass
        new_exp = (base + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE users SET expiry_date=?, is_vip=0 WHERE chat_id=?",
            (new_exp, uid),
        )
    try:
        bot.send_message(uid, "✅ تم تجديد اشتراكك لشهر إضافي!")
    except Exception:
        pass
    bot.send_message(m.chat.id,
        f"✅ أُضيف شهر للمستخدم `{uid}`\nينتهي: `{new_exp}`",
        parse_mode="Markdown")

@bot.message_handler(commands=["revoke"])
def cmd_revoke(m):
    if not _admin(m): return
    uid = _parse_uid(m, "/revoke [chat_id]")
    if uid is None: return
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_vip=0, expiry_date=NULL WHERE chat_id=?", (uid,)
        )
    try:
        bot.send_message(uid,
            "⚠️ تم إلغاء اشتراكك. تواصل مع الدعم للاستفسار.")
    except Exception:
        pass
    bot.send_message(m.chat.id, f"✅ تم إلغاء اشتراك `{uid}`.",
                     parse_mode="Markdown")

@bot.message_handler(commands=["userinfo"])
def cmd_userinfo(m):
    if not _admin(m): return
    uid = _parse_uid(m, "/userinfo [chat_id]")
    if uid is None: return
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, expiry_date, is_vip, last_report FROM users WHERE chat_id=?",
            (uid,),
        ).fetchone()
    if not row:
        bot.send_message(m.chat.id, f"❌ المستخدم `{uid}` غير موجود.",
                         parse_mode="Markdown")
        return
    if row["is_vip"]:
        sub = "🌟 VIP"
    elif row["expiry_date"]:
        sub = f"✅ حتى {row['expiry_date']}"
    else:
        sub = "❌ غير مشترك"
    bot.send_message(
        m.chat.id,
        f"👤 *معلومات المستخدم `{uid}`:*\n\n"
        f"🔗 الرقم الجامعي: `{row['username'] or 'غير مرتبط'}`\n"
        f"🎫 الاشتراك: {sub}\n"
        f"📅 آخر تقرير: {row['last_report'] or 'لم يُرسل'}",
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["report_all"])
def cmd_report_all(m):
    """
    /report_all — يرسل تقريراً فورياً لكل المستخدمين المرتبطين.
    لا يتحقق من last_hash (يرسل حتى لو نفس المحتوى).
    """
    if not _admin(m): return
    wm = bot.send_message(m.chat.id, "📤 جاري إرسال التقارير…")
    with get_db() as conn:
        users = conn.execute(
            "SELECT chat_id, username, password FROM users "
            "WHERE username IS NOT NULL"
        ).fetchall()
    ok = fail = skip = 0
    for row in users:
        uid, user, pwd = row["chat_id"], row["username"], row["password"]
        if not check_access(uid)[0]:
            skip += 1
            continue
        res = run_moodle(user, pwd)
        if res["status"] == "success" and res.get("has_content"):
            try:
                bot.send_message(
                    uid,
                    f"🔔 *تقرير المودل (يدوي من الإدارة):*\n\n{res['message']}",
                    parse_mode="Markdown",
                )
                # تحديث last_hash وlast_report
                h = hashlib.md5(res["message"].encode()).hexdigest()
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET last_hash=?, last_report=? WHERE chat_id=?",
                        (h, datetime.now().strftime("%Y-%m-%d %H:%M"), uid),
                    )
                ok += 1
            except Exception:
                fail += 1
        else:
            skip += 1
    bot.edit_message_text(
        f"✅ *report_all انتهى*\n\n"
        f"📨 أُرسل: {ok}\n"
        f"⏭️ تخطى (لا محتوى/لا اشتراك): {skip}\n"
        f"❌ فشل الإرسال: {fail}",
        m.chat.id, wm.message_id, parse_mode="Markdown",
    )

@bot.message_handler(commands=["holiday"])
def cmd_holiday(m):
    global IS_HOLIDAY
    if not _admin(m): return
    IS_HOLIDAY = not IS_HOLIDAY
    bot.send_message(
        m.chat.id,
        "🏖️ وضع العطلة *مفعّل* — التقارير متوقفة." if IS_HOLIDAY
        else "✅ وضع العطلة *ملغى* — التقارير ستُستأنف.",
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not _admin(m): return
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        linked  = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username IS NOT NULL"
        ).fetchone()[0]
        vip     = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_vip=1"
        ).fetchone()[0]
        active  = conn.execute(
            "SELECT COUNT(*) FROM users WHERE expiry_date > datetime('now')"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE status='pending'"
        ).fetchone()[0]
    trial = "نشطة" if datetime.now() < FREE_TRIAL_END else "انتهت"
    bot.send_message(
        m.chat.id,
        f"📊 *إحصائيات البوت*\n\n"
        f"👥 المستخدمون: {total}\n"
        f"🔗 مرتبطون: {linked}\n"
        f"🌟 VIP: {vip}\n"
        f"✅ اشتراك نشط: {active}\n"
        f"⏳ طلبات معلقة: {pending}\n"
        f"💵 السعر: {price_str()}\n"
        f"🆓 التجربة: {trial}\n"
        f"🏖️ العطلة: {'مفعّل' if IS_HOLIDAY else 'ملغى'}\n\n"
        f"📋 *أوامر الأدمن:*\n"
        f"`/vip [id]` — تفعيل VIP\n"
        f"`/addmonth [id]` — إضافة شهر\n"
        f"`/revoke [id]` — إلغاء اشتراك\n"
        f"`/userinfo [id]` — معلومات مستخدم\n"
        f"`/report_all` — تقرير فوري للجميع\n"
        f"`/broadcast [نص]` — إشعار للجميع\n"
        f"`/holiday` — تفعيل/إلغاء العطلة",
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not _admin(m): return
    text = m.text.replace("/broadcast", "", 1).strip()
    if not text:
        bot.send_message(m.chat.id, "الاستخدام: /broadcast [الرسالة]")
        return
    with get_db() as conn:
        uids = [r[0] for r in conn.execute("SELECT chat_id FROM users").fetchall()]
    ok = 0
    for uid in uids:
        try:
            bot.send_message(uid, f"📢 *إشعار:*\n\n{text}",
                             parse_mode="Markdown")
            ok += 1
        except Exception:
            pass
    bot.send_message(m.chat.id, f"✅ أُرسلت لـ {ok}/{len(uids)} مستخدم.")

# ══════════════════════════════════════════════════════════
# 25. التقارير الدورية
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
        
        # ملاحظة: إذا كنت تريد الإرسال حتى لو لم يكن هناك واجبات (رسالة "لا يوجد مهام")
        # قم بإلغاء تفعيل السطر التالي أو تعديله.
        if not res.get("has_content"): continue 

        msg = res["message"]
        
        # --- تم حذف التحقق من الـ Hash من هنا ---
        
        try:
            bot.send_message(
                uid,
                f"🔔 *تقرير المودل الدوري:*\n\n{msg}",
                parse_mode="Markdown",
            )
            
            # تحديث وقت آخر تقرير فقط (اختياري)
            with get_db() as conn:
                conn.execute(
                    "UPDATE users SET last_report=? WHERE chat_id=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M"), uid),
                )
        except Exception as e:
            log.warning(f"فشل إرسال تقرير لـ {uid}: {e}")

# ══════════════════════════════════════════════════════════
# 26. المُجدوِل
# ══════════════════════════════════════════════════════════
def _scheduler():
    schedule.every(6).hours.do(broadcast_reports)
    schedule.every(2).minutes.do(poll_payments)
    schedule.every(12).hours.do(refresh_rate)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ══════════════════════════════════════════════════════════
# 27. التشغيل
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    refresh_rate()
    threading.Thread(target=_scheduler, daemon=True).start()
    log.info("✅ البوت يعمل…")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)