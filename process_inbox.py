"""
پردازش‌گر پیام‌های ورودی تلگرام برای ثبت/ویرایش/حذف امتحان
------------------------------------------------------------
دو حالت اجرا:
  ۱) repository_dispatch (از Cloudflare Worker، لحظه‌ای) — پیام تلگرام در
     client_payload.update همراه event می‌آید، هیچ getUpdates زده نمی‌شود.
  ۲) workflow_dispatch دستی / اجرای بدون client_payload — برای تست یا
     زمانی که webhook هنوز تنظیم نشده، با getUpdates + offset قدیمی کار
     می‌کند (توجه: تا وقتی webhook تلگرام فعال است getUpdates خطای ۴۰۹
     می‌دهد؛ این مسیر فقط برای قبل از تنظیم webhook یا تست دستی است).

فرمت پیام برای افزودن امتحان (با خط تیره جدا شود):
  <درس> - <بودجه‌بندی (اختیاری)> - <روز هفته (اختیاری)> - <روز عددی> - <ماه>
مثال‌ها:
  "امتحان زبان - 20 - مهر"
  "امتحان پاتو عملی - فصل ۳ تا ۵ - سه‌شنبه - 27 - مهر"

اگر پیام خط تیره نداشته باشد، همچنان با روش قدیمی (جست‌وجوی آزاد عدد+ماه
در متن) هم تلاش می‌شود.

دستورات مدیریتی:
  /list  یا  لیست            → نمایش شماره‌دار همهٔ امتحان‌ها
  /del N  یا  حذف N          → حذف امتحان شماره N (بر اساس آخرین /list)
  /edit N <متن جدید>  یا  ویرایش N <متن جدید>  → جایگزینی کامل امتحان N
"""

import json
import os
import re
from datetime import datetime

import jdatetime
import pytz
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")

