"""Магазин с категориями, оптовыми Stars, зарядами и Чёрным рынком."""
import logging
import math
import random
from datetime import datetime, date, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command
from sqlalchemy import select, and_

from database import get_db
from models import User, Item, Inventory, MAX_BOX_COUNT
from utils.inventory_helpers import (
    add_item_to_inventory, activate_safe, activate_boost,
    activate_security, activate_roof,
    can_add_item, can_buy_for_coins, get_monthly_purchases, log_coin_purchase,
)
from utils.keyboards import get_main_keyboard, format_emoji
from utils.cooldown_config import COIN_PURCHASE_COOLDOWNS, get_cooldown_display
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()

BLACK_MARKET_CHANCE = 0.0777
BLACK_MARKET_DURATION_HOURS = 24
BULK_OPTIONS_COINS = [1, 3, 5, 10, 20]

BOOST_ITEM_NAMES = {"Журнал для взрослых", "Резиновая кукла", "Путана"}
CHARGE_ITEM_NAME = "Заряд теребления"
SAFE_ITEM_NAMES = {"Ржавый Сейф", "Элитный Сейф"}

# Категории магазина (Крыша УБРАНА из обычного магазина — только Чёрный рынок)
CATEGORY_BOOSTS = {"Журнал для взрослых", "Резиновая кукла", "Путана"}
CATEGORY_DEFENSE = {"Ржавый Сейф", "Элитный Сейф", "Охрана"}
CATEGORY_TOOLS = {"Отмычка", "Лом", "Адвокат", "Липкие Перчатки", "Вышибала"}

CATEGORY_EXCLUSIVE = {"Крыша", "Путана"}

# Предметы, которые нельзя купить за монеты в магазине (только Stars)
STARS_ONLY_ITEMS = {"Крыша", "Путана"}

# Скидки Stars
STARS_DISCOUNT = {1: 0, 5: 0.05, 10: 0.10}

# ============================================================================
# ЧЁРНЫЙ РЫНОК — конфиг
# ============================================================================

BM_DISCOUNT = 0.25  # 25% скидка
BM_NO_DISCOUNT = {"Durov's Figure"}  # без скидки

# Ассортимент ЧР
BM_ITEM_NAMES = [
    "Крыша",
    "Путана",
    "Элитный Сейф",
    "Durov's Figure",
    "Липкие Перчатки",
    "Журнал для взрослых",
]

# Спец-товар: 10 зарядов за 2500
BM_CHARGES_PACK = 10
BM_CHARGES_PRICE = 2_500
BM_CHARGES_ID = "bm_charges_pack"


def _is_private(message) -> bool:
    return message.chat.type == "private"


ITEM_DESCRIPTIONS = {
    "Ржавый Сейф": (
        "🧰 <b>Ржавый сейф</b>\n«Скрипит, но дело делает.»\n\n"
        "Спрячь <b>1 предмет</b> или <b>100К 🪙</b> (база).\n"
        "❤️ Прочность: 3 удара\n"
        "🔝 Прокачка: до ур.20 (+30%/ур.)\n"
        "⚠️ <i>Вскрывается Ломом.</i>"
    ),
    "Элитный Сейф": (
        "🏦 <b>Элитный сейф</b>\n«Титан и биометрия.»\n\n"
        "Спрячь <b>3 предмета</b> или <b>700К 🪙</b> (база).\n"
        "🛡 <b>Вечная прочность</b> — не изнашивается!\n"
        "🔝 Прокачка: ∞ уровней (+30%/ур.)\n"
        "🔒 Устойчив к Лому."
    ),
    "Охрана": "💂 <b>Охрана (6ч)</b>\n\nБлокирует ограбление на <b>6ч</b>.",
    "Крыша": "🕴 <b>Крыша (8ч)</b>\n\nБлокирует + <b>15%</b> залог грабителя. 8ч.",
    "Отмычка": "🗝 <b>Отмычка</b>\n\n<b>+1 попытка</b> кода сейфа.",
    "Лом": "🔨 <b>Лом</b>\n\n<b>70% шанс</b> вскрыть Ржавый сейф мгновенно.\n⚠️ <i>При неудаче ломается!</i>",
    "Адвокат": "💼 <b>Адвокат</b>\n\nМгновенно из тюрьмы.",
    "Липкие Перчатки": "🧤 <b>Липкие Перчатки</b>\n\n<b>x1.25</b> к шансу ограбления.",
    "Журнал для взрослых": "🔞 <b>Журнал для взрослых</b>\n«Для настроения...»\n\nКД теребления <b>4ч → 2ч</b> на 24 часа.",
    "Резиновая кукла": "🫦 <b>Резиновая кукла</b>\n«Лучший друг.»\n\nШанс топ-тир + легенд <b>x2</b> на 24ч.",
    "Путана": "💋 <b>Путана</b>\n«VIP.»\n\nШанс Rare + Epic + Legendary <b>x7</b> на 24ч.",
    "Заряд теребления": "⚡ <b>Заряд теребления</b>\n\n+1 попытка теребления.\n💰 500 🪙\n⭐ 3 Stars (без лимита)",
    "Durov's Figure": "🗿 <b>Durov's Figure</b>\n«Коллекционная фигурка Павла Дурова.»\n\n🏆 Символ статуса. Бесценна.",
    "Вышибала": "👊 <b>Вышибала</b>\n«Большой парень.»\n\nОдноразовый. Игнорирует охрану жертвы при ограблении.",
}


def get_item_description(item: Item) -> str:
    return ITEM_DESCRIPTIONS.get(item.name,
        f"{format_emoji(str(item.emoji))} <b>{item.name}</b>\n{item.description or ''}")


def _bm_price(item: Item) -> int:
    if item.name in BM_NO_DISCOUNT:
        return item.price
    return int(item.price * (1 - BM_DISCOUNT))


async def _handle_post_purchase(session, user, item, qty=1):
    """Сейфы, бусты, защита, заряды."""
    if item.name == "Ржавый Сейф":
        await activate_safe(session, user, "rusty")
        return "🧰 Ржавый сейф активирован!"
    elif item.name == "Элитный Сейф":
        await activate_safe(session, user, "elite")
        return "🏦 Элитный сейф активирован!"
    elif item.name == "Охрана":
        ok, msg = await activate_security(session, user)
        return msg
    elif item.name == "Крыша":
        ok, msg = await activate_roof(session, user)
        return msg
    elif item.name in BOOST_ITEM_NAMES:
        ok, msg = await activate_boost(session, user, item.name)
        return msg
    elif item.name == CHARGE_ITEM_NAME:
        user.box_count = min(MAX_BOX_COUNT, user.box_count + qty)
        return f"⚡ +{qty} заряд(ов)! Теперь: {user.box_count}/{MAX_BOX_COUNT}"
    return None


def _is_activatable_item(item_name: str) -> bool:
    """Предметы которые активируются сразу (не добавляются в инвентарь как предмет)."""
    return item_name in SAFE_ITEM_NAMES or item_name in BOOST_ITEM_NAMES or item_name == "Охрана" or item_name == "Крыша"


# ============================================================================
# /shop — Главное меню категорий
# ============================================================================


