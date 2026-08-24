#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram News Archiver  (نسخه‌ی تک‌فایلی + لینک raw گیت‌هاب)
===========================================================
در هر اجرا:
  1) فایل‌های JSON و رسانه‌ی قبلی پاک می‌شوند.
  2) پیام‌های اخیر از همه‌ی کانال‌ها دریافت می‌شوند.
  3) رسانه‌ها (در صورت ≤۱۰۰ مگابایت) دانلود می‌شوند.
  4) یک فایل JSON واحد با نام OUTPUT_FILE نوشته می‌شود.
  5) فیلد media_link برای رسانه‌های دانلودشده، لینک raw گیت‌هاب است.

تاریخ شمسی و ساعت تهران.
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
BASE_DIR      = Path(__file__).resolve().parent
CHANNELS_FILE = BASE_DIR / "channels.txt"
DATA_DIR      = BASE_DIR / "data"
MEDIA_DIR     = BASE_DIR / "media"
OUTPUT_FILE   = BASE_DIR / os.environ.get("OUTPUT_FILE", "archive.json")

# ───────────────────────── تنظیمات ────────────────────────────
MAX_MEDIA_MB       = int(os.environ.get("MAX_MEDIA_MB", "99"))
MAX_MEDIA_SIZE     = MAX_MEDIA_MB * 1024 * 1024
# تعداد کل پیام در هر اجرا که به‌طور مساوی بین کانال‌ها تقسیم می‌شود.
# مثال: 60 پیام و 3 کانال → از هر کانال 20 پیام.
TOTAL_MESSAGES     = int(os.environ.get("TOTAL_MESSAGES", "60"))
# متغیر قدیمی (فقط برای سازگاری برگشتی؛ اگر TOTAL_MESSAGES نباشد استفاده می‌شود)
MSG_PER_CHANNEL    = int(os.environ.get("MESSAGES_PER_CHANNEL", "0"))
MAX_MEDIA_PER_RUN  = int(os.environ.get("MAX_MEDIA_DOWNLOADS_PER_RUN", "60"))
RUN_TIME_BUDGET    = int(os.environ.get("RUN_TIME_BUDGET_SEC", "480"))  # ۸ دقیقه
TEHRAN_TZ          = timezone(timedelta(hours=3, minutes=30))

API_ID      = os.environ.get("TELEGRAM_API_ID", "")
API_HASH    = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STR = os.environ.get("TELEGRAM_SESSION", "")

# ── پایه‌ی لینک raw گیت‌هاب ──
# در GitHub Actions به‌طور خودکار GITHUB_REPOSITORY و GITHUB_REF_NAME ست می‌شوند.
# در صورت نیاز می‌توانید RAW_BASE_URL را دستی در env تنظیم کنید.
RAW_BASE_URL = os.environ.get("RAW_BASE_URL", "").strip().rstrip("/")
if not RAW_BASE_URL:
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    gh_ref  = os.environ.get("GITHUB_REF_NAME", "").strip()
    if gh_repo and gh_ref:
        RAW_BASE_URL = f"https://raw.githubusercontent.com/{gh_repo}/{gh_ref}"

NOTE_TOO_LARGE       = "پیام حاوی محتوای رسانه‌ای است اما به دلیل حجم بالا قابل دانلود نبود."
NOTE_DOWNLOAD_FAILED = "دانلود رسانه با خطا مواجه شد."
NOTE_NO_MEDIA        = "پیام فاقد محتوای رسانه‌ای است."
NOTE_WEBPAGE         = "پیام فقط شامل پیش‌نمایش لینک است (بدون فایل قابل دانلود)."
NOTE_UNSUPPORTED     = "نوع رسانه پشتیبانی نمی‌شود (نظرسنجی، موقعیت مکانی، مخاطب و...)."
NOTE_SKIPPED_LIMIT   = "به دلیل سقف دانلود در این اجرا، رسانه دانلود نشد."


# ═══════════════════════════════════════════════════════════════
#  ابزارهای کمکی
# ═══════════════════════════════════════════════════════════════

