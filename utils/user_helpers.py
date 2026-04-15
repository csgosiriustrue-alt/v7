from datetime import datetime
from sqlalchemy import select
from models import User, MAX_BOX_COUNT


async def get_or_create_user(session, user_id: int, username: str | None = None) -> User:
    """
    Возвращает пользователя из БД или создаёт нового с дефолтными значениями.
    Работает при любом взаимодействии: /start, inline, callback.
      - balance_vv = 0
      - balance_stars = 0
      - box_count = MAX_BOX_COUNT (6)
      - last_refill_at = now
    При создании не выполняет commit — это обязанность вызывающего кода.
    """
    r = await session.execute(select(User).where(User.tg_id == user_id))
    user = r.scalar_one_or_none()
    if user:
        if username and (user.username != username):
            user.username = username
        return user

    user = User(
        tg_id=user_id,
        username=username,
        balance_vv=0,
        balance_stars=0,
        box_count=MAX_BOX_COUNT,
        last_refill_at=datetime.utcnow(),
    )
    session.add(user)
    await session.flush()
    return user