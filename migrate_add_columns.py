"""Миграция: добавляет новые колонки в существующую таблицу users."""
import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from config import DATABASE_URL


MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS casino_bets_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_casino_reset DATE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS registered_at TIMESTAMP NOT NULL DEFAULT NOW()",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS action_count INTEGER NOT NULL DEFAULT 0",
]


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=True, poolclass=NullPool)
    async with engine.begin() as conn:
        for sql in MIGRATIONS:
            print(f"▶ {sql}")
            await conn.execute(__import__('sqlalchemy').text(sql))
    await engine.dispose()
    print("✅ Миграция завершена!")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate())