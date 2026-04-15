"""Выдаёт каждому игроку в БД набор инструментов для взлома сейфов."""
import asyncio
import sys
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from models import Base, User, Item, Inventory
from config import DATABASE_URL

# ============================================================================
# ЧТО ВЫДАЁМ
# ============================================================================

TOOLS_TO_GIVE = {
    "Стетоскоп": 3,       # 🩺 открывает 1 цифру кода
    "Рентген": 2,          # 🩻 открывает 3 цифры кода
    "Отмычка": 5,          # 🗝 +1 попытка ввода кода
    "Лом": 2,              # 🔨 вскрывает ржавый сейф мгновенно
    "Липкие Перчатки": 3,  # 🧤 x1.25 к шансу ограбления
}


async def main():
    engine = create_async_engine(DATABASE_URL, echo=False, poolclass=NullPool)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        try:
            # Загружаем всех юзеров
            users_r = await session.execute(select(User))
            users = users_r.scalars().all()

            if not users:
                print("❌ Нет игроков в БД!")
                await engine.dispose()
                return

            print(f"👥 Найдено игроков: {len(users)}")

            # Загружаем айтемы по именам
            item_names = list(TOOLS_TO_GIVE.keys())
            items_r = await session.execute(select(Item).where(Item.name.in_(item_names)))
            items = {item.name: item for item in items_r.scalars().all()}

            missing = [name for name in item_names if name not in items]
            if missing:
                print(f"⚠️  Не найдены предметы в БД: {missing}")
                print("   Сначала запусти сид предметов (seed_items)!")
                await engine.dispose()
                return

            print(f"🛠  Предметы для выдачи:")
            for name, qty in TOOLS_TO_GIVE.items():
                item = items[name]
                print(f"   {item.emoji} {name} x{qty}")

            total_given = 0
            total_updated = 0

            for user in users:
                for item_name, qty in TOOLS_TO_GIVE.items():
                    item = items[item_name]

                    # Проверяем, есть ли уже в инвентаре
                    inv_r = await session.execute(
                        select(Inventory).where(
                            and_(
                                Inventory.user_id == user.tg_id,
                                Inventory.item_id == item.id,
                            )
                        )
                    )
                    inv = inv_r.scalar_one_or_none()

                    if inv:
                        inv.quantity += qty
                        total_updated += 1
                    else:
                        session.add(Inventory(
                            user_id=user.tg_id,
                            item_id=item.id,
                            quantity=qty,
                        ))
                        total_given += 1

            await session.commit()

            print(f"\n✅ Готово!")
            print(f"   🆕 Новых записей: {total_given}")
            print(f"   🔄 Обновлено: {total_updated}")
            print(f"   👥 Игроков обработано: {len(users)}")
            print(f"   🛠  Предметов на игрока: {sum(TOOLS_TO_GIVE.values())} шт.")
            print(f"   📦 Всего выдано: {sum(TOOLS_TO_GIVE.values()) * len(users)} шт.")

        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
            raise
        finally:
            await session.close()

    await engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())