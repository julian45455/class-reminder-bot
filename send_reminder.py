"""
اسکریپت یادآوری روزانه — برای اجرا توسط GitHub Actions (نه اجرای دائمی)
برنامه شما بر اساس تاریخ دقیق شمسی است (نه تکرار هفتگی)، پس این اسکریپت
تاریخ فردا را از میلادی به شمسی تبدیل می‌کند و دقیقاً همان تاریخ را در
schedule.json جست‌وجو می‌کند.
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
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")

SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), "schedule.json")


def load_schedule():
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def classes_for_jalali_date(schedule, jalali_date_str):
    items = [c for c in schedule if c.get("date", "").strip() == jalali_date_str]
    items.sort(key=lambda c: c.get("time", ""))
    return items


def format_message(items, jalali_date_str):
    lines = [f"🔔 یادآوری: برنامه کلاسی فردا ({jalali_date_str})"]
    for c in items:
        day = f" - {c['day']}" if c.get("day") else ""
        lines.append(f"⏰ {c.get('time', '?')}  |  {c.get('course', '?')}{day}")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    tz = pytz.timezone(TIMEZONE)
    tomorrow_gregorian = (datetime.now(tz) + timedelta(days=1)).date()
    tomorrow_jalali = jdatetime.date.fromgregorian(date=tomorrow_gregorian)
    jalali_str = f"{tomorrow_jalali.year:04d}/{tomorrow_jalali.month:02d}/{tomorrow_jalali.day:02d}"

    schedule = load_schedule()
    items = classes_for_jalali_date(schedule, jalali_str)

    print(f"تاریخ فردا (شمسی): {jalali_str} — تعداد کلاس‌های پیدا‌شده: {len(items)}")

    if not items:
        print("فردا کلاسی نیست — پیامی ارسال نشد.")
        return

    text = format_message(items, jalali_str)
    result = send_telegram_message(text)
    print("ارسال شد:", result.get("ok"))


if __name__ == "__main__":
    sys.exit(main())
