"""
اسکریپت یادآوری روزانه — برای اجرا توسط GitHub Actions (نه اجرای دائمی)
هر بار که اجرا شود: چک می‌کند فردا کلاس دارید یا نه، اگر دارد پیام می‌فرستد.
از توکن ربات و chat_id به‌عنوان GitHub Secrets استفاده می‌کند.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")

WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday",
               "Friday", "Saturday", "Sunday"]

SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), "schedule.json")


def load_schedule():
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def classes_for_day(schedule, day_name):
    day_name = day_name.strip().lower()
    items = [c for c in schedule if c.get("day", "").strip().lower() == day_name]
    items.sort(key=lambda c: c.get("time", ""))
    return items


def format_message(items):
    lines = ["🔔 یادآوری: برنامه کلاسی فردا"]
    for c in items:
        loc = f" — {c['location']}" if c.get("location") else ""
        lines.append(f"⏰ {c.get('time', '?')}  |  {c.get('course', '?')}{loc}")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    tz = pytz.timezone(TIMEZONE)
    tomorrow = datetime.now(tz) + timedelta(days=1)
    schedule = load_schedule()
    items = classes_for_day(schedule, WEEKDAYS_EN[tomorrow.weekday()])

    if not items:
        print("فردا کلاسی نیست — پیامی ارسال نشد.")
        return

    text = format_message(items)
    result = send_telegram_message(text)
    print("ارسال شد:", result.get("ok"))


if __name__ == "__main__":
    sys.exit(main())
