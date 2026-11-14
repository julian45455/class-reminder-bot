"""ارسال پاسخ‌های ذخیره‌شده در outbox پس از commit شدن داده‌های ورودی."""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
OUTBOX_FILE = os.path.join(os.path.dirname(__file__), "outbox.json")
MAX_ATTEMPTS = 5


class TelegramSendError(RuntimeError):
    pass


def load_outbox():
    if not os.path.exists(OUTBOX_FILE):
        return []
    with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("outbox.json must contain a JSON list")
    return data


def save_outbox(items):
    tmp_path = OUTBOX_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, OUTBOX_FILE)


def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    # در صورت پاسخ 429، فقط یک بار و در صورت کوتاه بودن زمان انتظار retry می‌کنیم.
    for attempt in range(2):
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise TelegramSendError("Telegram returned a non-JSON response")

        if response.status_code == 429:
            retry_after = int(
                (payload.get("parameters") or {}).get("retry_after", 1)
            )
            if attempt == 0 and retry_after <= 20:
                time.sleep(max(1, retry_after))
                continue
            raise TelegramSendError(
                f"Telegram rate limit (429); retry_after={retry_after}"
            )

        response.raise_for_status()
        if not payload.get("ok"):
            raise TelegramSendError(
                payload.get("description", "Telegram sendMessage failed")
            )
        return payload

    raise TelegramSendError("Telegram sendMessage retry exhausted")


def main():
    outbox = load_outbox()
    pending = [
        item for item in outbox
        if item.get("status", "pending") == "pending"
    ]
    if not pending:
        print("پاسخ ارسال‌نشده‌ای در outbox وجود ندارد.")
        return 0

    retryable_failures = 0
    permanent_failures = 0
    sent_count = 0
    for index, item in enumerate(pending):
        item["attempts"] = int(item.get("attempts", 0)) + 1
        try:
            send_message(item["chat_id"], item["text"])
            item["status"] = "sent"
            item["sent_at"] = datetime.now(timezone.utc).isoformat()
            item.pop("last_error", None)
            sent_count += 1
            print(f"پاسخ ارسال شد: {item.get('id')}")
        except (requests.RequestException, TelegramSendError, ValueError) as exc:
            item["last_error"] = str(exc)[:500]
            if item["attempts"] >= MAX_ATTEMPTS:
                item["status"] = "failed"
                permanent_failures += 1
                print(
                    f"ارسال پس از {MAX_ATTEMPTS} تلاش ناموفق ماند "
                    f"({item.get('id')}): {exc}"
                )
            else:
                item["status"] = "pending"
                retryable_failures += 1
                print(f"ارسال ناموفق بود ({item.get('id')}): {exc}")
        finally:
            # وضعیت هر پاسخ جداگانه ذخیره می‌شود تا خطای پیام بعدی، قبلی‌ها را برنگرداند.
            save_outbox(outbox)

        # محدودیت نرخ تلگرام برای ارسال چند پیام به یک گفت‌وگو رعایت شود.
        if index < len(pending) - 1:
            time.sleep(1.1)

    # برای جلوگیری از رشد بی‌نهایت فایل، فقط ۱۰۰۰ پاسخ موفق اخیر نگه داشته می‌شود.
    sent_items = [item for item in outbox if item.get("status") == "sent"]
    pending_items = [item for item in outbox if item.get("status") != "sent"]
    outbox = sent_items[-1000:] + pending_items
    save_outbox(outbox)

    print(
        f"نتیجه: {sent_count} ارسال موفق، "
        f"{retryable_failures} نیازمند تلاش مجدد، "
        f"{permanent_failures} ناموفق نهایی"
    )
    return 1 if retryable_failures else 0


if __name__ == "__main__":
    sys.exit(main())
