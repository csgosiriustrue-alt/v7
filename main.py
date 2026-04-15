"""Главный файл бота Gift Heist."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, Message, TelegramObject

from handlers import admin_shop
from utils.throttling import ThrottlingMiddleware

from config import BOT_TOKEN, DATABASE_URL
from database import init_database, get_db
from handlers import user, box, shop, stats, admin, casino, robbery, inline_router, safe, game_21

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
logger = logging.getLogger(__name__)


class ChatTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            if event.chat and event.chat.type in ("group", "supergroup") and event.from_user:
                casino.remember_chat(event.from_user.id, event.chat.id)
                asyncio.create_task(_save_chat_mapping(event.from_user.id, event.chat.id))
        return await handler(event, data)


async def _save_chat_mapping(user_id: int, chat_id: int) -> None:
    try:
        from utils.pot_event import track_chat_activity
        db = get_db()
        async for session in db.get_session():
            try:
                await track_chat_activity(session, chat_id, user_id)
                await session.commit()
            except Exception:
                await session.rollback()
            finally:
                await session.close()
    except Exception:
        pass


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.message.middleware(ChatTrackingMiddleware())
dp.callback_query.middleware(ThrottlingMiddleware())

dp.include_router(inline_router.router)
dp.include_router(game_21.router)
dp.include_router(casino.router)
dp.include_router(user.router)
dp.include_router(safe.router)
dp.include_router(robbery.router)
dp.include_router(box.router)
dp.include_router(shop.router)
dp.include_router(stats.router)
dp.include_router(admin_shop.router)
dp.include_router(admin.router)

logger.info("✅ Конфигурация загружена")


async def on_startup() -> None:
    logger.info("🤖 Запуск Gift Heist...")
    try:
        await init_database(DATABASE_URL)
        logger.info("✅ БД инициализирована")
    except Exception as e:
        logger.error(f"❌ БД: {e}", exc_info=True)
        raise

    # Сид предметов
    from utils.seed_items import seed_items
    db = get_db()
    async for session in db.get_session():
        try:
            await seed_items(session)
            await session.commit()
            logger.info("🧬 Сид предметов выполнен")
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ Сид: {e}", exc_info=True)
        finally:
            await session.close()

    await casino._ensure_chat_map_loaded()

    private_commands = [
        BotCommand(command="start", description="Начать игру"),
        BotCommand(command="profile", description="Профиль"),
        BotCommand(command="inventory", description="Инвентарь"),
        BotCommand(command="safe", description="Сейф"),
        BotCommand(command="box", description="✊ Теребить"),
        BotCommand(command="shop", description="Магазин"),
        BotCommand(command="blackmarket", description="Черный рынок"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="help", description="Справка"),
    ]
    group_commands = [
        BotCommand(command="pot", description="Общак чата"),
        BotCommand(command="donat", description="Донат в общак"),
        BotCommand(command="help", description="Справка"),
    ]
    await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    logger.info("🎮 Gift Heist готов!")


async def on_shutdown() -> None:
    logger.info("🛑 Завершение...")
    db = get_db()
    await db.dispose()
    await bot.session.close()


async def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⌨️ Остановлен")
    except Exception as e:
        logger.error(f"❌ {e}", exc_info=True)
        raise
