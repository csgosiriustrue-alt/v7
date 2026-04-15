import asyncio
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

async def migrate():
    """Добавить колонку price_stars в таблицу items."""
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # ✅ Прямо указываем DATABASE_URL с asyncpg
    DATABASE_URL = "postgresql+asyncpg://postgres:rJCrmAzjGvqujvnILevnNijEMPnFZtPl@maglev.proxy.rlwy.net:48179/railway"
    
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
            # Проверяем, существует ли уже колонка
            result = await session.execute(
                text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='items' AND column_name='price_stars'
                """)
            )
            
            if result.fetchone():
                print("⚠️  Колонка price_stars уже существует!")
                await engine.dispose()
                return
            
            # Добавляем колонку
            print("📝 Добавляю колонк�� price_stars...")
            await session.execute(
                text("ALTER TABLE items ADD COLUMN price_stars INTEGER DEFAULT 0")
            )
            await session.commit()
            
            print("✅ Колонка price_stars успешно добавлена!")
            
        except Exception as e:
            await session.rollback()
            print(f"❌ Ошибка: {e}")
            raise
        finally:
            await session.close()
    
    await engine.dispose()
    print("🎉 Миграция завершена!")

if __name__ == "__main__":
    asyncio.run(migrate())