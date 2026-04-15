"""Административные хендлеры."""
import logging
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Админ-панель (на будущее)."""
    logger.info(f"👨‍💼 Пользователь {message.from_user.id} запросил админ-панель")
    await message.answer("👨‍💼 Админ-панель в разработке...")