@router.message(Command("shop"))
async def cmd_shop(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_shop_categories(message)


@router.message(lambda m: m.text == "🏢 Магазин" and m.chat.type == "private")
async def button_shop(message: Message) -> None:
    await _show_shop_categories(message)


async def _show_shop_categories(message: Message) -> None:
    buttons = [
        [InlineKeyboardButton(text="🚀 Бусты", callback_data="shop_cat_boosts")],
        [InlineKeyboardButton(text="🛡 Защита", callback_data="shop_cat_defense")],
        [InlineKeyboardButton(text="🛠 Инструменты", callback_data="shop_cat_tools")],
        [InlineKeyboardButton(text="⚡ Заряды теребления", callback_data="shop_cat_charges")],
    ]
    await message.answer(
        "<b>🏢 Магазин Gift Heist</b>\n\n"
        "⭐ Stars — Telegram Stars (без лимитов!)\n"
        "🪙 Монеты — внутриигровая валюта\n\n"
        "🖤 Крыша — только на Чёрном рынке!\n\n"
        "Выбери категорию 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(lambda c: c.data and c.data.startswith("shop_cat_"))
async def shop_category(call: CallbackQuery) -> None:
    cat = call.data.split("shop_cat_")[1]
    db = get_db()

    if cat == "charges":
        await _show_charge_card(call)
        return

    cat_map = {"boosts": CATEGORY_BOOSTS, "defense": CATEGORY_DEFENSE, "tools": CATEGORY_TOOLS}
    cat_titles = {"boosts": "🚀 Бусты", "defense": "🛡 Защита", "tools": "🛠 Инструменты"}
    names = cat_map.get(cat, set())
    title = cat_titles.get(cat, "Магазин")

    async for session in db.get_session():
        try:
            items_r = await session.execute(
                select(Item).where(Item.name.in_(names)).order_by(Item.price_stars.asc()))
            items = items_r.scalars().all()
            if not items:
                await call.answer("Пусто!", show_alert=True)
                return
            buttons = []
            for item in items:
                coin_tag = f" | {item.price:,}🪙" if item.price > 0 else ""
                buttons.append([InlineKeyboardButton(
                    text=f"{item.name} — {item.price_stars}⭐{coin_tag}",
                    callback_data=f"shop_view_{item.id}")])
            buttons.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="shop_categories")])
            await call.message.edit_text(
                f"<b>{title}</b>\n\nВыбери товар 👇", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception as e:
            logger.error(f"❌ shop_cat: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data == "shop_categories")
async def shop_categories_back(call: CallbackQuery) -> None:
    buttons = [
        [InlineKeyboardButton(text="🚀 Бусты", callback_data="shop_cat_boosts")],
        [InlineKeyboardButton(text="🛡 Защита", callback_data="shop_cat_defense")],
        [InlineKeyboardButton(text="🛠 Инструменты", callback_data="shop_cat_tools")],
        [InlineKeyboardButton(text="⚡ Заряды теребления", callback_data="shop_cat_charges")],
    ]
    await call.message.edit_text(
        "<b>🏢 Магазин Gift Heist</b>\n\nВыбери категорию 👇", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


# ============================================================================
# ЗАРЯДЫ
# ============================================================================


async def _show_charge_card(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            item_r = await session.execute(select(Item).where(Item.name == CHARGE_ITEM_NAME).limit(1))
            item = item_r.scalars().first()
            if not item:
                await call.answer("❌ Заряд не найден!", show_alert=True)
                return
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            box_count = user.box_count if user else 0

            desc = get_item_description(item)

            cd_text = ""
            can_buy_coins = True
            remaining_cd = 6
            if user and item.name in COIN_PURCHASE_COOLDOWNS:
                cd_info = user.get_purchase_cooldown_info(item.name)
                can_buy_coins = cd_info['can_buy']
                bought = cd_info['bought_in_window']
                limit = cd_info['limit']
                remaining_cd = max(0, limit - bought)
                cd_text = f"\n🔄 За 🪙 сегодня: {bought}/{limit} (ост. {remaining_cd})"

            text = (f"{desc}\n\n"
                    f"✊ Сейчас: <b>{box_count}/{MAX_BOX_COUNT}</b>"
                    f"{cd_text}")

            buttons = []

            if can_buy_coins:
                coin_options = [1, 3, 6]
                for qty in coin_options:
                    if qty <= remaining_cd:
                        total_price = item.price * qty
                        buttons.append([InlineKeyboardButton(
                            text=f"💰 {qty} заряд(ов) — {total_price:,} 🪙",
                            callback_data=f"shop_coins_{item.id}_{qty}")])
            else:
                buttons.append([InlineKeyboardButton(
                    text="❌ Лимит за 🪙 исчерпан (6/день)",
                    callback_data="noop")])

            for qty, discount in STARS_DISCOUNT.items():
                base = item.price_stars * qty
                final = math.ceil(base * (1 - discount))
                dt = f" (-{int(discount * 100)}%)" if discount > 0 else ""
                buttons.append([InlineKeyboardButton(
                    text=f"⭐ {qty}шт — {final} Stars{dt} (без лимита)",
                    callback_data=f"shop_stars_{item.id}_{qty}")])

            buttons.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="shop_categories")])
            await call.message.edit_text(text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception as e:
            logger.error(f"❌ charge: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# КАРТОЧКА ТОВАРА
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("shop_view_"))
async def shop_view(call: CallbackQuery) -> None:
    item_id = int(call.data.split("shop_view_")[1])
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not item:
                await call.answer("❌", show_alert=True)
                return

            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()

            desc = get_item_description(item)
            limit_text = f"\n📦 Лимит инвентаря: {item.max_in_inventory} шт." if item.max_in_inventory > 0 else ""

            # Кулдаун покупок за монеты
            cooldown_text = ""
            if user and item.name in COIN_PURCHASE_COOLDOWNS:
                cd_display = get_cooldown_display(item.name, user)
                if cd_display:
                    cooldown_text = f"\n{cd_display}"

            # Доп. инфо для сейфов
            safe_status_text = ""
            if item.name in SAFE_ITEM_NAMES and user:
                if user.has_active_safe():
                    st = "Элитный" if user.safe_type == "elite" else "Ржавый"
                    safe_status_text = f"\n\n🔐 <i>У вас уже есть {st} сейф. Покупка заменит его!</i>"

            buttons = []

            # ── Кнопки Stars — ВСЕГДА доступны (без лимитов) ──
            for qty, discount in STARS_DISCOUNT.items():
                base = item.price_stars * qty
                final = math.ceil(base * (1 - discount))
                dt = f" (-{int(discount * 100)}%)" if discount > 0 else ""
                buttons.append([InlineKeyboardButton(
                    text=f"⭐ {qty}шт — {final} Stars{dt} (без лимита)",
                    callback_data=f"shop_stars_{item.id}_{qty}")])

            # ── Кнопки монет — с проверкой лимитов ──
            if item.price > 0:
                can_buy_cd = True
                if user and item.name in COIN_PURCHASE_COOLDOWNS:
                    cd_info = user.get_purchase_cooldown_info(item.name)
                    can_buy_cd = cd_info['can_buy']

                if not can_buy_cd:
                    cd_info = user.get_purchase_cooldown_info(item.name)
                    hours = cd_info['cooldown_hours']
                    buttons.append([InlineKeyboardButton(
                        text=f"❌ Лимит за 🪙 ({hours}ч)",
                        callback_data="noop")])
                elif item.name in COIN_PURCHASE_COOLDOWNS or item.max_in_inventory > 0:
                    # Предметы с кулдауном — только по 1
                    buttons.append([InlineKeyboardButton(
                        text=f"💰 1 шт. — {item.price:,} 🪙",
                        callback_data=f"shop_coins_{item.id}_1")])
                else:
                    row1, row2 = [], []
                    for qty in BULK_OPTIONS_COINS:
                        btn = InlineKeyboardButton(
                            text=f"{qty}шт ({item.price * qty:,}🪙)",
                            callback_data=f"shop_coins_{item.id}_{qty}")
                        (row1 if qty <= 5 else row2).append(btn)
                    buttons.append(row1)
                    if row2:
                        buttons.append(row2)

            buttons.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="shop_categories")])

            price_text = f"\n\n💫 <b>{item.price_stars} ⭐</b> <i>(без лимитов)</i>"
            if item.price > 0:
                price_text += f" | 💰 <b>{item.price:,} 🪙</b>"

            await call.message.edit_text(
                desc + price_text + limit_text + cooldown_text + safe_status_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception as e:
            logger.error(f"❌ shop_view: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ПОКУПКА ЗА МОНЕТЫ — ИСПРАВЛЕНА ДЛЯ СЕЙФОВ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("shop_coins_"))
async def shop_buy_coins(call: CallbackQuery) -> None:
    parts = call.data.split("_")
    item_id, qty = int(parts[2]), int(parts[3])
    if qty <= 0 or item_id <= 0:
        await call.answer("❌ Некорректные данные!", show_alert=True)
        return
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not user or not item:
                await call.answer("❌", show_alert=True)
                return

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            # ── Блокируем Stars-only предметы ──
            if item.name in STARS_ONLY_ITEMS:
                await call.answer("❌ Только за ⭐ Stars!", show_alert=True)
                return

            # ══════════════════════════════════════════════
            # ПРОВЕРКА КУЛДАУНА ДЛЯ ВСЕХ ПРЕДМЕТОВ (включая сейфы!)
            # ══════════════════════════════════════════════
            if item.name in COIN_PURCHASE_COOLDOWNS:
                cd_info = user.get_purchase_cooldown_info(item.name)
                if not cd_info['can_buy']:
                    hours = cd_info['cooldown_hours']
                    period = "завтра" if hours == 24 else f"через {hours}ч"
                    if hours == 48:
                        period = "через 48ч"
                    await call.answer(
                        f"❌ Лимит исчерпан! Новые поставки {period}. "
                        f"За ⭐ Stars — без ограничений!",
                        show_alert=True)
                    return
                bought = cd_info['bought_in_window']
                limit = cd_info['limit']
                remaining_limit = max(0, limit - bought)
                if qty > remaining_limit:
                    await call.answer(
                        f"❌ Можно купить ещё {remaining_limit} шт. за 🪙!",
                        show_alert=True)
                    return

            # ── Заряды: проверка лимита box_count ──
            is_charge = (item.name == CHARGE_ITEM_NAME)
            if is_charge and user.box_count + qty > MAX_BOX_COUNT:
                space = MAX_BOX_COUNT - user.box_count
                if space <= 0:
                    await call.answer(f"❌ Уже {MAX_BOX_COUNT}/{MAX_BOX_COUNT}!", show_alert=True)
                    return
                await call.answer(f"❌ Можно только {space} шт. (макс {MAX_BOX_COUNT})", show_alert=True)
                return

            # ── Проверка баланса ──
            total = item.price * qty
            if user.balance_vv < total:
                await call.answer(f"❌ Нужно {total:,} 🪙!", show_alert=True)
                return

            # ── Проверка лимита инвентаря (для НЕ-активируемых предметов) ──
            if not is_charge and not _is_activatable_item(item.name):
                ok, reason = await can_add_item(session, user_id, item, qty)
                if not ok:
                    await call.answer(f"❌ {reason}", show_alert=True)
                    return

            ok, reason = await can_buy_for_coins(session, user_id, item, qty)
            if not ok:
                await call.answer(f"❌ {reason}", show_alert=True)
                return

            # ── Добавляем в инвентарь (для обычных предметов) ──
            if not is_charge and not _is_activatable_item(item.name):
                ok, reason = await add_item_to_inventory(session, user_id, item.id, qty)
                if not ok:
                    await call.answer(f"❌ {reason}", show_alert=True)
                    return

            # ── Списываем монеты ──
            user.balance_vv -= total
            await log_coin_purchase(session, user_id, item.id, qty)

            # ── Записываем кулдаун ──
            if item.name in COIN_PURCHASE_COOLDOWNS:
                for _ in range(qty):
                    user.record_coin_purchase(item.name)

            # ── Пост-покупка: активация сейфов, бустов, зарядов ──
            boost_msg = await _handle_post_purchase(session, user, item, qty)
            await session.commit()
            extra = f"\n\n{boost_msg}" if boost_msg else ""

            # Показываем кулдаун после покупки
            cd_after = ""
            if item.name in COIN_PURCHASE_COOLDOWNS:
                cd_after_info = user.get_purchase_cooldown_info(item.name)
                remaining = max(0, cd_after_info['limit'] - cd_after_info['bought_in_window'])
                if remaining <= 0:
                    cd_after = "\n\n🔄 <i>Дневной лимит за 🪙 исчерпан. За ⭐ Stars — без лимитов!</i>"
                else:
                    cd_after = f"\n\n🔄 <i>Осталось за 🪙: {remaining} шт.</i>"

            await call.message.edit_text(
                f"✅ <b>Куплено!</b>\n\n{format_emoji(str(item.emoji))} <b>{item.name}</b> × {qty}\n"
                f"💰 <b>{total:,} 🪙</b>\n💼 Баланс: <b>{user.balance_vv:,} 🪙</b>{extra}{cd_after}",
                parse_mode="HTML")
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ shop_coins: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ПОКУПКА ЗА STARS — инвойс
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("shop_stars_"))
async def shop_send_invoice(call: CallbackQuery) -> None:
    parts = call.data.split("_")
    item_id, qty = int(parts[2]), int(parts[3])
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not item:
                await call.answer("❌ Предмет не найден!", show_alert=True)
                return

            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            is_charge = (item.name == CHARGE_ITEM_NAME)

            # Для Stars проверяем ТОЛЬКО max_in_inventory (физический лимит)
            if not is_charge and not _is_activatable_item(item.name):
                if item.max_in_inventory > 0:
                    inv_r = await session.execute(
                        select(Inventory).where(
                            Inventory.user_id == user_id,
                            Inventory.item_id == item.id,
                        )
                    )
                    inv = inv_r.scalar_one_or_none()
                    current = inv.quantity if inv else 0
                    if current + qty > item.max_in_inventory:
                        await call.answer(
                            f"❌ Макс. {item.max_in_inventory} шт.! (сейчас {current})",
                            show_alert=True)
                        return

            if is_charge and user:
                if user.box_count + qty > MAX_BOX_COUNT:
                    space = MAX_BOX_COUNT - user.box_count
                    if space <= 0:
                        await call.answer(f"❌ Уже {MAX_BOX_COUNT}/{MAX_BOX_COUNT}!", show_alert=True)
                        return
                    await call.answer(f"❌ Можно только {space} шт.", show_alert=True)
                    return

            discount = STARS_DISCOUNT.get(qty, 0)
            base = item.price_stars * qty
            final = math.ceil(base * (1 - discount))
            dt = f" (скидка {int(discount * 100)}%)" if discount > 0 else ""

            payload = f"stars_{item_id}_{qty}_{user_id}"

            await call.message.answer_invoice(
                title=f"{item.name} × {qty}{dt}",
                description=f"{item.description or item.name}\n\n⭐ Без лимитов — мгновенная доставка!",
                payload=payload,
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=f"{item.name} x{qty}", amount=final)],
            )
            await call.answer()

        except Exception as e:
            logger.error(f"❌ invoice: {e}", exc_info=True)
            await call.answer("❌ Ошибка!", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# PRE-CHECKOUT
# ============================================================================


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


# ============================================================================
# SUCCESSFUL PAYMENT — без лимитов
# ============================================================================


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payload = message.successful_payment.invoice_payload
    stars_amount = message.successful_payment.total_amount

    try:
        parts = payload.split("_")
        if parts[0] in ("stars", "shop"):
            item_id = int(parts[1])
            qty = int(parts[2])
            user_id = int(parts[3])
        else:
            logger.error(f"❌ Неизвестный payload: {payload}")
            await message.answer("❌ Ошибка! Обратитесь в поддержку.", reply_markup=get_main_keyboard())
            return
    except (ValueError, IndexError) as e:
        logger.error(f"❌ payload '{payload}': {e}")
        await message.answer("❌ Ошибка! Обратитесь в поддержку.", reply_markup=get_main_keyboard())
        return

    if message.from_user.id != user_id:
        logger.warning(f"⚠️ Payment user mismatch: {message.from_user.id} != {user_id}")
        await message.answer("❌ Ошибка верификации!", reply_markup=get_main_keyboard())
        return

    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user:
                logger.error(f"❌ Payment: user {user_id} not found!")
                await message.answer("❌ /start!", reply_markup=get_main_keyboard())
                return

            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not item:
                logger.error(f"❌ Payment: item {item_id} not found!")
                await message.answer("❌ Предмет не найден!", reply_markup=get_main_keyboard())
                return

            is_charge = (item.name == CHARGE_ITEM_NAME)

            if not is_charge and not _is_activatable_item(item.name):
                ok, reason = await add_item_to_inventory(session, user_id, item.id, qty)
                if not ok:
                    logger.error(f"❌ Payment add_item: {reason}")
                    await message.answer(
                        f"❌ {reason}\n\n⚠️ Stars списаны — обратитесь в поддержку!",
                        reply_markup=get_main_keyboard())
                    return

            boost_msg = await _handle_post_purchase(session, user, item, qty)
            await session.commit()

            extra = f"\n\n{boost_msg}" if boost_msg else ""
            emoji_html = format_emoji(str(item.emoji))

            await message.answer(
                f"💫 <b>Оплата прошла!</b>\n\n"
                f"{emoji_html} <b>{item.name}</b> × {qty}\n"
                f"⭐ Списано: <b>{stars_amount} Stars</b>\n\n"
                f"✅ <i>Лимиты не затронуты!</i>{extra}",
                parse_mode="HTML",
                reply_markup=get_main_keyboard())

            logger.info(f"💫 Stars: user={user_id}, item={item.name}x{qty}, stars={stars_amount}")

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ Payment: {e}", exc_info=True)
            await message.answer("❌ Ошибка! Обратитесь в поддержку.", reply_markup=get_main_keyboard())
        finally:
            await session.close()


@router.callback_query(lambda c: c.data == "shop_back")
async def shop_back(call: CallbackQuery) -> None:
    await shop_categories_back(call)


# ============================================================================
# ЧЁРНЫЙ РЫНОК
# ============================================================================


_bm_session_purchases: dict[int, set] = {}


def _bm_already_bought(user_id: int, item_key: str) -> bool:
    return item_key in _bm_session_purchases.get(user_id, set())


def _bm_mark_bought(user_id: int, item_key: str) -> None:
    if user_id not in _bm_session_purchases:
        _bm_session_purchases[user_id] = set()
    _bm_session_purchases[user_id].add(item_key)


def _bm_reset(user_id: int) -> None:
    _bm_session_purchases.pop(user_id, None)


@router.message(Command("blackmarket"))
async def cmd_blackmarket(message: Message) -> None:
    if not _is_private(message):
        return
    await handle_blackmarket(message)


@router.message(lambda m: m.text == "🖤 Черный рынок" and m.chat.type == "private")
async def button_blackmarket(message: Message) -> None:
    await handle_blackmarket(message)


async def handle_blackmarket(message: Message) -> None:
    user_id = message.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user:
                await message.answer("❌ /start", reply_markup=get_main_keyboard())
                return
            now = datetime.utcnow()
            today = date.today()
            if user.black_market_until and user.black_market_until > now:
                await _show_bm_menu(message, session, user)
                return
            _bm_reset(user_id)
            if user.last_market_check is not None and user.last_market_check == today:
                await _show_bm_closed(message)
                return
            user.last_market_check = today
            if random.random() < BLACK_MARKET_CHANCE:
                user.black_market_until = now + timedelta(hours=BLACK_MARKET_DURATION_HOURS)
                _bm_reset(user_id)
                await session.commit()
                await message.answer("🏚 <b>Удача!</b> 🎉\nТорговец приглашает...",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
                await _show_bm_menu(message, session, user)
            else:
                await session.commit()
                await _show_bm_closed(message)
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ ЧР: {e}", exc_info=True)
            await message.answer("❌", reply_markup=get_main_keyboard())
        finally:
            await session.close()


async def _show_bm_closed(message):
    await message.answer(
        "🏚 <b>Черный рынок скрыт в тенях...</b>\n\n"
        "Шанс <b>7.77%</b> раз в сутки.\n"
        "<i>Приходи завтра.</i>\n\n"
        "💡 <i>Крыша продаётся только здесь!</i>",
        parse_mode="HTML", reply_markup=get_main_keyboard())


async def _show_bm_menu(message, session, user):
    user_id = user.tg_id
    remaining = user.black_market_until - datetime.utcnow()
    h = max(0, int(remaining.total_seconds() // 3600))
    m = max(0, int((remaining.total_seconds() % 3600) // 60))

    items_r = await session.execute(
        select(Item).where(Item.name.in_(BM_ITEM_NAMES)).order_by(Item.price.asc()))
    items = items_r.scalars().all()

    buttons = []
    for item in items:
        price = _bm_price(item)
        bought = _bm_already_bought(user_id, item.name)
        if bought:
            buttons.append([InlineKeyboardButton(
                text=f"✅ {item.name} — КУПЛЕНО",
                callback_data="noop")])
        else:
            discount_tag = " (-25%)" if item.name not in BM_NO_DISCOUNT else ""
            buttons.append([InlineKeyboardButton(
                text=f"{item.name} — {price:,} 🪙{discount_tag}",
                callback_data=f"bm_view_{item.id}")])

    charges_bought = _bm_already_bought(user_id, BM_CHARGES_ID)
    if charges_bought:
        buttons.append([InlineKeyboardButton(
            text=f"✅ ⚡ 10 зарядов — КУПЛЕНО",
            callback_data="noop")])
    else:
        buttons.append([InlineKeyboardButton(
            text=f"⚡ 10 зарядов — {BM_CHARGES_PRICE:,} 🪙 (-50%)",
            callback_data="bm_buy_charges")])

    await message.answer(
        f"🏚 <b>Черный рынок</b>\n\n"
        f"⏳ Закроется через <b>{h}ч {m}мин</b>\n"
        f"⚠️ Каждый товар — <b>1 шт.</b> за визит\n"
        f"💰 Скидка <b>25%</b> на всё (кроме фигурки)\n"
        f"🖤 <i>Крыша — эксклюзив ЧР!</i>\n\n"
        f"Выбери товар 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(lambda c: c.data and c.data.startswith("bm_view_"))
async def bm_view(call: CallbackQuery) -> None:
    item_id = int(call.data.split("bm_view_")[1])
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user or not user.black_market_until or user.black_market_until <= datetime.utcnow():
                await call.answer("❌ Рынок закрылся!", show_alert=True)
                return

            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not item:
                await call.answer("❌", show_alert=True)
                return

            if _bm_already_bought(user_id, item.name):
                await call.answer("❌ Уже куплено!", show_alert=True)
                return

            desc = get_item_description(item)
            price = _bm_price(item)
            discount_text = ""
            if item.name not in BM_NO_DISCOUNT:
                discount_text = f"\n🏷 Скидка 25%: <s>{item.price:,}</s> → <b>{price:,} 🪙</b>"
            else:
                discount_text = f"\n💰 Цена: <b>{price:,} 🪙</b>"

            buttons = [
                [InlineKeyboardButton(
                    text=f"💰 Купить — {price:,} 🪙",
                    callback_data=f"bm_buy_{item.id}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="bm_back")],
            ]

            await call.message.edit_text(
                f"{desc}{discount_text}\n\n⚠️ <i>1 шт. за визит</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception as e:
            logger.error(f"❌ bm_view: {e}")
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("bm_buy_") and c.data != "bm_buy_charges")
async def bm_buy(call: CallbackQuery) -> None:
    item_id = int(call.data.split("bm_buy_")[1])
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not user or not item:
                await call.answer("❌", show_alert=True)
                return
            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return
            if not user.black_market_until or user.black_market_until <= datetime.utcnow():
                await call.answer("❌ Закрылся!", show_alert=True)
                return
            if _bm_already_bought(user_id, item.name):
                await call.answer("❌ Уже куплено!", show_alert=True)
                return

            price = _bm_price(item)
            if user.balance_vv < price:
                await call.answer(f"❌ Нужно {price:,} 🪙!", show_alert=True)
                return

            if not _is_activatable_item(item.name):
                ok, reason = await can_add_item(session, user_id, item, 1)
                if not ok:
                    await call.answer(f"❌ {reason}", show_alert=True)
                    return
                ok, reason = await add_item_to_inventory(session, user_id, item.id, 1)
                if not ok:
                    await call.answer(f"❌ {reason}", show_alert=True)
                    return

            user.balance_vv -= price
            _bm_mark_bought(user_id, item.name)

            boost_msg = await _handle_post_purchase(session, user, item, 1)
            await session.commit()

            extra = f"\n\n{boost_msg}" if boost_msg else ""
            saved = item.price - price
            saved_text = f"\n💵 Сэкономлено: <b>{saved:,} 🪙</b>" if saved > 0 else ""

            await call.message.edit_text(
                f"✅ <b>Сделка на чёрном рынке!</b>\n\n"
                f"{format_emoji(str(item.emoji))} <b>{item.name}</b>\n"
                f"💰 Оплачено: <b>{price:,} 🪙</b>{saved_text}\n"
                f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>{extra}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад на рынок", callback_data="bm_back")]]))
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ bm_buy: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ЧР — 10 зарядов
# ============================================================================


@router.callback_query(lambda c: c.data == "bm_buy_charges")
async def bm_buy_charges(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            if not user:
                await call.answer("❌", show_alert=True)
                return
            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return
            if not user.black_market_until or user.black_market_until <= datetime.utcnow():
                await call.answer("❌ Закрылся!", show_alert=True)
                return
            if _bm_already_bought(user_id, BM_CHARGES_ID):
                await call.answer("❌ Уже куплено!", show_alert=True)
                return
            if user.balance_vv < BM_CHARGES_PRICE:
                await call.answer(f"❌ Нужно {BM_CHARGES_PRICE:,} 🪙!", show_alert=True)
                return

            user.balance_vv -= BM_CHARGES_PRICE
            user.box_count = min(MAX_BOX_COUNT, user.box_count + BM_CHARGES_PACK)
            _bm_mark_bought(user_id, BM_CHARGES_ID)
            await session.commit()

            await call.message.edit_text(
                f"✅ <b>Сделка!</b>\n\n"
                f"⚡ <b>+{BM_CHARGES_PACK} зарядов</b>\n"
                f"💰 Оплачено: <b>{BM_CHARGES_PRICE:,} 🪙</b>\n"
                f"✊ Заряды: <b>{user.box_count}/{MAX_BOX_COUNT}</b>\n"
                f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад на рынок", callback_data="bm_back")]]))
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ bm_charges: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ЧР — НАЗАД
# ============================================================================


@router.callback_query(lambda c: c.data == "bm_back")
async def bm_back(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user or not user.black_market_until or user.black_market_until <= datetime.utcnow():
                await call.message.edit_text("🏚 <b>Рынок закрылся!</b>", parse_mode="HTML")
                await call.answer()
                return

            remaining = user.black_market_until - datetime.utcnow()
            h = max(0, int(remaining.total_seconds() // 3600))
            m = max(0, int((remaining.total_seconds() % 3600) // 60))

            items_r = await session.execute(
                select(Item).where(Item.name.in_(BM_ITEM_NAMES)).order_by(Item.price.asc()))
            items = items_r.scalars().all()

            buttons = []
            for item in items:
                price = _bm_price(item)
                bought = _bm_already_bought(user_id, item.name)
                if bought:
                    buttons.append([InlineKeyboardButton(
                        text=f"✅ {item.name} — КУПЛЕНО", callback_data="noop")])
                else:
                    discount_tag = " (-25%)" if item.name not in BM_NO_DISCOUNT else ""
                    buttons.append([InlineKeyboardButton(
                        text=f"{item.name} — {price:,} 🪙{discount_tag}",
                        callback_data=f"bm_view_{item.id}")])

            charges_bought = _bm_already_bought(user_id, BM_CHARGES_ID)
            if charges_bought:
                buttons.append([InlineKeyboardButton(
                    text=f"✅ ⚡ 10 зарядов — КУПЛЕНО", callback_data="noop")])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"⚡ 10 зарядов — {BM_CHARGES_PRICE:,} 🪙 (-50%)",
                    callback_data="bm_buy_charges")])

            await call.message.edit_text(
                f"🏚 <b>Черный рынок</b>\n\n"
                f"⏳ <b>{h}ч {m}мин</b>\n"
                f"⚠️ Каждый товар — <b>1 шт.</b> за визит\n\n"
                f"Выбери 👇",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception as e:
            logger.error(f"❌ bm_back: {e}")
        finally:
            await session.close()
    await call.answer()


@router.callback_query(lambda c: c.data == "noop")
async def noop_shop(call: CallbackQuery) -> None:
    await call.answer()
