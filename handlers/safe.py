"""Хендлеры управления сейфом — ручной ввод суммы монет + апгрейд."""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from database import get_db
from models import (
    User, Item, Inventory,
    RUSTY_MAX_LEVEL, RUSTY_BASE_COIN_CAPACITY, ELITE_BASE_COIN_CAPACITY,
    RUSTY_BASE_ITEM_CAPACITY, ELITE_BASE_ITEM_CAPACITY, SAFE_CAPACITY_MULTIPLIER,
)
from utils.inventory_helpers import (
    activate_safe,
    put_item_in_safe,
    put_coins_in_safe,
    take_item_from_safe,
    take_coins_from_safe,
    return_safe_contents,
    is_gene_item,
)
from utils.keyboards import format_emoji, get_main_keyboard
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()

SAFE_COIN_AMOUNTS = [10_000, 50_000, 100_000, 250_000, 500_000]
MAX_SAFE_MANUAL_AMOUNT = 1_000_000_000  # Максимальная сумма ручного ввода


class SafeStates(StatesGroup):
    waiting_deposit_amount = State()
    waiting_withdraw_amount = State()


# ============================================================================
# УТИЛИТЫ ОТОБРАЖЕНИЯ УРОВНЯ
# ============================================================================


def _level_bar(level: int, max_level: int) -> str:
    if max_level <= 0:
        return f"⭐ {level}"
    filled = min(level, max_level)
    bar_len = 10
    progress = int((filled / max_level) * bar_len)
    bar = "🟩" * progress + "⬛" * (bar_len - progress)
    return f"{bar} {level}/{max_level}"


