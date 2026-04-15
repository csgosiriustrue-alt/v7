"""Исправление атрибутов User."""
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

async def fix():
    """Исправить User."""
    
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
            print("⚠️  Пересоздаю таблицу users...")
            
            # Удаляем зависимости
            await session.execute(text("DROP TABLE IF EXISTS inventory CASCADE"))
            await session.execute(text("DROP TABLE IF EXISTS safes CASCADE"))
            await session.execute(text("DROP TABLE IF EXISTS users CASCADE"))
            
            print("✅ Зависимые таблицы удалены")
            print("📝 Пересоздаю таблицы из models...")
            
            # Пересоздаём все таблицы
            from models import Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            
            print("✅ Таблицы пересозданы!")
            
            await session.commit()
            
        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            raise
        finally:
            await session.close()
    
    await engine.dispose()
    print("✅ Завершено!")

if __name__ == "__main__":
    response = input("⚠️  Это удалит ВСЕ данные! Продолжить? (yes/no): ")
    if response.lower() == "yes":
        asyncio.run(fix())
        print("\n✅ Теперь запусти: python main.py")
    else:
        print("Отмена")