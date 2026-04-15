import asyncio
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

DATABASE_URL = "postgresql+asyncpg://postgres:rJCrmAzjGvqujvnILevnNijEMPnFZtPl@maglev.proxy.rlwy.net:48179/railway"

async def migrate():
    """Увеличить размер колонки emoji."""
    
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
            print("📝 Увеличиваю размер колонки emoji с 10 на 50...")
            await session.execute(
                text("ALTER TABLE items ALTER COLUMN emoji TYPE VARCHAR(50)")
            )
            await session.commit()
            
            print("✅ Колонка emoji успешно обновлена!")
            
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