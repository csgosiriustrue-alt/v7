import logging
import random
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, Inventory, Item

logger = logging.getLogger(__name__)

HAZBIK_DURATION_MINUTES = 15

# Названия сейфов — эти предметы НЕ хранятся в Inventory, а активируются через поля User
SAFE_ITEM_NAMES = {"Ржавый Сейф", "Элитный Сейф"}


def generate_safe_code() -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(4))


def is_gene_item(item: Item) -> bool:
    return item.drop_chance > 0


def apply_hazbik_protection(victim: User) -> None:
    victim.hazbik_until = datetime.utcnow() + timedelta(minutes=HAZBIK_DURATION_MINUTES)


def destroy_safe(victim: User) -> None:
    """
    Полностью уничтожает сейф после взлома.
    Обнуляет ВСЕ поля сейфа.
    ВАЖНО: safe_level_rusty и safe_level_elite НЕ сбрасываются — уровни навсегда.
    """
    victim.safe_type = None
    victim.safe_code = None
    victim.safe_health = 0
    victim.elite_safe_health = 0
    victim.hidden_item_ids = []
    victim.hidden_coins = 0
    logger.info(f"🗑 Сейф уничтожен: user={victim.tg_id}")


async def return_safe_contents(session: AsyncSession, user: User) -> str:
    """
    Возвращает ВСЁ содержимое сейфа владельцу:
    - Монеты → balance_vv
    - Предметы → инвентарь
    Вызывается перед уничтожением или заменой сейфа.
    """
    returned_lines = []

    # Возвращаем монеты
    if user.hidden_coins > 0:
        user.balance_vv += user.hidden_coins
        returned_lines.append(f"💰 {user.hidden_coins:,} 🪙 возвращено на баланс")
        user.hidden_coins = 0

    # Возвращаем предметы
    if user.hidden_item_ids:
        for item_id in list(user.hidden_item_ids):
            await add_item_to_inventory(session, user.tg_id, item_id, 1)
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if item:
                returned_lines.append(f"🧬 {item.name} возвращён в инвентарь")
        user.hidden_item_ids = []

    if returned_lines:
        logger.info(f"📤 Содержимое сейфа возвращено: user={user.tg_id}")

    return "\n".join(returned_lines) if returned_lines else ""


async def activate_safe(session: AsyncSession, user: User, safe_type: str) -> None:
    """
    Активирует сейф. Если уже есть активный — сначала возвращает содержимое.
    Уровень берётся из сохранённого для данного типа.
    Также удаляет фантомные записи сейфов из Inventory.
    """
    # Если уже есть активный сейф — возвращаем содержимое
    if user.has_active_safe():
        await return_safe_contents(session, user)
        destroy_safe(user)

    # Удаляем любые фантомные записи сейфов из Inventory
    await _cleanup_safe_inventory(session, user.tg_id)

    user.safe_type = safe_type
    user.safe_code = generate_safe_code()
    user.hidden_item_ids = []
    user.hidden_coins = 0

    if safe_type == "rusty":
        user.safe_health = 3
    else:
        user.safe_health = -1  # Элитный — вечный
        user.elite_safe_health = 2  # Сброс здоровья элитного сейфа

    logger.info(f"🔐 Сейф '{safe_type}' активирован: user={user.tg_id}, "
                f"level={user.get_safe_level()}")


async def _cleanup_safe_inventory(session: AsyncSession, user_id: int) -> None:
    """Удаляет все записи сейфов из таблицы Inventory (они там не должны быть)."""
    for safe_name in SAFE_ITEM_NAMES:
        item_r = await session.execute(select(Item).where(Item.name == safe_name))
        item = item_r.scalar_one_or_none()
        if item:
            inv_r = await session.execute(
                select(Inventory).where(
                    Inventory.user_id == user_id,
                    Inventory.item_id == item.id,
                )
            )
            inv = inv_r.scalar_one_or_none()
            if inv:
                await session.delete(inv)
                logger.info(f"🧹 Фантомный сейф '{safe_name}' удалён из Inventory: user={user_id}")


async def put_item_in_safe(session: AsyncSession, user: User, item_id: int) -> tuple[bool, str]:
    if not user.has_active_safe():
        return False, "Нет активного сейфа!"
    if user.hidden_items_count() >= user.safe_item_limit():
        return False, "Сейф полон!"

    inv_r = await session.execute(
        select(Inventory).where(Inventory.user_id == user.tg_id, Inventory.item_id == item_id))
    inv = inv_r.scalar_one_or_none()
    if not inv or inv.quantity <= 0:
        return False, "Предмет не найден!"

    item = inv.item
    if not is_gene_item(item):
        return False, "Можно прятать только гены!"

    if inv.quantity <= 1:
        await session.delete(inv)
    else:
        inv.quantity -= 1

    ids = list(user.hidden_item_ids or [])
    ids.append(item_id)
    user.hidden_item_ids = ids
    return True, f"🧬 <b>{item.name}</b> спрятан в сейф!"


async def take_item_from_safe(session: AsyncSession, user: User, item_id: int) -> tuple[bool, str]:
    if not user.has_active_safe():
        return False, "Нет сейфа!"
    ids = list(user.hidden_item_ids or [])
    if item_id not in ids:
        return False, "Предмет не в сейфе!"
    ids.remove(item_id)
    user.hidden_item_ids = ids
    await add_item_to_inventory(session, user.tg_id, item_id, 1)

    item_r = await session.execute(select(Item).where(Item.id == item_id))
    item = item_r.scalar_one_or_none()
    name = item.name if item else "Предмет"
    return True, f"📤 <b>{name}</b> возвращён в инвентарь!"


async def put_coins_in_safe(session: AsyncSession, user: User, amount: int) -> tuple[bool, str]:
    if not user.has_active_safe():
        return False, "Нет сейфа!"
    if amount <= 0:
        return False, "Неверная сумма!"
    space = user.safe_coin_limit() - user.hidden_coins
    if amount > space:
        return False, f"Свободно только {space:,} 🪙!"
    if user.balance_vv < amount:
        return False, f"Недостаточно! Баланс: {user.balance_vv:,} 🪙"
    user.balance_vv -= amount
    user.hidden_coins += amount
    return True, f"💰 <b>{amount:,} 🪙</b> положено в сейф!"


async def take_coins_from_safe(session: AsyncSession, user: User, amount: int) -> tuple[bool, str]:
    if not user.has_active_safe():
        return False, "Нет сейфа!"
    if amount <= 0:
        return False, "Неверная сумма!"
    if amount > user.hidden_coins:
        return False, f"В сейфе только {user.hidden_coins:,} 🪙!"
    user.hidden_coins -= amount
    user.balance_vv += amount
    return True, f"💸 <b>{amount:,} 🪙</b> забрано из сейфа!"


async def add_item_to_inventory(session: AsyncSession, user_id: int, item_id: int, quantity: int = 1) -> tuple[bool, str]:
    inv_r = await session.execute(
        select(Inventory).where(Inventory.user_id == user_id, Inventory.item_id == item_id))
    inv = inv_r.scalar_one_or_none()
    if inv:
        inv.quantity += quantity
    else:
        session.add(Inventory(user_id=user_id, item_id=item_id, quantity=quantity))
    await session.flush()
    return True, "OK"


async def can_add_item(session: AsyncSession, user_id: int, item: Item, qty: int = 1) -> tuple[bool, str]:
    """
    Проверяет можно ли добавить предмет.
    Для сейфов — проверяет поля User (has_active_safe), а не Inventory.
    """
    # ── Специальная проверка для сейфов ──
    if item.name in SAFE_ITEM_NAMES:
        user_r = await session.execute(select(User).where(User.tg_id == user_id))
        user = user_r.scalar_one_or_none()
        if user and user.has_active_safe():
            if user.safe_type == "elite":
                current_safe = "Элитный"
            else:
                current_safe = "Ржавый"
            return False, f"У вас уже есть {current_safe} сейф!"
        return True, ""

    # ── Стандартная проверка max_in_inventory для остальных предметов ──
    if item.max_in_inventory <= 0:
        return True, ""
    inv_r = await session.execute(
        select(Inventory).where(Inventory.user_id == user_id, Inventory.item_id == item.id))
    inv = inv_r.scalar_one_or_none()
    current = inv.quantity if inv else 0
    if current + qty > item.max_in_inventory:
        return False, f"Макс. {item.max_in_inventory} шт. в инвентаре! (сейчас {current})"
    return True, ""


async def can_buy_for_coins(session, user_id, item, qty):
    return True, ""


async def get_monthly_purchases(session, user_id, item_id):
    return 0


async def log_coin_purchase(session, user_id, item_id, qty):
    pass


async def activate_boost(session: AsyncSession, user: User, item_name: str) -> tuple[bool, str]:
    now = datetime.utcnow()
    duration = timedelta(hours=24)

    if item_name == "Журнал для взрослых":
        user.magazine_until = now + duration
        return True, "🔞 Журнал активирован! КД теребления: 2ч на 24ч."
    elif item_name == "Резиновая кукла":
        user.doll_until = now + duration
        return True, "🫦 Кукла активирована! x2 шанс топ-тир на 24ч."
    elif item_name == "Путана":
        user.putana_until = now + duration
        return True, "💋 Путана активирована! x7 шанс редких, эпик и легенд. генов на 24ч."
    return False, "Неизвестный буст"


async def activate_security(session: AsyncSession, user: User) -> tuple[bool, str]:
    from models import SECURITY_DURATION_HOURS
    user.security_active = True
    user.security_until = datetime.utcnow() + timedelta(hours=SECURITY_DURATION_HOURS)
    return True, f"💂 Охрана активирована на {SECURITY_DURATION_HOURS}ч!"


async def activate_roof(session: AsyncSession, user: User) -> tuple[bool, str]:
    from models import ROOF_DURATION_HOURS
    user.roof_active = True
    user.roof_until = datetime.utcnow() + timedelta(hours=ROOF_DURATION_HOURS)
    return True, f"🕴 Крыша активирована на {ROOF_DURATION_HOURS}ч!"
