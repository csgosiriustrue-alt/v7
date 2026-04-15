"""Инлайн-запросы: теребление, казино, ограбление, перевод денег с проверкой уровня."""
import logging
import re
from aiogram import Router
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    ChosenInlineResult, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery,
)
from sqlalchemy import select

from database import get_db
from models import User, MAX_BOX_COUNT
from handlers.casino import casino_inline_handler, casino_chosen_result
from handlers.robbery import robbery_inline_handler, robbery_chosen_result
from handlers.user import build_profile_text
from utils.keyboards import get_box_keyboard
from utils.box_utils import update_user_boxes, get_time_until_next_box
from utils.user_helpers import get_or_create_user
from utils.levels import can_transfer, MIN_TRANSFER_LEVEL

logger = logging.getLogger(__name__)
router = Router()

_TRANSFER_RE = re.compile(r"^\s*(\d+)\s+@(\w+)\s*$")
TRANSFER_FEE = 0.03
_pending_transfers: dict[str, dict] = {}

_TG_ID_THRESHOLD = 1_000_000_000


async def _ensure_user_registered(user_id: int, username: str | None = None) -> User:
    db = get_db()
    user = None
    async for session in db.get_session():
        try:
            user = await get_or_create_user(session, user_id, username)
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ auto-register: {e}")
        finally:
            await session.close()
    return user


async def _build_hints(bot_username: str, user_id: int) -> list[InlineQueryResultArticle]:
    hints = []
    db = get_db()

    async for session in db.get_session():
        try:
            user_r = await session.execute(select(User).where(User.tg_id == user_id))
            user = user_r.scalar_one_or_none()
            if user:
                await update_user_boxes(user)
                box_count = user.box_count
                balance = user.balance_vv
                await session.commit()

                profile_text = await build_profile_text(user_id, session)
                hints.append(InlineQueryResultArticle(
                    id=f"profile_{user_id}",
                    title="👤 Мой профиль",
                    description=f"💰 {balance:,} 🪙 | ✊ {box_count}/{MAX_BOX_COUNT}",
                    input_message_content=InputTextMessageContent(
                        message_text=profile_text, parse_mode="HTML"),
                ))

                if box_count > 0:
                    boosts = user.active_boosts_text()
                    hints.append(InlineQueryResultArticle(
                        id="box_opener",
                        title=f"✊ Потеребить (Доступно: {box_count}/{MAX_BOX_COUNT})",
                        description="Гены Дурова: 1/555. Полюция: 7.77%!",
                        input_message_content=InputTextMessageContent(
                            message_text=(
                                f"✊ <b>{user.username or 'Игрок'}</b> собирается теребить...\n"
                                f"🚀 Бусты: {boosts}"
                            ), parse_mode="HTML"),
                        reply_markup=get_box_keyboard(owner_id=user_id),
                    ))
                else:
                    refill_h = user.get_refill_hours()
                    h, m = get_time_until_next_box(user.last_refill_at, refill_h)
                    hints.append(InlineQueryResultArticle(
                        id="box_empty",
                        title=f"✊ Нечего теребить (0/{MAX_BOX_COUNT})",
                        description=f"⏳ {h}ч {m}мин | Купи заряды в магазине!",
                        input_message_content=InputTextMessageContent(
                            message_text=(
                                f"✊ Нечего теребить! Через <b>{h}ч {m}мин</b>\n\n"
                                f"⚡ Заряды можно купить в магазине, в личніх сообщениях с ботом."
                            ), parse_mode="HTML"),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="⚡ Купить заряды",
                                switch_inline_query_current_chat="")]]),
                    ))
            else:
                hints.append(InlineQueryResultArticle(
                    id="not_registered",
                    title="❌ Не зарегистрированы",
                    description="/start в ЛС бота",
                    input_message_content=InputTextMessageContent(
                        message_text="❌ /start в ЛС бота!", parse_mode="HTML"),
                ))
        except Exception as e:
            logger.error(f"❌ hints: {e}")
        finally:
            await session.close()

    hints.append(InlineQueryResultArticle(
        id="hint_casino",
        title="🎰 Сделать ставку",
        description=f"@{bot_username} [сумма] (от 300)",
        input_message_content=InputTextMessageContent(
            message_text="🎰 Введите ставку!", parse_mode="HTML"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎰 Ставка",
                switch_inline_query_current_chat="1000")]]),
    ))

    hints.append(InlineQueryResultArticle(
        id="hint_robbery",
        title="🎭 Ограбить игрока",
        description=f"@{bot_username} @username или @{bot_username} ID",
        input_message_content=InputTextMessageContent(
            message_text="🔫 Введите @username или числовой ID игрока!", parse_mode="HTML"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔫 По нику",
                switch_inline_query_current_chat="@"),
            InlineKeyboardButton(text="🔫 По ID",
                switch_inline_query_current_chat=""),
        ]]),
    ))

    hints.append(InlineQueryResultArticle(
        id="hint_transfer",
        title="💸 Перевести деньги (с 5 ур.)",
        description=f"@{bot_username} [сумма] @[username]",
        input_message_content=InputTextMessageContent(
            message_text="💸 Введите сумму и @username получателя!", parse_mode="HTML"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💸 Перевод",
                switch_inline_query_current_chat="1000 @")]]),
    ))

    return hints


