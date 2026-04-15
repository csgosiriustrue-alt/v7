"""Миграция: добавление новых генов и обновление цен."""
import asyncio
import os
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Новые гены для добавления
NEW_GENES = [
    ("Ген Бога",                      "👁", 250_000, 0.01, "LEGENDARY"),
    ("Ген Тайного Правительства",      "🔺", 150_000, 0.03, "LEGENDARY"),
    ("Ген Иллюмината",                "👁‍🗨", 100_000, 0.09, "LEGENDARY"),
    ("Ген Роналдо",                   "⚽️",   7_777, 1.50, "EPIC"),
    ("Ген Тун-Тун Сахура",            "🗿",   5_000, 2.25, "EPIC"),
    ("Ген Австрийского Художника",    "🎨",   1_488, 3.20, "RARE"),
    ("Ген Задрота",                   "🤓",     888, 3.40, "RARE"),
    ("Ген Доброго Спермоеда",         "🍼",     596, 3.45, "RARE"),
    ("Ген Оффника",                   "👊",     500, 3.10, "COMMON"),
]

# Обновление цен существующих генов (name → new_price, new_drop_chance)
PRICE_UPDATES = {
    "Гены Скамера":            (550, 3.00),
    "Гены Альтушки":           (550, 3.00),
    "Гены Лудомана":           (550, 2.75),
    "Гены Инцела":             (550, 2.75),
    "Гены Холдера TON":        (400, 5.50),
    "Гены Холдера Подарков":   (400, 5.50),
    "Гены Холдера Стикеров":   (350, 5.00),
    "Гены Холдера NFT":        (350, 5.00),
    "Гены Доставщика":         (350, 5.00),
    "Гены Ивана Золо":         (280, 5.00),
    "Гены Фурри":              (275, 5.00),
    "Гены Фитоняши":           (275, 4.50),
    "Гены Тиктокера":          (275, 4.50),
    "Гены Карлика":            (200, 5.00),
    "Гены Результата инцеста": (170, 4.00),
    "Воздухан":                (155, 4.00),
    "Урод":                    (100, 4.00),
    "Нищета":                  (125, 3.50),
    "Пустышка":                (100, 3.50),
    "Гены Инфоцигана":         (800, 3.25),
    "Гены Онлифанщицы":        (800, 3.25),
}


async def migrate():
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # 1. Получаем существующие имена
        result = await session.execute(text("SELECT name FROM items"))
        existing_names = {row[0] for row in result.all()}
        print(f"📊 Существующих предметов: {len(existing_names)}")

        # 2. Добавляем новые гены
        added = 0
        for name, emoji, price, drop_chance, rarity in NEW_GENES:
            if name not in existing_names:
                await session.execute(text(
                    "INSERT INTO items (name, emoji, price, price_stars, drop_chance, rarity, "
                    "is_starter, max_in_inventory, monthly_coin_limit, description) "
                    "VALUES (:name, :emoji, :price, 0, :drop_chance, :rarity, "
                    "false, 0, 0, :desc)"
                ), {
                    "name": name, "emoji": emoji, "price": price,
                    "drop_chance": drop_chance, "rarity": rarity,
                    "desc": f"Генетический материал: {name}",
                })
                added += 1
                print(f"  ✅ Добавлен: {name} ({rarity}, {price:,}🪙, {drop_chance}%)")
            else:
                print(f"  ⏭ Уже есть: {name}")

        # 3. Обновляем цены существующих
        updated = 0
        for name, (new_price, new_drop) in PRICE_UPDATES.items():
            if name in existing_names:
                await session.execute(text(
                    "UPDATE items SET price = :price, drop_chance = :drop "
                    "WHERE name = :name"
                ), {"price": new_price, "drop": new_drop, "name": name})
                updated += 1
                print(f"  💰 Обновлён: {name} → {new_price:,}🪙")

        await session.commit()
        print(f"\n🎉 Готово! Добавлено: {added}, Обновлено цен: {updated}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(migrate())
