/**
 * Cloudflare Worker + Durable Object: Telegram webhook -> queued GitHub batches.
 *
 * Incoming Telegram updates are persisted in a Durable Object. Updates arriving
 * within a short window are combined into one repository_dispatch. Only one
 * batch is in flight at a time; GitHub acknowledges it after processing.
 */

const BATCH_SIZE = 20;
// GitHub repository_dispatch client_payload must stay below 64 KB.
const MAX_GITHUB_PAYLOAD_BYTES = 60_000;
const DEBOUNCE_MS = 3_000;
const ACK_TIMEOUT_MS = 20 * 60 * 1_000;
const MAX_QUEUE_UPDATES = 500;
const MAX_RECENT_UPDATE_IDS = 2_000;

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function getTextMessage(update) {
  const message =
    update?.message ||
    update?.edited_message ||
    update?.channel_post ||
    update?.edited_channel_post;
  return message && typeof message.text === "string" ? message : null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname !== "/ack") {
      return new Response("Telegram reminder bridge is running", { status: 200 });
    }

    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    if (!env.TELEGRAM_BATCHER) {
      console.error("Missing Durable Object binding: TELEGRAM_BATCHER");
      return new Response("Server configuration error", { status: 500 });
    }

    const id = env.TELEGRAM_BATCHER.idFromName("telegram-inbox");
    const stub = env.TELEGRAM_BATCHER.get(id);

    // GitHub calls this endpoint only after a batch was processed and committed.
    if (url.pathname === "/ack") {
      if (!env.WORKER_ACK_SECRET) {
        return new Response("Server configuration error", { status: 500 });
      }
      const ackSecret = request.headers.get("X-Worker-Ack-Secret");
      if (!ackSecret || ackSecret !== env.WORKER_ACK_SECRET) {
        return new Response("Forbidden", { status: 403 });
      }

      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("Bad Request", { status: 400 });
      }
      if (typeof body?.batch_id !== "string" || !body.batch_id) {
        return new Response("Missing batch_id", { status: 400 });
      }

      return stub.fetch("https://batcher/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: body.batch_id }),
      });
    }

    // Telegram webhook endpoint (root path or /webhook).
    if (url.pathname !== "/" && url.pathname !== "/webhook") {
      return new Response("Not Found", { status: 404 });
    }

    if (!env.WEBHOOK_SECRET) {
      console.error("WEBHOOK_SECRET is not configured");
      return new Response("Server configuration error", { status: 500 });
    }

    const telegramSecret = request.headers.get(
      "X-Telegram-Bot-Api-Secret-Token"
    );
    if (!telegramSecret || telegramSecret !== env.WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    if (!Number.isSafeInteger(update?.update_id)) {
      return new Response("Invalid Telegram update", { status: 400 });
    }

    // The Python bot only handles text commands/messages. Ignore other updates
    // here so they do not consume GitHub Actions runs.
    if (!getTextMessage(update)) {
      return new Response("Ignored", { status: 200 });
    }

    return stub.fetch("https://batcher/enqueue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update),
    });
  },
};

export class TelegramBatcher {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const url = new URL(request.url);
    if (url.pathname === "/enqueue") {
      let update;
      try {
        update = await request.json();
      } catch {
        return new Response("Bad Request", { status: 400 });
      }
      return this.enqueue(update);
    }

