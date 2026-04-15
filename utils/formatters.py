"""Форматирование текста."""
from models import RarityEnum


def format_price(price_vv: int, price_stars: int) -> str:
    parts = []
    if price_vv > 0:
        parts.append(f"{price_vv:,} 🪙")
    if price_stars > 0:
        parts.append(f"{price_stars} ⭐")
    return " или ".join(parts) if parts else "Бесплатно"


def get_rarity_emoji(rarity: RarityEnum) -> str:
    emojis = {
        RarityEnum.LEGENDARY: "🟤",
        RarityEnum.EPIC: "🟣",
        RarityEnum.RARE: "🟢",
        RarityEnum.COMMON: "⚪",
    }
    return emojis.get(rarity, "❓")


def get_rarity_name(rarity: RarityEnum) -> str:
    names = {
        RarityEnum.LEGENDARY: "Легендарный",
        RarityEnum.EPIC: "Эпический",
        RarityEnum.RARE: "Редкий",
        RarityEnum.COMMON: "Обычный",
    }
    return names.get(rarity, "???")


def format_balance(balance_vv: int, balance_stars: int) -> str:
    return f"💰 {balance_vv:,} 🪙\n⭐ {balance_stars} Stars"