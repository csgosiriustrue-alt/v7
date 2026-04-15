"""Админ-магазин: бесплатная покупка любых предметов без лимитов."""
import logging
from aiogram import Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select

from database import get_db
from models import User, Item, MAX_BOX_COUNT
from utils.inventory_helpers import add_item_to_inventory, activate_safe, activate_boost, activate_security, activate_roof

logger = logging.getLogger(__name__)
router = Router()

# ── Список админ ID (добавь свои) ──
ADMIN_IDS: set[int] = {
    1969951556,    # замени на свой Telegram ID
}

SAFE_ITEM_NAMES = {"Ржавый Сейф", "Элитный Сейф"}
BOOST_ITEM_NAMES = {"Журнал для взрослых", "Резиновая кукла", "Путана"}
CHARGE_ITEM_NAME = "Заряд теребления"
SECURITY_ITEM_NAMES = {"Охрана", "Крыша"}


def _is_activatable(item_name: str) -> bool:
    return (item_name in SAFE_ITEM_NAMES or item_name in BOOST_ITEM_NAMES
            or item_name in SECURITY_ITEM_NAMES or item_name == CHARGE_ITEM_NAME)


async def _admin_handle_activation(session, user, item, qty=1):
    """Обрабатывает активируемые предметы (сейфы, бусты, защита, заряды)."""
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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ============================================================================
# /ashop — список всех предметов
# ============================================================================