def setup_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def wipe_old_outputs():
    """پاک‌سازی فایل‌های JSON و رسانه‌ی قبلی در ابتدای هر اجرا."""
    removed = 0
    for folder in (DATA_DIR, MEDIA_DIR):
        if not folder.exists():
            continue
        for p in folder.iterdir():
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    # پوشه‌های تو در تو را هم پاک کن
                    for sub in p.rglob("*"):
                        if sub.is_file() or sub.is_symlink():
                            sub.unlink()
                            removed += 1
                    for sub in sorted(p.rglob("*"), reverse=True):
                        if sub.is_dir():
                            sub.rmdir()
                    p.rmdir()
            except Exception as e:
                print(f"  [هشدار] حذف {p} ناموفق بود: {e}")
    # حذف فایل خروجی قبلی (اگر خارج از data است)
    if OUTPUT_FILE.exists():
        try:
            OUTPUT_FILE.unlink()
            removed += 1
        except Exception:
            pass
    print(f"🧹 {removed} فایل/پوشه‌ی قدیمی پاک شد.")


def load_channels():
    if not CHANNELS_FILE.exists():
        return []
    result = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.append(line.lstrip("@"))
    return result


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


def raw_url_for(rel_path):
    """ساخت لینک raw گیت‌هاب برای یک مسیر相对 در مخزن."""
    if not RAW_BASE_URL:
        return None
    rel = str(rel_path).replace("\\", "/").lstrip("/")
    return f"{RAW_BASE_URL}/{rel}"


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
            f"{NOTE_TOO_LARGE} (حجم: {size_label})"
        )
        note = f"\n\n⚠️ {NOTE_TOO_LARGE} (حجم: {size_label})"
        if note.strip() not in text:
            text += note

    msg_link = (f"https://t.me/{ch_username}/{message.id}" if ch_username
                else f"https://t.me/c/{message.chat_id}/{message.id}")

    greg = message.date
    greg = greg.astimezone(timezone.utc).isoformat() if greg.tzinfo else greg.isoformat()

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
        "media_link":         None,           # لینک raw گیت‌هاب پس از دانلود پر می‌شود
        "no_media_reason":    no_media_reason,
        "archived_at":        datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
#  دریافت پیام‌های یک کانال
# ═══════════════════════════════════════════════════════════════

def compute_quotas(n_channels, total):
    """
    توزیع مساویِ «total» پیام بین «n_channels» کانال.
    باقیمانده (تقسیم ناکامل) یکی‌یکی به کانال‌های اولیه اضافه می‌شود.
    مثال: 60 پیام و 3 کانال → [20, 20, 20]
          61 پیام و 3 کانال → [21, 20, 20]
    """
    if n_channels <= 0:
        return []
    base = total // n_channels
    remainder = total % n_channels
    return [base + (1 if i < remainder else 0) for i in range(n_channels)]


async def fetch_channel(client, username, quota):
    try:
        entity = await client.get_entity(username)
    except UsernameNotOccupiedError:
        print(f"  [رد] @{username}: نام کاربری یافت نشد.")
        return [], None
    except ChannelPrivateError:
        print(f"  [رد] @{username}: کانال خصوصی/غیرقابل دسترس.")
        return [], None
    except Exception as e:
        print(f"  [رد] @{username}: خطا در شناسایی — {e}")
        return [], None

    ch_title = get_display_name(entity) or username
    real_user = getattr(entity, "username", None) or username

    if quota <= 0:
        return [], real_user

    try:
        # get_messages به‌طور پیش‌فرض پیام‌ها را از جدیدترین به قدیمی‌ترین برمی‌گرداند
        msgs = await client.get_messages(entity, limit=quota)
    except FloodWaitError as e:
        print(f"  [محدودیت] @{real_user}: {e.seconds} ثانیه صبر لازم است.")
        return [], real_user
    except Exception as e:
        print(f"  [خطا] دریافت @{real_user}: {e}")
        return [], real_user

    print(f"  @{real_user} ({ch_title}): {len(msgs)} پیام (سهمیه={quota})")

    items = []
    for msg in msgs:  # ترتیب: جدیدترین ابتدا
        mi = analyze_media(msg)
        items.append(build_item(msg, real_user, ch_title, mi))
    return items, real_user


# ═══════════════════════════════════════════════════════════════
#  چینش متوازن (round-robin) بین کانال‌ها
# ═══════════════════════════════════════════════════════════════