    if (url.pathname === "/ack") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("Bad Request", { status: 400 });
      }
      return this.acknowledge(body?.batch_id);
    }

    return new Response("Not Found", { status: 404 });
  }

  async enqueue(update) {
    const updateId = String(update?.update_id ?? "");
    if (!updateId) {
      return jsonResponse({ ok: false, error: "missing_update_id" }, 400);
    }

    const result = await this.ctx.blockConcurrencyWhile(async () => {
      const storage = this.ctx.storage;
      const recent = (await storage.get("recent_update_ids")) || [];
      const queue = (await storage.get("queue")) || [];
      const inflight = await storage.get("inflight");

      const duplicate =
        recent.includes(updateId) ||
        queue.some((item) => String(item.update_id) === updateId) ||
        (inflight && inflight.updates.some(
          (item) => String(item.update_id) === updateId
        ));

      if (duplicate) return { status: "duplicate" };
      if (queue.length >= MAX_QUEUE_UPDATES) return { status: "full" };

      queue.push(update);
      await storage.put("queue", queue);

      if (!inflight) {
        const alarm = await storage.getAlarm();
        if (alarm === null) {
          await storage.setAlarm(Date.now() + DEBOUNCE_MS);
        }
      }

      return { status: "queued", queue_length: queue.length };
    });

    if (result.status === "full") {
      // 503 tells Telegram to retry later; no update was discarded.
      return jsonResponse({ ok: false, error: "queue_full" }, 503);
    }

    return jsonResponse({ ok: true, ...result }, 200);
  }

  async acknowledge(batchId) {
    if (typeof batchId !== "string" || !batchId) {
      return jsonResponse({ ok: false, error: "missing_batch_id" }, 400);
    }

    const result = await this.ctx.blockConcurrencyWhile(async () => {
      const storage = this.ctx.storage;
      const inflight = await storage.get("inflight");

      // A repeated/late ACK must never clear a newer batch.
      if (!inflight || inflight.batch_id !== batchId) {
        return { status: "stale_ack" };
      }

      const recent = (await storage.get("recent_update_ids")) || [];
      const ids = inflight.updates.map((item) => String(item.update_id));
      const merged = [...recent, ...ids];
      await storage.put(
        "recent_update_ids",
        merged.slice(-MAX_RECENT_UPDATE_IDS)
      );
      await storage.delete("inflight");

      const queue = (await storage.get("queue")) || [];
      if (queue.length) {
        await storage.setAlarm(Date.now() + DEBOUNCE_MS);
      } else {
        await storage.deleteAlarm();
      }

      return { status: "acknowledged", remaining: queue.length };
    });

    return jsonResponse({ ok: true, ...result }, 200);
  }

  async alarm() {
    const plan = await this.ctx.blockConcurrencyWhile(async () => {
      const storage = this.ctx.storage;
      const now = Date.now();
      let inflight = await storage.get("inflight");

      if (inflight) {
        if (inflight.next_attempt_at && now < inflight.next_attempt_at) {
          await storage.setAlarm(inflight.next_attempt_at);
          return null;
        }
        inflight.attempts = (inflight.attempts || 0) + 1;
        inflight.last_attempt_at = now;
        inflight.next_attempt_at = now + ACK_TIMEOUT_MS;
        await storage.put("inflight", inflight);
        return { batch_id: inflight.batch_id, batch: inflight };
      }

      const queue = (await storage.get("queue")) || [];
      if (!queue.length) {
        await storage.deleteAlarm();
        return null;
      }

      const batchId = crypto.randomUUID();
      const updates = [];
      const encoder = new TextEncoder();

      while (queue.length && updates.length < BATCH_SIZE) {
        const candidate = [...updates, queue[0]];
        const candidatePayload = JSON.stringify({
          event_type: "telegram_updates_batch",
          client_payload: { batch_id: batchId, updates: candidate },
        });
        const payloadBytes = encoder.encode(candidatePayload).length;

        // اگر یک update به‌تنهایی بیش از حد مجاز باشد، آن را حذف نمی‌کنیم؛
        // خطا را ثبت می‌کنیم تا قابل مشاهده و قابل رفع باشد.
        if (payloadBytes > MAX_GITHUB_PAYLOAD_BYTES) {
          if (updates.length === 0) {
            console.error("A Telegram update exceeds the GitHub dispatch payload limit");
            await storage.setAlarm(now + 60_000);
            return null;
          }
          break;
        }

        updates.push(queue.shift());
      }

      inflight = {
        batch_id: batchId,
        updates,
        attempts: 1,
        created_at: now,
        last_attempt_at: now,
        next_attempt_at: now + ACK_TIMEOUT_MS,
      };
      await storage.put({ queue, inflight });
      return { batch_id: inflight.batch_id, batch: inflight };
    });

    if (!plan) return;

    const dispatchResult = await this.dispatchToGitHub(plan.batch);

    await this.ctx.blockConcurrencyWhile(async () => {
      const storage = this.ctx.storage;
      const current = await storage.get("inflight");
      // The workflow may have ACKed while the API request was returning.
      if (!current || current.batch_id !== plan.batch_id) return;

      if (dispatchResult.ok) {
        current.last_error = null;
        current.next_attempt_at = Date.now() + ACK_TIMEOUT_MS;
      } else {
        current.last_error = dispatchResult.error;
        const retrySeconds = Math.min(
          300,
          10 * (2 ** Math.min((current.attempts || 1) - 1, 5))
        );
        current.next_attempt_at = Date.now() + retrySeconds * 1_000;
      }

      await storage.put("inflight", current);
      await storage.setAlarm(current.next_attempt_at);
    });
  }

  async dispatchToGitHub(batch) {
    const required = ["GH_TOKEN", "GH_OWNER", "GH_REPO"];
    const missing = required.filter((key) => !this.env[key]);
    if (missing.length) {
      const error = `Missing Worker variables: ${missing.join(", ")}`;
      console.error(error);
      return { ok: false, error };
    }

    const dispatchUrl =
      `https://api.github.com/repos/${encodeURIComponent(this.env.GH_OWNER)}` +
      `/${encodeURIComponent(this.env.GH_REPO)}/dispatches`;

    try {
      const response = await fetch(dispatchUrl, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "class-reminder-bot-cloudflare-worker",
          "Content-Type": "application/json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          event_type: "telegram_updates_batch",
          client_payload: {
            batch_id: batch.batch_id,
            updates: batch.updates,
          },
        }),
      });

      if (!response.ok) {
        const body = await response.text();
        const error = `GitHub dispatch ${response.status}: ${body.slice(0, 500)}`;
        console.error(error);
        return { ok: false, error };
      }

      console.log(
        `GitHub batch dispatched: ${batch.batch_id} (${batch.updates.length} updates)`
      );
      return { ok: true };
    } catch (error) {
      const message = String(error?.message || error);
      console.error("GitHub dispatch network error:", message);
      return { ok: false, error: message.slice(0, 500) };
    }
  }
}
