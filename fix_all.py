"""Полная миграция для PostgreSQL."""
import asyncio
from sqlalchemy import text
from database import get_db, init_database
from config import DATABASE_URL


MIGRATIONS = [
    # Users
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS safe_type VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS safe_code VARCHAR(4)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS safe_health INTEGER DEFAULT 3",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS hidden_item_ids JSONB",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS hidden_coins INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS security_active BOOLEAN DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS security_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS roof_active BOOLEAN DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS roof_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS black_market_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_market_check DATE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS elite_safe_health INTEGER DEFAULT 2",
    "UPDATE users SET elite_safe_health = 2 WHERE safe_type = 'elite' AND elite_safe_health = 0",

    # Items
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS max_in_inventory INTEGER DEFAULT 0",
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS monthly_coin_limit INTEGER DEFAULT 0",
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS description VARCHAR(1000)",

    # Purchase logs
    """CREATE TABLE IF NOT EXISTS purchase_logs (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        quantity INTEGER NOT NULL DEFAULT 1,
        purchased_at TIMESTAMP NOT NULL DEFAULT NOW()
    )""",

    # Chat activity
    """CREATE TABLE IF NOT EXISTS chat_activity (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
        chat_id BIGINT NOT NULL REFERENCES group_chats(chat_id) ON DELETE CASCADE,
        CONSTRAINT uq_chat_activity_user_chat UNIQUE (user_id, chat_id)
    )""",
]


async def migrate():
    await init_database(DATABASE_URL)
    db = get_db()

    async for session in db.get_session():
        for sql in MIGRATIONS:
            try:
                await session.execute(text(sql))
                await session.commit()
                if "ADD COLUMN" in sql:
                    col = sql.split("IF NOT EXISTS ")[1].split(" ")[0]
                    print(f"  ✅ {col}")
                elif "CREATE TABLE" in sql:
                    tbl = sql.split("IF NOT EXISTS ")[1].split(" ")[0]
                    print(f"  ✅ table: {tbl}")
            except Exception as e:
                await session.rollback()
                err = str(e).lower()
                if "already exists" in err or "duplicate" in err:
                    if "ADD COLUMN" in sql:
                        col = sql.split("IF NOT EXISTS ")[1].split(" ")[0]
                        print(f"  ℹ️ {col}: уже есть")
                    else:
                        print(f"  ℹ️ таблица уже есть")
                else:
                    print(f"  ℹ️ {e}")

        # Проверка
        print("\n🔍 Проверка...")
        try:
            for tbl in ["users", "items", "purchase_logs", "chat_activity"]:
                r = await session.execute(text(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{tbl}' ORDER BY ordinal_position"
                ))
                cols = [row[0] for row in r.fetchall()]
                print(f"\n  {tbl} ({len(cols)} колонок):")
                for c in cols:
                    print(f"    ✅ {c}")
        except Exception as e:
            print(f"  ⚠️ {e}")

        r = await session.execute(text("SELECT COUNT(*) FROM items"))
        print(f"\n📦 Товаров: {r.scalar()}")
        print("✅ Готово!")

        await session.close()
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(migrate())
