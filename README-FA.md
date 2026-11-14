# راه‌اندازی ربات یادآوری دانشگاه

این نسخه برای دریافت پیام‌ها از Telegram Webhook، صف‌کردن آن‌ها در Cloudflare Durable Objects و پردازش دسته‌ای با GitHub Actions آماده شده است.

## زمان‌بندی

- یادآوری برنامه فردا: هر روز ساعت **۱۲:۰۰ به وقت تهران**.
- گزارش امتحان‌ها: هر چهارشنبه ساعت **۱۸:۰۰ به وقت تهران**.

زمان‌بندی در خود GitHub Actions با منطقه زمانی `Asia/Tehran` تعریف شده است. اجرای زمان‌بندی‌شده GitHub ممکن است در زمان شلوغی کمی دیرتر شروع شود.

## معماری دریافت پیام

1. تلگرام Update را به Worker می‌فرستد.
2. Worker با استفاده از Durable Object پیام را در صف پایدار ذخیره می‌کند و Update تکراری را رد می‌کند.
3. حداکثر ۲۰ Update در هر batch به GitHub Actions فرستاده می‌شود.
4. GitHub آن‌ها را به ترتیب پردازش می‌کند، داده‌ها را commit می‌کند، پاسخ‌های outbox را می‌فرستد و در پایان به Worker ACK می‌دهد.
5. اگر پردازش یا ارسال پاسخ شکست بخورد، batch تأیید نمی‌شود و Worker بعداً دوباره آن را می‌فرستد.

این روش تعداد اجراهای GitHub را با batch کردن پیام‌ها کاهش می‌دهد؛ سهمیه یا محدودیت GitHub Actions را حذف نمی‌کند.

## ۱) به‌روزرسانی GitHub Repository

1. از مخزن GitHub خود یک backup یا ZIP بگیر.
2. فایل ZIP این پروژه را استخراج کن.
3. فایل‌های کد و Workflow را در ریشه مخزن خود جایگزین کن.
4. تغییرات را روی **default branch** (معمولاً `main`) commit و push کن.

فایل‌هایی که باید به‌روزرسانی شوند:

- `.github/workflows/process-inbox.yml`
- `.github/workflows/reminder.yml`
- `.github/workflows/weekly-exams.yml`
- `process_inbox.py`
- `send_weekly_exams.py`
- `send_outbox.py`
- `worker.js`
- `wrangler.toml`
- `package.json`
- `outbox.json` (اگر این فایل را نداری، با محتوای `[]` بساز)

**مهم:** فایل‌های داده‌ی `schedule.json`، `exams.json`، `notes.json` و `bot_state.json` ممکن است در مخزن فعلی‌ات جدیدتر از نسخه داخل ZIP باشند. اگر بعد از تهیه این ZIP تغییری در برنامه کلاس‌ها، امتحان‌ها، یادداشت‌ها یا وضعیت ربات داده‌ای، نسخه فعلی همان فایل‌ها را حفظ کن و با نسخه قدیمی ZIP جایگزین نکن.

در GitHub از مسیر **Settings → Secrets and variables → Actions** این موارد را تنظیم کن:

### Repository secrets

- `BOT_TOKEN`: توکن ربات تلگرام.
- `CHAT_ID`: شناسه چت شخصی که ربات باید از آن فرمان بگیرد و پاسخ دهد.
- `WORKER_ACK_SECRET`: یک رشته تصادفی محرمانه؛ همین مقدار باید در Cloudflare هم ثبت شود.
- `BROADCAST_CHAT_IDS`: اختیاری؛ شناسه چت‌های اضافی برای دریافت یادآوری‌ها، با کاما جدا شوند.

### Repository variable

- `WORKER_URL`: آدرس اصلی Worker، بدون `/webhook` یا `/ack`؛ نمونه: `https://worker-name.account-name.workers.dev`.

## ۲) استقرار Cloudflare Worker جدید

این نسخه برای صف پایدار از Durable Object استفاده می‌کند؛ بنابراین صرفاً Paste کردن کد در Quick Edit کافی نیست. با Wrangler مستقرش کن.

1. Node.js نسخه LTS را نصب کن.
2. پوشه پروژه را روی کامپیوتر باز کن و ترمینال را در همان پوشه اجرا کن.
3. در `wrangler.toml` این موارد را اصلاح کن:
   - `name`: نام دقیق Worker فعلی‌ات در Cloudflare؛ اگر متفاوت باشد، نام جدید بساز و بعد URL وب‌هوک تلگرام را هم عوض کن.
   - `GH_OWNER`: نام کاربری یا سازمان GitHub.
   - `GH_REPO`: نام مخزن GitHub.