# ============================================================================
# Перевод — инлайн
# ============================================================================


async def transfer_inline_handler(inline_query: InlineQuery) -> None:
    query_text = inline_query.query.strip()
    m = _TRANSFER_RE.match(query_text)
    if not m:
        return

    amount = int(m.group(1))
    recipient_username = m.group(2)
    sender_id = inline_query.from_user.id
    sender_name = inline_query.from_user.first_name or "Отправитель"

    if amount <= 0:
        r = InlineQueryResultArticle(
            id="transfer_zero", title="❌ Сумма должна быть > 0",
            description="Введите корректную сумму",
            input_message_content=InputTextMessageContent(
                message_text="❌ Сумма перевода должна быть больше 0!", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    fee = int(amount * TRANSFER_FEE)
    total_cost = amount + fee

    db = get_db()
    sender = None
    recipient = None

    async for session in db.get_session():
        try:
            sr = await session.execute(select(User).where(User.tg_id == sender_id))
            sender = sr.scalar_one_or_none()
            rr = await session.execute(select(User).where(User.username == recipient_username))
            recipient = rr.scalar_one_or_none()
        except Exception as e:
            logger.error(f"❌ transfer lookup: {e}")
        finally:
            await session.close()

    if not sender:
        r = InlineQueryResultArticle(
            id="transfer_no_sender", title="❌ Вы не зарегистрированы",
            description="/start в ЛС бота",
            input_message_content=InputTextMessageContent(
                message_text="❌ /start в ЛС бота!", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    # ═══ ПРОВЕРКА УРОВНЯ ═══
    can_do, block_msg = can_transfer(sender)
    if not can_do:
        r = InlineQueryResultArticle(
            id="transfer_low_level",
            title=f"🔒 Переводы с {MIN_TRANSFER_LEVEL} уровня",
            description=f"Ваш уровень: {sender.level}. Зарабатывайте XP!",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🔒 <b>Переводы доступны с {MIN_TRANSFER_LEVEL} уровня!</b>\n\n"
                    f"⭐ Ваш уровень: <b>{sender.level}</b>\n"
                    f"📈 Зарабатывайте монеты: казино, ограбления, продажа генов."
                ), parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return
    # ═══════════════════════

    if not recipient:
        r = InlineQueryResultArticle(
            id="transfer_no_recipient", title=f"❌ @{recipient_username} не найден",
            description="Игрок не зарегистрирован",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ @{recipient_username} не найден!", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    if recipient.tg_id == sender_id:
        r = InlineQueryResultArticle(
            id="transfer_self", title="❌ Нельзя перевести себе",
            description="",
            input_message_content=InputTextMessageContent(
                message_text="❌ Нельзя перевести деньги самому себе!", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    if sender.balance_vv < total_cost:
        r = InlineQueryResultArticle(
            id="transfer_no_funds",
            title=f"❌ Недостаточно! Нужно {total_cost:,} 🪙",
            description=f"Баланс: {sender.balance_vv:,} 🪙 (сумма {amount:,} + комиссия {fee:,})",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Недостаточно! Нужно <b>{total_cost:,} 🪙</b> (сумма + 3% комиссия)",
                parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    result_id = f"transfer_{sender_id}_{recipient.tg_id}_{amount}"
    _pending_transfers[result_id] = {
        "sender_id": sender_id,
        "sender_name": sender_name,
        "recipient_id": recipient.tg_id,
        "recipient_username": recipient_username,
        "amount": amount,
        "fee": fee,
        "total_cost": total_cost,
    }

    r_clean = recipient_username.lstrip("@")
    result = InlineQueryResultArticle(
        id=result_id,
        title=f"💸 Перевести {amount:,} 🪙 → @{r_clean}",
        description=f"Комиссия 3%: {fee:,} 🪙 | Итого: {total_cost:,} 🪙",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"💸 <b>{sender_name}</b> переводит <b>{amount:,} 🪙</b> игроку <b>{r_clean}</b>\n"
                f"📊 Комиссия (3%): <b>{fee:,} 🪙</b>\n\n"
                f"⏳ Ожидание подтверждения..."
            ), parse_mode="HTML"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Подтвердить перевод {amount:,} 🪙",
                callback_data=f"transfer_confirm_{sender_id}_{recipient.tg_id}_{amount}")]
        ]),
    )

    await inline_query.bot.answer_inline_query(inline_query.id, [result], cache_time=1, is_personal=True)


async def transfer_chosen_result(chosen: ChosenInlineResult) -> None:
    rid = chosen.result_id
    if not rid or not rid.startswith("transfer_"):
        return
    logger.info(f"💸 Transfer chosen: {rid}")


# ============================================================================
# Callback подтверждения перевода
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("transfer_confirm_"))
async def transfer_confirm_handler(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        sender_id = int(parts[2])
        recipient_id = int(parts[3])
        amount = int(parts[4])
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка данных!", show_alert=True)
        return

    if call.from_user.id != sender_id:
        await call.answer("❌ Это не ваш перевод!", show_alert=True)
        return

    fee = int(amount * TRANSFER_FEE)
    total_cost = amount + fee

    db = get_db()
    async for session in db.get_session():
        try:
            sr = await session.execute(select(User).where(User.tg_id == sender_id))
            sender = sr.scalar_one_or_none()
            rr = await session.execute(select(User).where(User.tg_id == recipient_id))
            recipient = rr.scalar_one_or_none()

            if not sender or not recipient:
                await call.answer("❌ Игрок не найден!", show_alert=True)
                return

            # ═══ ПРОВЕРКА УРОВНЯ ═══
            can_do, block_msg = can_transfer(sender)
            if not can_do:
                await call.answer(
                    f"🔒 Переводы с {MIN_TRANSFER_LEVEL} уровня! Ваш: {sender.level}",
                    show_alert=True)
                return
            # ════════════════════════

            if sender.balance_vv < total_cost:
                await call.answer(
                    f"❌ Недостаточно! Нужно {total_cost:,} 🪙, у вас {sender.balance_vv:,} 🪙",
                    show_alert=True)
                return

            # ── Перевод: XP НЕ начисляется! ──
            sender.balance_vv -= total_cost
            recipient.balance_vv += amount

            await session.commit()

            sender_name = call.from_user.first_name or "Отправитель"
            r_clean = (recipient.username or str(recipient_id)).lstrip("@")

            success_text = (
                f"✅ <b>Перевод выполнен!</b>\n\n"
                f"👤 <b>{sender_name}</b> → <b>{r_clean}</b>\n"
                f"💰 Сумма: <b>{amount:,} 🪙</b>\n"
                f"📊 Комиссия (3%): <b>{fee:,} 🪙</b>\n"
                f"💸 Списано: <b>{total_cost:,} 🪙</b>\n\n"
                f"💼 Баланс: <b>{sender.balance_vv:,} 🪙</b>"
            )

            if call.inline_message_id:
                try:
                    await call.bot.edit_message_text(
                        text=success_text,
                        inline_message_id=call.inline_message_id,
                        parse_mode="HTML")
                except Exception:
                    pass
            elif call.message:
                try:
                    await call.message.edit_text(success_text, parse_mode="HTML")
                except Exception:
                    pass

            await call.answer(f"✅ Переведено {amount:,} 🪙!", show_alert=True)
            logger.info(f"💸 Transfer: {sender_id} → {recipient_id}, {amount} 🪙, fee {fee}")

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ transfer_confirm: {e}", exc_info=True)
            await call.answer("❌ Ошибка перевода!", show_alert=True)
        finally:
            await session.close()


# ============================================================================
# ГЛАВНЫЙ РОУТЕР
# ============================================================================


@router.inline_query()
async def global_inline_handler(inline_query: InlineQuery) -> None:
    query = inline_query.query.strip()
    user_id = inline_query.from_user.id
    username = inline_query.from_user.username or inline_query.from_user.first_name

    await _ensure_user_registered(user_id, username)

    # Перевод: "1000 @username"
    if _TRANSFER_RE.match(query):
        await transfer_inline_handler(inline_query)
        return

    # Ограбление по @username
    if query.startswith("@"):
        await robbery_inline_handler(inline_query)
        return

    # Число: ID (>1 000 000) → ограбление, иначе → казино
    if query.isdigit():
        num = int(query)
        if num > 1_000_000:
            await robbery_inline_handler(inline_query)
        else:
            await casino_inline_handler(inline_query)
        return

    # Пустой запрос — подсказки
    if not query:
        bot_me = await inline_query.bot.get_me()
        hints = await _build_hints(bot_me.username or "GiftHeistBot", user_id)
        await inline_query.bot.answer_inline_query(
            inline_query.id, hints, cache_time=1, is_personal=True)
        return


@router.chosen_inline_result()
async def global_chosen_handler(chosen: ChosenInlineResult) -> None:
    rid = chosen.result_id or ""
    if rid.startswith("casino_"):
        await casino_chosen_result(chosen)
    elif rid.startswith("rob_"):
        await robbery_chosen_result(chosen)
    elif rid.startswith("transfer_"):
        await transfer_chosen_result(chosen)
    elif rid == "box_opener":
        logger.info(f"✊ BOX CHOSEN: user={chosen.from_user.id}")
