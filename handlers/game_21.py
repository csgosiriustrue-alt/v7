"""Блэкджек (21) — инлайн-игра для Gift Heist."""
import asyncio
import logging
import random

from aiogram import Router
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    CallbackQuery, ChosenInlineResult, InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select

from database import get_db
from models import User, GroupChat, MAX_DAILY_BETS, MAX_DAILY_BJ
from utils.casino_utils import MIN_BET, MAX_BET, MAX_COMMON_POT, POT_PERCENT
from utils.pot_event import track_chat_activity, check_pot_explosion
from utils.levels import add_xp, grant_level_rewards
from utils.keyboards import get_casino_keyboard
from handlers.casino import remember_chat, _user_chat_map

logger = logging.getLogger(__name__)
router = Router()

# ── Колода ──
_SUITS = ["♠️", "♥️", "♦️", "♣️"]
_RANKS = [
    ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6),
    ("7", 7), ("8", 8), ("9", 9), ("10", 10),
    ("J", 10), ("Q", 10), ("K", 10), ("A", 11),
]


def _make_deck() -> list[tuple[str, int]]:
    deck = [(f"{rank}{suit}", val) for suit in _SUITS for rank, val in _RANKS]
    random.shuffle(deck)
    return deck


def calculate_hand(cards: list[tuple[str, int]]) -> int:
    """Считает сумму очков. Туз (11) становится 1, если сумма > 21."""
    total = sum(v for _, v in cards)
    aces = sum(1 for _, v in cards if v == 11)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _hand_str(cards: list[tuple[str, int]]) -> str:
    return ", ".join(f"🃏 {c}" for c, _ in cards)


def _bj_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Ещё", callback_data=f"bj_hit_{owner_id}"),
        InlineKeyboardButton(text="🛑 Стоп", callback_data=f"bj_stand_{owner_id}"),
    ]])


def _bj_again_keyboard(owner_id: int, bet: int, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔄 Играть снова",
            callback_data=f"bj_again_{owner_id}_{bet}_{chat_id}"),
    ]])


def _game_text(session_data: dict, hide_dealer: bool = True) -> str:
    player = session_data["player_hand"]
    dealer = session_data["dealer_hand"]
    bet = session_data["bet"]
    player_score = calculate_hand(player)
    if hide_dealer:
        dealer_str = f"{_hand_str([dealer[0]])}, ❓"
    else:
        dealer_score = calculate_hand(dealer)
        dealer_str = f"{_hand_str(dealer)} ({dealer_score} очков)"
    return (
        f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
        f"👤 Ваши карты:\n  {_hand_str(player)} ({player_score} очков)\n\n"
        f"🎩 Дилер:\n  {dealer_str}\n\n"
        f"💰 Ставка: {bet:,} 🪙"
    )


# ── Хранилище сессий ──
_bj_sessions: dict[str, dict] = {}     # inline_message_id → session
_bj_owner_map: dict[int, str] = {}     # owner_id → inline_message_id
_bj_locks: dict[int, asyncio.Lock] = {}  # owner_id → Lock
_pending_bj: dict[str, dict] = {}      # result_id → pending info


async def _resolve_chat_id(call: CallbackQuery, owner_id: int, chat_id_from_data: int = 0) -> int | None:
    """Определяет chat_id в порядке убывания надёжности источника."""
    # 1. Из call.message.chat (обычный callback в группе — самый надёжный)
    if call.message and call.message.chat:
        if call.message.chat.type in ("group", "supergroup"):
            return call.message.chat.id

    # 2. Из callback_data (явно передан != 0)
    if chat_id_from_data and chat_id_from_data != 0:
        return chat_id_from_data

    # 3. Из _user_chat_map (casino.py — последний известный чат пользователя)
    if owner_id in _user_chat_map:
        return _user_chat_map[owner_id]

    # 4. Из БД ChatActivity — последний фоллбэк
    db = get_db()
    async for sess in db.get_session():
        try:
            from models import ChatActivity
            act_r = await sess.execute(
                select(ChatActivity).where(ChatActivity.user_id == owner_id).limit(1))
            act = act_r.scalar_one_or_none()
            if act:
                return act.chat_id
        except Exception:
            pass
        finally:
            await sess.close()

    return None


