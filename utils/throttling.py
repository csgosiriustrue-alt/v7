"""Middleware для ограничения частоты нажатий на инлайн-кнопки."""
import time
from typing import Any, Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

THROTTLE_INTERVAL = 0.2  # секунды между разрешёнными нажатиями


class ThrottlingMiddleware(BaseMiddleware):
    """Игнорирует повторные нажатия на инлайн-кнопки чаще раза в THROTTLE_INTERVAL секунд."""

    def __init__(self, interval: float = THROTTLE_INTERVAL) -> None:
        self._interval = interval
        self._last_press: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_press.get(user_id, 0.0)
            if now - last < self._interval:
                await event.answer()
                return
            self._last_press[user_id] = now
        return await handler(event, data)
