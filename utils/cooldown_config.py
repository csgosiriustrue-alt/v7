"""Конфигурация кулдаунов покупок за монеты."""

# Формат: "Название предмета": {"limit": макс_за_период, "cooldown_hours": период_в_часах}
# За Stars — без ограничений, кулдауны только для монет.

COIN_PURCHASE_COOLDOWNS = {
    "Отмычка": {"limit": 5, "cooldown_hours": 24},          # 5 шт/день
    "Лом": {"limit": 1, "cooldown_hours": 24},              # 1 шт/день
    "Липкие Перчатки": {"limit": 1, "cooldown_hours": 24},  # 1 шт/день
    "Вышибала": {"limit": 2, "cooldown_hours": 24},         # 1 шт/день
    "Журнал для взрослых": {"limit": 1, "cooldown_hours": 48},  # 1 шт/48ч
    "Заряд теребления": {"limit": 6, "cooldown_hours": 24},     # 6 шт/день
    # ── Сейфы: 1 штука в сутки за монеты ──
    "Ржавый Сейф": {"limit": 1, "cooldown_hours": 24},      # 1 шт/день
    "Элитный Сейф": {"limit": 1, "cooldown_hours": 24},     # 1 шт/день
}


def get_cooldown_display(item_name: str, user) -> str:
    """Возвращает строку для отображения лимита в магазине."""
    config = COIN_PURCHASE_COOLDOWNS.get(item_name)
    if not config:
        return ""

    info = user.get_purchase_cooldown_info(item_name)
    limit = info['limit']
    bought = info['bought_in_window']
    remaining = max(0, limit - bought)
    hours = config['cooldown_hours']

    if hours == 24:
        period = "сегодня"
    elif hours == 48:
        period = "за 48ч"
    else:
        period = f"за {hours}ч"

    if remaining <= 0:
        if info['next_available']:
            from datetime import datetime
            now = datetime.utcnow()
            delta = info['next_available'] - now
            h = max(0, int(delta.total_seconds() // 3600))
            m = max(0, int((delta.total_seconds() % 3600) // 60))
            return f"🔄 {period}: {bought}/{limit} (через {h}ч {m}мин)"
        return f"🔄 {period}: {bought}/{limit} (исчерпано)"

    return f"🔄 {period}: {bought}/{limit} (ост. {remaining})"