# ============================================================================
# INLINE (комбинированный обработчик: казино + блэкджек)
# ============================================================================


async def bj_inline_handler(inline_query: InlineQuery) -> None:
    """Показывает два варианта: Казино и Блэкджек."""
    query_text = inline_query.query.strip()
    if not query_text.isdigit():
        return

    bet = int(query_text)
    user_id = inline_query.from_user.id
    user_first_name = inline_query.from_user.first_name or "Игрок"

    if bet < MIN_BET:
        r = InlineQueryResultArticle(
            id="bet_low",
            title=f"❌ Минимум: {MIN_BET} 🪙",
            description=f"Минимальная ставка — {MIN_BET} 🪙",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Минимум: <b>{MIN_BET} 🪙</b>", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return
    if bet > MAX_BET:
        r = InlineQueryResultArticle(
            id="bet_high",
            title=f"❌ Максимум: {MAX_BET:,} 🪙",
            description=f"Максимальная ставка — {MAX_BET:,} 🪙",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Максимум: <b>{MAX_BET:,} 🪙</b>", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    db = get_db()
    has_funds = False
    has_bets = False
    has_bj_games = False
    async for session in db.get_session():
        try:
            ur = await session.execute(select(User).where(User.tg_id == user_id))
            u = ur.scalar_one_or_none()
            if u:
                has_funds = u.balance_vv >= bet
                can_bet, _ = u.check_casino_limit()
                has_bets = can_bet
                can_bj, _ = u.check_bj_limit()
                has_bj_games = can_bj
        except Exception:
            pass
        finally:
            await session.close()

    if not has_funds:
        r = InlineQueryResultArticle(
            id="bet_no_funds",
            title="❌ Недостаточно средств",
            description=f"Нужно {bet:,} 🪙",
            input_message_content=InputTextMessageContent(
                message_text="❌ Недостаточно средств!", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    if not has_bets and not has_bj_games:
        r = InlineQueryResultArticle(
            id="bet_limit",
            title=f"❌ Лимит {MAX_DAILY_BETS} ставок/день (казино) и {MAX_DAILY_BJ} игр/день (блэкджек)",
            description="Сброс в полночь UTC",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ <b>Лимит исчерпан!</b> Казино: {MAX_DAILY_BETS}/день, Блэкджек: {MAX_DAILY_BJ}/день", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    results = []

    if has_bets:
        # Результат казино
        casino_result_id = f"casino_{user_id}_{bet}"
        casino_result = InlineQueryResultArticle(
            id=casino_result_id,
            title=f"🎰 Казино — ставка {bet:,} 🪙",
            description="Нажмите чтобы отправить в чат",
            input_message_content=InputTextMessageContent(
                message_text=f"🎰 <b>{user_first_name}</b> ставит <b>{bet:,} 🪙</b>!\n\nНажми кнопку 👇",
                parse_mode="HTML"),
            reply_markup=get_casino_keyboard(bet, owner_id=user_id, chat_id=0))

        # Заполняем _pending_casino для логирования в casino_chosen_result
        try:
            from handlers.casino import _pending_casino
            _pending_casino[casino_result_id] = {
                "user_id": user_id, "bet": bet, "user_first_name": user_first_name}
        except Exception:
            pass

        results.append(casino_result)

    if has_bj_games:
        # Результат блэкджека
        bj_result_id = f"bj_{user_id}_{bet}"
        bj_result = InlineQueryResultArticle(
            id=bj_result_id,
            title=f"🃏 Блэкджек — ставка {bet:,} 🪙",
            description="Нажмите чтобы отправить в чат",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🃏 <b>{user_first_name}</b> садится за стол блэкджека!\n"
                    f"💰 Ставка: <b>{bet:,} 🪙</b>\n\n"
                    f"⏳ Ожидание начала игры..."
                ),
                parse_mode="HTML"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🃏 Начать игру",
                    callback_data=f"bj_start_{user_id}_{bet}_0")
            ]]),
        )
        _pending_bj[bj_result_id] = {
            "user_id": user_id, "bet": bet, "user_first_name": user_first_name}

        results.append(bj_result)

    await inline_query.bot.answer_inline_query(
        inline_query.id, results, cache_time=1, is_personal=True)


