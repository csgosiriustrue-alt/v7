import asyncio
import sys
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from models import Item, RarityEnum, Base

# ✅ Прямо указываем DATABASE_URL с asyncpg
DATABASE_URL = "postgresql+asyncpg://postgres:rJCrmAzjGvqujvnILevnNijEMPnFZtPl@maglev.proxy.rlwy.net:48179/railway"

# Данные подарков
ITEMS_DATA = [
    # ============================================================================
    # ОБЫЧНЫЕ ПОДАРКИ (выпадают случайно)
    # ============================================================================
    
    # Legendary (0.05% - 1.00%)
    ("Plush Pepe", "5388878432450944423", 600000, 0, 0.05, RarityEnum.LEGENDARY),
    ("Heart Locked", "5389088129934207748", 135000, 0, 0.20, RarityEnum.LEGENDARY),
    ("Durov's Cap", "5388682779510743204", 55000, 0, 0.50, RarityEnum.LEGENDARY),
    ("Precious Peach", "5391193892269894320", 30000, 0, 1.00, RarityEnum.LEGENDARY),

    # Epic (1.80% - 3.50%)
    ("Redo", "5388680279839776397", 16000, 0, 1.80, RarityEnum.EPIC),
    ("Scared Cat", "5388715335362847638", 12500, 0, 2.50, RarityEnum.EPIC),
    ("Astral Shard", "5391348463847907201", 12000, 0, 2.70, RarityEnum.EPIC),
    ("Loot bag", "5388608665555081549", 10000, 0, 3.50, RarityEnum.EPIC),

    # Rare (4.50% - 8.50%)
    ("Ion gem", "5389063296433298487", 6000, 0, 4.50, RarityEnum.RARE),
    ("Artisan Brick", "5390986054507465118", 5000, 0, 5.50, RarityEnum.RARE),
    ("Magic potion", "5388927635596286886", 5000, 0, 5.50, RarityEnum.RARE),
    ("Swiss Watch", "5388670581803618693", 3500, 0, 6.50, RarityEnum.RARE),
    ("Kissed Frog", "5388926772307865112", 3500, 0, 6.50, RarityEnum.RARE),
    ("Sharp Tongue", "5391254030401967461", 3000, 0, 7.50, RarityEnum.RARE),
    ("Vintage Cigar", "5388787963259818939", 2500, 0, 8.50, RarityEnum.RARE),
    ("Vodoo Doll", "5391162225476016098", 2500, 0, 8.50, RarityEnum.RARE),

    # Common (10.00% - 55.00%)
    ("Electric Skull", "5391364462601085547", 2000, 0, 10.00, RarityEnum.COMMON),
    ("Cupid Charm", "5391080088521446376", 1500, 0, 12.00, RarityEnum.COMMON),
    ("Rare Bird", "5391066868612113434", 1500, 0, 12.00, RarityEnum.COMMON),
    ("Sakura Flower", "5388967256669591191", 800, 0, 18.00, RarityEnum.COMMON),
    ("Jelly Bunny", "5388964044034053291", 550, 0, 25.00, RarityEnum.COMMON),
    ("Jolly Chimp", "5389040992668130533", 500, 0, 30.00, RarityEnum.COMMON),
    ("Snoop Dogg", "5390866169085335815", 400, 0, 35.00, RarityEnum.COMMON),
    ("Light Sword", "5388802261205951866", 400, 0, 35.00, RarityEnum.COMMON),
    ("Fresh socks", "5388633490466055984", 300, 0, 45.00, RarityEnum.COMMON),
    ("Lol pop", "5391232607105094611", 250, 0, 55.00, RarityEnum.COMMON),
    ("Chill flame", "5389055612736805491", 250, 0, 55.00, RarityEnum.COMMON),

    # ============================================================================
    # ПРЕМИУМ ПРЕДМЕТЫ (только покупка)
    # ============================================================================

    # Фигуры и коллекционные предметы
    ("Durov's Figure", "5389117717963907438", 1000000, 0, 0.0, RarityEnum.LEGENDARY),
    ("Pin", "5388872204748366072", 430000, 0, 0.0, RarityEnum.EPIC),
    ("Budda", "5391245612266065395", 100000, 0, 0.0, RarityEnum.RARE),

    # Сейфы
    ("Ржавый сейф", "5388878432450944423", 15000, 10, 0.0, RarityEnum.RARE),
    ("Элитный сейф", "5389088129934207748", 0, 100, 0.0, RarityEnum.LEGENDARY),

    # Защита и утилиты
    ("Охрана (24ч)", "5388682779510743204", 0, 45, 0.0, RarityEnum.EPIC),
    ("Рентген", "5391193892269894320", 0, 10, 0.0, RarityEnum.RARE),
    ("Отмычка", "5388680279839776397", 0, 2, 0.0, RarityEnum.COMMON),
    ("Лом", "5388715335362847638", 0, 15, 0.0, RarityEnum.RARE),
    ("Липкие Перчатки", "5391348463847907201", 0, 20, 0.0, RarityEnum.EPIC),

    # ============================================================================
    # СТАРТОВЫЕ ПРЕДМЕТЫ (выдаются при регистрации)
    # ============================================================================
    ("Стетоскоп", "5388608665555081549", 5000, 5, 0.0, RarityEnum.COMMON, True),
    ("Адвокат", "5389063296433298487", 0, 2, 0.0, RarityEnum.COMMON, True),
]


