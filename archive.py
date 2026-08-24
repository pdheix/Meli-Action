#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram News Archiver  (نسخه‌ی مقاوم در برابر timeout)
========================================================
- اجرای اول فقط تعداد کمی پیام از هر کانال را بک‌فیل می‌کند.
- متادیتای JSON فوراً ذخیره می‌شود (قبل از دانلود رسانه).
- state بعد از هر کانال ذخیره می‌شود.
- دانلود رسانه‌ها سقف تعداد و بودجه‌ی زمانی دارد.
- رسانه‌های دانلودنشده در یک صف قرار می‌گیرند و در اجراهای بعدی تکمیل می‌شوند.
- تاریخ شمسی و ساعت تهران.
"""

import os
import sys
import json
import asyncio
import mimetypes
import time as _time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import jdatetime
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
)
from telethon.utils import get_display_name
from telethon.errors import (
    FloodWaitError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    AuthKeyError,
)

# ─────────────────────────── مسیرها ───────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
CHANNELS_FILE   = BASE_DIR / "channels.txt"
STATE_FILE      = BASE_DIR / "state" / "last_ids.json"
QUEUE_FILE      = BASE_DIR / "state" / "media_queue.json"
DATA_DIR        = BASE_DIR / "data"
MEDIA_DIR       = BASE_DIR / "media"
STATE_DIR       = BASE_DIR / "state"

# ───────────────────────── تنظیمات ────────────────────────────
BATCH_SIZE           = int(os.environ.get("BATCH_SIZE", "20"))
MAX_MEDIA_MB         = int(os.environ.get("MAX_MEDIA_MB", "99"))
MAX_MEDIA_SIZE       = MAX_MEDIA_MB * 1024 * 1024
MSG_PER_CHANNEL      = int(os.environ.get("MESSAGES_PER_CHANNEL", "200"))
FIRST_RUN_MSGS       = int(os.environ.get("FIRST_RUN_MESSAGES", "20"))   # بک‌فیل اولیه
MAX_MEDIA_PER_RUN    = int(os.environ.get("MAX_MEDIA_DOWNLOADS_PER_RUN", "40"))
RUN_TIME_BUDGET      = int(os.environ.get("RUN_TIME_BUDGET_SEC", "480")) # ۸ دقیقه
TEHRAN_TZ            = timezone(timedelta(hours=3, minutes=30))

API_ID      = os.environ.get("TELEGRAM_API_ID", "")
API_HASH    = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STR = os.environ.get("TELEGRAM_SESSION", "")

NOTE_TOO_LARGE       = "⚠️ پیام حاوی محتوای رسانه‌ای است اما به دلیل حجم بالا قابل دانلود نبود."
NOTE_DOWNLOAD_FAILED = "⚠️ دانلود رسانه با خطا مواجه شد."
NOTE_NO_MEDIA        = "پیام فاقد محتوای رسانه‌ای است."
NOTE_WEBPAGE         = "پیام فقط شامل پیش‌نمایش لینک است (بدون فایل قابل دانلود)."
NOTE_UNSUPPORTED     = "نوع رسانه پشتیبانی نمی‌شود (نظرسنجی، موقعیت مکانی، مخاطب و...)."
NOTE_QUEUED          = "رسانه در صف دانلود قرار دارد و در اجراهای بعدی تکمیل می‌شود."


# ═══════════════════════════════════════════════════════════════
#  ابزارهای کمکی
# ═══════════════════════════════════════════════════════════════

def setup_dirs():
    for d in (DATA_DIR, MEDIA_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_channels():
    if not CHANNELS_FILE.exists():
        return []
    result = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.append(line.lstrip("@"))
    return result


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state():
    return load_json(STATE_FILE, {})


def save_state(state):
    save_json(STATE_FILE, state)


def load_queue():
    return load_json(QUEUE_FILE, [])


def save_queue(q):
    save_json(QUEUE_FILE, q)


def safe_name(s):
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in str(s))


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def to_shamsi(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_teh = dt.astimezone(TEHRAN_TZ)
    jd = jdatetime.datetime.fromgregorian(datetime=dt_teh)
    return jd.strftime("%Y/%m/%d"), jd.strftime("%H:%M:%S")


def classify_mime(mime):
    if not mime:
        return "document"
    mime = mime.lower()
    if mime.startswith("image/"):
        return "sticker" if mime == "image/webp" else "photo"
    if mime.startswith("video/"):
        return "gif" if mime == "image/gif" else "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


# ═══════════════════════════════════════════════════════════════
#  تحلیل رسانه
# ═══════════════════════════════════════════════════════════════

def analyze_media(message):
    res = {
        "has_media": False, "media_type": None, "size": 0,
        "ext": "", "mime": "", "reason": None,
    }

    if message.media is None:
        res["reason"] = NOTE_NO_MEDIA
        return res

    if isinstance(message.media, MessageMediaWebPage):
        res["media_type"] = "webpage"
        res["reason"] = NOTE_WEBPAGE
        return res

    if isinstance(message.media, MessageMediaPhoto):
        photo = message.media.photo
        res["has_media"] = True
        res["media_type"] = "photo"
        res["ext"] = ".jpg"
        if photo:
            sizes = [getattr(s, "size", 0) for s in photo.sizes
                     if isinstance(getattr(s, "size", None), int)]
            res["size"] = max(sizes, default=0)
        f = getattr(message, "file", None)
        if f is not None:
            res["size"] = getattr(f, "size", None) or res["size"]
            res["ext"] = getattr(f, "ext", None) or res["ext"]
            res["mime"] = getattr(f, "mime_type", None) or res["mime"]
        return res

    if isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if doc is None:
            res["media_type"] = "unsupported"
            res["reason"] = NOTE_UNSUPPORTED
            return res

        res["has_media"] = True
        res["size"] = doc.size or 0
        res["mime"] = doc.mime_type or ""

        has_video = has_audio = False
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                has_video = True
            elif isinstance(attr, DocumentAttributeAudio):
                has_audio = True
                if getattr(attr, "voice", False):
                    res["media_type"] = "voice"
            elif isinstance(attr, DocumentAttributeFilename):
                fn = attr.file_name
                if fn and "." in fn:
                    res["ext"] = "." + fn.rsplit(".", 1)[-1].lower()

        if has_video:
            res["media_type"] = "gif" if res["mime"] == "image/gif" else "video"
        elif res.get("media_type") == "voice":
            pass
        elif has_audio:
            res["media_type"] = "audio"
        elif res["mime"] in ("image/webp", "application/x-tgsticker"):
            res["media_type"] = "sticker"
        elif res["mime"] == "image/gif":
            res["media_type"] = "gif"
        else:
            res["media_type"] = classify_mime(res["mime"])

        if not res["ext"]:
            res["ext"] = (mimetypes.guess_extension(res["mime"]) or "") if res["mime"] else ""
        if not res["ext"]:
            res["ext"] = {
                "photo": ".jpg", "video": ".mp4", "audio": ".mp3",
                "voice": ".ogg", "gif": ".mp4", "sticker": ".webp",
            }.get(res["media_type"], "")

        f = getattr(message, "file", None)
        if f is not None:
            res["size"] = getattr(f, "size", None) or res["size"]
            res["ext"] = getattr(f, "ext", None) or res["ext"]
            res["mime"] = getattr(f, "mime_type", None) or res["mime"]
        return res

    res["media_type"] = "unsupported"
    res["reason"] = NOTE_UNSUPPORTED
    return res


# ═══════════════════════════════════════════════════════════════
#  ساخت آیتم JSON
# ═══════════════════════════════════════════════════════════════

def build_item(message, ch_username, ch_title, mi):
    shamsi_date, tehran_time = to_shamsi(message.date)
    text = message.text or ""
    no_media_reason = mi.get("reason")

    if mi["has_media"] and mi["size"] > MAX_MEDIA_SIZE:
        size_label = human_size(mi["size"])
        no_media_reason = (
            f"پیام حاوی محتوای رسانه‌ای ({mi['media_type']}) است اما "
            f"به دلیل حجم بالا ({size_label}) قابل دانلود نبود."
        )
        note = f"\n\n{NOTE_TOO_LARGE} (حجم: {size_label})"
        if note.strip() not in text:
            text += note

    msg_link = (f"https://t.me/{ch_username}/{message.id}" if ch_username
                else f"https://t.me/c/{message.chat_id}/{message.id}")

    greg = message.date
    if greg.tzinfo:
        greg = greg.astimezone(timezone.utc).isoformat()
    else:
        greg = greg.isoformat()

    return {
        "channel_name":     ch_title,
        "channel_username": ch_username,
        "message_id":       message.id,
        "message_link":     msg_link,
        "text":             text,
        "shamsi_date":      shamsi_date,
        "tehran_time":      tehran_time,
        "gregorian_utc":    greg,
        "media_type":       mi["media_type"],
        "media_size_bytes": mi["size"],
        "media_size_human": human_size(mi["size"]) if mi["size"] else None,
        "media_downloaded": False,
        "media_local_path": None,
        "media_link":       msg_link,
        "no_media_reason":  no_media_reason,
        "archived_at":      datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
#  دانلود یک رسانه (با آبجکت پیام)
# ═══════════════════════════════════════════════════════════════

async def download_message_media(client, message, ch_username, mi):
    """خروجی: (ok, local_path_or_None, error_or_None)"""
    ext = mi.get("ext") or ""
    fname = f"{safe_name(ch_username)}_{message.id}_{mi['media_type']}{ext}"
    fpath = MEDIA_DIR / fname
    try:
        dl = await message.download_media(file=str(fpath))
        if dl and Path(dl).exists() and Path(dl).stat().st_size > 0:
            return True, str(Path(dl).relative_to(BASE_DIR)), None
        return False, None, "فایل دانلودشده خالی یا موجود نیست"
    except Exception as e:
        return False, None, str(e)


def update_batch_item(batch_path, message_id, updates):
    """به‌روزرسانی یک آیتم در فایل JSON دسته."""
    data = json.loads(batch_path.read_text(encoding="utf-8"))
    changed = False
    for m in data.get("messages", []):
        if m.get("message_id") == message_id:
            m.update(updates)
            changed = True
            break
    if changed:
        batch_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return changed


# ═══════════════════════════════════════════════════════════════
#  پردازش یک کانال (فقط متادیتا، بدون دانلود)
# ═══════════════════════════════════════════════════════════════

async def fetch_channel(client, username, state):
    try:
        entity = await client.get_entity(username)
    except UsernameNotOccupiedError:
        print(f"  [رد کردن] @{username}: نام کاربری یافت نشد.")
        return [], None
    except ChannelPrivateError:
        print(f"  [رد کردن] @{username}: کانال خصوصی/غیرقابل دسترس.")
        return [], None
    except Exception as e:
        print(f"  [رد کردن] @{username}: خطا در شناسایی — {e}")
        return [], None

    ch_title = get_display_name(entity) or username
    real_user = getattr(entity, "username", None) or username
    state_key = real_user.lower()
    last_id = state.get(state_key, 0)

    limit = FIRST_RUN_MSGS if last_id == 0 else MSG_PER_CHANNEL

    try:
        msgs = await client.get_messages(entity, min_id=last_id, limit=limit)
    except FloodWaitError as e:
        print(f"  [محدودیت] @{real_user}: {e.seconds} ثانیه صبر لازم است.")
        return [], None
    except Exception as e:
        print(f"  [خطا] دریافت @{real_user}: {e}")
        return [], None

    msgs = sorted(msgs, key=lambda m: m.id)
    label = "اجرای اول" if last_id == 0 else "جدید"
    print(f"  @{real_user} ({ch_title}): {len(msgs)} پیام {label} "
          f"(بعد از id={last_id}, سقف={limit})")

    items = []
    highest = last_id
    for msg in msgs:
        highest = max(highest, msg.id)
        mi = analyze_media(msg)
        items.append(build_item(msg, real_user, ch_title, mi))

    if highest > last_id:
        state[state_key] = highest

    return items, real_user


# ═══════════════════════════════════════════════════════════════
#  نوشتن دسته‌های ۲۰تایی round-robin + افزودن به صف دانلود
# ═══════════════════════════════════════════════════════════════

def next_batch_number():
    mx = 0
    for p in DATA_DIR.glob("batch_*.json"):
        try:
            n = int(p.stem.split("_")[1])
            mx = max(mx, n)
        except (IndexError, ValueError):
            continue
    return mx + 1


def write_batches(all_items, channel_order, queue):
    grouped = {}
    for it in all_items:
        grouped.setdefault(it["channel_username"], []).append(it)

    queues = {}
    for ch in channel_order:
        if ch in grouped:
            queues[ch] = list(grouped[ch])
    for ch, lst in grouped.items():
        queues.setdefault(ch, list(lst))

    batches, current = [], []
    remaining = True
    while remaining:
        remaining = False
        for ch in list(queues.keys()):
            if queues[ch]:
                current.append(queues[ch].pop(0))
                remaining = True
                if len(current) == BATCH_SIZE:
                    batches.append(current)
                    current = []
    if current:
        batches.append(current)

    start = next_batch_number()
    existing_keys = {(q["channel_username"], q["message_id"]) for q in queue}

    for i, batch in enumerate(batches):
        num = start + i
        rel_path = f"data/batch_{num:06d}.json"
        full_path = BASE_DIR / rel_path
        payload = {
            "batch_number": num,
            "batch_size":   len(batch),
            "created_at":   datetime.now(timezone.utc).isoformat(),
            "channels":     sorted({it["channel_username"] for it in batch}),
            "messages":     batch,
        }
        full_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  📄 {rel_path} — {len(batch)} پیام")

        # افزودن رسانه‌های قابل‌دانلود به صف
        for it in batch:
            if (it["media_type"] in ("photo", "video", "audio", "voice",
                                      "gif", "sticker", "document")
                    and it["media_size_bytes"]
                    and 0 < it["media_size_bytes"] <= MAX_MEDIA_SIZE
                    and not it["media_downloaded"]):
                key = (it["channel_username"], it["message_id"])
                if key not in existing_keys:
                    queue.append({
                        "channel_username": it["channel_username"],
                        "message_id":       it["message_id"],
                        "media_type":       it["media_type"],
                        "ext":              "",  # هنگام دانلود دوباره تشخیص داده می‌شود
                        "batch_path":       rel_path,
                        "added_at":         datetime.now(timezone.utc).isoformat(),
                        "attempts":         0,
                    })
                    existing_keys.add(key)
                    # علامت‌گذاری در آیتم
                    it["no_media_reason"] = NOTE_QUEUED

        # بازنویسی فایل با NOTE_QUEUED
        full_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return len(batches)


# ═══════════════════════════════════════════════════════════════
#  پردازش صف دانلود رسانه
# ═══════════════════════════════════════════════════════════════

async def process_media_queue(client, queue):
    if not queue:
        return 0, 0

    start = _time.monotonic()
    done = 0
    failed = 0
    remaining = []

    print(f"\n── پردازش صف دانلود رسانه: {len(queue)} مورد "
          f"(سقف {MAX_MEDIA_PER_RUN} فایل، بودجه {RUN_TIME_BUDGET}s) ──")

    for qitem in queue:
        if done >= MAX_MEDIA_PER_RUN:
            print("  ⏹ به سقف دانلود این اجرا رسیدیم؛ بقیه در صف می‌مانند.")
            remaining.append(qitem)
            continue
        if _time.monotonic() - start > RUN_TIME_BUDGET:
            print("  ⏹ بودجه‌ی زمانی تمام شد؛ بقیه در صف می‌مانند.")
            remaining.append(qitem)
            continue

        qitem["attempts"] = qitem.get("attempts", 0) + 1
        ch = qitem["channel_username"]
        mid = qitem["message_id"]
        batch_rel = qitem["batch_path"]
        batch_path = BASE_DIR / batch_rel

        try:
            entity = await client.get_entity(ch)
            msgs = await client.get_messages(entity, ids=mid)
            if not msgs:
                print(f"  [رد] پیام @{ch}/{mid} یافت نشد.")
                failed += 1
                continue
            msg = msgs[0] if isinstance(msgs, list) else msgs
            mi = analyze_media(msg)
            ok, local_path, err = await download_message_media(
                client, msg, ch, mi
            )
            if ok:
                update_batch_item(batch_path, mid, {
                    "media_downloaded": True,
                    "media_local_path": local_path,
                    "no_media_reason":  None,
                })
                print(f"    ⬇ @{ch}/{mid} → {local_path} "
                      f"({human_size(mi['size'])})")
                done += 1
            else:
                print(f"    [هشدار] دانلود @{ch}/{mid} ناموفق: {err}")
                if qitem["attempts"] >= 5:
                    update_batch_item(batch_path, mid, {
                        "no_media_reason": f"{NOTE_DOWNLOAD_FAILED} ({err})"
                    })
                    failed += 1
                else:
                    remaining.append(qitem)
        except FloodWaitError as e:
            print(f"  [محدودیت] باید {e.seconds} ثانیه صبر شود؛ توقف صف.")
            remaining.append(qitem)
            # بقیه را هم برای اجرای بعدی بگذار
            break
        except Exception as e:
            print(f"    [خطا] @{ch}/{mid}: {e}")
            if qitem["attempts"] >= 5:
                failed += 1
            else:
                remaining.append(qitem)

    queue[:] = remaining
    return done, failed


# ═══════════════════════════════════════════════════════════════
#  اصلی
# ═══════════════════════════════════════════════════════════════

async def main():
    setup_dirs()
    run_start = _time.monotonic()

    channels = load_channels()
    if not channels:
        print("❌ هیچ کانالی در channels.txt تعریف نشده است.")
        return

    if not API_ID or not API_HASH:
        print("❌ متغیرهای TELEGRAM_API_ID و TELEGRAM_API_HASH تنظیم نشده‌اند.")
        sys.exit(1)

    state = load_state()
    queue = load_queue()

    print("🔌 اتصال به تلگرام...")
    client = TelegramClient(
        StringSession(SESSION_STR) if SESSION_STR else StringSession(),
        int(API_ID), API_HASH,
    )

    all_items = []
    seen_channels = []

    async with client:
        if not await client.is_user_authorized():
            print("❌ سشن تلگرام معتبر نیست. ابتدا generate_session.py را اجرا کنید.")
            sys.exit(1)

        me = await client.get_me()
        print(f"✅ ورود موفق: {me.first_name} (id={me.id})")

        # ── فاز ۱: دریافت متادیتای پیام‌ها ──
        for username in channels:
            print(f"\n── دریافت @{username} ──")
            try:
                items, real_user = await fetch_channel(client, username, state)
            except Exception as e:
                print(f"  [خطای پیش‌بینی‌نشده] @{username}: {e}")
                traceback.print_exc()
                items, real_user = [], None

            if items:
                all_items.extend(items)
                if real_user and real_user not in seen_channels:
                    seen_channels.append(real_user)

            # ذخیره‌ی فوری state بعد از هر کانال (جلوگیری از دست رفت پیشرفت)
            save_state(state)

        # ── فاز ۲: نوشتن JSON و پر کردن صف دانلود ──
        if all_items:
            print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print(f"مجموع پیام‌های جدید: {len(all_items)} — ذخیره در دسته‌های {BATCH_SIZE}تایی...")
            n_batches = write_batches(all_items, seen_channels, queue)
            print(f"✅ {n_batches} فایل JSON ذخیره شد.")
        else:
            print("\nℹ️ پیام جدیدی یافت نشد.")

        # ذخیره‌ی state و صف قبل از شروع دانلودهای سنگین
        save_state(state)
        save_queue(queue)

        # ── فاز ۳: دانلود رسانه‌ها (best-effort با سقف/بودجه) ──
        try:
            done, failed = await process_media_queue(client, queue)
            print(f"\n✅ دانلود این اجرا: {done} موفق، {failed} ناموفق، "
                  f"{len(queue)} مورد در صف برای اجراهای بعدی.")
        except Exception as e:
            print(f"  [خطا در فاز دانلود] {e}")
            traceback.print_exc()

        save_queue(queue)

    elapsed = _time.monotonic() - run_start
    print(f"\n🏁 پایان اجرا در {elapsed:.1f} ثانیه.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except AuthKeyError:
        print("❌ رشته‌ی سشن نامعتبر است. لطفاً دوباره آن را تولید کنید.")
        sys.exit(1)