async def bj_chosen_result(chosen: ChosenInlineResult) -> None:
    """Сохраняет inline_message_id когда результат блэкджека выбран."""
    rid = chosen.result_id
    if not rid or not rid.startswith("bj_"):
        return
    info = _pending_bj.pop(rid, None)
    if not info:
        return
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        return
    _bj_owner_map[info["user_id"]] = inline_message_id
    logger.info(f"🃏 BJ Chosen: {rid}, inline_id={inline_message_id}")


# ============================================================================
# СТАРТ ИГРЫ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("bj_start_"))
async def bj_start_handler(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        # bj_start_{owner_id}_{bet}_{chat_id}
        owner_id = int(parts[2])
        bet = int(parts[3])
        chat_id_from_data = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка данных!", show_alert=True)
        return

    if call.from_user.id != owner_id:
        await call.answer("❌ Не твоя игра!", show_alert=True)
        return
    if bet < MIN_BET or bet > MAX_BET:
        await call.answer("❌ Некорректная ставка!", show_alert=True)
        return

    await call.answer()

    inline_id = call.inline_message_id or _bj_owner_map.get(owner_id)
    user_first_name = call.from_user.first_name or "Игрок"

    chat_id = await _resolve_chat_id(call, owner_id, chat_id_from_data)
    if not chat_id:
        try:
            await call.bot.edit_message_text(
                text="❌ Добавьте бота в группу или напишите в чат — тогда можно играть!",
                inline_message_id=inline_id, parse_mode="HTML")
        except Exception:
            pass
        return

    # Списываем ставку
    db = get_db()
    async for session in db.get_session():
        try:
            await track_chat_activity(session, chat_id, owner_id)
            ur = await session.execute(select(User).where(User.tg_id == owner_id).with_for_update())
            user = ur.scalar_one_or_none()
            if not user:
                if inline_id:
                    try:
                        await call.bot.edit_message_text(
                            text="❌ /start в ЛС бота!", inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                return
            if user.balance_vv < bet:
                if inline_id:
                    try:
                        await call.bot.edit_message_text(
                            text=f"❌ Баланс: <b>{user.balance_vv:,} 🪙</b>",
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                return
            if not user.use_bj_game():
                if inline_id:
                    try:
                        await call.bot.edit_message_text(
                            text=f"❌ <b>Лимит игр в блэкджек!</b> {MAX_DAILY_BJ}/день",
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                await session.commit()
                return
            user.balance_vv -= bet
            user.increment_action()
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ bj_start bet: {e}", exc_info=True)
            if inline_id:
                try:
                    await call.bot.edit_message_text(
                        text="❌ Ошибка при списании ставки!",
                        inline_message_id=inline_id, parse_mode="HTML")
                except Exception:
                    pass
            return
        finally:
            await session.close()

    # Раздача карт
    deck = _make_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]

    session_data = {
        "owner_id": owner_id,
        "owner_name": user_first_name,
        "bet": bet,
        "chat_id": chat_id,
        "deck": deck,
        "player_hand": player_hand,
        "dealer_hand": dealer_hand,
    }

    if inline_id:
        _bj_sessions[inline_id] = session_data
        _bj_owner_map[owner_id] = inline_id
        remember_chat(owner_id, chat_id)

    player_score = calculate_hand(player_hand)

    # Натуральный блэкджек (21 на первых двух картах)
    if player_score == 21:
        winnings = int(bet * 1.5)
        payout = bet + winnings  # 2.5x ставки
        text = "❌ Ошибка финала игры!"

        db2 = get_db()
        async for session2 in db2.get_session():
            try:
                ur2 = await session2.execute(select(User).where(User.tg_id == owner_id))
                user2 = ur2.scalar_one_or_none()
                new_levels = []
                if user2:
                    user2.balance_vv += payout
                    old_level = user2.level
                    new_levels = add_xp(user2, winnings)
                await session2.commit()
                if user2 and new_levels:
                    await grant_level_rewards(call.bot, session2, user2, old_level, new_levels)
                    await session2.commit()

                dealer_reveal = calculate_hand(dealer_hand)
                _, remaining = user2.check_bj_limit() if user2 else (False, 0)
                balance = user2.balance_vv if user2 else 0

                text = (
                    f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                    f"👤 Ваши карты:\n  {_hand_str(player_hand)} ({player_score} очков)\n\n"
                    f"🎩 Дилер:\n  {_hand_str(dealer_hand)} ({dealer_reveal} очков)\n\n"
                    f"💰 Ставка: {bet:,} 🪙\n"
                    f"🃏 <b>НАТУРАЛЬНЫЙ БЛЭКДЖЕК!</b> Выигрыш x1.5!\n"
                    f"🎉 Получено: <b>+{payout:,} 🪙</b>\n\n"
                    f"💼 Баланс: <b>{balance:,} 🪙</b>\n"
                    f"🃏 Игры: {remaining}/{MAX_DAILY_BJ}"
                )
            except Exception as e:
                await session2.rollback()
                logger.error(f"❌ bj natural: {e}", exc_info=True)
            finally:
                await session2.close()

        if inline_id:
            try:
                await call.bot.edit_message_text(
                    text=text, inline_message_id=inline_id, parse_mode="HTML",
                    reply_markup=_bj_again_keyboard(owner_id, bet, chat_id))
            except Exception as e:
                if "MessageNotModified" not in str(e):
                    logger.error(f"❌ bj edit natural: {e}")
            _bj_sessions.pop(inline_id, None)
        _bj_owner_map.pop(owner_id, None)
        _bj_locks.pop(owner_id, None)
        return

    # Обычное начало игры
    text = _game_text(session_data)
    if inline_id:
        try:
            await call.bot.edit_message_text(
                text=text, inline_message_id=inline_id,
                parse_mode="HTML", reply_markup=_bj_keyboard(owner_id))
        except Exception as e:
            if "MessageNotModified" not in str(e):
                logger.error(f"❌ bj edit start: {e}")


# ============================================================================
# ЕЩЁ (взять карту)
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("bj_hit_"))
async def bj_hit_handler(call: CallbackQuery) -> None:
    try:
        owner_id = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        await call.answer()
        return

    if call.from_user.id != owner_id:
        await call.answer("❌ Не твоя игра!", show_alert=True)
        return

    await call.answer()

    inline_id = call.inline_message_id or _bj_owner_map.get(owner_id)
    if not inline_id or inline_id not in _bj_sessions:
        return

    # Защита от двойного нажатия
    if owner_id not in _bj_locks:
        _bj_locks[owner_id] = asyncio.Lock()
    lock = _bj_locks[owner_id]
    if lock.locked():
        return

    async with lock:
        session_data = _bj_sessions.get(inline_id)
        if not session_data:
            return

        deck = session_data["deck"]
        if not deck:
            deck.extend(_make_deck())
        card = deck.pop()
        session_data["player_hand"].append(card)

        player_score = calculate_hand(session_data["player_hand"])

        if player_score > 21:
            # Перебор
            bet = session_data["bet"]
            chat_id = session_data["chat_id"]
            dealer_hand = session_data["dealer_hand"]
            pa = int(bet * POT_PERCENT)
            burned = bet - pa
            text = "❌ Ошибка обработки перебора!"

            db = get_db()
            async for session_db in db.get_session():
                try:
                    await track_chat_activity(session_db, chat_id, owner_id)
                    gr = await session_db.execute(
                        select(GroupChat).where(GroupChat.chat_id == chat_id))
                    group = gr.scalar_one_or_none()
                    if not group:
                        group = GroupChat(chat_id=chat_id, common_pot=0)
                        session_db.add(group)
                        await session_db.flush()
                    group.common_pot = min(MAX_COMMON_POT, group.common_pot + pa)
                    await check_pot_explosion(session_db, chat_id, call.bot)
                    await session_db.commit()
                    if group:
                        await session_db.refresh(group)
                    pot = group.common_pot if group else 0

                    ur = await session_db.execute(
                        select(User).where(User.tg_id == owner_id))
                    user_obj = ur.scalar_one_or_none()
                    _, remaining = user_obj.check_bj_limit() if user_obj else (False, 0)
                    balance = user_obj.balance_vv if user_obj else 0
                    dealer_reveal = calculate_hand(dealer_hand)

                    text = (
                        f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                        f"👤 Ваши карты:\n  {_hand_str(session_data['player_hand'])} ({player_score} очков)\n\n"
                        f"🎩 Дилер:\n  {_hand_str(dealer_hand)} ({dealer_reveal} очков)\n\n"
                        f"💰 Ставка: {bet:,} 🪙\n"
                        f"💥 <b>Перебор! {player_score} > 21</b>\n"
                        f"🏦 В общак (50%): <b>+{pa:,} 🪙</b>\n"
                        f"🔥 Сгорело (50%): <b>{burned:,} 🪙</b>\n\n"
                        f"💼 Баланс: <b>{balance:,} 🪙</b>\n"
                        f"🃏 Игры: {remaining}/{MAX_DAILY_BJ}"
                    )
                except Exception as e:
                    await session_db.rollback()
                    logger.error(f"❌ bj bust: {e}", exc_info=True)
                finally:
                    await session_db.close()

            try:
                await call.bot.edit_message_text(
                    text=text, inline_message_id=inline_id, parse_mode="HTML",
                    reply_markup=_bj_again_keyboard(owner_id, bet, chat_id))
            except Exception as e:
                if "MessageNotModified" not in str(e):
                    logger.error(f"❌ bj edit bust: {e}")

            _bj_sessions.pop(inline_id, None)
            _bj_owner_map.pop(owner_id, None)
            _bj_locks.pop(owner_id, None)
        else:
            # Игра продолжается
            text = _game_text(session_data)
            try:
                await call.bot.edit_message_text(
                    text=text, inline_message_id=inline_id,
                    parse_mode="HTML", reply_markup=_bj_keyboard(owner_id))
            except Exception as e:
                if "MessageNotModified" not in str(e):
                    logger.error(f"❌ bj edit hit: {e}")


# ============================================================================
# СТОП (дилер добирает карты, итог)
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("bj_stand_"))
async def bj_stand_handler(call: CallbackQuery) -> None:
    try:
        owner_id = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        await call.answer()
        return

    if call.from_user.id != owner_id:
        await call.answer("❌ Не твоя игра!", show_alert=True)
        return

    await call.answer()

    inline_id = call.inline_message_id or _bj_owner_map.get(owner_id)
    if not inline_id or inline_id not in _bj_sessions:
        return

    if owner_id not in _bj_locks:
        _bj_locks[owner_id] = asyncio.Lock()
    lock = _bj_locks[owner_id]
    if lock.locked():
        return

    async with lock:
        session_data = _bj_sessions.get(inline_id)
        if not session_data:
            return

        bet = session_data["bet"]
        chat_id = session_data["chat_id"]
        dealer_hand = session_data["dealer_hand"]
        player_hand = session_data["player_hand"]
        deck = session_data["deck"]
        player_score = calculate_hand(player_hand)

        # Дилер добирает карты до 17+
        while calculate_hand(dealer_hand) < 17:
            if not deck:
                deck.extend(_make_deck())
            dealer_hand.append(deck.pop())
            dealer_score_now = calculate_hand(dealer_hand)

            text_progress = (
                f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                f"👤 Ваши карты:\n  {_hand_str(player_hand)} ({player_score} очков)\n\n"
                f"🎩 Дилер тянет карту...\n  {_hand_str(dealer_hand)} ({dealer_score_now} очков)\n\n"
                f"💰 Ставка: {bet:,} 🪙"
            )
            try:
                await call.bot.edit_message_text(
                    text=text_progress, inline_message_id=inline_id, parse_mode="HTML")
            except Exception as e:
                if "MessageNotModified" not in str(e):
                    logger.error(f"❌ bj dealer draw: {e}")
            await asyncio.sleep(0.5)

        dealer_score = calculate_hand(dealer_hand)
        text = "❌ Ошибка обработки итога!"

        db = get_db()
        async for session_db in db.get_session():
            try:
                await track_chat_activity(session_db, chat_id, owner_id)
                ur = await session_db.execute(select(User).where(User.tg_id == owner_id))
                user_obj = ur.scalar_one_or_none()
                gr = await session_db.execute(
                    select(GroupChat).where(GroupChat.chat_id == chat_id))
                group = gr.scalar_one_or_none()
                if not group:
                    group = GroupChat(chat_id=chat_id, common_pot=0)
                    session_db.add(group)
                    await session_db.flush()

                if dealer_score > 21 or player_score > dealer_score:
                    # Победа игрока
                    winnings = bet * 2
                    new_levels = []
                    if user_obj:
                        user_obj.balance_vv += winnings
                        old_level = user_obj.level
                        new_levels = add_xp(user_obj, winnings)
                    await session_db.commit()
                    if user_obj and new_levels:
                        await grant_level_rewards(call.bot, session_db, user_obj, old_level, new_levels)
                        await session_db.commit()
                    if group:
                        await session_db.refresh(group)
                    pot = group.common_pot if group else 0
                    _, remaining = user_obj.check_bj_limit() if user_obj else (False, 0)
                    balance = user_obj.balance_vv if user_obj else 0

                    if dealer_score > 21:
                        outcome = f"💥 <b>Дилер перебрал ({dealer_score})!</b> Вы выиграли!"
                    else:
                        outcome = f"🏆 <b>Вы выиграли!</b> ({player_score} vs {dealer_score})"

                    text = (
                        f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                        f"👤 Ваши карты:\n  {_hand_str(player_hand)} ({player_score} очков)\n\n"
                        f"🎩 Дилер:\n  {_hand_str(dealer_hand)} ({dealer_score} очков)\n\n"
                        f"💰 Ставка: {bet:,} 🪙\n"
                        f"{outcome}\n"
                        f"🎉 Получено: <b>+{winnings:,} 🪙</b>\n\n"
                        f"💼 Баланс: <b>{balance:,} 🪙</b>\n"
                        f"🃏 Игры: {remaining}/{MAX_DAILY_BJ}"
                    )

                elif player_score == dealer_score:
                    # Ничья — возврат ставки
                    if user_obj:
                        user_obj.balance_vv += bet
                    await session_db.commit()
                    if group:
                        await session_db.refresh(group)
                    pot = group.common_pot if group else 0
                    _, remaining = user_obj.check_bj_limit() if user_obj else (False, 0)
                    balance = user_obj.balance_vv if user_obj else 0

                    text = (
                        f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                        f"👤 Ваши карты:\n  {_hand_str(player_hand)} ({player_score} очков)\n\n"
                        f"🎩 Дилер:\n  {_hand_str(dealer_hand)} ({dealer_score} очков)\n\n"
                        f"💰 Ставка: {bet:,} 🪙\n"
                        f"🤝 <b>Ничья!</b> Ставка возвращена.\n"
                        f"💰 Возврат: <b>{bet:,} 🪙</b>\n\n"
                        f"💼 Баланс: <b>{balance:,} 🪙</b>\n"
                        f"🃏 Игры: {remaining}/{MAX_DAILY_BJ}"
                    )

                else:
                    # Победа дилера
                    pa = int(bet * POT_PERCENT)
                    burned = bet - pa
                    group.common_pot = min(MAX_COMMON_POT, group.common_pot + pa)
                    await check_pot_explosion(session_db, chat_id, call.bot)
                    await session_db.commit()
                    if group:
                        await session_db.refresh(group)
                    pot = group.common_pot if group else 0
                    _, remaining = user_obj.check_bj_limit() if user_obj else (False, 0)
                    balance = user_obj.balance_vv if user_obj else 0

                    text = (
                        f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                        f"👤 Ваши карты:\n  {_hand_str(player_hand)} ({player_score} очков)\n\n"
                        f"🎩 Дилер:\n  {_hand_str(dealer_hand)} ({dealer_score} очков)\n\n"
                        f"💰 Ставка: {bet:,} 🪙\n"
                        f"😞 <b>Дилер выиграл!</b> ({dealer_score} vs {player_score})\n"
                        f"🏦 В общак (50%): <b>+{pa:,} 🪙</b>\n"
                        f"🔥 Сгорело (50%): <b>{burned:,} 🪙</b>\n\n"
                        f"💼 Баланс: <b>{balance:,} 🪙</b>\n"
                        f"🃏 Игры: {remaining}/{MAX_DAILY_BJ}"
                    )
            except Exception as e:
                await session_db.rollback()
                logger.error(f"❌ bj stand result: {e}", exc_info=True)
            finally:
                await session_db.close()

        try:
            await call.bot.edit_message_text(
                text=text, inline_message_id=inline_id, parse_mode="HTML",
                reply_markup=_bj_again_keyboard(owner_id, bet, chat_id))
        except Exception as e:
            if "MessageNotModified" not in str(e):
                logger.error(f"❌ bj edit stand: {e}")

        _bj_sessions.pop(inline_id, None)
        _bj_owner_map.pop(owner_id, None)
        _bj_locks.pop(owner_id, None)


# ============================================================================
# ИГРАТЬ СНОВА (реванш)
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("bj_again_"))
async def bj_again_handler(call: CallbackQuery) -> None:
    try:
        parts = call.data.split("_")
        # bj_again_{owner_id}_{bet}_{chat_id}
        owner_id = int(parts[2])
        bet = int(parts[3])
        chat_id = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка данных!", show_alert=True)
        return

    if call.from_user.id != owner_id:
        await call.answer("❌ Не твоя игра!", show_alert=True)
        return
    if bet < MIN_BET or bet > MAX_BET:
        await call.answer("❌ Некорректная ставка!", show_alert=True)
        return

    await call.answer()

    inline_id = call.inline_message_id or _bj_owner_map.get(owner_id)
    if not inline_id:
        return

    # Защита от двойного нажатия
    if owner_id not in _bj_locks:
        _bj_locks[owner_id] = asyncio.Lock()
    lock = _bj_locks[owner_id]
    if lock.locked():
        return

    async with lock:
        user_first_name = call.from_user.first_name or "Игрок"

        # Проверяем баланс и лимит ставок
        db = get_db()
        async for session in db.get_session():
            try:
                await track_chat_activity(session, chat_id, owner_id)
                ur = await session.execute(select(User).where(User.tg_id == owner_id).with_for_update())
                user = ur.scalar_one_or_none()
                if not user:
                    try:
                        await call.bot.edit_message_text(
                            text="❌ /start в ЛС бота!",
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                    return
                if user.balance_vv < bet:
                    balance = user.balance_vv
                    try:
                        await call.bot.edit_message_text(
                            text=(
                                f"❌ Недостаточно средств для повторной ставки ({bet:,} 🪙)!\n"
                                f"💼 Баланс: {balance:,} 🪙"
                            ),
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception as e:
                        if "MessageNotModified" not in str(e):
                            logger.error(f"❌ bj again no funds: {e}")
                    return
                if not user.use_bj_game():
                    try:
                        await call.bot.edit_message_text(
                            text=f"❌ <b>Лимит игр в блэкджек!</b> {MAX_DAILY_BJ}/день",
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                    await session.commit()
                    return
                user.balance_vv -= bet
                user.increment_action()
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"❌ bj_again bet: {e}", exc_info=True)
                try:
                    await call.bot.edit_message_text(
                        text="❌ Ошибка при списании ставки!",
                        inline_message_id=inline_id, parse_mode="HTML")
                except Exception:
                    pass
                return
            finally:
                await session.close()

        # Раздача карт
        deck = _make_deck()
        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]

        session_data = {
            "owner_id": owner_id,
            "owner_name": user_first_name,
            "bet": bet,
            "chat_id": chat_id,
            "deck": deck,
            "player_hand": player_hand,
            "dealer_hand": dealer_hand,
        }

        _bj_sessions[inline_id] = session_data
        _bj_owner_map[owner_id] = inline_id
        remember_chat(owner_id, chat_id)

        player_score = calculate_hand(player_hand)

        # Натуральный блэкджек при реванше
        if player_score == 21:
            winnings = int(bet * 1.5)
            payout = bet + winnings
            text = "❌ Ошибка финала игры!"

            db2 = get_db()
            async for session2 in db2.get_session():
                try:
                    ur2 = await session2.execute(select(User).where(User.tg_id == owner_id))
                    user2 = ur2.scalar_one_or_none()
                    new_levels = []
                    if user2:
                        user2.balance_vv += payout
                        old_level = user2.level
                        new_levels = add_xp(user2, winnings)
                    await session2.commit()
                    if user2 and new_levels:
                        await grant_level_rewards(call.bot, session2, user2, old_level, new_levels)
                        await session2.commit()

                    dealer_reveal = calculate_hand(dealer_hand)
                    _, remaining = user2.check_bj_limit() if user2 else (False, 0)
                    balance = user2.balance_vv if user2 else 0

                    text = (
                        f"🃏 <b>БЛЭКДЖЕК</b>\n\n"
                        f"👤 Ваши карты:\n  {_hand_str(player_hand)} ({player_score} очков)\n\n"
                        f"🎩 Дилер:\n  {_hand_str(dealer_hand)} ({dealer_reveal} очков)\n\n"
                        f"💰 Ставка: {bet:,} 🪙\n"
                        f"🃏 <b>НАТУРАЛЬНЫЙ БЛЭКДЖЕК!</b> Выигрыш x1.5!\n"
                        f"🎉 Получено: <b>+{payout:,} 🪙</b>\n\n"
                        f"💼 Баланс: <b>{balance:,} 🪙</b>\n"
                        f"🃏 Игры: {remaining}/{MAX_DAILY_BJ}"
                    )
                except Exception as e:
                    await session2.rollback()
                    logger.error(f"❌ bj again natural: {e}", exc_info=True)
                finally:
                    await session2.close()

            try:
                await call.bot.edit_message_text(
                    text=text, inline_message_id=inline_id, parse_mode="HTML",
                    reply_markup=_bj_again_keyboard(owner_id, bet, chat_id))
            except Exception as e:
                if "MessageNotModified" not in str(e):
                    logger.error(f"❌ bj again edit natural: {e}")
            _bj_sessions.pop(inline_id, None)
            _bj_owner_map.pop(owner_id, None)
            _bj_locks.pop(owner_id, None)
            return

        # Обычное начало новой игры — редактируем старое финальное сообщение
        text = _game_text(session_data)
        try:
            await call.bot.edit_message_text(
                text=text, inline_message_id=inline_id,
                parse_mode="HTML", reply_markup=_bj_keyboard(owner_id))
        except Exception as e:
            if "MessageNotModified" not in str(e):
                logger.error(f"❌ bj again edit start: {e}")

