"""Миграция: добавление системы боксов."""
import asyncio
import sys
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv
import os

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def migrate():
    """Добавить колонки для боксов."""
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    print("🔄 Подключение к БД...")
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        poolclass=NullPool,
    )
    
    async_session_maker = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    async with async_session_maker() as session:
        try:
            print("📝 Проверяю наличие колонок...")
            
            # Добавляем box_count если её нет
            await session.execute(
                text("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS box_count INTEGER DEFAULT 3
                """)
            )
            print("✅ Колонка box_count добавлена/проверена")
            
            # Добавляем last_refill_at если её нет
            await session.execute(
                text("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS last_refill_at TIMESTAMP DEFAULT NOW()
                """)
            )
            print("✅ Колонка last_refill_at добавлена/проверена")
            
            # Устанавливаем значения для существующих пользователей
            await session.execute(
                text("""
                    UPDATE users
                    SET box_count = COALESCE(box_count, 3),
                        last_refill_at = COALESCE(last_refill_at, NOW())
                    WHERE box_count IS NULL OR last_refill_at IS NULL
                """)
            )
            print("✅ Значения по умолчанию установлены для существующих пользователей")
            
            await session.commit()
            print("✅ Миграция завершена успешно!")
            
        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
            raise
        finally:
            await session.close()
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(migrate())