4. اجرا کن:

```bash
npm install
npx wrangler login
```

5. سه Secret را جداگانه ثبت کن (هر فرمان مقدار را تعاملی از تو می‌گیرد):

```bash
npx wrangler secret put GH_TOKEN
npx wrangler secret put WEBHOOK_SECRET
npx wrangler secret put WORKER_ACK_SECRET
```

- `GH_TOKEN`: یک Fine-grained Personal Access Token برای همان مخزن با دسترسی **Contents: Read and write**.
- `WEBHOOK_SECRET`: رشته تصادفی با حروف انگلیسی، عدد، `_` یا `-`؛ همین مقدار را در مرحله بعد به Telegram می‌دهی.
- `WORKER_ACK_SECRET`: دقیقاً همان مقداری که در GitHub به‌عنوان Repository secret ثبت کردی.

6. Worker را deploy کن:

```bash
npm run deploy
```

Wrangler آدرس Worker را در خروجی نشان می‌دهد. آن را در GitHub به‌عنوان `WORKER_URL` ثبت کن.

> اگر Cloudflare هنگام deploy درباره Durable Object یا migration خطا داد، چیزی را از داشبورد حذف نکن؛ متن کامل خطا را نگه دار. وضعیت namespaceهای قبلی ممکن است نیاز به migration مخصوص همان حساب داشته باشد.

## ۳) اتصال Webhook تلگرام

Webhook باید به آدرس زیر اشاره کند:

`WORKER_URL/webhook`

مقدار `secret_token` تلگرام باید دقیقاً با `WEBHOOK_SECRET` در Cloudflare یکسان باشد. پس از deploy، `setWebhook` را دوباره اجرا کن تا تلگرام به Worker جدید وصل شود. نمونه دستور (مقادیر واقعی را فقط در ترمینال خودت قرار بده و آن‌ها را در GitHub یا چت منتشر نکن):

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  --data-urlencode "url=<WORKER_URL>/webhook" \
  --data-urlencode "secret_token=<WEBHOOK_SECRET>"
```

برای بررسی اتصال:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

در خروجی باید URL جدید Worker را ببینی و `last_error_message` نباید خطای فعال داشته باشد.

## ۴) آزمایش

1. در GitHub → Actions بررسی کن Workflowها فعال باشند.
2. به ربات تلگرام یک پیام متنی معتبر بفرست؛ مثلاً `لیست`.
3. باید Workflow با نام **Process Telegram Inbox** اجرا شود.
4. پس از موفقیت، در مخزن باید اجرای پردازش ثبت شود و پاسخ ربات به تلگرام برگردد.
5. برای آزمایش زمان‌بندی، از دکمه **Run workflow** در Workflow روزانه یا هفتگی استفاده نکن مگر اینکه عمداً می‌خواهی همان لحظه پیام واقعی ارسال شود.

## نکات مهم

- Workflow دریافت پیام دیگر `getUpdates` یا polling اجرا نمی‌کند؛ فقط از Webhook و `repository_dispatch` استفاده می‌کند تا با Webhook خطای 409 تداخل نداشته باشد.
- یک batch حداکثر ۲۰ Update دارد و تا ACK موفق batch قبلی، batch بعدی dispatch نمی‌شود.
- اگر ارسال پاسخ تلگرام ناموفق باشد، batch تا حداکثر ۵ تلاش ACK نمی‌شود و Worker آن را بعداً retry می‌کند؛ پس از ۵ تلاش ناموفق، پاسخ در `outbox.json` با وضعیت `failed` نگه داشته می‌شود تا اجرای GitHub بی‌نهایت تکرار نشود.
- در یک حالت نادر، اگر Telegram پیام را دریافت کرده باشد اما GitHub قبل از ثبت وضعیت `sent` متوقف شود، ممکن است پاسخ تکراری ارسال شود؛ Bot API برای `sendMessage` کلید idempotency عمومی ارائه نمی‌کند.
- GitHub Actions ممکن است اجرای زمان‌بندی‌شده را به‌خاطر بار سرورها با تأخیر آغاز کند؛ زمان Cron تضمین شروع دقیق در همان دقیقه نیست.
- `BOT_TOKEN`، `GH_TOKEN` و Secretها را داخل فایل‌ها یا commit قرار نده.
