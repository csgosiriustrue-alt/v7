import os
import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from models import Base

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, database_url: str):
        self.engine = create_async_engine(
            database_url,
            echo=False,
            poolclass=NullPool,
        )
        self.async_session_maker = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_db(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("✅ Database tables created successfully!")
        await self._run_migrations()

    async def _run_migrations(self) -> None:
        migrations = [
            ("users", "casino_bets_today", "ALTER TABLE users ADD COLUMN casino_bets_today INTEGER NOT NULL DEFAULT 0"),
            ("users", "last_casino_reset", "ALTER TABLE users ADD COLUMN last_casino_reset DATE"),
            ("users", "purchase_cooldowns", "ALTER TABLE users ADD COLUMN purchase_cooldowns JSON"),
            # ── Новые миграции: уровни сейфов ──
            ("users", "safe_level_rusty", "ALTER TABLE users ADD COLUMN safe_level_rusty INTEGER NOT NULL DEFAULT 1"),
            ("users", "safe_level_elite", "ALTER TABLE users ADD COLUMN safe_level_elite INTEGER NOT NULL DEFAULT 1"),
            ("users", "notifications_enabled", "ALTER TABLE users ADD COLUMN notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE"),
            ("users", "elite_safe_health", "ALTER TABLE users ADD COLUMN elite_safe_health INTEGER NOT NULL DEFAULT 2"),
        ]

        async with self.engine.begin() as conn:
            for table, column, sql in migrations:
                try:
                    result = await conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :table AND column_name = :column"
                    ), {"table": table, "column": column})
                    exists = result.scalar_one_or_none()
                    if not exists:
                        await conn.execute(text(sql))
                        logger.info(f"✅ Миграция: добавлена колонка {table}.{column}")
                    else:
                        logger.debug(f"⏭ Колонка {table}.{column} уже существует")
                except Exception as e:
                    logger.warning(f"⚠️ Миграция {table}.{column}: {e}")

    async def dispose(self) -> None:
        await self.engine.dispose()
        print("✅ Database connections closed!")

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self.async_session_maker() as session:
            try:
                yield session
            finally:
                await session.close()


db: Database | None = None


async def init_database(database_url: str | None = None) -> Database:
    global db

    if database_url is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError(
                "DATABASE_URL не установлен в переменных окружения или не передан параметр"
            )

    db = Database(database_url)
    await db.init_db()
    return db


def get_db() -> Database:
    if db is None:
        raise RuntimeError("Database не инициализирована. Вызови init_database() сначала.")
    return db
