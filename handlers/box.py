"""Теребление с кнопкой 'Продать улов' при 0 зарядах."""
import logging
import asyncio
import random
from datetime import datetime
from aiogram import Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, and_

from database import get_db
from models import User, Item, Inventory, MAX_BOX_COUNT
from utils.keyboards import get_main_keyboard, get_box_keyboard, format_emoji
from utils.box_utils import update_user_boxes, get_weighted_random_item, get_time_until_next_box
from utils.formatters import get_rarity_emoji, get_rarity_name

logger = logging.getLogger(__name__)
router = Router()

_inline_history: dict[str, list[dict]] = {}
processing_users: dict[int, bool] = {}
POLLUTION_CHANCE = 0.0777


def _item_info(item, rarity_emoji, rarity_name):
    return {"name": item.name, "rarity": item.rarity.value,
        "rarity_emoji": rarity_emoji, "rarity_name": rarity_name, "price": item.price}


def _build_result_text_inline(user_first_name, items_history, box_count, boosts_text, pollution=False):
    if len(items_history) == 1:
        it = items_history[0]
        items_text = (f"🧬 <b>{it['name']}</b> {it['rarity_emoji']}\n"
            f"<code>{it['rarity_name']}</code> | 💰 {it['price']:,} 🪙")
    else:
        lines = [f"{i}. 🧬 <b>{it['name']}</b> {it['rarity_emoji']}\n"
            f"   <code>{it['rarity_name']}</code> | 💰 {it['price']:,} 🪙"
            for i, it in enumerate(items_history, 1)]
        items_text = "\n".join(lines)

    pollution_text = ""
    if pollution:
        pollution_text = "\n\n💦 <b>Ой! Ночная полюция!</b> Выделилось сразу два головастика!"

    return (f"🧬 <b>{user_first_name} теребит и получает:</b>\n\n"
        f"{items_text}{pollution_text}\n\n"
        f"🚀 Бусты: {boosts_text}\n"
        f"Осталось: <b>{box_count}/{MAX_BOX_COUNT}</b>")


def _build_dm_text(items, boosts_text, box_count, pollution=False):
    if len(items) == 1:
        it = items[0]
        emoji_html = format_emoji(str(it["emoji"]))
        items_text = (f"{emoji_html} <b>{it['name']}</b> {it['rarity_emoji']}\n"
            f"<code>{it['rarity_name']}</code> | 💰 {it['price']:,} 🪙")
    else:
        lines = [f"{format_emoji(str(it['emoji']))} <b>{it['name']}</b> {it['rarity_emoji']}\n"
            f"<code>{it['rarity_name']}</code> | 💰 {it['price']:,} 🪙" for it in items]
        items_text = "\n".join(lines)

    pollution_text = ""
    if pollution:
        pollution_text = "\n\n💦 <b>Ой! Ночная полюция!</b> Выделилось сразу два!"

    return (f"🧬 <b>Выделился головастик с особым геном!</b>\n\n"
        f"{items_text}{pollution_text}\n\n"
        f"🚀 Бусты: {boosts_text}\n"
        f"Осталось: <b>{box_count}/{MAX_BOX_COUNT}</b>")


def _parse_box_callback(data):
    if data == "open_box":
        return None
    try:
        return int(data.split("open_box_")[1])
    except (ValueError, IndexError):
        return None


