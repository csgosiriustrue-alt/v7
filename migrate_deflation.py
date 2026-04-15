"""Миграция: делим все цены, балансы и общаки на 10."""
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from config import DATABASE_URL

async def run():
    engine = create_async_engine(DATABASE_URL, echo=True)
    async with engine.begin() as conn:
        # Цены предметов / 10
        await conn.execute(text("UPDATE items SET price = price / 10"))
        await conn.execute(text("UPDATE items SET price_stars = CASE WHEN price_stars > 1 THEN price_stars ELSE price_stars END"))
        # Балансы / 10
        await conn.execute(text("UPDATE users SET balance_vv = balance_vv / 10"))
        await conn.execute(text("UPDATE users SET hidden_coins = hidden_coins / 10"))
        # Общаки / 10
        await conn.execute(text("UPDATE group_chats SET common_pot = common_pot / 10"))
    await engine.dispose()
    print("✅ Дефляция x10 применена!")

asyncio.run(run())