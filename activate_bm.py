"""Активирует Чёрный рынок на 24ч для указанного tg_id."""
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select

from config import DATABASE_URL
from database import init_database, get_db
from models import User

YOUR_TG_ID = 1969951556  # ← замените на свой tg_id


async def main():
    await init_database(DATABASE_URL)
    db = get_db()
    async for session in db.get_session():
        user_r = await session.execute(select(User).where(User.tg_id == YOUR_TG_ID))
        user = user_r.scalar_one_or_none()
        if not user:
            print("❌ Юзер не найден!")
            return
        user.black_market_until = datetime.utcnow() + timedelta(hours=24)
        user.last_market_check = None  # сбрасываем дневной лимит
        await session.commit()
        print(f"✅ ЧР активирован до {user.black_market_until} для {user.username}")

asyncio.run(main())