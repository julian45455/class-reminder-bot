"""
اسکریپت یادآوری هفتگی امتحان‌ها — برای اجرا توسط GitHub Actions
هر پنج‌شنبه ساعت ۸ صبح اجرا می‌شود، امتحان‌های ۷ روز پیش‌رو (از امروز تا
۶ روز بعد) را از exams.json پیدا می‌کند و پیام می‌فرستد.
اگر امتحانی در این بازه نباشد، هیچ پیامی ارسال نمی‌شود.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import jdatetime
import pytz
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
# آی‌دی گروه/کانال‌های اضافی برای ارسال یادآوری (فقط ارسال، نه خواندن دستور).
# مقدار می‌تواند چند آی‌دی جدا شده با کاما باشد، مثلاً: "-1001234567890,-1009876543210"
BROADCAST_CHAT_IDS = [c.strip() for c in os.environ.get("BROADCAST_CHAT_IDS", "").split(",") if c.strip()]
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")
WINDOW_DAYS = int(os.environ.get("EXAM_WINDOW_DAYS", 7))  # امروز + ۶ روز بعد = ۷ روز

EXAMS_FILE = os.path.join(os.path.dirname(__file__), "exams.json")

PERSIAN_MONTHS = {
    "فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5, "شهریور": 6,
    "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10, "بهمن": 11, "اسفند": 12,
}
MONTH_NAMES_REV = {v: k for k, v in PERSIAN_MONTHS.items()}


def load_exams():
    if not os.path.exists(EXAMS_FILE):
        return []
    with open(EXAMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def jalali_str(greg_date):
    jd = jdatetime.date.fromgregorian(date=greg_date)
    return f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d}"


def jalali_display(date_str):
    """ '1403/07/20' -> '20 مهر' """
    try:
        y, m, d = date_str.split("/")
        return f"{int(d)} {MONTH_NAMES_REV[int(m)]}"
    except Exception:
        return date_str or "?"


def exams_in_window(exams, start_str, end_str):
    # فرمت تاریخ‌ها YYYY/MM/DD با صفرِ ابتدایی است، پس مقایسه رشته‌ای همان
    # ترتیب زمانی واقعی را می‌دهد.
    items = [e for e in exams if start_str <= e.get("date", "") <= end_str]
    items.sort(key=lambda e: e.get("date", ""))
    return items


def format_message(items, start_str, end_str, window_days):
    lines = [f"📚 کارهای مهم دانشگاه در {window_days} روز آینده ({jalali_display(start_str)} تا {jalali_display(end_str)}):"]
    for e in items:
        extra = []
        if e.get("budget"):
            extra.append(e["budget"])
        if e.get("weekday"):
            extra.append(e["weekday"])
        if e.get("type"):
            extra.append(e["type"])
        if e.get("location"):
            extra.append(e["location"])
        extra_str = f" — {' — '.join(extra)}" if extra else ""
        lines.append(f"🗓 {e.get('course', '?')}{extra_str} — {jalali_display(e.get('date', ''))}")
    return "\n".join(lines)


def send_telegram_message(text, chat_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    end_date = today + timedelta(days=WINDOW_DAYS - 1)

    start_str = jalali_str(today)
    end_str = jalali_str(end_date)

    exams = load_exams()
    items = exams_in_window(exams, start_str, end_str)

    print(f"بازه بررسی‌شده (شمسی): {start_str} تا {end_str} — تعداد امتحان‌های پیدا‌شده: {len(items)}")

    if not items:
        print("امتحانی در این بازه نیست — پیامی ارسال نشد.")
        return

    text = format_message(items, start_str, end_str, WINDOW_DAYS)
    targets = [CHAT_ID] + BROADCAST_CHAT_IDS
    for chat_id in targets:
        try:
            result = send_telegram_message(text, chat_id)
            print(f"ارسال شد به {chat_id}:", result.get("ok"))
        except requests.RequestException as exc:
            print(f"ارسال به {chat_id} ناموفق بود:", exc)


if __name__ == "__main__":
    sys.exit(main())
