"""
پردازش‌گر پیام‌های ورودی تلگرام برای ثبت/ویرایش/حذف امتحان
------------------------------------------------------------
دریافت پیام‌ها فقط از طریق Cloudflare Webhook و repository_dispatch انجام می‌شود.
Cloudflare پیام‌ها را در Durable Object صف می‌کند و به‌صورت دسته‌ای می‌فرستد.
این اسکریپت عمداً از getUpdates استفاده نمی‌کند تا با Webhook تلگرام تداخل 409
ایجاد نشود. اجرای دستی Workflow بدون payload فقط یک اجرای بی‌اثر خواهد بود.

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
NOTES_FILE = os.path.join(os.path.dirname(__file__), "notes.json")
OUTBOX_FILE = os.path.join(os.path.dirname(__file__), "outbox.json")
PROCESSED_IDS_LIMIT = 1000

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
    "چند مورد در یک پیام: هر مورد را در یک خط جدا (یا با ؛ جدا) بنویسید؛ همه جداگانه ثبت می‌شوند\n"
    "کارهای مهم  →  گزارش کارهای مهم دانشگاه در بازهٔ پیش‌رو (از امروز)\n"
    "یادداشت - <متن> - <روز یا چند روز با کاما> - <ماه>  →  یادداشتی که در پیام روزانهٔ شب قبل از آن روزها می‌آید\n"
    "مثال: یادداشت - بخش جراحی خالیه - 20, 27 - مهر\n"
    "یادداشت‌ها  →  لیست شماره‌دار یادداشت‌ها\n"
    "حذف یادداشت N  (یا چند شماره / بازه)  →  حذف یادداشت\n"
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
    """ذخیره اتمیک JSON تا فایل در صورت توقف ناگهانی ناقص نماند."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


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

    if t in ("/upcoming", "کارهای مهم", "/کارهای_مهم"):
        from send_weekly_exams import build_report, WINDOW_DAYS
        text, start_str, end_str = build_report(exams, tz, WINDOW_DAYS, start_offset=0)
        if text is None:
            return f"کار مهمی در {WINDOW_DAYS} روز آینده ثبت نشده.", False, exams
        return text, False, exams

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



NOTE_PREFIXES = ("یادداشت", "/note")
NOTE_USAGE = (
    "فرمت یادداشت:\n"
    "یادداشت - <متن> - <روز یا چند روز با کاما> - <ماه>\n"
    "مثال: یادداشت - بخش جراحی خالیه - 20, 27 - مهر"
)


def parse_index_list(raw):
    raw = normalize_digits(raw).strip()
    tokens = [tok for tok in re.split(r"[,،\s]+", raw) if tok]
    indices, invalid = set(), []
    for tok in tokens:
        rng = re.fullmatch(r"(\d+)-(\d+)", tok)
        if rng:
            a, b = sorted((int(rng.group(1)), int(rng.group(2))))
            indices.update(range(a, b + 1))
        elif re.fullmatch(r"\d+", tok):
            indices.add(int(tok))
        else:
            invalid.append(tok)
    return indices, invalid


def format_note_line(idx, n):
    return f"{idx}) {n.get('text', '?')} — {jalali_display(n.get('date', ''))}"


