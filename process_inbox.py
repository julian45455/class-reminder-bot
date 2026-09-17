"""
پردازش‌گر پیام‌های ورودی تلگرام برای ثبت خودکار امتحان
------------------------------------------------------
هر بار اجرا می‌شود: پیام‌های جدید تلگرام را می‌خواند (getUpdates)، سعی می‌کند
از هرکدام یک امتحان استخراج کند (الگو: [توضیح] + [روز هفته اختیاری] +
[عدد روز] + [نام ماه شمسی])، آن را به exams.json اضافه می‌کند، و به شما
پاسخ تأیید یا خطا می‌فرستد.

فقط پیام‌هایی که با فرمت زیر شبیه باشند تشخیص داده می‌شوند:
  <هر متنی> <روز هفته (اختیاری)> <عدد روز> <نام ماه شمسی>
مثال‌های معتبر:
  "امتحان ورود پاتو عملی سه شنبه 27 مهر"
  "امتحان زبان 20 مهر"
  "میان ترم آسیب چهارشنبه 15 آبان"
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
WEEKDAY_WORDS = [
    "شنبه", "یک‌شنبه", "یکشنبه", "دوشنبه", "دو‌شنبه",
    "سه‌شنبه", "سه شنبه", "چهارشنبه", "چهار‌شنبه", "چهار شنبه",
    "پنج‌شنبه", "پنجشنبه", "پنج شنبه", "جمعه",
]

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

MONTH_PATTERN = "|".join(PERSIAN_MONTHS.keys())
DATE_RE = re.compile(rf"(\d{{1,2}})\s*({MONTH_PATTERN})")


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


def parse_exam_message(text, tz):
    text_norm = normalize_digits(text)
    m = DATE_RE.search(text_norm)
    if not m:
        return None

    day = int(m.group(1))
    month = PERSIAN_MONTHS[m.group(2)]
    try:
        jalali_date = guess_jalali_year(month, day, tz)
    except ValueError:
        return None

    course = text_norm[:m.start()] + text_norm[m.end():]
    for w in WEEKDAY_WORDS:
        course = course.replace(w, " ")
    course = re.sub(r"\s+", " ", course).strip(" \u200c-،,")
    if not course:
        course = "امتحان"

    date_str = f"{jalali_date.year:04d}/{jalali_date.month:02d}/{jalali_date.day:02d}"
    return {"date": date_str, "course": course}


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


def main():
    tz = pytz.timezone(TIMEZONE)
    state = load_json(STATE_FILE, {"last_update_id": None})
    offset = (state["last_update_id"] + 1) if state.get("last_update_id") is not None else None

    updates = get_updates(offset)
    if not updates:
        print("پیام جدیدی نیست.")
        return

    exams = load_json(EXAMS_FILE, [])
    changed = False

    for upd in updates:
        state["last_update_id"] = upd["update_id"]
        msg = upd.get("message") or upd.get("edited_message")
        if not msg or "text" not in msg:
            continue
        chat_id = str(msg["chat"]["id"])
        if chat_id != str(CHAT_ID):
            continue  # فقط پیام‌های چت خودتان پردازش شود

        text = msg["text"].strip()
        if text.startswith("/"):
            continue  # دستورات را نادیده بگیر

        parsed = parse_exam_message(text, tz)
        if parsed is None:
            send_telegram_message(
                "متوجه تاریخ نشدم. لطفاً به این شکل بنویسید:\n"
                "«<نام درس> <روز هفته اختیاری> <عدد روز> <نام ماه شمسی>»\n"
                "مثال: امتحان زبان سه‌شنبه ۲۰ مهر"
            )
            continue

        exams.append(parsed)
        changed = True
        send_telegram_message(
            f"✅ ثبت شد: «{parsed['course']}» — تاریخ {parsed['date']}"
        )

    if changed:
        exams.sort(key=lambda e: e.get("date", ""))
        save_json(EXAMS_FILE, exams)

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