def interleave_round_robin(grouped, channel_order):
    """
    چینش متوازن پیام‌ها از کانال‌های مختلف به‌صورت round-robin.

    ورودیِ هر کانال از جدیدترین به قدیمی‌ترین مرتب شده است.
    در خروجی، اولین آیتم از اولین کانال، سپس اولین آیتم از کانال بعدی و ...
    قرار می‌گیرد؛ به این ترتیب جدیدترین پیامِ هر کانال در ابتدای نوبت خودش
    و در بالاترین جایگاهِ ممکن در فایل JSON ظاهر می‌شود.

    مثال با ۳ کانال و ۳ پیام از هر کدام (a1 جدیدترین پیام a است):
      [a1, b1, c1, a2, b2, c2, a3, b3, c3]
    """
    queues = {ch: list(grouped[ch]) for ch in channel_order if ch in grouped}
    for ch, lst in grouped.items():
        queues.setdefault(ch, list(lst))

    result = []
    remaining = True
    while remaining:
        remaining = False
        for ch in channel_order:
            q = queues.get(ch)
            if q:
                result.append(q.pop(0))   # جدیدترینِ باقی‌مانده از این کانال
                remaining = True
        # کانال‌هایی که شاید در channel_order نبوده‌اند
        for ch in list(queues.keys()):
            if ch not in channel_order and queues[ch]:
                result.append(queues[ch].pop(0))
                remaining = True
    return result


# ═══════════════════════════════════════════════════════════════
#  دانلود رسانه‌ها با سقف و بودجه‌ی زمانی
# ═══════════════════════════════════════════════════════════════

