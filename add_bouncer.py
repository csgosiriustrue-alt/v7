"""Скрипт для добавления предмета 'Вышибала' в БД.
Запуск: python scripts/add_bouncer.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from models import Base, Item, RarityEnum


async def add_bouncer():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("❌ DATABASE_URL не найден в .env!")
        return

    engine = create_async_engine(database_url, echo=False, poolclass=NullPool)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        try:
            # Проверяем, есть ли уже Вышибала
            existing = await session.execute(
                select(Item).where(Item.name == "Вышибала")
            )
            item = existing.scalar_one_or_none()

            if item:
                print(f"⚠️ Вышибала уже существует в БД (id={item.id})")
                print(f"   Цена: {item.price} 🪙 / {item.price_stars} ⭐")
                print(f"   Макс. инвентарь: {item.max_in_inventory}")
                print(f"   Описание: {item.description}")

                # Обновляем параметры на актуальные
                item.price = 5_000
                item.price_stars = 10
                item.rarity = RarityEnum.EPIC
                item.max_in_inventory = 3
                item.monthly_coin_limit = 0  # Месячные лимиты удалены
                item.description = "Одноразовый. Игнорирует охрану жертвы при ограблении."
                item.emoji = "👊"
                item.drop_chance = 0
                item.is_starter = False
                await session.commit()
                print("✅ Вышибала обновлён!")
            else:
                bouncer = Item(
                    name="Вышибала",
                    emoji="👊",
                    price=5_000,
                    price_stars=10,
                    drop_chance=0,
                    rarity=RarityEnum.EPIC,
                    is_starter=False,
                    max_in_inventory=3,
                    monthly_coin_limit=0,
                    description="Одноразовый. Игнорирует охрану жертвы при ограблении.",
                )
                session.add(bouncer)
                await session.commit()
                print("✅ Вышибала добавлен в БД!")
                print(f"   Цена: 5,000 🪙 / 10 ⭐")
                print(f"   Редкость: Epic")
                print(f"   Макс. инвентарь: 3")

        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
        finally:
            await session.close()

    await engine.dispose()
    print("✅ Готово!")


if __name__ == "__main__":
    asyncio.run(add_bouncer())