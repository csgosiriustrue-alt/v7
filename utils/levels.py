"""Система уровней игрока."""
import logging
from sqlalchemy import select

logger = logging.getLogger(__name__)

MAX_LEVEL = 80
BASE_THRESHOLD = 100_000
SCALING_FACTOR = 1.25
MIN_TRANSFER_LEVEL = 5

# Кэш порогов чтобы не считать каждый раз
_xp_cache: dict[int, int] = {}


def get_required_xp(level: int) -> int:
    """Порог XP для перехода с level на level+1."""
    if level >= MAX_LEVEL:
        return 0
    if level in _xp_cache:
        return _xp_cache[level]

    if level < 5:
        threshold = BASE_THRESHOLD
    else:
        threshold = BASE_THRESHOLD
        for _ in range(5, level + 1):
            threshold = int(threshold * SCALING_FACTOR)

    _xp_cache[level] = threshold
    return threshold


def add_xp(user, amount: int) -> list[int]:
    """
    Начисляет XP и проверяет повышение уровня.
    Возвращает список новых уровней (может быть несколько за раз).

    Вызывай после ЛЮБОГО заработка монет:
    - Выигрыш казино
    - Успешное ограбление
    - Продажа генов
    - Лут из сейфа
    - НЕ переводы!
    """
    if amount <= 0:
        return []

    user.xp += amount
    new_levels = []

    while user.level < MAX_LEVEL:
        required = get_required_xp(user.level)
        if required <= 0:
            break
        if user.xp >= required:
            user.xp -= required
            user.level += 1
            new_levels.append(user.level)
            logger.info(f"🆙 User {user.tg_id} → level {user.level}")
        else:
            break

    return new_levels


def can_transfer(user) -> tuple[bool, str]:
    """Проверяет может ли игрок делать переводы."""
    if user.level < MIN_TRANSFER_LEVEL:
        return False, (
            f"❌ <b>Переводы доступны с {MIN_TRANSFER_LEVEL} уровня!</b>\n\n"
            f"⭐ Ваш уровень: <b>{user.level}</b>\n"
            f"📈 До {MIN_TRANSFER_LEVEL} уровня: зарабатывайте монеты (казино, ограбления, продажа генов)"
        )
    return True, ""


def build_progress_bar(current_xp: int, required_xp: int, length: int = 10) -> str:
    """Прогресс-бар: [▓▓▓▓░░░░░░] 40%"""
    if required_xp <= 0:
        return "[▓▓▓▓▓▓▓▓▓▓] MAX"
    ratio = min(1.0, current_xp / required_xp)
    filled = int(ratio * length)
    empty = length - filled
    pct = int(ratio * 100)
    return f"[{'▓' * filled}{'░' * empty}] {pct}%"


def format_level_line(user) -> str:
    """Готовая строка для /profile."""
    req = get_required_xp(user.level)
    bar = build_progress_bar(user.xp, req)
    if user.level >= MAX_LEVEL:
        return f"⭐ Уровень: <b>{user.level}</b> {bar}"
    return f"⭐ Уровень: <b>{user.level}</b> {bar} <code>{user.xp:,}/{req:,}</code>"


# Эмодзи предметов (дублируется здесь для избежания циклических импортов с моделями БД)
_ITEM_EMOJI: dict[str, str] = {
    "Отмычка": "🗝",
    "Адвокат": "💼",
    "Заряд теребления": "⚡",
    "Охрана": "💂",
    "Лом": "🔨",
    "Элитный Сейф": "🏦",
    "Вышибала": "👊",
    "Резиновая кукла": "🫦",
    "Крыша": "🕴",
    "Путана": "💋",
    "Журнал для взрослых": "🔞",
}


async def grant_level_rewards(bot, session, user, old_level: int, new_levels: list[int]) -> None:
    """
    Выдаёт награды за каждый уровень из new_levels и отправляет ЛС игроку.
    Для «Элитный Сейф» — активирует сейф через activate_safe, а не добавляет в инвентарь.
    """
    from level_rewards import LEVEL_REWARDS
    from models import Item, MAX_BOX_COUNT
    from utils.inventory_helpers import add_item_to_inventory, activate_safe

    levels_with_rewards = [lvl for lvl in new_levels if lvl in LEVEL_REWARDS]
    if not levels_with_rewards:
        return

    level_lines: dict[int, list[str]] = {}

    for level in levels_with_rewards:
        rewards = LEVEL_REWARDS[level]
        lines = []
        for item_name, count in rewards.items():
            # Специальная обработка для Элитного Сейфа
            if item_name == "Элитный Сейф":
                if user.safe_type == "elite":
                    lines.append(f"🏦 Элитный Сейф × {count} <i>(уже есть)</i>")
                    continue
                await activate_safe(session, user, "elite")
                lines.append(f"🏦 Элитный Сейф × {count}")
                continue

            # Специальная обработка для Заряда теребления — начисляем напрямую в box_count
            if item_name == "Заряд теребления":
                user.box_count += count  # Бонус за уровень не пропадает, лимит не применяется
                lines.append(f"⚡ Заряд теребления × {count}")
                continue

            # Обычные предметы — добавляем в инвентарь
            item_r = await session.execute(select(Item).where(Item.name == item_name))
            item = item_r.scalar_one_or_none()
            if not item:
                logger.warning(f"⚠️ grant_level_rewards: предмет '{item_name}' не найден в БД")
                continue

            await add_item_to_inventory(session, user.tg_id, item.id, count)
            emoji = _ITEM_EMOJI.get(item_name, "🎁")
            lines.append(f"{emoji} {item_name} × {count}")

        if lines:
            level_lines[level] = lines

    if not level_lines:
        return

    # Формируем красивое ЛС
    # Проверяем, были ли начислены заряды теребления
    charges_awarded = sum(
        LEVEL_REWARDS[lvl].get("Заряд теребления", 0)
        for lvl in levels_with_rewards
        if lvl in level_lines
    )

    if len(level_lines) == 1:
        level = next(iter(level_lines))
        items_text = "\n".join(f"  • {line}" for line in level_lines[level])
        text = (
            f"🎉 <b>Поздравляем с уровнем {level}!</b>\n\n"
            f"🎁 <b>Ваши награды:</b>\n{items_text}\n\n"
            f"Продолжайте в том же духе! 💪"
        )
    else:
        parts = ["🎉 <b>Вы достигли нескольких уровней!</b>"]
        for level, lines in sorted(level_lines.items()):
            items_text = "\n".join(f"  • {line}" for line in lines)
            parts.append(f"\n🏆 <b>Уровень {level}:</b>\n{items_text}")
        parts.append("\nПродолжайте в том же духе! 💪")
        text = "\n".join(parts)

    if charges_awarded:
        text += f"\n\n⚡ Вам начислен(ы) заряд(ы) теребления за новый уровень! (Текущий запас: {user.box_count}/{MAX_BOX_COUNT})"

    try:
        await bot.send_message(chat_id=user.tg_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"⚠️ grant_level_rewards: не удалось отправить ЛС user={user.tg_id}: {e}")