EXAMS_FILE = os.path.join(os.path.dirname(__file__), "exams.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "bot_state.json")

PERSIAN_MONTHS = {
    "فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5, "شهریور": 6,
    "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10, "بهمن": 11, "اسفند": 12,
}
MONTH_NAMES_REV = {v: k for k, v in PERSIAN_MONTHS.items()}

WEEKDAY_WORDS = [
    "شنبه", "یک‌شنبه", "یکشنبه", "دوشنبه", "دو‌شنبه",
    "سه‌شنبه", "سه شنبه", "چهارشنبه", "چهار‌شنبه", "چهار شنبه",
    "پنج‌شنبه", "پنجشنبه", "پنج شنبه", "جمعه",
]

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
DASH_RE = re.compile(r"[-–—ـ]")
MONTH_PATTERN = "|".join(PERSIAN_MONTHS.keys())
LEGACY_DATE_RE = re.compile(rf"(\d{{1,2}})\s*({MONTH_PATTERN})")

HELP_TEXT = (
    "متوجه نشدم. برای افزودن امتحان این‌طور بنویسید:\n"
    "«<درس> - <بودجه‌بندی اختیاری> - <روز هفته اختیاری> - <روز عددی> - <ماه>»\n"
    "مثال: امتحان زبان - فصل ۱ تا ۳ - سه‌شنبه - 20 - مهر\n\n"
    "دستورات مدیریتی:\n"
    "لیست  →  نمایش شماره‌دار امتحان‌ها\n"
    "حذف N  →  حذف امتحان شماره N\n"
    "حذف N, M, K  یا  حذف N-K  →  حذف چند امتحان یا یک بازه با هم\n"
    "ویرایش N- متن جدید-  →  جایگزینی کامل امتحان N (خط تیرهٔ آخر اختیاری است)"
)


def normalize_digits(text):
    return text.translate(DIGIT_MAP)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def guess_jalali_year(month, day, tz):
    today_jalali = jdatetime.date.fromgregorian(date=datetime.now(tz).date())
    candidate = jdatetime.date(today_jalali.year, month, day)
    if candidate.togregorian() < datetime.now(tz).date():
        candidate = jdatetime.date(today_jalali.year + 1, month, day)
    return candidate


def jalali_display(date_str):
    """ '1403/07/20' -> '20 مهر' """
    try:
        y, m, d = date_str.split("/")
        return f"{int(d)} {MONTH_NAMES_REV[int(m)]}"
    except Exception:
        return date_str or "?"


def parse_exam_message_v2(text_norm, tz):
    """فرمت جدید با خط تیره: درس - [بودجه‌بندی] - [روز هفته] - روز - ماه"""
    parts = [p.strip() for p in DASH_RE.split(text_norm)]
    parts = [p for p in parts if p]
    if len(parts) < 3:
        return None

    month = PERSIAN_MONTHS.get(parts[-1])
    if month is None:
        return None

    if not re.fullmatch(r"\d{1,2}", parts[-2]):
        return None
    day = int(parts[-2])

    rest = parts[:-2]
    if not rest:
        return None
    course = rest[0]
    remainder = rest[1:]

    weekday = None
    for i, p in enumerate(remainder):
        if p in WEEKDAY_WORDS:
            weekday = remainder.pop(i)
            break

    budget = " - ".join(remainder) if remainder else None

    try:
        jalali_date = guess_jalali_year(month, day, tz)
    except ValueError:
        return None

    date_str = f"{jalali_date.year:04d}/{jalali_date.month:02d}/{jalali_date.day:02d}"
    result = {"date": date_str, "course": course}
    if budget:
        result["budget"] = budget
    if weekday:
        result["weekday"] = weekday
    return result


def parse_exam_message_legacy(text_norm, tz):
    """روش قدیمی: جست‌وجوی آزاد عدد+ماه در متن، بدون خط تیره."""
    m = LEGACY_DATE_RE.search(text_norm)
    if not m:
        return None

    day = int(m.group(1))
    month = PERSIAN_MONTHS[m.group(2)]
    try:
        jalali_date = guess_jalali_year(month, day, tz)
    except ValueError:
        return None

    course = text_norm[:m.start()] + text_norm[m.end():]
    for w in sorted(WEEKDAY_WORDS, key=len, reverse=True):
        course = course.replace(w, " ")
    course = re.sub(r"\s+", " ", course).strip(" \u200c-،,")
    if not course:
        course = "امتحان"

    date_str = f"{jalali_date.year:04d}/{jalali_date.month:02d}/{jalali_date.day:02d}"
    return {"date": date_str, "course": course}


def parse_exam_message(text, tz):
    text_norm = normalize_digits(text)
    if DASH_RE.search(text_norm):
        parsed = parse_exam_message_v2(text_norm, tz)
        if parsed is not None:
            return parsed
    return parse_exam_message_legacy(text_norm, tz)


def format_exam_line(idx, e):
    extra = []
    if e.get("budget"):
        extra.append(e["budget"])
    if e.get("weekday"):
        extra.append(e["weekday"])
    extra_str = f" — {' — '.join(extra)}" if extra else ""
    return f"{idx}) {e.get('course', '?')}{extra_str} — {jalali_display(e.get('date', ''))}"


def handle_command(text, exams, tz):
    """
    اگر text یک دستور مدیریتی باشد (لیست/حذف/ویرایش) پردازش می‌کند.
    خروجی: (reply_text یا None اگر دستور نبود, changed:bool, exams جدید)
    """
    t = text.strip()

    if t in ("/help", "راهنما", "/راهنما", "کمک"):
        return HELP_TEXT, False, exams

    if t in ("/list", "لیست", "/لیست"):
        if not exams:
            return "هیچ امتحانی ثبت نشده.", False, exams
        sorted_exams = sorted(exams, key=lambda x: x.get("date", ""))
        lines = ["📋 امتحان‌های ثبت‌شده:"]
        lines += [format_exam_line(i, e) for i, e in enumerate(sorted_exams, 1)]
        return "\n".join(lines), False, exams

    m = re.match(r"^(?:/del(?:ete)?|حذف)\s+([0-9۰-۹,،\s\-]+)$", t)
    if m:
        raw = normalize_digits(m.group(1)).strip()
        tokens = [tok for tok in re.split(r"[,،\s]+", raw) if tok]
        indices = set()
        invalid = []
        for tok in tokens:
            rng = re.fullmatch(r"(\d+)-(\d+)", tok)
            if rng:
                a, b = int(rng.group(1)), int(rng.group(2))
                if a > b:
                    a, b = b, a
                indices.update(range(a, b + 1))
            elif re.fullmatch(r"\d+", tok):
                indices.add(int(tok))
            else:
                invalid.append(tok)
        if invalid:
            return f"ورودی نامعتبر: {', '.join(invalid)}", False, exams
        if not indices:
            return "شماره‌ای برای حذف داده نشده.", False, exams

        sorted_exams = sorted(exams, key=lambda x: x.get("date", ""))
        bad = sorted(i for i in indices if not (1 <= i <= len(sorted_exams)))
        if bad:
            return f"شماره‌های نامعتبر: {', '.join(map(str, bad))}. برای دیدن شماره‌ها «لیست» بفرستید.", False, exams

        targets = [sorted_exams[i - 1] for i in sorted(indices)]
        target_ids = {id(e) for e in targets}
        new_exams = [e for e in exams if id(e) not in target_ids]
        lines = ["🗑 حذف شد:"]
        lines += [format_exam_line(i, sorted_exams[i - 1]) for i in sorted(indices)]
        return "\n".join(lines), True, new_exams

    m = re.match(r"^(?:/edit|ویرایش)\s*(\d+)[\s\-]+(.+)$", t, re.S)
    if m:
        idx = int(m.group(1))
        new_text = re.sub(r"-+\s*$", "", m.group(2)).strip()
        sorted_exams = sorted(exams, key=lambda x: x.get("date", ""))
        if not (1 <= idx <= len(sorted_exams)):
            return f"شماره {idx} معتبر نیست. برای دیدن شماره‌ها «لیست» بفرستید.", False, exams
        target = sorted_exams[idx - 1]
        parsed = parse_exam_message(new_text, tz)
        if parsed is None:
            return HELP_TEXT, False, exams
        new_exams = [parsed if e is target else e for e in exams]
        return f"✏️ ویرایش شد → {format_exam_line(idx, parsed)}", True, new_exams

    return None, False, exams



def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", [])


def get_dispatch_message():
    """اگر اجرا از طریق repository_dispatch با client_payload.update بوده، پیام را برمی‌گرداند."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None
    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)
    payload = event.get("client_payload") or {}
    update = payload.get("update")
    if not update:
        return None
    return update.get("message") or update.get("edited_message")


def collect_messages(tz):
    """پیام‌های قابل‌پردازش این اجرا را برمی‌گرداند (حالت webhook یا polling قدیمی)."""
    dispatch_msg = get_dispatch_message()
    if dispatch_msg is not None:
        return [dispatch_msg]

    state = load_json(STATE_FILE, {"last_update_id": None})
    offset = (state["last_update_id"] + 1) if state.get("last_update_id") is not None else None
    updates = get_updates(offset)
    messages = []
    for upd in updates:
        state["last_update_id"] = upd["update_id"]
        m = upd.get("message") or upd.get("edited_message")
        if m:
            messages.append(m)
    save_json(STATE_FILE, state)
    return messages


def main():
    tz = pytz.timezone(TIMEZONE)
    messages = collect_messages(tz)
    if not messages:
        print("پیام جدیدی نیست.")
        return

    exams = load_json(EXAMS_FILE, [])
    changed = False

    for msg in messages:
        if "text" not in msg:
            continue
        chat_id = str(msg["chat"]["id"])
        if chat_id != str(CHAT_ID):
            continue

        text = msg["text"].strip()

        reply, cmd_changed, exams = handle_command(text, exams, tz)
        if reply is not None:
            send_telegram_message(reply)
            changed = changed or cmd_changed
            continue

        if text.startswith("/"):
            continue  # سایر دستورات ناشناخته را نادیده بگیر

        parsed = parse_exam_message(text, tz)
        if parsed is None:
            send_telegram_message(HELP_TEXT)
            continue

        exams.append(parsed)
        changed = True
        extra = []
        if parsed.get("budget"):
            extra.append(parsed["budget"])
        if parsed.get("weekday"):
            extra.append(parsed["weekday"])
        extra_str = f" — {' — '.join(extra)}" if extra else ""
        send_telegram_message(
            f"✅ ثبت شد: «{parsed['course']}»{extra_str} — {jalali_display(parsed['date'])}"
        )

    if changed:
        exams.sort(key=lambda e: e.get("date", ""))
        save_json(EXAMS_FILE, exams)


if __name__ == "__main__":
    main()
