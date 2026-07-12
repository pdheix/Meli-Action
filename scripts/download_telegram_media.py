#!/usr/bin/env python3
"""Download media from a Telegram public post and save it locally.

Environment variables required:
    TELEGRAM_API_ID         Telegram API ID (integer)
    TELEGRAM_API_HASH       Telegram API hash
    TELEGRAM_SESSION_STRING StringSession content for Telethon
    TELEGRAM_LINK           Public Telegram post URL, e.g. https://t.me/channel/123

Optional:
    GITHUB_OUTPUT           Path to GitHub Actions step output file
"""

import asyncio
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto


def parse_telegram_url(url: str) -> tuple:
    """Extract entity name and message id from a public Telegram post URL."""
    patterns = [
        r"https?://t\.me/(?P<entity>[^/]+)/(?P<msg_id>\d+)",
        r"https?://telegram\.me/(?P<entity>[^/]+)/(?P<msg_id>\d+)",
    ]
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return match.group("entity"), int(match.group("msg_id"))
    raise ValueError(f"Unsupported Telegram link format: {url}")


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are illegal in file names."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def print_progress(current: int, total: int, file_name: str) -> None:
    """Print a simple progress bar to stdout (GitHub Actions friendly)."""
    if total and total > 0:
        percent = current / total * 100
        bar_len = 40
        filled = int(bar_len * current / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        sys.stdout.write(
            f"\r|{bar}| {percent:6.2f}% "
            f"({current / 1024 / 1024:.2f} / {total / 1024 / 1024:.2f} MB) - {file_name}"
        )
    else:
        sys.stdout.write(
            f"\r⏳ Downloaded {current / 1024 / 1024:.2f} MB - {file_name}"
        )
    sys.stdout.flush()


def guess_extension(media_obj) -> str:
    """Return a reasonable file extension based on mime_type."""
    mime = getattr(media_obj, "mime_type", None) or "application/octet-stream"
    ext = mime.split("/")[-1]
    # Common mime sub-types map to better extensions
    mapping = {
        "jpeg": "jpg",
        "mpeg": "mp3",
        "mp4": "mp4",
        "quicktime": "mov",
        "x-matroska": "mkv",
        "pdf": "pdf",
        "zip": "zip",
    }
    return mapping.get(ext, ext)


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_string = os.environ["TELEGRAM_SESSION_STRING"]
    link = os.environ["TELEGRAM_LINK"]

    entity, msg_id = parse_telegram_url(link)

    output_dir = Path("downloaded_media")
    output_dir.mkdir(exist_ok=True)

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.start()

    print(f"🔍 Fetching message {msg_id} from '{entity}' ...")
    message = await client.get_messages(entity, ids=msg_id)

    if not message:
        print("❌ Message not found. Check the link and your access to the chat.")
        await client.disconnect()
        sys.exit(1)

    if not message.media:
        print("❌ The requested message does not contain any media.")
        await client.disconnect()
        sys.exit(1)

    # Determine a human readable file name
    file_name = None
    if isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                file_name = attr.file_name
                break
        if not file_name:
            ext = guess_extension(doc)
            file_name = f"telegram_file_{msg_id}.{ext}"
    elif isinstance(message.media, MessageMediaPhoto):
        file_name = f"telegram_photo_{msg_id}.jpg"
    else:
        file_name = f"telegram_media_{msg_id}"

    file_name = sanitize_filename(file_name)
    output_path = output_dir / file_name

    # Avoid overwriting existing files
    if output_path.exists():
        stem = output_path.stem
        suffix = output_path.suffix
        counter = 1
        while output_path.exists():
            output_path = output_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    print(f"📥 Downloading: {output_path.name}")

    await client.download_media(
        message,
        file=str(output_path),
        progress_callback=lambda c, t: print_progress(c, t, output_path.name),
    )
    print()  # newline after progress bar

    size = output_path.stat().st_size
    print(f"✅ Saved: {output_path} ({size / 1024 / 1024:.2f} MB)")

    # Expose outputs for the GitHub Actions workflow
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"file_path={output_path}\n")
            f.write(f"file_name={output_path.name}\n")
            f.write(f"file_size={size}\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