def _get_result_keyboard(user_id: int, box_count: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    if box_count > 0:
        buttons.append([InlineKeyboardButton(text="✊ Теребить!",
            callback_data=f"open_box_{user_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="💰 Продать весь улов",
            callback_data=f"sell_genes_{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _roll_items(session, user):
    all_items_r = await session.execute(select(Item).where(Item.drop_chance > 0))
    all_items = all_items_r.scalars().all()
    if not all_items:
        return [], False
    multiplier = user.get_drop_multiplier()
    pollution = random.random() < POLLUTION_CHANCE
    count = 2 if pollution else 1
    results = []
    for _ in range(count):
        item = get_weighted_random_item(all_items, multiplier)
        if item:
            results.append(item)
    return results, pollution


async def _add_items_to_inv(session, user_id, items):
    for item in items:
        existing = await session.execute(
            select(Inventory).where(and_(Inventory.user_id == user_id, Inventory.item_id == item.id)))
        inv = existing.scalar_one_or_none()
        if inv:
            inv.quantity += 1
        else:
            session.add(Inventory(user_id=user_id, item_id=item.id, quantity=1))


# ============================================================================
# ПРОДАТЬ ВЕСЬ ГЕНОФОНД (ИСПРАВЛЕНО: только гены, drop_chance > 0)
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("sell_genes_"))
async def sell_genes_handler(call: CallbackQuery) -> None:
    user_id = int(call.data.split("sell_genes_")[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Не твоё!", show_alert=True)
        return
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = user_r.scalar_one_or_none()
            if not user:
                await call.answer("❌", show_alert=True)
                return

            inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
            inv_items = inv_r.scalars().all()

            # ── ИСПРАВЛЕНО: фильтруем СТРОГО по drop_chance > 0 (только гены) ──
            # Инструменты, бусты, сейфы и прочие предметы имеют drop_chance == 0
            # и никогда не попадут в этот список
            sellable = [inv for inv in inv_items
                if inv.item.drop_chance > 0
                and inv.item.price > 0
                and inv.quantity > 0]

            if not sellable:
                await call.answer("❌ У вас нет генов на продажу!", show_alert=True)
                return

            total = 0
            total_qty = 0
            for inv in sellable:
                amount = inv.item.price * inv.quantity
                total += amount
                total_qty += inv.quantity
                await session.delete(inv)

            user.balance_vv += total
            await session.commit()

            await call.answer(f"✅ Продано {total_qty} генов на {total:,} 🪙!", show_alert=True)

            try:
                await call.bot.edit_message_text(
                    text=(f"💰 <b>Улов продан!</b>\n\n"
                        f"🧬 {total_qty} генов → <b>{total:,} 🪙</b>\n"
                        f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>"),
                    inline_message_id=call.inline_message_id,
                    parse_mode="HTML") if call.inline_message_id else None
            except Exception:
                pass
            try:
                if call.message:
                    await call.message.edit_text(
                        f"💰 <b>Улов продан!</b>\n\n"
                        f"🧬 {total_qty} генов → <b>{total:,} 🪙</b>\n"
                        f"💼 Баланс: <b>{user.balance_vv:,} 🪙</b>",
                        parse_mode="HTML")
            except Exception:
                pass

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ sell_genes: {e}", exc_info=True)
            await call.answer("❌ Ошибка", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# CALLBACK — открытие бокса
# ============================================================================


@router.callback_query(lambda call: call.data and call.data.startswith("open_box"))
async def open_box_handler(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    user_first_name = call.from_user.first_name or "Друг"

    owner_id = _parse_box_callback(call.data)
    if owner_id is not None and owner_id != user_id:
        await call.answer("❌ Это не твоё!", show_alert=True)
        return

    if user_id in processing_users:
        await call.answer("⏳ Обработка...")
        return
    processing_users[user_id] = True
    await call.answer()

    has_inline_id = bool(call.inline_message_id)
    has_message = call.message is not None
    chat_type = call.message.chat.type if has_message and call.message.chat else None
    db = get_db()

    try:
        async for session in db.get_session():
            try:
                user_r = await session.execute(select(User).where(User.tg_id == user_id))
                user = user_r.scalar_one_or_none()
                if not user:
                    user = User(tg_id=user_id, username=call.from_user.username or user_first_name,
                        box_count=MAX_BOX_COUNT, last_refill_at=datetime.utcnow())
                    session.add(user)
                    await session.flush()

                await update_user_boxes(user)

                if user.box_count <= 0:
                    refill_h = user.get_refill_hours()
                    hours, minutes = get_time_until_next_box(user.last_refill_at, refill_h)
                    no_text = (f"❌ <b>Нечего теребить!</b>\n\n"
                        f"Осталось: <b>0/{MAX_BOX_COUNT}</b>\n"
                        f"Через <b>{hours}ч. {minutes}мин.</b>\n\n"
                        f"⚡ Заряды можно купить в ЛС бота, в разделе магазин.")
                    if has_inline_id:
                        try:
                            await call.bot.edit_message_text(text=no_text,
                                inline_message_id=call.inline_message_id, parse_mode="HTML")
                        except Exception:
                            pass
                    elif has_message:
                        try:
                            await call.message.edit_text(no_text, parse_mode="HTML")
                        except Exception:
                            pass
                    return

                # Атомарное списание заряда ДО анимации
                user.box_count -= 1
                user.increment_action()
                await session.commit()

                # Анимация (после фиксации списания)
                if has_inline_id:
                    try:
                        await call.bot.edit_message_text(
                            text=f"✊ <b>{user_first_name} теребит...</b>",
                            inline_message_id=call.inline_message_id, parse_mode="HTML")
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
                elif has_message:
                    try:
                        await call.message.edit_text("✊ <b>Теребим...</b>", parse_mode="HTML")
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)

                items, pollution = await _roll_items(session, user)
                if not items:
                    logger.warning(f"✊ {user_id}: нет предметов для выдачи")
                    err_text = "❌ <b>Нет предметов для выдачи.</b>"
                    if has_inline_id:
                        try:
                            await call.bot.edit_message_text(text=err_text,
                                inline_message_id=call.inline_message_id, parse_mode="HTML")
                        except Exception:
                            pass
                    elif has_message:
                        try:
                            await call.message.edit_text(err_text, parse_mode="HTML")
                        except Exception:
                            pass
                    return

                await _add_items_to_inv(session, user_id, items)
                await session.commit()

                boosts_text = user.active_boosts_text()
                reply_markup = _get_result_keyboard(user_id, user.box_count)

                dm_items = [{"name": it.name, "emoji": str(it.emoji), "price": it.price,
                    "rarity_emoji": get_rarity_emoji(it.rarity),
                    "rarity_name": get_rarity_name(it.rarity)} for it in items]

                if has_inline_id:
                    inline_id = call.inline_message_id
                    if inline_id not in _inline_history:
                        _inline_history[inline_id] = []
                    for it in items:
                        _inline_history[inline_id].append(
                            _item_info(it, get_rarity_emoji(it.rarity), get_rarity_name(it.rarity)))
                    inline_text = _build_result_text_inline(
                        user_first_name, _inline_history[inline_id],
                        user.box_count, boosts_text, pollution)
                    try:
                        await call.bot.edit_message_text(text=inline_text, inline_message_id=inline_id,
                            parse_mode="HTML", reply_markup=reply_markup)
                    except Exception:
                        pass

                elif has_message and chat_type == "private":
                    dm_text = _build_dm_text(dm_items, boosts_text, user.box_count, pollution)
                    try:
                        await call.message.edit_text(dm_text, parse_mode="HTML", reply_markup=reply_markup)
                    except Exception:
                        await call.bot.send_message(chat_id=user_id, text=dm_text, parse_mode="HTML")

                elif has_message and chat_type != "private":
                    inline_items = [_item_info(it, get_rarity_emoji(it.rarity), get_rarity_name(it.rarity))
                        for it in items]
                    group_text = _build_result_text_inline(
                        user_first_name, inline_items, user.box_count, boosts_text, pollution)
                    try:
                        await call.message.delete()
                    except Exception:
                        pass
                    try:
                        await call.bot.send_message(chat_id=call.message.chat.id, text=group_text,
                            parse_mode="HTML", reply_markup=reply_markup)
                    except Exception:
                        pass
                else:
                    dm_text = _build_dm_text(dm_items, boosts_text, user.box_count, pollution)
                    try:
                        await call.bot.send_message(chat_id=user_id, text=dm_text, parse_mode="HTML")
                    except Exception:
                        pass

                names = " + ".join(it.name for it in items)
                logger.info(f"✊ {user_id} → {names} {'(ПОЛЮЦИЯ)' if pollution else ''}")

            except Exception as e:
                await session.rollback()
                logger.error(f"❌ Теребление: {e}", exc_info=True)
            finally:
                await session.close()
    finally:
        processing_users.pop(user_id, None)


# ============================================================================
# /box (только ЛС)
# ============================================================================


@router.message(Command("box"))
async def cmd_box(message: Message) -> None:
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    db = get_db()
    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if not user:
                await message.answer("❌ /start", reply_markup=get_main_keyboard())
                return
            await update_user_boxes(user)
            if user.box_count <= 0:
                h, m = get_time_until_next_box(user.last_refill_at, user.get_refill_hours())
                await message.answer(
                    f"❌ <b>Нечего теребить!</b>\n\n0/{MAX_BOX_COUNT}\n"
                    f"Через <b>{h}ч. {m}мин.</b>\n\n⚡ Или купите заряды в магазине, перейдя в чат с ботом.",
                    parse_mode="HTML", reply_markup=get_main_keyboard())
                return
            user.box_count -= 1
            user.increment_action()

            items, pollution = await _roll_items(session, user)
            if not items:
                await message.answer("❌ Нет предметов", reply_markup=get_main_keyboard())
                return
            await _add_items_to_inv(session, user_id, items)
            await session.commit()

            boosts_text = user.active_boosts_text()
            dm_items = [{"name": it.name, "emoji": str(it.emoji), "price": it.price,
                "rarity_emoji": get_rarity_emoji(it.rarity),
                "rarity_name": get_rarity_name(it.rarity)} for it in items]
            dm_text = _build_dm_text(dm_items, boosts_text, user.box_count, pollution)
            reply_markup = _get_result_keyboard(user_id, user.box_count) if user.box_count == 0 else get_main_keyboard()
            await message.answer(dm_text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ /box: {e}", exc_info=True)
            await message.answer("❌ Ошибка", reply_markup=get_main_keyboard())
        finally:
            await session.close()


@router.message(lambda m: m.text == "✊ Теребить" and m.chat.type == "private")
async def button_box(message: Message) -> None:
    await cmd_box(message)
