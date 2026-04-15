"""Утилиты для казино — парсинг слот-машины Telegram."""
import logging

logger = logging.getLogger(__name__)

SLOT_MAP = [1, 2, 3, 0]

MAX_COMMON_POT = 100_000
MIN_BET = 300
MAX_BET = 10_000_000  # Максимальная ставка
POT_PERCENT = 0.50  # 50% в общак, 50% сгорается
MAX_DAILY_BETS = 25


def decode_slot_offsets(value: int) -> tuple[int, int, int]:
    left = SLOT_MAP[(value - 1) & 3]
    center = SLOT_MAP[((value - 1) >> 2) & 3]
    right = SLOT_MAP[((value - 1) >> 4) & 3]
    return (left, center, right)


def calculate_winnings(
    value: int,
    bet: int,
    common_pot: int,
) -> tuple[int, float, str]:
    # CHANGED: Джекпот 777 → x20 (было x77)
    if value == 64:
        pot_bonus = int(common_pot * 0.10)
        gross = bet * 20 + pot_bonus
        desc = f"🎰 <b>ДЖЕКПОТ 777!</b> x20 + 10% общака (+{pot_bonus:,} 🪙)"
        return (gross, 20, desc)

    left, center, right = decode_slot_offsets(value)
    logger.info(f"🎰 Decode: value={value} → ({left}, {center}, {right})")

    # CHANGED: Три в ряд → x7 (было x10)
    if left == center == right:
        gross = bet * 7
        desc = "🎰 <b>Три в ряд!</b> x7"
        return (gross, 7, desc)

    if left == center:
        gross = bet * 2
        desc = "🎰 <b>Два совпадения!</b> x2"
        return (gross, 2, desc)

    return (0, 0, "😞 <b>Не повезло...</b>")