@router.message(Command("ashop"))
async def cmd_admin_shop(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not is_admin(message.from_user.id):
        return

    db = get_db()
    async for session in db.get_session():
        try:
            items_r = await session.execute(select(Item).order_by(Item.id))
            items = items_r.scalars().all()

            if not items:
                await message.answer("❌ Предметы не найдены.")
                return

            # Разделяем на гены и инструменты
            genes = [i for i in items if i.drop_chance > 0]
            tools = [i for i in items if i.drop_chance <= 0]

            btns = []

            if tools:
                btns.append([InlineKeyboardButton(
                    text="══ 🛠 ИНСТРУМЕНТЫ ══",
                    callback_data="noop")])
                for item in tools:
                    btns.append([InlineKeyboardButton(
                        text=f"{item.emoji} {item.name} (🪙{item.price:,})",
                        callback_data=f"ashop_item_{item.id}")])

            if genes:
                btns.append([InlineKeyboardButton(
                    text="══ 🧬 ГЕНЫ ══",
                    callback_data="noop")])
                for item in genes:
                    rarity = item.rarity.value if item.rarity else "?"
                    btns.append([InlineKeyboardButton(
                        text=f"{item.emoji} {item.name} [{rarity}] (🪙{item.price:,})",
                        callback_data=f"ashop_item_{item.id}")])

            await message.answer(
                "<b>🔧 Админ-магазин</b>\n\n"
                "<i>Бесплатно, бе�� лимитов.</i>\n"
                "Выберите предмет 👇",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

        except Exception as e:
            logger.error(f"❌ ashop: {e}", exc_info=True)
            await message.answer("❌ Ошибка")
        finally:
            await session.close()


# ============================================================================
# Выбор предмета — количество
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("ashop_item_"))
async def ashop_item_select(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа!", show_alert=True)
        return

    item_id = int(call.data.split("ashop_item_")[1])

    db = get_db()
    async for session in db.get_session():
        try:
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not item:
                await call.answer("❌ Предмет не найден!", show_alert=True)
                return

            btns = []
            for qty in [1, 5, 10, 25, 50, 100]:
                btns.append([InlineKeyboardButton(
                    text=f"📦 {qty} шт.",
                    callback_data=f"ashop_buy_{item_id}_{qty}")])

            btns.append([InlineKeyboardButton(
                text="🔢 Указать количество",
                callback_data=f"ashop_custom_{item_id}")])
            btns.append([InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="ashop_back")])

            try:
                await call.message.edit_text(
                    f"<b>🔧 Админ-магазин</b>\n\n"
                    f"{item.emoji} <b>{item.name}</b>\n"
                    f"💰 Цена: {item.price:,} 🪙\n"
                    f"⭐ {item.rarity.value if item.rarity else '?'}\n\n"
                    f"<i>Выберите количество:</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
            except Exception:
                pass
            await call.answer()

        except Exception as e:
            logger.error(f"❌ ashop_item: {e}", exc_info=True)
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# Покупка
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("ashop_buy_"))
async def ashop_buy(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа!", show_alert=True)
        return

    parts = call.data.split("_")
    item_id = int(parts[2])
    qty = int(parts[3])

    if qty <= 0 or qty > 9999:
        await call.answer("❌ Некорректное количество!", show_alert=True)
        return

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
            if not user:
                await call.answer("❌ Пользователь не найден!", show_alert=True)
                return

            activation_msg = None
            if _is_activatable(item.name):
                # Активируемые предметы — не в inventory, а сразу активация
                activation_msg = await _admin_handle_activation(session, user, item, qty)
            else:
                # Обычные предметы — в inventory
                await add_item_to_inventory(session, user_id, item_id, qty)

            await session.commit()

            extra = f"\n{activation_msg}" if activation_msg else ""
            btns = [
                [InlineKeyboardButton(
                    text=f"🔄 Ещё {item.name}",
                    callback_data=f"ashop_item_{item_id}")],
                [InlineKeyboardButton(
                    text="⬅️ К списку",
                    callback_data="ashop_back")],
            ]

            try:
                await call.message.edit_text(
                    f"✅ <b>Получено!</b>\n\n"
                    f"{item.emoji} <b>{item.name}</b> × {qty}\n"
                    f"💰 Бесплатно (админ){extra}\n\n"
                    f"<i>🔧 Админ-магазин</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
            except Exception:
                pass

            await call.answer(f"✅ +{qty} {item.name}", show_alert=False)
            logger.info(f"🔧 ADMIN SHOP: {user_id} получил {item.name} x{qty}")

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ ashop_buy: {e}", exc_info=True)
            await call.answer("❌ Ошибка!", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# Кастомное количество
# ============================================================================


_admin_custom_state: dict[int, int] = {}  # user_id -> item_id


@router.callback_query(lambda c: c.data and c.data.startswith("ashop_custom_"))
async def ashop_custom_prompt(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return

    item_id = int(call.data.split("ashop_custom_")[1])
    _admin_custom_state[call.from_user.id] = item_id

    db = get_db()
    async for session in db.get_session():
        try:
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            name = item.name if item else "?"

            try:
                await call.message.edit_text(
                    f"🔢 <b>Введите количество</b>\n\n"
                    f"Предмет: <b>{name}</b>\n\n"
                    f"Отправьте число в чат (1-9999):",
                    parse_mode="HTML")
            except Exception:
                pass
            await call.answer()
        except Exception:
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.message(lambda m: m.chat.type == "private" and m.text and m.text.isdigit()
                and m.from_user.id in _admin_custom_state)
async def ashop_custom_quantity(message: Message) -> None:
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    item_id = _admin_custom_state.pop(user_id, None)
    if not item_id:
        return

    qty = int(message.text)
    if qty <= 0 or qty > 9999:
        await message.answer("❌ От 1 до 9999!")
        return

    db = get_db()
    async for session in db.get_session():
        try:
            item_r = await session.execute(select(Item).where(Item.id == item_id))
            item = item_r.scalar_one_or_none()
            if not item:
                await message.answer("❌ Предмет не найден!")
                return

            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user:
                await message.answer("❌ Пользователь не найден!")
                return

            activation_msg = None
            if _is_activatable(item.name):
                activation_msg = await _admin_handle_activation(session, user, item, qty)
            else:
                await add_item_to_inventory(session, user_id, item_id, qty)

            await session.commit()

            extra = f"\n{activation_msg}" if activation_msg else ""
            await message.answer(
                f"✅ <b>Получено!</b>\n\n"
                f"{item.emoji} <b>{item.name}</b> × {qty}\n"
                f"💰 Бесплатно (админ){extra}\n\n"
                f"/ashop — вернуться в магазин",
                parse_mode="HTML")

            logger.info(f"🔧 ADMIN SHOP: {user_id} получил {item.name} x{qty}")

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ ashop_custom: {e}", exc_info=True)
            await message.answer("❌ Ошибка!")
        finally:
            await session.close()


# ============================================================================
# Назад к списку
# ============================================================================


@router.callback_query(lambda c: c.data == "ashop_back")
async def ashop_back(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("❌", show_alert=True)
        return
    # Просто вызываем заново
    await cmd_admin_shop(call.message)
    await call.answer()


@router.callback_query(lambda c: c.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()