async def seed_items():
    """Наполнение таблицы Item."""
    
    print("🌱 Инициализация БД...")
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        poolclass=NullPool,
    )
    
    # Создаем таблицы
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async_session_maker = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    print(f"📝 Добавляю {len(ITEMS_DATA)} предметов...")

    async with async_session_maker() as session:
        try:
            # Проверяем, есть ли уже предметы
            result = await session.execute(select(Item))
            existing_items = result.scalars().all()

            if existing_items:
                print(f"⚠️  Таблица уже содержит {len(existing_items)} предметов!")
                response = input("Перезаписать? (yes/no): ").strip().lower()
                if response != "yes":
                    print("❌ Отмена операции")
                    await engine.dispose()
                    return

                # Удаляем старые данные
                await session.execute(text("DELETE FROM inventory"))
                await session.execute(text("DELETE FROM items"))
                await session.commit()
                print("🗑️  Старые данные удалены")

            # Добавляем новые предметы
            items_to_add = []
            for item_data in ITEMS_DATA:
                if len(item_data) == 7:
                    name, emoji, price, price_stars, drop_chance, rarity, is_starter = item_data
                else:
                    name, emoji, price, price_stars, drop_chance, rarity = item_data
                    is_starter = False

                item = Item(
                    name=name,
                    emoji=emoji,
                    price=price,
                    price_stars=price_stars,
                    drop_chance=drop_chance,
                    rarity=rarity,
                    is_starter=is_starter,
                )
                items_to_add.append(item)

            session.add_all(items_to_add)
            await session.commit()

            print(f"✅ Успешно добавлено {len(items_to_add)} предметов!\n")
            
            # Статистика по категориям
            print("📊 СТАТИСТИКА ПО РЕДКОСТИ:")
            legendary = sum(1 for i in ITEMS_DATA if i[5] == RarityEnum.LEGENDARY)
            epic = sum(1 for i in ITEMS_DATA if i[5] == RarityEnum.EPIC)
            rare = sum(1 for i in ITEMS_DATA if i[5] == RarityEnum.RARE)
            common = sum(1 for i in ITEMS_DATA if i[5] == RarityEnum.COMMON)
            
            print(f"  🟤 Legendary: {legendary}")
            print(f"  🟣 Epic: {epic}")
            print(f"  🟢 Rare: {rare}")
            print(f"  ⚪ Common: {common}")
            print(f"  👤 Starter items: 2\n")

        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
            raise
        finally:
            await session.close()

    await engine.dispose()
    print("🎉 Готово!")


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(seed_items())