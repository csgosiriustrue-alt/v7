"""Казино без пингов — first_name вместо @username. + XP за выигрыш.
ИСПРАВЛЕНО: chat_id строго привязан к контексту, убран ненадёжный _user_chat_map фоллбэк в инлайне."""
import logging
import asyncio
import random
import re
from aiogram import Router
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    CallbackQuery, ChosenInlineResult, Message,
)
from aiogram.filters import Command
from sqlalchemy import select

from database import get_db
from models import User, GroupChat, MAX_DAILY_BETS
from utils.casino_utils import calculate_winnings, MIN_BET, MAX_BET, MAX_COMMON_POT, POT_PERCENT
from utils.keyboards import get_casino_keyboard
from utils.pot_event import track_chat_activity, check_pot_explosion, POT_EXPLOSION_THRESHOLD
from utils.levels import add_xp, grant_level_rewards

logger = logging.getLogger(__name__)
router = Router()

_user_chat_map: dict[int, int] = {}
_inline_chat_map: dict[str, int] = {}
_pending_casino: dict[str, dict] = {}
_last_dice: dict[int, tuple[int, int]] = {}
_chat_map_loaded = False


async def _ensure_chat_map_loaded() -> None:
    global _chat_map_loaded
    if _chat_map_loaded:
        return
    _chat_map_loaded = True
    db = get_db()
    async for session in db.get_session():
        try:
            from models import ChatActivity
            result = await session.execute(select(ChatActivity))
            for a in result.scalars().all():
                _user_chat_map[a.user_id] = a.chat_id
            logger.info(f"📌 Загружено {len(_user_chat_map)} маппингов user→chat")
        except Exception as e:
            logger.error(f"❌ load chat_map: {e}")
        finally:
            await session.close()


def remember_chat(user_id: int, chat_id: int) -> None:
    _user_chat_map[user_id] = chat_id


async def _delete_last_dice(bot, user_id: int) -> None:
    prev = _last_dice.pop(user_id, None)
    if prev:
        try:
            await bot.delete_message(chat_id=prev[0], message_id=prev[1])
        except Exception:
            pass


_CASINO_TRIGGERS = re.compile(
    r"^(?:🎰|казино|ставка|крутить|крутануть|слот|слоты|casino|bet|spin)\s+(\d+)",
    re.IGNORECASE)


def _match_casino_trigger(text: str) -> int | None:
    if not text:
        return None
    m = _CASINO_TRIGGERS.match(text.strip())
    return int(m.group(1)) if m else None


def _build_lvl_text(new_levels: list[int]) -> str:
    """Текст поздравления с уровнем для казино."""
    lvl_text = ""
    for lvl in new_levels:
        lvl_text += f"\n\n🆙 <b>Уровень {lvl}!</b> Ваше влияние растёт!"
    return lvl_text


# ============================================================================
# ТЕКСТОВЫЕ ТРИГГЕРЫ (chat_id берётся из message.chat.id — корректно)
# ============================================================================


@router.message(
    lambda m: m.chat.type in ("group", "supergroup") and m.text and _match_casino_trigger(m.text) is not None)
async def text_casino_handler(message: Message) -> None:
    bet = _match_casino_trigger(message.text)
    if bet is None:
        return
    user_id = message.from_user.id
    chat_id = message.chat.id  # ← строго из сообщения
    user_first_name = message.from_user.first_name or "Игрок"
    remember_chat(user_id, chat_id)
    if bet < MIN_BET:
        await message.reply(f"❌ Минимум: <b>{MIN_BET} 🪙</b>", parse_mode="HTML")
        return
    if bet > MAX_BET:
        await message.reply(f"❌ Максимум: <b>{MAX_BET:,} 🪙</b>", parse_mode="HTML")
        return
    await _play_casino(bot=message.bot, user_id=user_id, user_first_name=user_first_name,
        chat_id=chat_id, bet=bet, reply_to_message_id=message.message_id)


# ============================================================================
# ЛОГИКА (текстовый триггер — chat_id всегда из message)
# ============================================================================