def _format_upgrade_info(user: User) -> str:
    level = user.get_safe_level()
    cost = user.get_upgrade_cost()

    if user.safe_type == "rusty":
        max_lvl = RUSTY_MAX_LEVEL
        type_name = "Ржавый"
    elif user.safe_type == "elite":
        max_lvl = 0
        type_name = "Элитный"
    else:
        return ""

    bar = _level_bar(level, max_lvl)

    if cost is not None:
        next_level = level + 1
        if user.safe_type == "rusty":
            next_coins = int(RUSTY_BASE_COIN_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
            next_items = int(RUSTY_BASE_ITEM_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
        else:
            next_coins = int(ELITE_BASE_COIN_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
            next_items = int(ELITE_BASE_ITEM_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
        upgrade_text = (
            f"\n\n📈 <b>Следующий уровень ({next_level}):</b>\n"
            f"  📦 {next_items} предметов | 💰 {next_coins:,} 🪙\n"
            f"  💵 Стоимость: <b>{cost:,} 🪙</b>"
        )
    else:
        upgrade_text = "\n\n🏆 <b>Максимальный уровень достигнут!</b>"

    return f"\n\n⭐ <b>Уровень:</b> {level}\n{bar}{upgrade_text}"


# ============================================================================
# Построить меню сейфа (переиспользуемый)
# ============================================================================


async def _build_safe_menu(session, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Строит текст и кнопки меню сейфа. Возвращает (text, markup)."""
    user_result = await session.execute(select(User).where(User.tg_id == user_id))
    user = user_result.scalar_one_or_none()

    if not user:
        return "❌ Вы не зарегистрированы. /start", None

    if not user.has_active_safe():
        rusty_lvl = user.safe_level_rusty
        elite_lvl = user.safe_level_elite
        saved_text = ""
        if rusty_lvl > 1:
            saved_text += f"\n💾 Сохранённый уровень Ржавого: <b>{rusty_lvl}</b>"
        if elite_lvl > 1:
            saved_text += f"\n💾 Сохранённый уровень Элитного: <b>{elite_lvl}</b>"

        return (
            "🔐 <b>У вас нет сейфа</b>\n\n"
            "Купите сейф в 🏢 Магазине или на 🖤 Черном рынке.\n\n"
            "🧰 <b>Ржавый сейф</b> — 1 предмет или 100К 🪙\n"
            f"🏦 <b>Элитный сейф</b> — 3 предмета или 700К 🪙{saved_text}"
        ), None

    safe_emoji = "🏦" if user.safe_type == "elite" else "🧰"
    safe_name = "Элитный сейф" if user.safe_type == "elite" else "Ржавый сейф"
    item_limit = user.safe_item_limit()
    coin_limit = user.safe_coin_limit()
    items_count = user.hidden_items_count()

    hidden_text = ""
    if user.hidden_item_ids:
        for hid in user.hidden_item_ids:
            item_result = await session.execute(select(Item).where(Item.id == hid))
            item = item_result.scalar_one_or_none()
            if item:
                hidden_text += f"  • 🧬 {item.name}\n"

    if user.hidden_coins > 0:
        hidden_text += f"  • 💰 {user.hidden_coins:,} 🪙\n"

    if not hidden_text:
        hidden_text = "  <i>Пусто</i>\n"

    if user.safe_type == "elite":
        health = user.elite_safe_health
        if health >= 2:
            durability_line = "🛡 Состояние: 🟢 <b>Новое (2/2)</b>"
        elif health == 1:
            durability_line = "🛡 Состояние: 🟡 <b>Повреждено (1/2)</b>"
        else:
            durability_line = "🛡 Состояние: 🔴 <b>Критическое (0/2)</b>"
    else:
        durability_line = f"❤️ Прочность: <b>{user.safe_health}/3</b>"

    upgrade_info = _format_upgrade_info(user)

    text = (
        f"{safe_emoji} <b>{safe_name}</b>\n"
        f"🔑 Код: <tg-spoiler>{user.safe_code}</tg-spoiler>\n"
        f"{durability_line}\n\n"
        f"📦 Предметы: {items_count}/{item_limit}\n"
        f"💰 Монеты: {user.hidden_coins:,}/{coin_limit:,} 🪙\n\n"
        f"<b>Содержимое:</b>\n{hidden_text}"
        f"{upgrade_info}"
    )

    buttons = [
        [InlineKeyboardButton(
            text="📥 Положить ген",
            callback_data=f"safe_put_item_{user_id}",
        )],
        [InlineKeyboardButton(
            text="📤 Забрать ген",
            callback_data=f"safe_take_item_{user_id}",
        )],
        [InlineKeyboardButton(
            text="💰 Положить монеты",
            callback_data=f"safe_put_coins_{user_id}",
        )],
        [InlineKeyboardButton(
            text="💸 Забрать монеты",
            callback_data=f"safe_take_coins_{user_id}",
        )],
    ]

    cost = user.get_upgrade_cost()
    if cost is not None:
        level = user.get_safe_level()
        buttons.append([InlineKeyboardButton(
            text=f"🔝 Улучшить (ур.{level}→{level+1}) — {cost:,} 🪙",
            callback_data=f"safeupgrade_{user_id}",
        )])
    else:
        if user.safe_type == "rusty":
            buttons.append([InlineKeyboardButton(
                text="🏆 Макс. уровень достигнут",
                callback_data="safe_noop",
            )])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ============================================================================
# /safe — ТОЛЬКО ЛС
# ============================================================================


@router.message(Command("safe"))
async def cmd_safe(message: Message) -> None:
    if message.chat.type != "private":
        return
    await show_safe_menu(message)


@router.message(lambda m: m.text == "🔐 Сейф" and m.chat.type == "private")
async def button_safe(message: Message) -> None:
    await show_safe_menu(message)


async def show_safe_menu(message: Message) -> None:
    user_id = message.from_user.id
    db = get_db()

    async for session in db.get_session():
        try:
            text, markup = await _build_safe_menu(session, user_id)
            await message.answer(
                text, parse_mode="HTML",
                reply_markup=markup or get_main_keyboard(),
            )
        except Exception as e:
            logger.error(f"❌ Ошибка /safe: {e}", exc_info=True)
            await message.answer("❌ Ошибка загрузки сейфа")
        finally:
            await session.close()


# ============================================================================
# NOOP для сейфа
# ============================================================================


@router.callback_query(lambda c: c.data == "safe_noop")
async def safe_noop(call: CallbackQuery) -> None:
    await call.answer()


# ============================================================================
# АПГРЕЙД СЕЙФА — подтверждение
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safeupgrade_"))
async def safe_upgrade_confirm_screen(call: CallbackQuery) -> None:
    """Шаг 1: Показываем экран подтверждения с деталями апгрейда."""
    user_id = int(call.data.split("safeupgrade_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if not user or not user.has_active_safe():
                await call.answer("❌ Нет сейфа!", show_alert=True)
                return

            cost = user.get_upgrade_cost()
            if cost is None:
                await call.answer("🏆 Максимальный уровень!", show_alert=True)
                return

            level = user.get_safe_level()
            next_level = level + 1
            safe_emoji = "🏦" if user.safe_type == "elite" else "🧰"
            safe_name = "Элитный" if user.safe_type == "elite" else "Ржавый"

            cur_coins = user.safe_coin_limit()
            cur_items = user.safe_item_limit()

            if user.safe_type == "rusty":
                next_coins = int(RUSTY_BASE_COIN_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
                next_items = int(RUSTY_BASE_ITEM_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
            else:
                next_coins = int(ELITE_BASE_COIN_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))
                next_items = int(ELITE_BASE_ITEM_CAPACITY * (1 + SAFE_CAPACITY_MULTIPLIER * (next_level - 1)))

            can_afford = user.balance_vv >= cost
            balance_tag = "✅" if can_afford else "❌"

            confirm_text = (
                f"🔝 <b>Улучшение {safe_emoji} {safe_name} сейфа</b>\n\n"
                f"⭐ Уровень: <b>{level}</b> → <b>{next_level}</b>\n\n"
                f"<b>📦 Предметы:</b> {cur_items} → <b>{next_items}</b>\n"
                f"<b>💰 Монеты:</b> {cur_coins:,} → <b>{next_coins:,} 🪙</b>\n\n"
                f"💵 Стоимость: <b>{cost:,} 🪙</b>\n"
                f"{balance_tag} Ваш баланс: <b>{user.balance_vv:,} 🪙</b>\n\n"
                f"⚠️ <i>Подтвердите улучшение:</i>"
            )

            buttons = []
            if can_afford:
                buttons.append([InlineKeyboardButton(
                    text=f"✅ Улучшить за {cost:,} 🪙",
                    callback_data=f"safeupgradedo_{user_id}",
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"❌ Не хватает {cost - user.balance_vv:,} 🪙",
                    callback_data="safe_noop",
                )])
            buttons.append([InlineKeyboardButton(
                text="⬅️ Назад к сейфу",
                callback_data=f"safeback_{user_id}",
            )])

            await call.message.edit_text(
                confirm_text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await call.answer()

        except Exception as e:
            logger.error(f"❌ safe_upgrade_confirm: {e}", exc_info=True)
            await call.answer("❌ Ошибка!", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# АПГРЕЙД СЕЙФА — выполнение
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safeupgradedo_"))
async def safe_upgrade_execute(call: CallbackQuery) -> None:
    """Шаг 2: Выполняем апгрейд после подтверждения."""
    user_id = int(call.data.split("safeupgradedo_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_result.scalar_one_or_none()

            if not user or not user.has_active_safe():
                await call.answer("❌ Нет сейфа!", show_alert=True)
                return

            old_level = user.get_safe_level()
            old_coin_limit = user.safe_coin_limit()
            old_item_limit = user.safe_item_limit()

            success, msg, cost = user.do_upgrade_safe()

            if not success:
                await call.answer(f"❌ {msg}", show_alert=True)
                return

            await session.commit()

            new_level = user.get_safe_level()
            new_coin_limit = user.safe_coin_limit()
            new_item_limit = user.safe_item_limit()

            safe_emoji = "🏦" if user.safe_type == "elite" else "🧰"
            safe_name = "Элитный" if user.safe_type == "elite" else "Ржавый"

            next_cost = user.get_upgrade_cost()
            next_text = ""
            if next_cost is not None:
                next_text = f"\n\n💵 Следующий апгрейд: <b>{next_cost:,} 🪙</b>"
            elif user.safe_type == "rusty":
                next_text = "\n\n🏆 Максимальный уровень!"

            result_text = (
                f"🔝 <b>Сейф улучшен!</b>\n\n"
                f"{safe_emoji} <b>{safe_name} сейф</b>\n"
                f"⭐ Уровень: <b>{old_level}</b> → <b>{new_level}</b>\n\n"
                f"📦 Предметы: {old_item_limit} → <b>{new_item_limit}</b>\n"
                f"💰 Монеты: {old_coin_limit:,} → <b>{new_coin_limit:,} 🪙</b>\n\n"
                f"💵 Потрачено: <b>{cost:,} 🪙</b>\n"
                f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>"
                f"{next_text}"
            )

            buttons = []
            if next_cost is not None:
                buttons.append([InlineKeyboardButton(
                    text=f"🔝 Ещё апгрейд (ур.{new_level}→{new_level+1}) — {next_cost:,} 🪙",
                    callback_data=f"safeupgrade_{user_id}",
                )])
            buttons.append([InlineKeyboardButton(
                text="⬅️ Назад к сейфу",
                callback_data=f"safeback_{user_id}",
            )])

            await call.message.edit_text(
                result_text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await call.answer(f"✅ Ур. {new_level}!", show_alert=False)

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_upgrade_do: {e}", exc_info=True)
            await call.answer("❌ Ошибка!", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ПОЛОЖИТЬ ПРЕДМЕТ (только гены)
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safe_put_item_"))
async def safe_put_item_list(call: CallbackQuery) -> None:
    user_id = int(call.data.split("safe_put_item_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if not user or not user.has_active_safe():
                await call.answer("❌ Нет сейфа!", show_alert=True)
                return

            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            if user.hidden_items_count() >= user.safe_item_limit():
                await call.answer("❌ Сейф заполнен!", show_alert=True)
                return

            inv_result = await session.execute(
                select(Inventory).where(Inventory.user_id == user_id)
            )
            inventory = inv_result.scalars().all()

            hideable = [
                inv for inv in inventory
                if inv.item.drop_chance > 0
                and inv.item.price > 0
                and inv.quantity > 0
            ]

            if not hideable:
                await call.answer("❌ Нет генов для укрытия!", show_alert=True)
                return

            hideable.sort(key=lambda inv: inv.item.price, reverse=True)

            buttons = []
            for inv in hideable[:8]:
                item = inv.item
                buttons.append([InlineKeyboardButton(
                    text=f"🧬 {item.name} ({item.price:,} 🪙)",
                    callback_data=f"safe_hide_{user_id}_{item.id}",
                )])

            buttons.append([InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"safeback_{user_id}",
            )])

            await call.message.edit_text(
                "📥 <b>Выберите ген для укрытия:</b>\n\n"
                "<i>В сейф можно класть только Гены.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await call.answer()

        except Exception as e:
            logger.error(f"❌ safe_put_item: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("safe_hide_"))
async def safe_hide_item(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        user_id = int(parts[2])
        item_id = int(parts[3])
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка", show_alert=True)
        return

    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if not user:
                await call.answer("❌ Пользователь не найден!", show_alert=True)
                return

            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            success, msg = await put_item_in_safe(session, user, item_id)
            if success:
                await session.commit()
                await call.message.edit_text(
                    f"✅ {msg}", parse_mode="HTML",
                )
            else:
                await call.answer(f"❌ {msg}", show_alert=True)

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_hide: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ЗАБРАТЬ ПРЕДМЕТ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safe_take_item_"))
async def safe_take_item_list(call: CallbackQuery) -> None:
    user_id = int(call.data.split("safe_take_item_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            if not user or not user.hidden_item_ids:
                await call.answer("❌ Сейф пуст!", show_alert=True)
                return

            buttons = []
            for hid in user.hidden_item_ids:
                item_result = await session.execute(select(Item).where(Item.id == hid))
                item = item_result.scalar_one_or_none()
                if item:
                    buttons.append([InlineKeyboardButton(
                        text=f"📤 🧬 {item.name}",
                        callback_data=f"safe_retrieve_{user_id}_{item.id}",
                    )])

            buttons.append([InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"safeback_{user_id}",
            )])

            await call.message.edit_text(
                "📤 <b>Выберите предмет для изъятия:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await call.answer()

        except Exception as e:
            logger.error(f"❌ safe_take_item: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("safe_retrieve_"))
async def safe_retrieve_item(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        user_id = int(parts[2])
        item_id = int(parts[3])
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка", show_alert=True)
        return

    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            success, msg = await take_item_from_safe(session, user, item_id)
            if success:
                await session.commit()
                await call.message.edit_text(f"✅ {msg}", parse_mode="HTML")
            else:
                await call.answer(f"❌ {msg}", show_alert=True)

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_retrieve: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ПОЛОЖИТЬ МОНЕТЫ — кнопки + ручной ввод
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safe_put_coins_"))
async def safe_put_coins_menu(call: CallbackQuery, state: FSMContext) -> None:
    user_id = int(call.data.split("safe_put_coins_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if not user or not user.has_active_safe():
                await call.answer("❌ Нет сейфа!", show_alert=True)
                return

            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            space = user.safe_coin_limit() - user.hidden_coins

            buttons = []
            for amount in SAFE_COIN_AMOUNTS:
                if amount <= space and amount <= user.balance_vv:
                    buttons.append([InlineKeyboardButton(
                        text=f"💰 {amount:,} 🪙",
                        callback_data=f"safe_deposit_{user_id}_{amount}",
                    )])

            buttons.append([InlineKeyboardButton(
                text="✏️ Ввести сумму вручную",
                callback_data=f"safe_deposit_manual_{user_id}",
            )])

            buttons.append([InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"safeback_{user_id}",
            )])

            await call.message.edit_text(
                f"💰 <b>Положить монеты</b>\n\n"
                f"Баланс: <b>{user.balance_vv:,} 🪙</b>\n"
                f"В сейфе: <b>{user.hidden_coins:,}/{user.safe_coin_limit():,} 🪙</b>\n"
                f"Свободно: <b>{space:,} 🪙</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await call.answer()

        except Exception as e:
            logger.error(f"❌ safe_put_coins: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("safe_deposit_manual_"))
async def safe_deposit_manual_start(call: CallbackQuery, state: FSMContext) -> None:
    user_id = int(call.data.split("safe_deposit_manual_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()
    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()
            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return
        except Exception as e:
            logger.error(f"❌ safe_deposit_manual_start: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
            return
        finally:
            await session.close()

    await state.set_state(SafeStates.waiting_deposit_amount)
    await state.update_data(user_id=user_id)

    await call.message.edit_text(
        "✏️ <b>Введите сумму для внесения в сейф:</b>\n\n"
        "<i>Отправьте число в чат (например: 75000)</i>\n"
        "<i>Для отмены отправьте: отмена</i>",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(SafeStates.waiting_deposit_amount, F.chat.type == "private")
async def safe_deposit_manual_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data.get("user_id")

    if message.from_user.id != user_id:
        return

    text = message.text.strip().lower()
    if text in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard())
        return

    if not text.isdigit() or int(text) <= 0:
        await message.answer("❌ Введите положительное число!\n<i>Для отмены: отмена</i>", parse_mode="HTML")
        return

    amount = int(text)
    if amount > MAX_SAFE_MANUAL_AMOUNT:
        await message.answer("❌ Слишком большая сумма!", reply_markup=get_main_keyboard())
        return
    await state.clear()

    db = get_db()
    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_result.scalar_one_or_none()

            if not user or not user.has_active_safe():
                await message.answer("❌ Нет сейфа!", reply_markup=get_main_keyboard())
                return

            if user.is_being_robbed:
                await message.answer(
                    "⛔ <b>Вы не можете распоряжаться финансами, пока вас грабят!</b>",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
                return

            success, msg = await put_coins_in_safe(session, user, amount)
            if success:
                await session.commit()
                await message.answer(
                    f"✅ {msg}\n\n"
                    f"💰 В сейфе: <b>{user.hidden_coins:,}/{user.safe_coin_limit():,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
            else:
                await message.answer(f"❌ {msg}", reply_markup=get_main_keyboard())

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_deposit_manual: {e}")
            await message.answer("❌ Ошибка", reply_markup=get_main_keyboard())
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("safe_deposit_") and "_manual_" not in c.data)
async def safe_deposit_coins(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        user_id = int(parts[2])
        amount = int(parts[3])
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка", show_alert=True)
        return

    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return
    if amount <= 0:
        await call.answer("❌ Некорректная сумма!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_result.scalar_one_or_none()

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            success, msg = await put_coins_in_safe(session, user, amount)
            if success:
                await session.commit()
                await call.message.edit_text(
                    f"✅ {msg}\n\n"
                    f"💰 В сейфе: <b>{user.hidden_coins:,}/{user.safe_coin_limit():,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>",
                    parse_mode="HTML")
            else:
                await call.answer(f"❌ {msg}", show_alert=True)

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_deposit: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ЗАБРАТЬ МОНЕТЫ — кнопки + ручной ввод
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safe_take_coins_"))
async def safe_take_coins_menu(call: CallbackQuery, state: FSMContext) -> None:
    user_id = int(call.data.split("safe_take_coins_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            if not user or user.hidden_coins <= 0:
                await call.answer("❌ В сейфе нет монет!", show_alert=True)
                return

            buttons = []
            for amount in SAFE_COIN_AMOUNTS:
                if amount <= user.hidden_coins:
                    buttons.append([InlineKeyboardButton(
                        text=f"💸 {amount:,} 🪙",
                        callback_data=f"safe_withdraw_{user_id}_{amount}",
                    )])

            buttons.append([InlineKeyboardButton(
                text=f"📦 Забрать всё ({user.hidden_coins:,} 🪙)",
                callback_data=f"safe_withdraw_{user_id}_{user.hidden_coins}",
            )])

            buttons.append([InlineKeyboardButton(
                text="✏️ Ввести сумму вручную",
                callback_data=f"safe_withdraw_manual_{user_id}",
            )])

            buttons.append([InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"safeback_{user_id}",
            )])

            await call.message.edit_text(
                f"💸 <b>Забрать монеты</b>\n\n"
                f"В сейфе: <b>{user.hidden_coins:,} 🪙</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await call.answer()

        except Exception as e:
            logger.error(f"❌ safe_take_coins: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("safe_withdraw_manual_"))
async def safe_withdraw_manual_start(call: CallbackQuery, state: FSMContext) -> None:
    user_id = int(call.data.split("safe_withdraw_manual_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return

    db = get_db()
    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_result.scalar_one_or_none()
            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return
        except Exception as e:
            logger.error(f"❌ safe_withdraw_manual_start: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
            return
        finally:
            await session.close()

    await state.set_state(SafeStates.waiting_withdraw_amount)
    await state.update_data(user_id=user_id)

    await call.message.edit_text(
        "✏️ <b>Введите сумму для изъятия из сейфа:</b>\n\n"
        "<i>Отправьте число в чат (например: 75000)</i>\n"
        "<i>Для отмены отправьте: отмена</i>",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(SafeStates.waiting_withdraw_amount, F.chat.type == "private")
async def safe_withdraw_manual_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data.get("user_id")

    if message.from_user.id != user_id:
        return

    text = message.text.strip().lower()
    if text in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard())
        return

    if not text.isdigit() or int(text) <= 0:
        await message.answer("❌ Введите положительное число!\n<i>Для отмены: отмена</i>", parse_mode="HTML")
        return

    amount = int(text)
    if amount > MAX_SAFE_MANUAL_AMOUNT:
        await message.answer("❌ Слишком большая сумма!", reply_markup=get_main_keyboard())
        return
    await state.clear()

    db = get_db()
    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_result.scalar_one_or_none()

            if user and user.is_being_robbed:
                await message.answer(
                    "⛔ <b>Вы не можете распоряжаться финансами, пока вас грабят!</b>",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
                return

            if not user or not user.has_active_safe():
                await message.answer("❌ Нет сейфа!", reply_markup=get_main_keyboard())
                return

            success, msg = await take_coins_from_safe(session, user, amount)
            if success:
                await session.commit()
                await message.answer(
                    f"✅ {msg}\n\n"
                    f"💰 В сейфе: <b>{user.hidden_coins:,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
            else:
                await message.answer(f"❌ {msg}", reply_markup=get_main_keyboard())

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_withdraw_manual: {e}")
            await message.answer("❌ Ошибка", reply_markup=get_main_keyboard())
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("safe_withdraw_") and "_manual_" not in c.data)
async def safe_withdraw_coins(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        user_id = int(parts[2])
        amount = int(parts[3])
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка", show_alert=True)
        return

    if call.from_user.id != user_id:
        await call.answer("❌ Это не ваш сейф!", show_alert=True)
        return
    if amount <= 0:
        await call.answer("❌ Некорректная сумма!", show_alert=True)
        return

    db = get_db()

    async for session in db.get_session():
        try:
            user_result = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_result.scalar_one_or_none()

            if user and user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return

            success, msg = await take_coins_from_safe(session, user, amount)
            if success:
                await session.commit()
                await call.message.edit_text(
                    f"✅ {msg}\n\n"
                    f"💰 В сейфе: <b>{user.hidden_coins:,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>",
                    parse_mode="HTML")
            else:
                await call.answer(f"❌ {msg}", show_alert=True)

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ safe_withdraw: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# КНОПКА НАЗАД — возвращает в меню сейфа
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safeback_"))
async def safe_back(call: CallbackQuery) -> None:
    user_id = int(call.data.split("safeback_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌", show_alert=True)
        return

    db = get_db()
    async for session in db.get_session():
        try:
            text, markup = await _build_safe_menu(session, user_id)
            await call.message.edit_text(
                text, parse_mode="HTML",
                reply_markup=markup,
            )
            await call.answer()
        except Exception as e:
            logger.error(f"❌ safe_back: {e}", exc_info=True)
            await call.message.delete()
            await call.answer()
        finally:
            await session.close()
