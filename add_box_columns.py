"""Добавить колонки box_count и last_refill_at в таблицу users."""
import asyncio
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv
import os

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def add_columns():
    """Добавить недостающие колонки."""
    
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
            print("📝 Добавляю колонки...")
            
            # Проверяем и добавляем box_count
            try:
                await session.execute(text("""
                    ALTER TABLE users
                    ADD COLUMN box_count INTEGER NOT NULL DEFAULT 3
                """))
                print("✅ Колонка box_count добавлена")
            except Exception as e:
                if "already exists" in str(e):
                    print("✅ Колонка box_count уже существует")
                else:
                    raise
            
            # Проверяем и добавляем last_refill_at
            try:
                await session.execute(text("""
                    ALTER TABLE users
                    ADD COLUMN last_refill_at TIMESTAMP NOT NULL DEFAULT NOW()
                """))
                print("✅ Колонка last_refill_at добавлена")
            except Exception as e:
                if "already exists" in str(e):
                    print("✅ Колонка last_refill_at уже существует")
                else:
                    raise
            
            await session.commit()
            print("✅ Все колонки добавлены успешно!")
            
        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
        finally:
            await session.close()
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(add_columns())