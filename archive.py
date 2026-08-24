#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram News Archiver
======================
پیام‌های جدید را از کانال‌های تعریف‌شده در channels.txt می‌گیرد،
رسانه (عکس/ویدیو/سند) را در صورت ≤۱۰۰ مگابایت دانلود می‌کند،
و پیام‌ها را به‌صورت دسته‌های ۲۰تایی (به‌طور مساوی از هر کانال)
در فایل‌های JSON مجزا ذخیره می‌کند.

تاریخ‌ها به شمسی و ساعت به وقت تهران ثبت می‌شوند.
"""

import os
import sys
import json
import asyncio
import mimetypes
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
BASE_DIR    = Path(__file__).resolve().parent
CHANNELS    = BASE_DIR / "channels.txt"
STATE_FILE  = BASE_DIR / "state" / "last_ids.json"
DATA_DIR    = BASE_DIR / "data"
MEDIA_DIR   = BASE_DIR / "media"
STATE_DIR   = BASE_DIR / "state"

# ───────────────────────── تنظیمات ────────────────────────────
BATCH_SIZE           = int(os.environ.get("BATCH_SIZE", "20"))
MAX_MEDIA_MB         = int(os.environ.get("MAX_MEDIA_MB", "99"))
MAX_MEDIA_SIZE       = MAX_MEDIA_MB * 1024 * 1024           # بایت
MSG_PER_CHANNEL      = int(os.environ.get("MESSAGES_PER_CHANNEL", "200"))
TEHRAN_TZ            = timezone(timedelta(hours=3, minutes=30))

API_ID      = os.environ.get("TELEGRAM_API_ID", "")
API_HASH    = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STR = os.environ.get("TELEGRAM_SESSION", "")

# متنی که در صورت بزرگ بودن حجم رسانه به انتهای پیام اضافه می‌شود
NOTE_TOO_LARGE = (
    "⚠️ پیام حاوی محتوای رسانه‌ای است اما به دلیل حجم بالا "
    "قابل دانلود نبود."
)
NOTE_DOWNLOAD_FAILED = "⚠️ دانلود رسانه با خطا مواجه شد."
NOTE_NO_MEDIA        = "پیام فاقد محتوای رسانه‌ای است."
NOTE_WEBPAGE         = "پیام فقط شامل پیش‌نمایش لینک است (بدون فایل قابل دانلود)."
NOTE_UNSUPPORTED     = "نوع رسانه پشتیبانی نمی‌شود (نظرسنجی، موقعیت مکانی، مخاطب و...)."


# ═══════════════════════════════════════════════════════════════
#  ابزارهای کمکی
# ═══════════════════════════════════════════════════════════════

def setup_dirs():
    for d in (DATA_DIR, MEDIA_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_channels():
    """خواندن فهرست کانال‌ها از channels.txt"""
    if not CHANNELS.exists():
        return []
    result = []
    for line in CHANNELS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.append(line.lstrip("@"))
    return result


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def safe_name(s):
    """تبدیل رشته به نام فایل امن"""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in str(s))


def human_size(n):
    """تبدیل بایت به فرمت خوانا"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def to_shamsi(dt):
    """تبدیل datetime به (تاریخ شمسی, ساعت تهران)"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_teh = dt.astimezone(TEHRAN_TZ)
    jd = jdatetime.datetime.fromgregorian(datetime=dt_teh)
    return jd.strftime("%Y/%m/%d"), jd.strftime("%H:%M:%S")


def analyze_media(message):
    """
    بررسی نوع و حجم رسانه‌ی پیام.
    خروجی: dict با کلیدهای has_media, media_type, size, ext, mime, reason
    """
    res = {
        "has_media": False,
        "media_type": None,
        "size": 0,
        "ext": "",
        "mime": "",
        "reason": None,
    }

    if message.media is None:
        res["reason"] = NOTE_NO_MEDIA
        return res

    # ── پیش‌نمایش لینک (رسانه‌ی قابل دانلود نیست) ──
    if isinstance(message.media, MessageMediaWebPage):
        res["media_type"] = "webpage"
        res["reason"] = NOTE_WEBPAGE
        return res

    # ── روش راحت: message.file در Telethon ──
    f = getattr(message, "file", None)
    if f is not None:
        res["has_media"] = True
        res["size"] = f.size or 0
        res["ext"]  = f.ext or ""
        res["mime"] = getattr(f, "mime_type", "") or ""

        if isinstance(message.media, MessageMediaPhoto):
            res["media_type"] = "photo"
        elif f.is_video:
            res["media_type"] = "video"
        elif f.is_audio:
            res["media_type"] = "audio"
        elif f.is_gif:
            res["media_type"] = "gif"
        elif f.is_sticker:
            res["media_type"] = "sticker"
        else:
            res["media_type"] = "document"
        return res

    # ── روش پشتیبان: بررسی مستقیم آبجکت رسانه ──
    if isinstance(message.media, MessageMediaPhoto):
        photo = message.media.photo
        if photo:
            res["has_media"] = True
            res["media_type"] = "photo"
            res["ext"] = ".jpg"
            sizes = [s.size for s in photo.sizes
                     if hasattr(s, "size") and isinstance(s.size, int)]
            res["size"] = max(sizes, default=0)
            return res

    if isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if doc:
            res["has_media"] = True
            res["size"] = doc.size or 0
            res["mime"] = doc.mime_type or ""
            res["media_type"] = "document"
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    res["media_type"] = "video"
                elif isinstance(attr, DocumentAttributeAudio):
                    res["media_type"] = "audio"
                elif isinstance(attr, DocumentAttributeFilename):
                    fn = attr.file_name
                    if fn and "." in fn:
                        res["ext"] = "." + fn.rsplit(".", 1)[-1].lower()
            if not res["ext"] and res["mime"]:
                res["ext"] = mimetypes.guess_extension(res["mime"]) or ""
            return res

    # ── سایر انواع پشتیبانی‌نشده ──
    res["media_type"] = "unsupported"
    res["reason"] = NOTE_UNSUPPORTED
    return res


def next_batch_number():
    """پیدا کردن شماره‌ی بعدی فایل دسته"""
    mx = 0
    for p in DATA_DIR.glob("batch_*.json"):
        try:
            n = int(p.stem.split("_")[1])
            if n > mx:
                mx = n
        except (IndexError, ValueError):
            continue
    return mx + 1


# ═══════════════════════════════════════════════════════════════
#  ساخت آیتم JSON برای هر پیام
# ═══════════════════════════════════════════════════════════════

def build_item(message, ch_username, ch_title, mi):
    msg_date = message.date
    shamsi_date, tehran_time = to_shamsi(msg_date)
    now_utc = datetime.now(timezone.utc).isoformat()

    text = message.text or ""
    no_media_reason = mi.get("reason")

    # اگر رسانه وجود دارد ولی حجمش زیاد است → یادداشت پای پیام
    if mi["has_media"] and mi["size"] > MAX_MEDIA_SIZE:
        size_label = human_size(mi["size"])
        no_media_reason = (
            f"پیام حاوی محتوای رسانه‌ای ({mi['media_type']}) است اما "
            f"به دلیل حجم بالا ({size_label}) قابل دانلود نبود."
        )
        note = f"\n\n{NOTE_TOO_LARGE} (حجم: {size_label})"
        if note.strip() not in text:
            text = text + note

    # لینک پیام
    if ch_username:
        msg_link = f"https://t.me/{ch_username}/{message.id}"
    else:
        cid = message.chat_id
        msg_link = f"https://t.me/c/{cid}/{message.id}"

    greg = msg_date.astimezone(timezone.utc).isoformat() if msg_date.tzinfo \
        else msg_date.isoformat()

    return {
        "channel_name":       ch_title,
        "channel_username":   ch_username,
        "message_id":         message.id,
        "message_link":       msg_link,
        "text":               text,
        "shamsi_date":        shamsi_date,
        "tehran_time":        tehran_time,
        "gregorian_utc":      greg,
        "media_type":         mi["media_type"],
        "media_size_bytes":   mi["size"],
        "media_size_human":   human_size(mi["size"]) if mi["size"] else None,
        "media_downloaded":   False,
        "media_local_path":   None,
        "media_link":         msg_link,
        "no_media_reason":    no_media_reason,
        "archived_at":        now_utc,
    }


# ═══════════════════════════════════════════════════════════════
#  پردازش یک کانال
# ═══════════════════════════════════════════════════════════════

async def process_channel(client, username, state):
    items = []

    # دریافت entity کانال
    try:
        entity = await client.get_entity(username)
    except UsernameNotOccupiedError:
        print(f"  [رد کردن] @{username}: نام کاربری یافت نشد.")
        return items
    except ChannelPrivateError:
        print(f"  [رد کردن] @{username}: کانال خصوصی یا غیرقابل دسترس است.")
        return items
    except Exception as e:
        print(f"  [رد کردن] @{username}: خطا در شناسایی — {e}")
        return items

    ch_title = get_display_name(entity) or username
    real_user = getattr(entity, "username", None) or username
    state_key = real_user.lower()
    last_id = state.get(state_key, 0)

    # دریافت پیام‌های جدید
    try:
        msgs = await client.get_messages(
            entity, min_id=last_id, limit=MSG_PER_CHANNEL
        )
    except FloodWaitError as e:
        print(f"  [محدودیت] @{real_user}: باید {e.seconds} ثانیه صبر کنید.")
        return items
    except Exception as e:
        print(f"  [خطا] دریافت @{real_user}: {e}")
        traceback.print_exc()
        return items

    # مرتب‌سازی از قدیم به جدید
    msgs = sorted(msgs, key=lambda m: m.id)
    print(f"  @{real_user} ({ch_title}): {len(msgs)} پیام جدید "
          f"(بعد از id={last_id})")

    highest = last_id
    for msg in msgs:
        if msg.id > highest:
            highest = msg.id

        mi = analyze_media(msg)
        item = build_item(msg, real_user, ch_title, mi)

        # دانلود رسانه در صورت مجاز بودن حجم
        if (mi["has_media"]
                and 0 < mi["size"] <= MAX_MEDIA_SIZE):
            ext = mi["ext"] or ""
            fname = f"{safe_name(real_user)}_{msg.id}_{mi['media_type']}{ext}"
            fpath = MEDIA_DIR / fname
            try:
                dl = await msg.download_media(file=str(fpath))
                if dl and Path(dl).exists():
                    item["media_downloaded"] = True
                    item["media_local_path"] = str(Path(dl).relative_to(BASE_DIR))
                    print(f"    ⬇ دانلود شد: {item['media_local_path']} "
                          f"({human_size(mi['size'])})")
                else:
                    item["no_media_reason"] = NOTE_DOWNLOAD_FAILED
                    item["text"] = (item["text"] or "") + \
                                   f"\n\n{NOTE_DOWNLOAD_FAILED}"
            except Exception as e:
                item["no_media_reason"] = f"خطا در دانلود: {e}"
                print(f"    [هشدار] دانلود پیام {msg.id} ناموفق: {e}")
                item["text"] = (item["text"] or "") + \
                               f"\n\n{NOTE_DOWNLOAD_FAILED}"

        items.append(item)

    if highest > last_id:
        state[state_key] = highest

    return items


# ═══════════════════════════════════════════════════════════════
#  دسته‌بندی ۲۰تایی به‌صورت round-robin بین کانال‌ها
# ═══════════════════════════════════════════════════════════════

def write_batches(all_items, channel_order):
    grouped = {}
    for it in all_items:
        grouped.setdefault(it["channel_username"], []).append(it)

    # صف هر کانال (حفظ ترتیب قدیم→جدید)
    queues = {}
    for ch in channel_order:
        if ch in grouped:
            queues[ch] = list(grouped[ch])
    # کانال‌هایی که شاید در channel_order نبوده‌اند
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
    written = []
    for i, batch in enumerate(batches):
        num = start + i
        path = DATA_DIR / f"batch_{num:06d}.json"
        payload = {
            "batch_number": num,
            "batch_size":   len(batch),
            "created_at":   datetime.now(timezone.utc).isoformat(),
            "channels":     sorted({it["channel_username"] for it in batch}),
            "messages":     batch,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(str(path.relative_to(BASE_DIR)))
        print(f"  📄 {path.name} — {len(batch)} پیام")
    return written


# ═══════════════════════════════════════════════════════════════
#  تابع اصلی
# ═══════════════════════════════════════════════════════════════

async def main():
    setup_dirs()

    channels = load_channels()
    if not channels:
        print("❌ هیچ کانالی در channels.txt تعریف نشده است.")
        return

    if not API_ID or not API_HASH:
        print("❌ متغیرهای TELEGRAM_API_ID و TELEGRAM_API_HASH تنظیم نشده‌اند.")
        sys.exit(1)

    state = load_state()

    print("🔌 اتصال به تلگرام...")
    client = TelegramClient(
        StringSession(SESSION_STR) if SESSION_STR else StringSession(),
        int(API_ID), API_HASH,
    )

    async with client:
        if not await client.is_user_authorized():
            print("❌ سشن تلگرام معتبر نیست.")
            print("   ابتدا با اجرای generate_session.py رشته‌ی سشن را بسازید "
                  "و در GitHub Secret قرار دهید.")
            sys.exit(1)

        me = await client.get_me()
        print(f"✅ ورود موفق: {me.first_name} (id={me.id})")

        all_items = []
        seen_channels = []

        for username in channels:
            print(f"\n── پردازش @{username} ──")
            items = await process_channel(client, username, state)
            all_items.extend(items)
            if items:
                ch = items[-1]["channel_username"]
                if ch not in seen_channels:
                    seen_channels.append(ch)

    # ذخیره‌ی وضعیت (آخرین پیام پردازش‌شده)
    save_state(state)

    if not all_items:
        print("\nℹ️ پیام جدیدی یافت نشد.")
        return

    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"مجموع پیام‌های جدید: {len(all_items)}")
    print(f"ذخیره در دسته‌های {BATCH_SIZE}تایی (round-robin)...")
    written = write_batches(all_items, seen_channels)
    print(f"\n✅ {len(written)} فایل JSON ذخیره شد.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except AuthKeyError:
        print("❌ رشته‌ی سشن نامعتبر است. لطفاً دوباره آن را تولید کنید.")
        sys.exit(1)
