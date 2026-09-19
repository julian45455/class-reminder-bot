"""
اسکریپت یادآوری روزانه — برای اجرا توسط GitHub Actions (نه اجرای دائمی)
هر بار که اجرا شود: چک می‌کند فردا کلاس دارید یا نه، اگر دارد پیام می‌فرستد.
از توکن ربات و chat_id به‌عنوان GitHub Secrets استفاده می‌کند.
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
# چند آی‌دی با کاما جدا شود، مثلاً: "-1001234567890,-1009876543210"
BROADCAST_CHAT_IDS = [c.strip() for c in os.environ.get("BROADCAST_CHAT_IDS", "").split(",") if c.strip()]
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")

SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), "schedule.json")


def load_schedule():
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def jalali_str(greg_date):
    jd = jdatetime.date.fromgregorian(date=greg_date)
    return f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d}"


def classes_for_day(schedule, date_str):
    items = [c for c in schedule if c.get("date", "").strip() == date_str]
    items.sort(key=lambda c: c.get("time", ""))
    return items


def format_message(items):
    lines = ["🔔 یادآوری: برنامه کلاسی فردا"]
    for c in items:
        loc = f" — {c['location']}" if c.get("location") else ""
        lines.append(f"⏰ {c.get('time', '?')}  |  {c.get('course', '?')}{loc}")
    return "\n".join(lines)


def send_telegram_message(text, chat_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    tz = pytz.timezone(TIMEZONE)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    tomorrow_jalali = jalali_str(tomorrow)
    schedule = load_schedule()
    items = classes_for_day(schedule, tomorrow_jalali)

    if not items:
        print("فردا کلاسی نیست — پیامی ارسال نشد.")
        return

    text = format_message(items)
    targets = [CHAT_ID] + BROADCAST_CHAT_IDS
    for chat_id in targets:
        try:
            result = send_telegram_message(text, chat_id)
            print(f"ارسال شد به {chat_id}:", result.get("ok"))
        except requests.RequestException as exc:
            print(f"ارسال به {chat_id} ناموفق بود:", exc)


if __name__ == "__main__":
    sys.exit(main())
