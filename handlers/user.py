"""Хендлеры пользователя: профиль, инвентарь, ломбард, донат, уровни."""
import logging
from datetime import datetime
from aiogram import Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select, delete

from database import get_db
from models import User, Inventory, Item, GroupChat, MAX_BOX_COUNT, BOX_REFILL_HOURS, MAX_DAILY_BETS
from utils.keyboards import get_main_keyboard, format_emoji, format_emoji_button
from utils.formatters import format_balance
from utils.pot_event import track_chat_activity, check_pot_explosion
from utils.box_utils import update_user_boxes, get_time_until_next_box
from utils.inventory_helpers import add_item_to_inventory
from utils.levels import format_level_line, add_xp, grant_level_rewards

logger = logging.getLogger(__name__)
router = Router()

WEALTH_TAX_THRESHOLD = 100_000
WEALTH_TAX_RATE = 0.05

TOOL_NAMES = {
    "Охрана", "Крыша", "Липкие Перчатки", "Рентген", "Стетоскоп",
    "Отмычка", "Лом", "Адвокат", "Ржавый Сейф", "Элитный Сейф",
    "Журнал для взрослых", "Резиновая кукла", "Путана",
    "Durov's Figure", "Заряд теребления",
}

PHANTOM_ACTIVATABLE_NAMES = {
    "Ржавый Сейф", "Элитный Сейф",
    "Охрана", "Крыша",
    "Журнал для взрослых", "Резиновая кукла", "Путана",
}

STARTER_ITEMS = ["Адвокат", "Стетоскоп", "Отмычка"]


def _is_private(msg):
    return msg.chat.type == "private"


def _is_tool(item_name: str) -> bool:
    return item_name in TOOL_NAMES


def _is_sellable(inv) -> bool:
    return (
        not inv.item.is_starter
        and inv.item.price > 0
        and inv.quantity > 0
        and inv.item.name not in TOOL_NAMES
    )


def _build_lvl_text(new_levels: list[int]) -> str:
    """Формирует текст поздравлений с уровнями."""
    lvl_text = ""
    for lvl in new_levels:
        lvl_text += (
            f"\n\n🆙 <b>Уровень повышен!</b> "
            f"Теперь вы игрок <b>{lvl}</b> уровня. "
            f"Ваше влияние в городе растёт!"
        )
    return lvl_text


# ============================================================================
# ПРОФИЛЬ
# ============================================================================


