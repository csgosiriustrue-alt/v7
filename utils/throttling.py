"""Middleware для ограничения частоты нажатий на инлайн-кнопки."""
import time
from typing import Any, Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

THROTTLE_INTERVAL = 1.0  # секунды между разрешёнными действиями


class ThrottlingMiddleware(BaseMiddleware):
    """Игнорирует слишком частые CallbackQuery и Message от одного пользователя."""

    def __init__(self, interval: float = THROTTLE_INTERVAL, message_interval: float = 1.0) -> None:
        self._interval = interval
        self._last_press: dict[int, float] = {}
        self._message_interval = message_interval
        self._last_message: dict[int, float] = {}

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
        elif isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_message.get(user_id, 0.0)
            if now - last < self._message_interval:
                return
            self._last_message[user_id] = now
        return await handler(event, data)
