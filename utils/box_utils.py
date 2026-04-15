"""Утилиты для теребления с бустами и нормализацией весов."""
import random
from datetime import datetime, timedelta
from models import MAX_BOX_COUNT, BOX_REFILL_HOURS, RarityEnum

BOOSTED_RARITIES = {RarityEnum.RARE, RarityEnum.EPIC, RarityEnum.LEGENDARY}


async def update_user_boxes(user) -> None:
    now = datetime.utcnow()
    if user.box_count >= MAX_BOX_COUNT:
        user.last_refill_at = now
        return
    refill_hours = user.get_refill_hours()
    elapsed = (now - user.last_refill_at).total_seconds()
    refill_seconds = refill_hours * 3600

    if refill_seconds <= 0:
        return

    new_boxes = int(elapsed // refill_seconds)
    if new_boxes > 0:
        user.box_count = min(MAX_BOX_COUNT, user.box_count + new_boxes)
        user.last_refill_at += timedelta(seconds=new_boxes * refill_seconds)
        if user.box_count >= MAX_BOX_COUNT:
            user.last_refill_at = now
    elif elapsed >= refill_seconds:
        # Страховка: если elapsed >= refill_seconds но int дал 0
        user.box_count = min(MAX_BOX_COUNT, user.box_count + 1)
        user.last_refill_at = now


def get_time_until_next_box(last_refill_at: datetime, refill_hours: int = BOX_REFILL_HOURS) -> tuple[int, int]:
    now = datetime.utcnow()
    elapsed = (now - last_refill_at).total_seconds()
    refill_seconds = refill_hours * 3600

    if refill_seconds <= 0:
        return 0, 0

    remaining = refill_seconds - elapsed
    if remaining <= 0:
        return 0, 0
    return int(remaining // 3600), int((remaining % 3600) // 60)


def get_weighted_random_item(items: list, multiplier: float = 1.0):
    """
    random.choices с бустом шансов для редких редкостей.
    multiplier применяется к drop_chance каждого предмета с BOOSTED_RARITIES,
    а остальные предметы сохраняют свои оригинальные веса.
    """
    if not items:
        return None

    if multiplier <= 1.0:
        weights = [item.drop_chance for item in items]
    else:
        weights = []
        for item in items:
            if item.rarity in BOOSTED_RARITIES:
                weights.append(item.drop_chance * multiplier)
            else:
                weights.append(item.drop_chance)

    total = sum(weights)
    if total <= 0:
        return random.choice(items)
    return random.choices(items, weights=weights, k=1)[0]
