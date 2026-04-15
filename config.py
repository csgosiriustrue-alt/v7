"""Конфигурация приложения."""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = os.getenv("ADMIN_ID", "").split(",") if os.getenv("ADMIN_ID") else []

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env файле!")

if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL не найден в .env файле!")