"""Миграция: добавляет hazbik_until и last_safe_coin_purchase."""
import asyncio
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from config import DATABASE_URL


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False, poolclass=NullPool)

    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS hazbik_until TIMESTAMP NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_safe_coin_purchase DATE NULL",
    ]

    async with engine.begin() as conn:
        for sql in migrations:
            try:
                await conn.execute(text(sql))
                print(f"✅ {sql[:60]}...")
            except Exception as e:
                print(f"⚠️  {sql[:60]}... — {e}")

    await engine.dispose()
    print("🎉 Миграция завершена!")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate())