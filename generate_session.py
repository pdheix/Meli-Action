#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ساخت رشته‌ی Session برای تلگرام (Telethon StringSession)
─────────────────────────────────────────────────────────
این اسکریپت را یک بار روی کامپیوتر خودتان اجرا کنید:

    pip install telethon
    python generate_session.py

از شما API ID و API Hash و شماره موبایل و کد ورود خواسته می‌شود.
در پایان یک رشته‌ی طولانی چاپ می‌شود؛ آن را کپی کرده و در
GitHub Secrets با نام TELEGRAM_SESSION_STRING ذخیره کنید.

دریافت API ID / API Hash:
    https://my.telegram.org  →  API development tools
"""

import os
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = os.environ.get("TELEGRAM_API_ID") or input("API ID را وارد کنید: ").strip()
API_HASH = os.environ.get("TELEGRAM_API_HASH") or input("API Hash را وارد کنید: ").strip()

with TelegramClient(StringSession(), int(API_ID), API_HASH) as client:
    print("\n" + "═" * 60)
    print("✅ رشته‌ی سشن شما (TELEGRAM_SESSION_STRING):")
    print("─" * 60)
    print(client.session.save())
    print("═" * 60)
    print(
        "\nرشته‌ی بالا را در GitHub مخزن خود در مسیر زیر وارد کنید:\n"
        "  Settings → Secrets and variables → Actions → New repository secret\n"
        "سه Secret لازم است:\n"
        "  1) TELEGRAM_API_ID\n"
        "  2) TELEGRAM_API_HASH\n"
        "  3) TELEGRAM_SESSION_STRING"
    )