def handle_note_command(text, notes, tz):
    """
    یادداشت‌های تاریخ‌دار برای پیام روزانه (notes.json).
    خروجی: (reply یا None اگر دستور یادداشت نبود, changed, notes)
    """
    tn = normalize_digits(text.strip()).replace("\u200c", " ")

    if tn in ("/notes", "یادداشت ها"):
        if not notes:
            return "هیچ یادداشتی ثبت نشده.", False, notes
        ordered = sorted(notes, key=lambda x: x.get("date", ""))
        lines = ["📝 یادداشت‌های ثبت‌شده:"]
        lines += [format_note_line(i, n) for i, n in enumerate(ordered, 1)]
        return "\n".join(lines), False, notes

    m = re.match(r"^(?:/delnote|حذف یادداشت)\s+([0-9,،\s\-]+)$", tn)
    if m:
        indices, invalid = parse_index_list(m.group(1))
        if invalid or not indices:
            return "ورودی نامعتبر. مثال: حذف یادداشت 2 یا حذف یادداشت 1, 3-4", False, notes
        ordered = sorted(notes, key=lambda x: x.get("date", ""))
        bad = sorted(i for i in indices if not (1 <= i <= len(ordered)))
        if bad:
            return f"شماره‌های نامعتبر: {', '.join(map(str, bad))}. برای دیدن شماره‌ها «یادداشت‌ها» بفرستید.", False, notes
        targets = [ordered[i - 1] for i in sorted(indices)]
        target_ids = {id(n) for n in targets}
        new_notes = [n for n in notes if id(n) not in target_ids]
        lines = ["🗑 یادداشت حذف شد:"]
        lines += [format_note_line(i, ordered[i - 1]) for i in sorted(indices)]
        return "\n".join(lines), True, new_notes

    parts = [p.strip() for p in DASH_RE.split(tn)]
    parts = [p for p in parts if p]
    if not parts or parts[0] not in NOTE_PREFIXES:
        return None, False, notes
    if len(parts) < 4:
        return NOTE_USAGE, False, notes

    month = PERSIAN_MONTHS.get(parts[-1])
    day_tokens = [d for d in re.split(r"[,،\s]+", parts[-2]) if d]
    if month is None or not day_tokens or not all(re.fullmatch(r"\d{1,2}", d) for d in day_tokens):
        return NOTE_USAGE, False, notes
    note_text = " - ".join(parts[1:-2])

    added, new_notes = [], list(notes)
    for d in day_tokens:
        try:
            jd = guess_jalali_year(month, int(d), tz)
        except ValueError:
            return f"روز {d} برای این ماه معتبر نیست.", False, notes
        date_str = f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d}"
        if any(n.get("date") == date_str and n.get("text") == note_text for n in new_notes):
            continue
        new_notes.append({"date": date_str, "text": note_text})
        added.append(jalali_display(date_str))
    if not added:
        return "این یادداشت از قبل برای همین تاریخ‌ها ثبت شده.", False, notes
    return f"📝 ثبت شد: «{note_text}» — {'، '.join(added)}", True, new_notes


LIST_MARKER_RE = re.compile(r"^\s*(?:[•*]|\d+[.)])\s*")


def split_entries(text):
    """متن چندخطی (یا جداشده با ؛) را به موارد جدا می‌شکند؛ شماره‌گذاری/بولت ابتدای خط حذف می‌شود."""
    entries = []
    for chunk in re.split(r"[\n;؛]+", text):
        chunk = LIST_MARKER_RE.sub("", chunk).strip()
        if chunk:
            entries.append(chunk)
    return entries