async def download_media_for_items(client, items):
    """
    موارد قابل دانلود را تا سقف/بودجه دانلود می‌کند و آیتم‌ها را در محل به‌روز می‌کند.
    """
    candidates = []
    for it in items:
        if (it["media_type"] in ("photo", "video", "audio", "voice",
                                 "gif", "sticker", "document")
                and it["media_size_bytes"]
                and 0 < it["media_size_bytes"] <= MAX_MEDIA_SIZE
                and not it["media_downloaded"]):
            candidates.append(it)

    if not candidates:
        print("\nℹ️ هیچ رسانه‌ای برای دانلود وجود ندارد.")
        return

    print(f"\n── دانلود رسانه‌ها: {len(candidates)} مورد قابل دانلود "
          f"(سقف {MAX_MEDIA_PER_RUN} فایل، بودجه {RUN_TIME_BUDGET}s) ──")

    start = _time.monotonic()
    done = skipped = failed = 0

    for it in candidates:
        if done >= MAX_MEDIA_PER_RUN:
            it["no_media_reason"] = NOTE_SKIPPED_LIMIT
            skipped += 1
            continue
        if _time.monotonic() - start > RUN_TIME_BUDGET:
            it["no_media_reason"] = NOTE_SKIPPED_LIMIT
            skipped += 1
            continue

        ch = it["channel_username"]
        mid = it["message_id"]
        ext = ""
        # پسوند را از روی نوع می‌سازیم
        ext = {
            "photo": ".jpg", "video": ".mp4", "audio": ".mp3",
            "voice": ".ogg", "gif": ".mp4", "sticker": ".webp",
            "document": "",
        }.get(it["media_type"], "")
        # اگر در آیتم پسوند دقیق‌تر موجود بود استفاده کن
        # (در analyze_media پسوند در mi محاسبه شده ولی در آیتم ذخیره نشده؛ دوباره با get_messages)

        fname = f"{safe_name(ch)}_{mid}_{it['media_type']}{ext}"
        fpath = MEDIA_DIR / fname
        try:
            entity = await client.get_entity(ch)
            fetched = await client.get_messages(entity, ids=mid)
            msg = fetched[0] if isinstance(fetched, list) else fetched
            if not msg:
                it["no_media_reason"] = NOTE_DOWNLOAD_FAILED
                failed += 1
                continue

            mi = analyze_media(msg)
            # اگر فایل سند است و پسوند واقعی دارد، اصلاح کن
            if mi.get("ext") and not ext:
                fname = f"{safe_name(ch)}_{mid}_{it['media_type']}{mi['ext']}"
                fpath = MEDIA_DIR / fname

            dl = await msg.download_media(file=str(fpath))
            if dl and Path(dl).exists() and Path(dl).stat().st_size > 0:
                rel = Path(dl).relative_to(BASE_DIR)
                it["media_downloaded"] = True
                it["media_local_path"] = str(rel)
                it["media_link"] = raw_url_for(rel)
                it["no_media_reason"] = None
                done += 1
                print(f"    ⬇ @{ch}/{mid} → {rel} ({human_size(mi['size'])})")
            else:
                it["no_media_reason"] = NOTE_DOWNLOAD_FAILED
                failed += 1
                print(f"    [هشدار] دانلود @{ch}/{mid} ناموفق بود.")
        except FloodWaitError as e:
            print(f"  [محدودیت] باید {e.seconds} ثانیه صبر شود؛ توقف دانلود.")
            it["no_media_reason"] = NOTE_SKIPPED_LIMIT
            skipped += 1
            break
        except Exception as e:
            it["no_media_reason"] = f"{NOTE_DOWNLOAD_FAILED} ({e})"
            failed += 1
            print(f"    [خطا] @{ch}/{mid}: {e}")

    print(f"\n✅ دانلود: {done} موفق، {failed} ناموفق، "
          f"{skipped} مورد رد شده (سقف/زمان).")


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

    # ۱) پاک‌سازی خروجی‌های قبلی
    wipe_old_outputs()

    # محاسبه‌ی سهمیه‌ی هر کانال از کل پیام‌ها
    n_channels = len(channels)
    if TOTAL_MESSAGES > 0:
        quotas = compute_quotas(n_channels, TOTAL_MESSAGES)
    else:
        # سازگاری برگشتی: اگر TOTAL_MESSAGES=0 بود از MESSAGES_PER_CHANNEL استفاده کن
        quotas = [MSG_PER_CHANNEL] * n_channels
    print(f"📊 توزیع سهمیه: مجموع {sum(quotas)} پیام بین {n_channels} کانال → {quotas}")

    print(f"🔗 پایه‌ی لینک raw: {RAW_BASE_URL or '(تنظیم نشده — media_link خالی خواهد ماند)'}" )

    print("🔌 اتصال به تلگرام...")
    client = TelegramClient(
        StringSession(SESSION_STR) if SESSION_STR else StringSession(),
        int(API_ID), API_HASH,
    )

    grouped = {}
    channel_order = []

    async with client:
        if not await client.is_user_authorized():
            print("❌ سشن تلگرام معتبر نیست. ابتدا generate_session.py را اجرا کنید.")
            sys.exit(1)

        me = await client.get_me()
        print(f"✅ ورود موفق: {me.first_name} (id={me.id})")

        # ۲) دریافت پیام‌های همه‌ی کانال‌ها با توزیع سهمیه
        for idx, username in enumerate(channels):
            print(f"\n── دریافت @{username} ──")
            quota = quotas[idx] if idx < len(quotas) else 0
            try:
                items, real_user = await fetch_channel(client, username, quota)
            except Exception as e:
                print(f"  [خطای پیش‌بینی‌نشده] @{username}: {e}")
                traceback.print_exc()
                items, real_user = [], None

            key = real_user or username
            if items:
                grouped[key] = items
                if key not in channel_order:
                    channel_order.append(key)

            # اگر این کانال کمتر از سهمیه‌اش پیام داشت، باقی‌مانده را
            # به کانال‌های بعدی منتقل کن تا مجموع پیام‌ها تا حد امکان به TOTAL_MESSAGES برسد.
            if idx + 1 < len(quotas):
                deficit = quota - len(items)
                if deficit > 0:
                    quotas[idx + 1] += deficit
                    print(f"  ↪️ {deficit} سهمیه‌ی بلااستفاده به کانال بعدی منتقل شد.")

        # ۳) چینش متوازن
        all_items = interleave_round_robin(grouped, channel_order)
        print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"مجموع پیام‌ها: {len(all_items)} از {len(grouped)} کانال.")

        # ۴) دانلود رسانه‌ها
        try:
            await download_media_for_items(client, all_items)
        except Exception as e:
            print(f"  [خطا در فاز دانلود] {e}")
            traceback.print_exc()

    # ۵) نوشتن یک فایل JSON واحد
    payload = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "shamsi_generated": jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S") + " تهران",
        "channels":         [
            {"username": u, "name": (grouped[u][0]["channel_name"] if grouped.get(u) else u)}
            for u in channel_order
        ],
        "total_messages":   len(all_items),
        "messages":         all_items,
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n📄 فایل خروجی: {OUTPUT_FILE.relative_to(BASE_DIR)} "
          f"({OUTPUT_FILE.stat().st_size / 1024:.1f} KB)")

    elapsed = _time.monotonic() - run_start
    print(f"🏁 پایان اجرا در {elapsed:.1f} ثانیه.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except AuthKeyError:
        print("❌ رشته‌ی سشن نامعتبر است. لطفاً دوباره آن را تولید کنید.")
        sys.exit(1)
