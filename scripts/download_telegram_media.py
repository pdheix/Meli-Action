#!/usr/bin/env python3
"""Download media from one or more Telegram public posts.

Environment variables required:
    TELEGRAM_API_ID         Telegram API ID (integer)
    TELEGRAM_API_HASH       Telegram API hash
    TELEGRAM_SESSION_STRING StringSession content for Telethon
    TELEGRAM_LINK           One or more public Telegram post URLs separated by commas
                            e.g. https://t.me/channel/123,https://t.me/channel/124

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
    url = url.strip()
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


def resolve_output_path(output_dir: Path, file_name: str) -> Path:
    """Return a unique output path, appending a counter if needed."""
    output_path = output_dir / file_name
    if not output_path.exists():
        return output_path
    stem = output_path.stem
    suffix = output_path.suffix
    counter = 1
    while True:
        candidate = output_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def determine_file_name(message, msg_id: int) -> str:
    """Pick a file name for the media attached to a message."""
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
    return sanitize_filename(file_name)


async def download_single_link(
    client: TelegramClient, link: str, output_dir: Path
) -> Path | None:
    """Download media from a single Telegram post URL. Return path or None."""
    entity, msg_id = parse_telegram_url(link)

    print(f"\n🔍 Fetching message {msg_id} from '{entity}' ...")
    message = await client.get_messages(entity, ids=msg_id)

    if not message:
        print(f"⚠️  Message not found for {link}. Check access.")
        return None

    if not message.media:
        print(f"⚠️  No media in message {msg_id}.")
        return None

    file_name = determine_file_name(message, msg_id)
    output_path = resolve_output_path(output_dir, file_name)

    print(f"📥 Downloading: {output_path.name}")
    await client.download_media(
        message,
        file=str(output_path),
        progress_callback=lambda c, t: print_progress(c, t, output_path.name),
    )
    print()  # newline after progress bar

    size = output_path.stat().st_size
    print(f"✅ Saved: {output_path} ({size / 1024 / 1024:.2f} MB)")
    return output_path


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_string = os.environ["TELEGRAM_SESSION_STRING"]
    raw_links = os.environ["TELEGRAM_LINK"]

    links = [link.strip() for link in raw_links.split(",") if link.strip()]
    if not links:
        print("❌ No valid Telegram links provided.")
        sys.exit(1)

    print(f"🚀 Processing {len(links)} Telegram link(s) sequentially...")

    output_dir = Path("downloaded_media")
    output_dir.mkdir(exist_ok=True)

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.start()

    downloaded_paths: list[Path] = []
    failed_links: list[str] = []

    for idx, link in enumerate(links, start=1):
        print(f"\n═══════════════════════════════════════════════════════════════")
        print(f"[{idx}/{len(links)}] Processing: {link}")
        print(f"═══════════════════════════════════════════════════════════════")
        try:
            path = await download_single_link(client, link, output_dir)
            if path:
                downloaded_paths.append(path)
            else:
                failed_links.append(link)
        except Exception as e:
            print(f"❌ Failed to process {link}: {e}")
            failed_links.append(link)

    await client.disconnect()

    # Summary
    print(f"\n═══════════════════════════════════════════════════════════════")
    print(f"📊 Summary: {len(downloaded_paths)} succeeded, {len(failed_links)} failed")
    print(f"═══════════════════════════════════════════════════════════════")

    if downloaded_paths:
        # Write a manifest so the workflow can iterate over files safely
        manifest = output_dir / "downloaded_files.txt"
        manifest.write_text(
            "\n".join(str(p.resolve()) for p in downloaded_paths), encoding="utf-8"
        )
        print("📄 Manifest written to:", manifest)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"downloaded_count={len(downloaded_paths)}\n")
            f.write(f"failed_count={len(failed_links)}\n")
            f.write(f"file_list={','.join(str(p) for p in downloaded_paths)}\n")

    if failed_links:
        print("\n⚠️  Failed links:")
        for link in failed_links:
            print(f"   - {link}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
