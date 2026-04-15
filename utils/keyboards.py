"""Клавиатуры Telegram."""
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


def get_main_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📦 Инвентарь"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🏢 Магазин"), KeyboardButton(text="🖤 Черный рынок")],
        [KeyboardButton(text="🔐 Сейф"), KeyboardButton(text="✊ Теребить")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="❓ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True,
        input_field_placeholder="Выберите действие...")


def get_group_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Инвентарь", callback_data=f"menu_inventory_{user_id}"),
         InlineKeyboardButton(text="👤 Профиль", callback_data=f"menu_profile_{user_id}")],
    ])


def get_shop_keyboard(items) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{format_emoji_button(str(item.emoji))} {item.name} — {item.price_stars} ⭐",
            callback_data=f"shop_select_{item.id}")]
        for item in items
    ])


def get_shop_payment_keyboard(item_id: int, price_stars: int, price_vv: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"💫 Купить за {price_stars} ⭐", callback_data=f"shop_buy_stars_{item_id}")],
    ]
    if price_vv > 0:
        buttons.append([InlineKeyboardButton(
            text=f"💰 Купить за {price_vv:,} 🪙", callback_data=f"shop_buy_vv_{item_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="shop_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_blackmarket_keyboard(items) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{format_emoji_button(str(item.emoji))} {item.name} — {item.price:,} 🪙",
            callback_data=f"bm_buy_{item.id}")]
        for item in items
    ])


def get_blackmarket_confirm_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"bm_confirm_{item_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="bm_cancel")],
    ])


def get_box_keyboard(owner_id: int | None = None) -> InlineKeyboardMarkup:
    cb = f"open_box_{owner_id}" if owner_id else "open_box"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✊ Теребить!", callback_data=cb)]
    ])


def get_inventory_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    suffix = f"_{user_id}" if user_id else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Продать предметы", callback_data=f"sell_items{suffix}")],
    ])


def get_sell_keyboard(inventory_items, user_id: int | None = None) -> InlineKeyboardMarkup:
    suffix = f"_{user_id}" if user_id else ""
    buttons = []
    for inv_item in inventory_items:
        item = inv_item.item
        buttons.append([InlineKeyboardButton(
            text=f"🧬 {item.name} ({inv_item.quantity} шт.) — {item.price:,} 🪙/шт",
            callback_data=f"sell_item_{item.id}{suffix}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"sell_cancel{suffix}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_sell_confirm_keyboard(item_id: int, quantity: int) -> InlineKeyboardMarkup:
    buttons = []
    if quantity > 1:
        buttons.append([InlineKeyboardButton(text="1️⃣ Продать 1 шт.", callback_data=f"sell_confirm_{item_id}_1")])
    if quantity >= 5:
        buttons.append([InlineKeyboardButton(text="5️⃣ Продать 5 шт.", callback_data=f"sell_confirm_{item_id}_5")])
    if quantity >= 10:
        buttons.append([InlineKeyboardButton(text="🔟 Продать 10 шт.", callback_data=f"sell_confirm_{item_id}_10")])
    buttons.append([InlineKeyboardButton(
        text=f"📦 Продать всё ({quantity} шт.)", callback_data=f"sell_confirm_{item_id}_all")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="sell_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_casino_keyboard(bet: int, owner_id: int, chat_id: int | None = None) -> InlineKeyboardMarkup:
    cid = chat_id or 0
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎰 Испытать удачу!",
            callback_data=f"casino_spin_{owner_id}_{bet}_{cid}")
    ]])


def format_emoji(emoji_id: str, fallback: str = "🧬") -> str:
    if emoji_id.isdigit():
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return emoji_id


def format_emoji_button(emoji_id: str, fallback: str = "🧬") -> str:
    if emoji_id.isdigit():
        return fallback
    return emoji_id