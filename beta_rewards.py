"""
Раздача наград бета-тестерам.
Запуск: python scripts/beta_rewards.py

Выдаёт каждому существующему игроку:
  - Липкие Перчатки: 1 шт.
  - Отмычка: 10 шт.
  - Лом: 2 шт.
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

from models import User, Item, Inventory


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL не найден!")
    sys.exit(1)

# ── Награды ──
BETA_REWARDS = {
    "Липкие Перчатки": 1,
    "Отмычка": 10,
    "Лом": 2,
}


async def add_item_to_inventory(session: AsyncSession, user_id: int, item_id: int, quantity: int):
    """Добавляет предмет в инвентарь или увеличивает количество."""
    inv_r = await session.execute(
        select(Inventory).where(
            Inventory.user_id == user_id,
            Inventory.item_id == item_id,
        )
    )
    inv = inv_r.scalar_one_or_none()
    if inv:
        inv.quantity += quantity
    else:
        session.add(Inventory(user_id=user_id, item_id=item_id, quantity=quantity))


async def distribute_beta_rewards():
    engine = create_async_engine(DATABASE_URL, echo=False, poolclass=NullPool)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        try:
            # ── 1. Загружаем предметы наград ──
            reward_items = {}
            for item_name, qty in BETA_REWARDS.items():
                item_r = await session.execute(
                    select(Item).where(Item.name == item_name)
                )
                item = item_r.scalar_one_or_none()
                if not item:
                    print(f"❌ Предмет '{item_name}' не найден в БД!")
                    print("   Убедитесь, что seed_items был выполнен.")
                    return
                reward_items[item_name] = (item.id, qty)
                print(f"✅ Найден: {item_name} (id={item.id}) — выдать {qty} шт.")

            # ── 2. Загружаем всех игроков ──
            users_r = await session.execute(select(User))
            users = users_r.scalars().all()
            total_users = len(users)

            if total_users == 0:
                print("\n⚠️ Нет игроков в базе!")
                return

            print(f"\n👥 Всего игроков: {total_users}")
            print(f"🎁 Раздача наград...\n")

            # ── 3. Раздаём ──
            rewarded = 0
            for user in users:
                for item_name, (item_id, qty) in reward_items.items():
                    await add_item_to_inventory(session, user.tg_id, item_id, qty)
                rewarded += 1

                if rewarded % 50 == 0:
                    print(f"   ... обработано {rewarded}/{total_users}")

            # ── КОММИТ ──
            await session.commit()

            print("\n" + "=" * 50)
            print("🎉 РАЗДАЧА ЗАВЕРШЕНА!")
            print(f"👥 Награждено: {rewarded} игроков")
            print(f"🎁 Каждый получил:")
            for item_name, qty in BETA_REWARDS.items():
                print(f"   • {item_name} × {qty}")
            print("=" * 50)

        except Exception as e:
            await session.rollback()
            print(f"\n❌ ОШИБКА: {e}")
            raise
        finally:
            await session.close()

    await engine.dispose()


if __name__ == "__main__":
    print("=" * 50)
    print("🎁  РАЗДАЧА НАГРАД БЕТА-ТЕСТЕРАМ")
    print("=" * 50)
    print(f"\nНаграды:")
    for item_name, qty in BETA_REWARDS.items():
        print(f"  • {item_name} × {qty}")

    confirm = input("\nВведите 'REWARD' для подтверждения: ")
    if confirm.strip() != "REWARD":
        print("❌ Отменено.")
        sys.exit(0)

    print("\nЗапуск раздачи...\n")
    asyncio.run(distribute_beta_rewards())