def format_added(parsed):
    extra = [x for x in (parsed.get("budget"), parsed.get("weekday")) if x]
    extra_str = f" — {' — '.join(extra)}" if extra else ""
    return f"«{parsed['course']}»{extra_str} — {jalali_display(parsed['date'])}"


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_dispatch_updates():
    """دریافت یک batch از repository_dispatch؛ بدون polling تلگرام."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return []

    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)

    payload = event.get("client_payload") or {}
    updates = payload.get("updates")
    if isinstance(updates, list):
        return [u for u in updates if isinstance(u, dict)]

    # سازگاری موقت با Worker قدیمی که هر بار فقط یک update می‌فرستاد.
    update = payload.get("update")
    return [update] if isinstance(update, dict) else []


def message_from_update(update):
    return (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
    )


def exam_is_duplicate(exams, parsed):
    """یکسان بودن امتحان بر اساس تاریخ و نام درس؛ برای پیام تکی و چندتایی."""
    course = " ".join(str(parsed.get("course", "")).split())
    return any(
        e.get("date") == parsed.get("date")
        and " ".join(str(e.get("course", "")).split()) == course
        for e in exams
    )


def add_outbox_reply(outbox, update_id, reply_text):
    if not reply_text:
        return
    reply_id = f"telegram-update-{update_id}"
    if any(item.get("id") == reply_id for item in outbox):
        return
    outbox.append({
        "id": reply_id,
        "chat_id": CHAT_ID,
        "text": reply_text,
        "status": "pending",
        "attempts": 0,
    })


def main():
    tz = pytz.timezone(TIMEZONE)
    print(
        f"زمان اجرا (تهران): {datetime.now(tz):%Y-%m-%d %H:%M} | "
        f"trigger: {os.environ.get('GITHUB_EVENT_NAME', '?')}"
    )

    updates = get_dispatch_updates()
    if not updates:
        print("هیچ payload وب‌هوکی وجود ندارد؛ polling عمداً غیرفعال است.")
        return

    # ترتیب updateها حفظ می‌شود تا فرمان‌هایی مثل «لیست» و «حذف» قابل پیش‌بینی باشند.
    updates.sort(key=lambda item: item.get("update_id", 0))

    exams = load_json(EXAMS_FILE, [])
    notes = load_json(NOTES_FILE, [])
    state = load_json(STATE_FILE, {})
    outbox = load_json(OUTBOX_FILE, [])

    processed_ids = [str(x) for x in state.get("processed_update_ids", [])]
    processed_set = set(processed_ids)
    outbox_before = len(outbox)
    exams_changed = False
    notes_changed = False
    state_changed = False

    for update in updates:
        raw_update_id = update.get("update_id")
        update_id = str(raw_update_id) if raw_update_id is not None else None

        if update_id is not None and update_id in processed_set:
            print(f"Update تکراری نادیده گرفته شد: {update_id}")
            continue

        reply_text = None
        msg = message_from_update(update)

        if not msg or not isinstance(msg.get("text"), str):
            print(f"Update بدون پیام متنی نادیده گرفته شد: {update_id}")
        else:
            msg_chat_id = str((msg.get("chat") or {}).get("id", ""))
            if msg_chat_id != str(CHAT_ID):
                print(
                    "پیام از چت ناشناس نادیده گرفته شد — "
                    f"chat_id={msg_chat_id}"
                )
            else:
                text = msg["text"].strip()

                reply_text, note_changed, notes = handle_note_command(text, notes, tz)
                notes_changed = notes_changed or note_changed

                if reply_text is None:
                    reply_text, cmd_changed, exams = handle_command(text, exams, tz)
                    exams_changed = exams_changed or cmd_changed

                if reply_text is None and not text.startswith("/"):
                    entries = split_entries(text)

                    if len(entries) <= 1:
                        parsed = parse_exam_message(text, tz)
                        if parsed is None:
                            reply_text = HELP_TEXT
                        elif exam_is_duplicate(exams, parsed):
                            reply_text = (
                                "↩️ این امتحان از قبل ثبت شده بود و دوباره اضافه نشد: "
                                f"{format_added(parsed)}"
                            )
                        else:
                            exams.append(parsed)
                            exams_changed = True
                            reply_text = f"✅ ثبت شد: {format_added(parsed)}"
                    else:
                        added, duplicates, failed = [], [], []
                        for entry in entries:
                            parsed = parse_exam_message(entry, tz)
                            if parsed is None:
                                failed.append(entry)
                            elif exam_is_duplicate(exams, parsed):
                                duplicates.append(parsed)
                            else:
                                exams.append(parsed)
                                added.append(parsed)

                        if added:
                            exams_changed = True
                        lines = []
                        if added:
                            added.sort(key=lambda e: e.get("date", ""))
                            lines.append(f"✅ {len(added)} مورد ثبت شد:")
                            lines += [f"• {format_added(e)}" for e in added]
                        if duplicates:
                            lines.append(f"↩️ {len(duplicates)} مورد تکراری بود (ثبت نشد):")
                            lines += [f"• {format_added(e)}" for e in duplicates]
                        if failed:
                            lines.append(f"⚠️ {len(failed)} مورد قابل‌تشخیص نبود (ثبت نشد):")
                            lines += [f"• {item}" for item in failed]
                            lines.append(
                                "فرمت: درس - بودجه‌بندی (اختیاری) - "
                                "روز هفته (اختیاری) - روز - ماه"
                            )
                        reply_text = "\n".join(lines) or "موردی برای ثبت پیدا نشد."

                # فرمان‌های ناشناخته با / مثل قبل بی‌پاسخ می‌مانند.

        if reply_text and update_id is not None:
            add_outbox_reply(outbox, update_id, reply_text)
        elif reply_text and update_id is None:
            # Worker جدید همیشه update_id می‌فرستد؛ این مسیر فقط برای سازگاری است.
            fallback_id = f"legacy-{len(outbox) + 1}"
            add_outbox_reply(outbox, fallback_id, reply_text)

        if update_id is not None:
            processed_ids.append(update_id)
            processed_set.add(update_id)
            try:
                numeric_id = int(raw_update_id)
                old_last = state.get("last_update_id")
                if old_last is None or numeric_id > int(old_last):
                    state["last_update_id"] = numeric_id
            except (TypeError, ValueError):
                pass
            state_changed = True

    # فقط ۱۰۰۰ شناسهٔ اخیر نگه داشته می‌شوند؛ صف Cloudflare نیز dedupe مستقل دارد.
    state["processed_update_ids"] = processed_ids[-PROCESSED_IDS_LIMIT:]

    if exams_changed:
        exams.sort(key=lambda e: e.get("date", ""))
        save_json(EXAMS_FILE, exams)
    if notes_changed:
        notes.sort(key=lambda n: n.get("date", ""))
        save_json(NOTES_FILE, notes)
    if state_changed:
        save_json(STATE_FILE, state)
    if len(outbox) != outbox_before:
        save_json(OUTBOX_FILE, outbox)

    print(
        f"پردازش batch تمام شد: {len(updates)} update | "
        f"پاسخ جدید: {len(outbox) - outbox_before}"
    )


if __name__ == "__main__":
    main()