async def _play_casino(bot, user_id, user_first_name, chat_id, bet, reply_to_message_id=None):
    db = get_db()
    async for session in db.get_session():
        try:
            await track_chat_activity(session, chat_id, user_id)
            ur = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = ur.scalar_one_or_none()
            if not user:
                try:
                    await bot.send_message(chat_id=chat_id, text="❌ /start в ЛС!",
                        reply_to_message_id=reply_to_message_id)
                except Exception:
                    pass
                return
            if user.balance_vv < bet:
                try:
                    await bot.send_message(chat_id=chat_id,
                        text=f"❌ Баланс: <b>{user.balance_vv:,} 🪙</b>",
                        parse_mode="HTML", reply_to_message_id=reply_to_message_id)
                except Exception:
                    pass
                return

            if not user.use_casino_bet():
                can_bet, remaining = user.check_casino_limit()
                try:
                    await bot.send_message(chat_id=chat_id,
                        text=f"🎰 <b>Лимит ставок исчерпан!</b>\n\n"
                             f"Максимум <b>{MAX_DAILY_BETS}</b> ставок в день.\n"
                             f"Сброс по UTC в полночь.",
                        parse_mode="HTML", reply_to_message_id=reply_to_message_id)
                except Exception:
                    pass
                await session.commit()
                return

            user.balance_vv -= bet
            user.increment_action()

            gr = await session.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
            group = gr.scalar_one_or_none()
            if not group:
                group = GroupChat(chat_id=chat_id, common_pot=0)
                session.add(group)
                await session.flush()
            common_pot_before = group.common_pot
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ casino bet: {e}", exc_info=True)
            return
        finally:
            await session.close()

    await _delete_last_dice(bot, user_id)
    dice_value = None
    try:
        dice_msg = await bot.send_dice(chat_id=chat_id, emoji="🎰",
            reply_to_message_id=reply_to_message_id)
        dice_value = dice_msg.dice.value
        _last_dice[user_id] = (chat_id, dice_msg.message_id)
    except Exception as e:
        logger.error(f"❌ Dice: {e}")
    if dice_value is None:
        dice_value = random.randint(1, 64)
    await asyncio.sleep(3)

    gross, multiplier, description = calculate_winnings(dice_value, bet, common_pot_before)

    async for session2 in db.get_session():
        try:
            u2r = await session2.execute(select(User).where(User.tg_id == user_id))
            user2 = u2r.scalar_one_or_none()
            g2r = await session2.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
            group2 = g2r.scalar_one_or_none()

            if gross > 0:
                user2.balance_vv += gross

                # ── XP за выигрыш в казино ──
                old_level = user2.level
                new_levels = add_xp(user2, gross)
                lvl_text = _build_lvl_text(new_levels)

                if group2 and multiplier == 20:
                    group2.common_pot = max(0, group2.common_pot - int(common_pot_before * 0.10))
                await session2.commit()
                if new_levels:
                    await grant_level_rewards(bot, session2, user2, old_level, new_levels)
                    await session2.commit()
                if group2:
                    await session2.refresh(group2)
                pd = group2.common_pot if group2 else 0

                _, remaining = user2.check_casino_limit()
                rt = (
                    f"🎰 <b>{user_first_name}</b>\n{description}\n\n"
                    f"💰 Ставка: <b>{bet:,}</b>\n🎉 Выигрыш: <b>+{gross:,} 🪙</b>\n\n"
                    f"💼 Баланс: <b>{user2.balance_vv:,} 🪙</b>\n"
                    f"🏦 Общак: <b>{pd:,} 🪙</b>\n"
                    f"🎰 Ставки: {remaining}/{MAX_DAILY_BETS}"
                    f"{lvl_text}")
            else:
                pa = int(bet * POT_PERCENT)
                burned = bet - pa
                if group2:
                    group2.common_pot = min(MAX_COMMON_POT, group2.common_pot + pa)
                    await check_pot_explosion(session2, chat_id, bot)
                await session2.commit()
                if group2:
                    await session2.refresh(group2)
                pd = group2.common_pot if group2 else 0

                _, remaining = user2.check_casino_limit()
                rt = (
                    f"🎰 <b>{user_first_name}</b>\n{description}\n\n"
                    f"💸 Ставка: <b>{bet:,}</b>\n🏦 В общак (50%): <b>+{pa:,} 🪙</b>\n"
                    f"🔥 Сгорело (50%): <b>{burned:,} 🪙</b>\n\n"
                    f"💼 Баланс: <b>{user2.balance_vv:,} 🪙</b>\n"
                    f"🏦 Общак: <b>{pd:,} 🪙</b>\n"
                    f"🎰 Ставки: {remaining}/{MAX_DAILY_BETS}")

            await _delete_last_dice(bot, user_id)
            try:
                await bot.send_message(chat_id=chat_id, text=rt, parse_mode="HTML",
                    reply_to_message_id=reply_to_message_id)
            except Exception:
                try:
                    await bot.send_message(chat_id=chat_id, text=rt, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"❌ result: {e}")
        except Exception as e:
            await session2.rollback()
            logger.error(f"❌ casino result: {e}", exc_info=True)
        finally:
            await session2.close()