async def build_profile_text(user_id: int, session) -> str:
    user_r = await session.execute(select(User).where(User.tg_id == user_id))
    user = user_r.scalar_one_or_none()
    if not user:
        return "❌ Игрок не найден. /start в ЛС бота."

    now = datetime.utcnow()
    await update_user_boxes(user)

    # ── Уровень ──
    level_line = format_level_line(user)

    refill_h = user.get_refill_hours()
    next_box_text = ""
    if user.box_count < MAX_BOX_COUNT:
        h, m = get_time_until_next_box(user.last_refill_at, refill_h)
        if h > 0 or m > 0:
            next_box_text = f"\n⏳ Перезарядка: {h}ч {m}мин"
            if refill_h == 2:
                next_box_text += " (🔞 ускорено)"

    jail_status = "🟢 На свободе"
    if user.jail_until and user.jail_until > now:
        mins = max(1, int((user.jail_until - now).total_seconds() // 60))
        jail_status = f"🔒 В тюрьме ({mins} мин.)"

    security = "🛡️ Да" if user.is_security_active() else "❌"
    roof = "🕴 Да" if user.is_roof_active() else "❌"
    safe_text = "❌"
    if user.has_active_safe():
        level = user.get_safe_level()
        if user.safe_type == "elite":
            health = user.elite_safe_health
            if health >= 2:
                safe_text = f"🏦 Элитный ур.{level} (🟢 2/2)"
            elif health == 1:
                safe_text = f"🏦 Элитный ур.{level} (🟡 1/2)"
            else:
                safe_text = f"🏦 Элитный ур.{level} (🔴 0/2)"
        else:
            safe_text = f"🧰 Ржавый ур.{level} (❤️ {user.safe_health}/3)"

    inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
    inv_items = inv_r.scalars().all()

    gene_items = [inv for inv in inv_items if inv.item.drop_chance > 0]
    total_genes = sum(inv.quantity for inv in gene_items)
    genes_value = sum(inv.item.price * inv.quantity for inv in gene_items)

    tool_items = [inv for inv in inv_items if inv.item.drop_chance <= 0]
    total_tools = sum(inv.quantity for inv in tool_items)

    boosts = user.active_boosts_text()
    mult = user.get_drop_multiplier()
    mult_text = f" (x{mult:.0f})" if mult > 1 else ""

    can_bet, bets_remaining = user.check_casino_limit()
    casino_text = f"🎰 Ставки: <b>{bets_remaining}/{MAX_DAILY_BETS}</b>"

    return (
        f"<b>👤 {user.username}</b>\n"
        f"{level_line}\n\n"
        f"{format_balance(user.balance_vv, user.balance_stars)}\n\n"
        f"✊ Заряды: <b>{user.box_count}/{MAX_BOX_COUNT}</b>{next_box_text}\n"
        f"🧬 Генофонд: <b>{total_genes}</b> шт. (≈{genes_value:,} 🪙){mult_text}\n"
        f"🛠 Инструменты: <b>{total_tools}</b> шт.\n"
        f"🚀 Бусты: {boosts}\n"
        f"{casino_text}\n\n"
        f"<b>Статусы:</b>\n{jail_status}\n"
        f"Охрана: {security} | Крыша: {roof}\nСейф: {safe_text}"
    )


async def build_inventory_text(user_id: int, session) -> str:
    inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
    inv_items = inv_r.scalars().all()
    if not inv_items:
        return "📦 <b>Рюкзак пуст...</b>"

    tools = []
    genes = []
    for inv in inv_items:
        item = inv.item
        # Пропускаем фантомные активируемые предметы (они должны быть в полях User, не в inventory)
        if item.name in PHANTOM_ACTIVATABLE_NAMES:
            continue
        line = f"{format_emoji(str(item.emoji))} <b>{item.name}</b> — {inv.quantity} шт."
        if item.drop_chance > 0:
            price_tag = f" (💰 {item.price:,} 🪙/шт)" if item.price > 0 else ""
            genes.append(f"{format_emoji(str(item.emoji))} <b>{item.name}</b> — {inv.quantity} шт.{price_tag}")
        else:
            tools.append(line)

    total_genes = sum(inv.quantity for inv in inv_items if inv.item.drop_chance > 0)
    genes_value = sum(inv.item.price * inv.quantity for inv in inv_items if inv.item.drop_chance > 0)

    text = "<b>📦 Инвентарь</b>\n\n"
    text += "<b>🛠 Инструменты:</b>\n"
    text += "\n".join(tools) if tools else "<i>Пусто</i>"
    text += f"\n\n<b>🧬 Генофонд ({total_genes} шт. ≈ {genes_value:,} 🪙):</b>\n"
    text += "\n".join(genes) if genes else "<i>Пусто</i>"
    return text


# ============================================================================
# Стартовые расходники
# ============================================================================


async def _give_starter_items(session, user_id: int) -> str:
    given = []
    for item_name in STARTER_ITEMS:
        item_r = await session.execute(select(Item).where(Item.name == item_name))
        item = item_r.scalar_one_or_none()
        if item:
            ok, _ = await add_item_to_inventory(session, user_id, item.id, 1)
            if ok:
                given.append(f"  • {item.emoji} {item.name}")
    if given:
        return "\n🎁 <b>Стартовый набор:</b>\n" + "\n".join(given)
    return ""


# ============================================================================
# УТИЛИТА: Использование адвоката
# ============================================================================


async def use_lawyer(session, user_id: int) -> tuple[bool, str]:
    user_r = await session.execute(select(User).where(User.tg_id == user_id))
    user = user_r.scalar_one_or_none()
    if not user:
        return False, "❌ Пользователь не найден!"

    if not user.jail_until or user.jail_until <= datetime.utcnow():
        return False, "✅ Вы уже на свободе!"

    inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
    inv_all = inv_r.scalars().all()
    lawyer = next((inv for inv in inv_all if inv.item.name == "Адвокат" and inv.quantity > 0), None)

    if not lawyer:
        return False, "❌ Нет Адвоката в инвентаре!"

    if lawyer.quantity <= 1:
        await session.delete(lawyer)
    else:
        lawyer.quantity -= 1

    user.jail_until = None
    return True, "💼 <b>Адвокат вас вытащил!</b> Вы ��вободны! ✅"


# ============================================================================
# /start
# ============================================================================


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_private(message):
        return
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    db = get_db()
    async for session in db.get_session():
        try:
            result = await session.execute(select(User).where(User.tg_id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.username = username
                await session.commit()
                await message.answer(
                    f"👋 С возвращением, <b>{username}</b>!\n\n"
                    f"{format_balance(user.balance_vv, user.balance_stars)}",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
            else:
                new_user = User(tg_id=user_id, username=username, balance_vv=0, balance_stars=0)
                session.add(new_user)
                await session.flush()
                starter_text = await _give_starter_items(session, user_id)
                await session.commit()
                await message.answer(
                    f"🧬 <b>Привет, {username}!</b>\n\n"
                    f"Добро пожаловать в <b>Gift Heist</b> 💰\n"
                    f"Теребите, торгуйте генами, грабьте чужие сейфы!"
                    f"{starter_text}",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ /start: {e}", exc_info=True)
            await message.answer("❌ Ошибка.", reply_markup=get_main_keyboard())
        finally:
            await session.close()


# ============================================================================
# /profile
# ============================================================================


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if not _is_private(message):
        return
    await _handle_profile_dm(message)


@router.message(lambda m: m.text == "👤 Профиль" and m.chat.type == "private")
async def button_profile(message: Message) -> None:
    await _handle_profile_dm(message)


async def _handle_profile_dm(message: Message) -> None:
    user_id = message.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            text = await build_profile_text(user_id, session)

            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()

            buttons_rows = []
            if user and user.jail_until and user.jail_until > datetime.utcnow():
                inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
                inv_all = inv_r.scalars().all()
                has_lawyer = any(inv.item.name == "Адвокат" and inv.quantity > 0 for inv in inv_all)
                lawyer_count = sum(inv.quantity for inv in inv_all if inv.item.name == "Адвокат" and inv.quantity > 0)
                if has_lawyer:
                    text += f"\n\n💼 <i>Адвокат доступен! ({lawyer_count} шт.)</i>"
                    buttons_rows.append([InlineKeyboardButton(text="💼 Вызвать адвоката",
                        callback_data=f"lawyer_{user_id}")])

            if user:
                notif_label = '🔔 Уведомления: ВКЛ' if user.notifications_enabled else '🔕 Уведомления: ВЫКЛ'
                buttons_rows.append([InlineKeyboardButton(text=notif_label,
                    callback_data=f"toggle_notif_{user_id}")])

            markup = InlineKeyboardMarkup(inline_keyboard=buttons_rows) if buttons_rows else None
            await message.answer(text, parse_mode="HTML",
                reply_markup=markup or get_main_keyboard())
        except Exception as e:
            logger.error(f"❌ Профиль: {e}", exc_info=True)
            await message.answer("❌ Ошибка")
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("lawyer_"))
async def lawyer_callback(call: CallbackQuery) -> None:
    user_id = int(call.data.split("lawyer_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Не ваш!", show_alert=True)
        return
    db = get_db()
    async for session in db.get_session():
        try:
            ok, msg = await use_lawyer(session, user_id)
            if ok:
                await session.commit()
                new_text = await build_profile_text(user_id, session)
                new_text += f"\n\n{msg}"
                try:
                    await call.message.edit_text(new_text, parse_mode="HTML")
                except Exception:
                    await call.message.answer(msg, parse_mode="HTML")
                await call.answer("✅ Свободны!", show_alert=False)
            else:
                await call.answer(msg, show_alert=True)
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ Адвокат: {e}", exc_info=True)
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("toggle_notif_"))
async def toggle_notifications(call: CallbackQuery) -> None:
    user_id = int(call.data.split("toggle_notif_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Не ваш профиль!", show_alert=True)
        return
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user:
                await call.answer("❌ Пользователь не найден!", show_alert=True)
                return
            user.notifications_enabled = not user.notifications_enabled
            await session.commit()
            new_text = await build_profile_text(user_id, session)
            notif_label = '🔔 Уведомления: ВКЛ' if user.notifications_enabled else '🔕 Уведомления: ВЫКЛ'
            buttons_rows = []
            if user.jail_until and user.jail_until > datetime.utcnow():
                inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
                inv_all = inv_r.scalars().all()
                has_lawyer = any(inv.item.name == "Адвокат" and inv.quantity > 0 for inv in inv_all)
                lawyer_count = sum(inv.quantity for inv in inv_all if inv.item.name == "Адвокат" and inv.quantity > 0)
                if has_lawyer:
                    new_text += f"\n\n💼 <i>Адвокат доступен! ({lawyer_count} шт.)</i>"
                    buttons_rows.append([InlineKeyboardButton(text="💼 Вызвать адвоката", callback_data=f"lawyer_{user_id}")])
            buttons_rows.append([InlineKeyboardButton(text=notif_label, callback_data=f"toggle_notif_{user_id}")])
            markup = InlineKeyboardMarkup(inline_keyboard=buttons_rows)
            try:
                await call.message.edit_text(new_text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
            status = "включены" if user.notifications_enabled else "выключены"
            await call.answer(f"✅ Уведомления {status}!", show_alert=False)
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ toggle_notif: {e}", exc_info=True)
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ИНВЕНТАРЬ
# ============================================================================


@router.message(Command("inventory"))
async def cmd_inventory(message: Message) -> None:
    if not _is_private(message):
        return
    await _handle_inventory_dm(message)


@router.message(lambda m: m.text == "📦 Инвентарь" and m.chat.type == "private")
async def button_inventory(message: Message) -> None:
    await _handle_inventory_dm(message)


async def _handle_inventory_dm(message: Message) -> None:
    user_id = message.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            # Чистим фантомные активируемые предметы из inventory через JOIN на уровне БД
            phantom_subq = select(Item.id).where(Item.name.in_(PHANTOM_ACTIVATABLE_NAMES))
            await session.execute(
                delete(Inventory).where(
                    Inventory.user_id == user_id,
                    Inventory.item_id.in_(phantom_subq)))
            await session.flush()

            text = await build_inventory_text(user_id, session)
            inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
            inv_items = inv_r.scalars().all()
            sellable = [inv for inv in inv_items if _is_sellable(inv)]
            buttons = None
            if sellable:
                buttons = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💰 Продать", callback_data=f"sell_items_{user_id}")],
                    [InlineKeyboardButton(text="📦 Продать всё",
                        callback_data=f"sellall_menu_{user_id}")],
                ])
            await message.answer(text, parse_mode="HTML",
                reply_markup=buttons or get_main_keyboard())
        except Exception as e:
            logger.error(f"❌ Инвентарь: {e}")
            await message.answer("❌ Ошибка", reply_markup=get_main_keyboard())
        finally:
            await session.close()


# ============================================================================
# ЛОМБАРД
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("sell_items_"))
async def sell_items_list(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
            inv_items = inv_r.scalars().all()
            sellable = [inv for inv in inv_items if _is_sellable(inv)]
            if not sellable:
                await call.answer("❌ Нечего продавать!", show_alert=True)
                return
            sellable.sort(key=lambda inv: inv.item.price, reverse=True)
            buttons = [[InlineKeyboardButton(
                text=f"{inv.item.name} ({inv.quantity}шт) — {inv.item.price:,}🪙/шт",
                callback_data=f"sell_item_{inv.item.id}")] for inv in sellable]
            buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sell_cancel")])
            try:
                await call.message.edit_text("<b>💰 Выберите предмет:</b>", parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except Exception:
                pass
        except Exception as e:
            logger.error(f"❌ sell: {e}")
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("sell_item_"))
async def sell_item_confirm(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    item_id = int(call.data.split("sell_item_")[1])
    db = get_db()
    async for session in db.get_session():
        try:
            inv_r = await session.execute(
                select(Inventory).where(Inventory.user_id == user_id, Inventory.item_id == item_id))
            inv = inv_r.scalar_one_or_none()
            if not inv:
                await call.answer("❌!", show_alert=True)
                return
            if inv.item.name in TOOL_NAMES:
                await call.answer("❌ Инструменты нельзя продать!", show_alert=True)
                return

            item = inv.item
            qty = inv.quantity
            buttons = []
            for q in [1, 5, 10]:
                if q <= qty:
                    total = item.price * q
                    tax_t = f" (налог {int(total * WEALTH_TAX_RATE):,})" if total > WEALTH_TAX_THRESHOLD else ""
                    buttons.append([InlineKeyboardButton(
                        text=f"🔸 {q} шт. = {total:,}🪙{tax_t}",
                        callback_data=f"sell_exec_{item_id}_{q}")])
            total_all = item.price * qty
            tax_a = f" (налог {int(total_all * WEALTH_TAX_RATE):,})" if total_all > WEALTH_TAX_THRESHOLD else ""
            buttons.append([InlineKeyboardButton(
                text=f"📦 Все {qty} шт. = {total_all:,}🪙{tax_a}",
                callback_data=f"sell_exec_{item_id}_all")])
            buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sell_items_{user_id}")])
            try:
                await call.message.edit_text(
                    f"<b>💰 {item.name}</b>\n📦 {qty} шт. | 💵 {item.price:,} 🪙/шт\n\n"
                    f"⚠️ <i>Продажа >100К 🪙 → налог 5%</i>",
                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except Exception:
                pass
        except Exception as e:
            logger.error(f"❌ sell_confirm: {e}")
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("sell_exec_"))
async def sell_execute(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    parts = call.data.split("_")
    item_id = int(parts[2])
    qty_str = parts[3]
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            inv_r = await session.execute(
                select(Inventory).where(Inventory.user_id == user_id, Inventory.item_id == item_id))
            inv = inv_r.scalar_one_or_none()
            if not user or not inv:
                await call.answer("❌!", show_alert=True)
                return
            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return
            if inv.item.name in TOOL_NAMES:
                await call.answer("❌ Инструменты нельзя продать!", show_alert=True)
                return

            sell_qty = inv.quantity if qty_str == "all" else int(qty_str)
            if sell_qty <= 0 or sell_qty > inv.quantity:
                await call.answer("❌!", show_alert=True)
                return
            gross = inv.item.price * sell_qty
            tax = int(gross * WEALTH_TAX_RATE) if gross > WEALTH_TAX_THRESHOLD else 0
            net = gross - tax

            user.balance_vv += net

            # ── XP за продажу ──
            old_level = user.level
            new_levels = add_xp(user, net)

            if sell_qty >= inv.quantity:
                await session.delete(inv)
            else:
                inv.quantity -= sell_qty
            await session.commit()
            if new_levels:
                await grant_level_rewards(call.bot, session, user, old_level, new_levels)
                await session.commit()

            tax_text = f"\n💸 Налог (5%): <b>-{tax:,} 🪙</b>" if tax > 0 else ""
            lvl_text = _build_lvl_text(new_levels)

            try:
                await call.message.edit_text(
                    f"✅ <b>Продано!</b>\n\n🧬 {inv.item.name} × {sell_qty}\n"
                    f"💰 Сумма: {gross:,} 🪙{tax_text}\n💵 Получено: <b>{net:,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>{lvl_text}",
                    parse_mode="HTML")
            except Exception:
                pass
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ sell_exec: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("sellall_menu_"))
async def sellall_menu(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
            inv_items = inv_r.scalars().all()
            sellable = [inv for inv in inv_items if _is_sellable(inv)]
            if not sellable:
                await call.answer("❌ Нечего!", show_alert=True)
                return
            sellable.sort(key=lambda inv: inv.item.price, reverse=True)
            buttons = []
            for inv in sellable:
                total = inv.item.price * inv.quantity
                tag = " 💸" if total > WEALTH_TAX_THRESHOLD else ""
                buttons.append([InlineKeyboardButton(
                    text=f"{format_emoji_button(str(inv.item.emoji))} {inv.item.name} × {inv.quantity} = {total:,}🪙{tag}",
                    callback_data=f"sellall_confirm_{inv.item.id}")])
            buttons.append([InlineKeyboardButton(text="🔥 ПРОДАТЬ ВСЁ",
                callback_data=f"sellall_everything_{user_id}")])
            buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sell_cancel")])
            try:
                await call.message.edit_text(
                    "<b>📦 Продать всё</b>\n\n<i>Инструменты не продаются</i>\n💸 = налог 5%",
                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except Exception:
                pass
        except Exception as e:
            logger.error(f"❌ sellall: {e}")
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("sellall_confirm_"))
async def sellall_confirm(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    item_id = int(call.data.split("sellall_confirm_")[1])
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            inv_r = await session.execute(
                select(Inventory).where(Inventory.user_id == user_id, Inventory.item_id == item_id))
            inv = inv_r.scalar_one_or_none()
            if not user or not inv or inv.quantity <= 0:
                await call.answer("❌!", show_alert=True)
                return
            if user.is_being_robbed:
                await call.answer(
                    "⛔ Вы не можете распоряжаться финансами, пока вас грабят!",
                    show_alert=True)
                return
            if inv.item.name in TOOL_NAMES:
                await call.answer("❌ Инструменты нельзя продать!", show_alert=True)
                return

            item = inv.item
            qty = inv.quantity
            gross = item.price * qty
            tax = int(gross * WEALTH_TAX_RATE) if gross > WEALTH_TAX_THRESHOLD else 0
            net = gross - tax

            user.balance_vv += net

            # ── XP за продажу ──
            old_level = user.level
            new_levels = add_xp(user, net)

            await session.delete(inv)
            await session.commit()
            if new_levels:
                await grant_level_rewards(call.bot, session, user, old_level, new_levels)
                await session.commit()

            tax_text = f"\n💸 Налог: <b>-{tax:,} 🪙</b>" if tax > 0 else ""
            lvl_text = _build_lvl_text(new_levels)

            try:
                await call.message.edit_text(
                    f"✅ <b>Продано!</b>\n\n🧬 {item.name} × {qty}\n"
                    f"💰 {gross:,} 🪙{tax_text}\n💵 Получено: <b>{net:,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>{lvl_text}",
                    parse_mode="HTML")
            except Exception:
                pass
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ sellall: {e}")
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("sellall_everything_"))
async def sellall_everything(call: CallbackQuery) -> None:
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
            inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
            inv_items = inv_r.scalars().all()
            sellable = [inv for inv in inv_items if _is_sellable(inv)]
            if not sellable:
                await call.answer("❌!", show_alert=True)
                return
            total_gross = 0
            total_qty = 0
            sold_lines = []
            for inv in sellable:
                g = inv.item.price * inv.quantity
                total_gross += g
                total_qty += inv.quantity
                sold_lines.append(f"  • {inv.item.name} × {inv.quantity} = {g:,}")
                await session.delete(inv)
            tax = int(total_gross * WEALTH_TAX_RATE) if total_gross > WEALTH_TAX_THRESHOLD else 0
            net = total_gross - tax

            user.balance_vv += net

            # ── XP за продажу ──
            old_level = user.level
            new_levels = add_xp(user, net)

            await session.commit()
            if new_levels:
                await grant_level_rewards(call.bot, session, user, old_level, new_levels)
                await session.commit()

            tax_text = f"\n💸 Налог: <b>-{tax:,} 🪙</b>" if tax > 0 else ""
            sold_text = "\n".join(sold_lines[:10])
            if len(sold_lines) > 10:
                sold_text += f"\n  ... и ещё {len(sold_lines) - 10}"
            lvl_text = _build_lvl_text(new_levels)

            try:
                await call.message.edit_text(
                    f"🔥 <b>Всё продано!</b>\n\n{sold_text}\n\n"
                    f"📦 {total_qty} шт.\n💰 {total_gross:,} 🪙{tax_text}\n"
                    f"💵 Получено: <b>{net:,} 🪙</b>\n"
                    f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>{lvl_text}",
                    parse_mode="HTML")
            except Exception:
                pass
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ sellall_everything: {e}", exc_info=True)
            await call.answer("❌", show_alert=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data == "sell_cancel")
async def sell_cancel(call: CallbackQuery) -> None:
    try:
        await call.message.edit_text("❌ Отменено.")
    except Exception:
        pass
    await call.answer()


# ============================================================================
# /donat
# ============================================================================


@router.message(Command("donat"))
async def cmd_donat(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("❌ Только в группах!")
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("💡 <code>/donat 1000</code>", parse_mode="HTML")
        return
    amount = int(args[1])
    if amount <= 0 or amount > 10_000_000:
        await message.answer("❌ От 1 до 10,000,000!")
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.first_name or "Игрок"
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            if not user:
                await message.answer("❌ /start в ЛС!")
                return
            if user.balance_vv < amount:
                await message.answer(f"❌ Баланс: {user.balance_vv:,} 🪙")
                return
            user.balance_vv -= amount
            gc_r = await session.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
            group = gc_r.scalar_one_or_none()
            if not group:
                group = GroupChat(chat_id=chat_id, common_pot=0)
                session.add(group)
                await session.flush()
            group.common_pot += amount
            await track_chat_activity(session, chat_id, user_id)
            exploded = await check_pot_explosion(session, chat_id, message.bot)
            await session.commit()
            await session.refresh(group)
            if not exploded:
                await message.answer(
                    f"🙏 <b>{username}</b> → <b>{amount:,} 🪙</b> в общак!\n\n"
                    f"🏦 Общак: <b>{group.common_pot:,} 🪙</b>", parse_mode="HTML")
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ /donat: {e}", exc_info=True)
            await message.answer("❌ Ошибка!")
        finally:
            await session.close()


# ============================================================================
# /help
# ============================================================================


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📚 CUM GEN: ПРАВИЛА УЛИЦ\n"
        "ОСНОВНОЙ ЦИКЛ:\n"
        "💦 Тереби — получай случайные гены.\n"
        "🧬 Гены — продавай их, чтобы накопить монеты.\n"
        "💰 Монеты — твоя власть. Трать на защиту, уровни или азарт.\n\n"
        "⚙️ МЕХАНИКИ ИГРЫ:\n"
        "🧬 ГЕНЕТИКА\n\n"
        "Выпадают при тереблении. Чем реже ген, тем дороже продажа.\n\n"
        "Самые редкие (0.01%) — гены Бога и Тайного Правительства.\n\n"
        "🔫 ОГРАБЛЕНИЯ\n\n"
        "Шанс: Зависит от твоего баланса и жертвы. Грабить бедных — шанс 1%. Грабить равных — профит.\n\n"
        "Сейф: Взламывай код, чтобы вынести до 25% заначки.\n\n"
        "Тюрьма: Неудачный грабеж = срок. 💼 Адвокат вытащит тебя мгновенно.\n\n"
        "🛡 ЗАЩИТА И БАЛАНС\n\n"
        "Охрана: Защищает сейф на 6 часов.\n\n"
        "Вышибала: Наемник, который игнорирует любую охрану жертвы (20k 🪙).\n\n"
        "Заморозка: Тебя грабят? Финансовые операции блокируются.\n\n"
        "🎰 АЗАРТНЫЕ ИГРЫ\n\n"
        "Казино: Испытай удачу с множителями до x20.\n\n"
        "Блек Джек (21): Классика против дилера. Победа — x2.\n\n"
        "📈 ПРОГРЕСС\n\n"
        "Уровни: Получай XP за продажи, игры и грабежи. За каждый Level Up — заряд теребления!\n\n"
        "Переводы: Доступны с 5 уровня (Комиссия 3%).",
        parse_mode="HTML",
        reply_markup=get_main_keyboard() if _is_private(message) else None)


@router.message(lambda m: m.text == "❓ Помощь" and m.chat.type == "private")
async def button_help(message: Message) -> None:
    await cmd_help(message)