# ============================================================================
# INLINE
# ============================================================================


async def casino_inline_handler(inline_query: InlineQuery) -> None:
    query_text = inline_query.query.strip()
    if not query_text.isdigit():
        return
    await _ensure_chat_map_loaded()
    bet = int(query_text)
    user_id = inline_query.from_user.id
    user_first_name = inline_query.from_user.first_name or "Игрок"
    if bet < MIN_BET:
        r = InlineQueryResultArticle(id="casino_low_bet", title=f"❌ Минимум: {MIN_BET} 🪙",
            description=f"Минимум — {MIN_BET}.",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Минимум: <b>{MIN_BET} 🪙</b>", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return
    if bet > MAX_BET:
        r = InlineQueryResultArticle(id="casino_high_bet", title=f"❌ Максимум: {MAX_BET:,} 🪙",
            description=f"Максимум — {MAX_BET:,}.",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Максимум: <b>{MAX_BET:,} 🪙</b>", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    db = get_db()
    has_funds = False
    has_bets = False
    async for session in db.get_session():
        try:
            ur = await session.execute(select(User).where(User.tg_id == user_id))
            u = ur.scalar_one_or_none()
            if u:
                if u.balance_vv >= bet:
                    has_funds = True
                can_bet, _ = u.check_casino_limit()
                has_bets = can_bet
        except Exception:
            pass
        finally:
            await session.close()

    if not has_funds:
        r = InlineQueryResultArticle(id="casino_no_funds", title="❌ Недостаточно",
            description=f"Нет {bet} 🪙",
            input_message_content=InputTextMessageContent(message_text="❌ Недостаточно!", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    if not has_bets:
        r = InlineQueryResultArticle(id="casino_limit", title=f"❌ Лимит {MAX_DAILY_BETS} ставок/день",
            description="Сброс в полночь UTC",
            input_message_content=InputTextMessageContent(
                message_text=f"🎰 <b>Лимит ставок!</b> {MAX_DAILY_BETS}/день", parse_mode="HTML"))
        await inline_query.bot.answer_inline_query(inline_query.id, [r], cache_time=1, is_personal=True)
        return

    result_id = f"casino_{user_id}_{bet}"
    result = InlineQueryResultArticle(
        id=result_id, title=f"🎰 Казино — ставка {bet:,} 🪙",
        description="Нажмите чтобы отправить в чат",
        input_message_content=InputTextMessageContent(
            message_text=f"🎰 <b>{user_first_name}</b> ставит <b>{bet:,} 🪙</b>!\n\nНажми кнопку 👇",
            parse_mode="HTML"),
        reply_markup=get_casino_keyboard(bet, owner_id=user_id, chat_id=0))
    _pending_casino[result_id] = {"user_id": user_id, "bet": bet, "user_first_name": user_first_name}
    await inline_query.bot.answer_inline_query(inline_query.id, [result], cache_time=1, is_personal=True)


async def casino_chosen_result(chosen: ChosenInlineResult) -> None:
    rid = chosen.result_id
    if not rid or not rid.startswith("casino_"):
        return
    info = _pending_casino.pop(rid, None)
    if not info:
        return
    user_id = info["user_id"]
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        return
    logger.info(f"🎰 Chosen: {rid}")


# ============================================================================
# SPIN (ИСПРАВЛЕНО: надёжное определение chat_id)
# ============================================================================


@router.callback_query(lambda call: call.data and call.data.startswith("casino_spin_"))
async def casino_spin_handler(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    user_first_name = call.from_user.first_name or "Игрок"
    inline_id = call.inline_message_id
    try:
        parts = call.data.split("_")
        owner_id = int(parts[2])
        bet = int(parts[3])
        chat_id_from_button = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await call.answer("❌", show_alert=True)
        return
    if owner_id != user_id:
        await call.answer("❌ Не твоя ставка!", show_alert=True)
        return
    if bet < MIN_BET or bet > MAX_BET:
        await call.answer("❌ Некорректная ставка!", show_alert=True)
        return

    # ══════════════════════════════════════════════════════════════════
    # Цепочка определения chat_id (от наиболее надёжного к запасному)
    # 1. call.message.chat — надёжный источник для не-инлайн callback
    # 2. _inline_chat_map  — если был сохранён ранее (надёжный маппинг)
    # 3. chat_id_from_button — из callback_data, только если != 0
    # 4. БД ChatActivity   — последний фоллбэк
    #
    # НЕ используем _user_chat_map — он может указывать на другой чат!
    # ══════════════════════════════════════════════════════════════════
    chat_id = None

    # Шаг 1: из call.message.chat (обычный callback в группе — самый надёжный)
    if call.message and call.message.chat:
        if call.message.chat.type in ("group", "supergroup"):
            chat_id = call.message.chat.id

    # Шаг 2: из _inline_chat_map (привязан к конкретному inline_message_id)
    if not chat_id and inline_id and inline_id in _inline_chat_map:
        chat_id = _inline_chat_map[inline_id]

    # Шаг 3: из callback_data (только если явно задан, т.е. != 0)
    if not chat_id and chat_id_from_button and chat_id_from_button != 0:
        chat_id = chat_id_from_button

    # Шаг 4: последний фоллбэк — из БД (НЕ из _user_chat_map!)
    if not chat_id:
        db = get_db()
        async for session in db.get_session():
            try:
                from models import ChatActivity
                act_r = await session.execute(
                    select(ChatActivity).where(ChatActivity.user_id == user_id).limit(1))
                act = act_r.scalar_one_or_none()
                if act:
                    chat_id = act.chat_id
            except Exception:
                pass
            finally:
                await session.close()

    if not chat_id:
        await call.answer("❌ Добавьте бота в группу, или отправьте /top!", show_alert=True)
        return

    # Обновляем маппинг ПОСЛЕ того, как точно определили правильный chat_id
    remember_chat(user_id, chat_id)
    if inline_id:
        _inline_chat_map[inline_id] = chat_id
        try:
            await call.bot.edit_message_text(
                text=f"🎰 <b>{user_first_name}</b> крутит...\nСтавка: <b>{bet:,} 🪙</b>",
                inline_message_id=inline_id, parse_mode="HTML")
        except Exception:
            pass

    await _play_casino_inline(bot=call.bot, user_id=user_id, user_first_name=user_first_name,
        chat_id=chat_id, bet=bet, inline_id=inline_id)
    await call.answer()


# ============================================================================
# ИНЛАЙН КАЗИНО (chat_id передаётся явно из spin_handler)
# ============================================================================


async def _play_casino_inline(bot, user_id, user_first_name, chat_id, bet, inline_id=None):
    db = get_db()
    async for session in db.get_session():
        try:
            await track_chat_activity(session, chat_id, user_id)
            ur = await session.execute(select(User).where(User.tg_id == user_id).with_for_update())
            user = ur.scalar_one_or_none()
            if not user or user.balance_vv < bet:
                if inline_id:
                    bal = user.balance_vv if user else 0
                    try:
                        await bot.edit_message_text(text=f"❌ Баланс: <b>{bal:,} 🪙</b>",
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                return

            if not user.use_casino_bet():
                if inline_id:
                    try:
                        await bot.edit_message_text(
                            text=f"🎰 <b>Лимит ставок!</b> {MAX_DAILY_BETS}/день",
                            inline_message_id=inline_id, parse_mode="HTML")
                    except Exception:
                        pass
                await session.commit()
                return

            user.balance_vv -= bet
            user.increment_action()

            gr = await session.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
            group = gr.scalar_one_or_none()
            if not group:
                group = GroupChat(chat_id=chat_id, common_pot=0)
                session.add(group)
                await session.flush()
            common_pot_before = group.common_pot
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ inline bet: {e}", exc_info=True)
            return
        finally:
            await session.close()

    # ── Dice отправляется строго в переданный chat_id ──
    await _delete_last_dice(bot, user_id)
    dice_value = None
    try:
        dice_msg = await bot.send_dice(chat_id=chat_id, emoji="🎰")
        dice_value = dice_msg.dice.value
        _last_dice[user_id] = (chat_id, dice_msg.message_id)
    except Exception as e:
        logger.error(f"❌ Dice: {e}")
    if dice_value is None:
        dice_value = random.randint(1, 64)
    await asyncio.sleep(3)

    gross, multiplier, description = calculate_winnings(dice_value, bet, common_pot_before)

    async for session2 in db.get_session():
        try:
            u2r = await session2.execute(select(User).where(User.tg_id == user_id))
            user2 = u2r.scalar_one_or_none()
            g2r = await session2.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
            group2 = g2r.scalar_one_or_none()

            if gross > 0:
                user2.balance_vv += gross

                # ── XP за выигрыш в казино ──
                old_level = user2.level
                new_levels = add_xp(user2, gross)
                lvl_text = _build_lvl_text(new_levels)

                if group2 and multiplier == 20:
                    group2.common_pot = max(0, group2.common_pot - int(common_pot_before * 0.10))
                await session2.commit()
                if new_levels:
                    await grant_level_rewards(bot, session2, user2, old_level, new_levels)
                    await session2.commit()
                if group2:
                    await session2.refresh(group2)
                pd = group2.common_pot if group2 else 0

                _, remaining = user2.check_casino_limit()
                rt = (
                    f"🎰 <b>{user_first_name}</b>\n{description}\n\n"
                    f"💰 Ставка: <b>{bet:,}</b>\n🎉 <b>+{gross:,} 🪙</b>\n\n"
                    f"💼 <b>{user2.balance_vv:,} 🪙</b>\n🏦 Общак: <b>{pd:,} 🪙</b>\n"
                    f"🎰 Ставки: {remaining}/{MAX_DAILY_BETS}"
                    f"{lvl_text}")
            else:
                pa = int(bet * POT_PERCENT)
                burned = bet - pa
                if group2:
                    group2.common_pot = min(MAX_COMMON_POT, group2.common_pot + pa)
                    await check_pot_explosion(session2, chat_id, bot)
                await session2.commit()
                if group2:
                    await session2.refresh(group2)
                pd = group2.common_pot if group2 else 0

                _, remaining = user2.check_casino_limit()
                rt = (
                    f"🎰 <b>{user_first_name}</b>\n{description}\n\n"
                    f"💸 <b>{bet:,}</b>\n🏦 50%: <b>+{pa:,}</b> | 🔥 50%: <b>{burned:,}</b>\n\n"
                    f"💼 <b>{user2.balance_vv:,} 🪙</b>\n🏦 Общак: <b>{pd:,} 🪙</b>\n"
                    f"🎰 Ставки: {remaining}/{MAX_DAILY_BETS}")

            await _delete_last_dice(bot, user_id)
            if inline_id:
                try:
                    await bot.edit_message_text(text=rt, inline_message_id=inline_id, parse_mode="HTML",
                        reply_markup=get_casino_keyboard(bet, owner_id=user_id, chat_id=chat_id))
                except Exception as e:
                    logger.error(f"❌ edit inline: {e}")
        except Exception as e:
            await session2.rollback()
            logger.error(f"❌ inline result: {e}", exc_info=True)
        finally:
            await session2.close()


# ============================================================================
# /pot
# ============================================================================


@router.message(Command("pot"))
async def cmd_pot(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("❌ Только в группах!")
        return
    remember_chat(message.from_user.id, message.chat.id)
    chat_id = message.chat.id
    db = get_db()
    async for session in db.get_session():
        try:
            gr = await session.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
            group = gr.scalar_one_or_none()
            pot = group.common_pot if group else 0
            until = max(0, POT_EXPLOSION_THRESHOLD - pot)
            pct = min(100, int((pot / POT_EXPLOSION_THRESHOLD) * 100)) if POT_EXPLOSION_THRESHOLD > 0 else 0
            bar = "🟩" * (pct // 10) + "⬛" * (10 - pct // 10)
            await message.answer(
                f"🏦 <b>Общак:</b> <code>{pot:,}</code> 🪙\n\n"
                f"💥 До взлома: <b>{until:,} 🪙</b>\n{bar} {pct}%\n\n"
                f"📊 Порог: <code>{POT_EXPLOSION_THRESHOLD:,}</code> 🪙",
                parse_mode="HTML")
        except Exception as e:
            logger.error(f"❌ /pot: {e}")
            await message.answer("❌ Ошибка")
        finally:
            await session.close()
