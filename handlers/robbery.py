"""Ограбления v7.1 — Оптимизация взлома сейфа: answer() первым, MessageNotModified, антиспам."""
import logging
import random
import asyncio
import time
from datetime import datetime, timedelta
from aiogram import Router, Bot
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    CallbackQuery, ChosenInlineResult, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from database import get_db
from models import User, Inventory, Item, GroupChat
from utils.keyboards import format_emoji
from utils.inventory_helpers import (
    add_item_to_inventory, return_safe_contents, generate_safe_code,
    is_gene_item, apply_hazbik_protection, destroy_safe,
    HAZBIK_DURATION_MINUTES,
)
from utils.pot_event import track_chat_activity, check_pot_explosion
from utils.levels import add_xp, grant_level_rewards
from handlers.user import use_lawyer

logger = logging.getLogger(__name__)
router = Router()

JAIL_DURATION_MINUTES = 10
JAIL_SAFE_FAIL_MINUTES = 10
COMPENSATION_PERCENT = 0.50
POT_TAX_PERCENT = 0.10
MAX_CHANCE_MONEY = 70.0
GLOVES_MULTIPLIER = 1.25
ROOF_COUNTER_CHANCE = 0.15

SAFE_LOOT_COIN_PERCENT = 0.25
CROWBAR_SUCCESS_CHANCE = 0.70

HEIST_PERCENT_OPTIONS = [10, 20, 30]

GENE_STEAL_CHANCE = 60.0
GENE_BAIL_PERCENT = 0.50
GENE_GLOVES_BONUS = 1.25

ITEM_SECURITY = "Охрана"
ITEM_ROOF = "Крыша"
ITEM_GLOVES = "Липкие Перчатки"
ITEM_LOCKPICK = "Отмычка"
ITEM_CROWBAR = "Лом"
ITEM_BOUNCER = "Вышибала"

SAFE_MAX_ATTEMPTS = 3

WITNESS_CHANCE = 0.05

MIN_BALANCE_TO_ROB = 200
INACTIVITY_TIMEOUT_SECONDS = 60
INACTIVITY_PENALTY = 200
VICTIM_LOCK_TIMEOUT_SECONDS = 120  # Максимальное время блокировки жертвы

_robbery_sessions: dict[str, dict] = {}
_pending_robberies: dict[str, dict] = {}
_safe_fail_tracker: dict[int, set] = {}
_failed_crowbar_attempts: int = 0


def calculate_rob_chance(attacker_money: int, victim_money: int, base_chance: float) -> float:
    """
    Рассчитывает динамический шанс ограбления.
    - Если victim_money >= attacker_money * 0.6 → возвращает base_chance (без изменений).
    - Иначе → base_chance * (victim_money / (attacker_money * 0.6)).
    - Минимальный порог: 1% (шанс никогда не падает ниже 1.0).
    """
    threshold = attacker_money * 0.6
    if victim_money >= threshold:
        return base_chance
    if threshold == 0:
        return max(1.0, base_chance)
    reduced = base_chance * (victim_money / threshold)
    return max(1.0, reduced)


def calculate_loot_percent(attacker_total: int, victim_total: int) -> float:
    """
    Рассчитывает процент добычи из сейфа.
    max_loot = 0.25 (25%)
    threshold = attacker_total * 0.6
    Если victim_total >= threshold → loot_percent = max_loot
    Иначе → loot_percent = max_loot * (victim_total / threshold)
    Минимум: 0.01 (1%)
    """
    max_loot = 0.25
    threshold = attacker_total * 0.6
    if threshold == 0:
        return max_loot
    if victim_total >= threshold:
        return max_loot
    result = max_loot * (victim_total / threshold)
    return max(0.01, result)

_inactivity_timers: dict[str, asyncio.Task] = {}

_bot_ref: Bot | None = None

# ── Антиспам: блокировка одновременных нажатий ──
_processing_locks: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    """Получить или создать Lock для данного ключа (inline_message_id или user_id)."""
    if key not in _processing_locks:
        _processing_locks[key] = asyncio.Lock()
    return _processing_locks[key]


def _cleanup_lock(key: str) -> None:
    """Удалить Lock если он не заблокирован."""
    lock = _processing_locks.get(key)
    if lock and not lock.locked():
        _processing_locks.pop(key, None)


# ── Безопасное редактирование сообщений (обработка MessageNotModified) ──


async def _safe_edit_text(bot, inline_message_id=None, message=None, **kwargs):
    """Редактирует сообщение, игнорируя MessageNotModified и другие Telegram-ошибки."""
    try:
        if inline_message_id:
            await bot.edit_message_text(inline_message_id=inline_message_id, **kwargs)
        elif message:
            await message.edit_text(**kwargs)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            pass  # Спам по кнопкам — игнорируем
        elif "message to edit not found" in err:
            pass  # Сообщение удалено — игнорируем
        elif "query is too old" in err:
            pass
        else:
            logger.warning(f"⚠️ edit_text: {e}")
    except Exception as e:
        logger.warning(f"⚠️ edit_text unexpected: {e}")


async def _safe_edit_reply_markup(bot, inline_message_id=None, message=None, **kwargs):
    """Редактирует reply_markup, игнорируя MessageNotModified."""
    try:
        if inline_message_id:
            await bot.edit_message_reply_markup(inline_message_id=inline_message_id, **kwargs)
        elif message:
            await message.edit_reply_markup(**kwargs)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            pass
        elif "message to edit not found" in err:
            pass
        else:
            logger.warning(f"⚠️ edit_markup: {e}")
    except Exception:
        pass


async def _safe_answer(call: CallbackQuery, text: str = "", **kwargs):
    """Безопасный answer() — не падает если query устарел."""
    try:
        await call.answer(text, **kwargs)
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower():
            pass
        else:
            logger.warning(f"⚠️ answer: {e}")
    except Exception:
        pass


async def _send_robbery_notification(bot: Bot, victim: "User", robber_name: str, amount: int = 0, used_bouncer: bool = False, elite_safe_damaged: bool = False, elite_safe_destroyed: bool = False) -> None:
    """Отправляет уведомление жертве об успешном ограблении в ЛС."""
    if not victim.notifications_enabled:
        return

    text = f'⚠️ <b>Вас ограбили!</b>\nИгрок {robber_name} ограбил вас на {amount:,} 🪙.'

    if used_bouncer:
        text += '\n❗ Охрана была бессильна против Вышибалы!'

    if elite_safe_damaged:
        text += '\n🏦 Ваш Элитный сейф выдержал! Осталось взломов до разрушения: 1'
    elif elite_safe_destroyed:
        text += '\n💔 Ваш Элитный сейф сломан и уничтожен!'

    try:
        await bot.send_message(chat_id=victim.tg_id, text=text, parse_mode="HTML")
    except Exception:
        pass  # Игнорируем ошибку Forbidden если бот заблокирован


def _set_bot_ref(bot: Bot) -> None:
    global _bot_ref
    if bot is not None:
        _bot_ref = bot


def _build_lvl_text(new_levels: list[int]) -> str:
    lvl_text = ""
    for lvl in new_levels:
        lvl_text += f"\n\n🆙 <b>Уровень {lvl}!</b> Ваше влияние растёт!"
    return lvl_text


# ============================================================================
# БЛОКИРОВКА ЖЕРТВЫ (с таймаутом)
# ============================================================================
_victim_locks: dict[int, tuple[int, float]] = {}  # victim_id → (robber_id, monotonic timestamp)


def _is_lock_expired(locked_at: float) -> bool:
    """Проверяет, истёк ли таймаут блокировки (monotonic time)."""
    return time.monotonic() - locked_at > VICTIM_LOCK_TIMEOUT_SECONDS


def _lock_victim(victim_id: int, robber_id: int) -> bool:
    if victim_id in _victim_locks:
        locked_robber, locked_at = _victim_locks[victim_id]
        if _is_lock_expired(locked_at):
            logger.warning(f"⏰ Автоснятие просроченной блокировки жертвы {victim_id} (грабитель {locked_robber})")
            _victim_locks.pop(victim_id, None)
        elif locked_robber == robber_id:
            return True
        else:
            return False
    _victim_locks[victim_id] = (robber_id, time.monotonic())
    return True


def _unlock_victim(victim_id: int, robber_id: int) -> None:
    entry = _victim_locks.get(victim_id)
    if entry and entry[0] == robber_id:
        _victim_locks.pop(victim_id, None)


def _is_victim_locked(victim_id: int, robber_id: int) -> bool:
    entry = _victim_locks.get(victim_id)
    if entry is None:
        return False
    locked_robber, locked_at = entry
    if _is_lock_expired(locked_at):
        _victim_locks.pop(victim_id, None)
        return False
    return locked_robber != robber_id


# ============================================================================
# ТАЙМЕР БЕЗДЕЙСТВИЯ
# ============================================================================


def _start_inactivity_timer(iid: str, rid: int, vid: int) -> None:
    _cancel_inactivity_timer(iid)

    async def _timeout_wrapper():
        try:
            await _inactivity_timeout_handler(iid, rid, vid)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"❌ inactivity_timer error: {e}", exc_info=True)

    task = asyncio.ensure_future(_timeout_wrapper())
    _inactivity_timers[iid] = task


def _cancel_inactivity_timer(iid: str) -> None:
    task = _inactivity_timers.pop(iid, None)
    if task and not task.done():
        task.cancel()


def _reset_timer(iid: str, rid: int, vid: int) -> None:
    _start_inactivity_timer(iid, rid, vid)


async def _inactivity_timeout_handler(iid: str, rid: int, vid: int) -> None:
    await asyncio.sleep(INACTIVITY_TIMEOUT_SECONDS)

    logger.warning(f"⏰ ШТРАФ за бездействие: robber={rid}, victim={vid}, iid={iid}")

    _robbery_sessions.pop(iid, None)
    _unlock_victim(vid, rid)
    _inactivity_timers.pop(iid, None)

    bot = _bot_ref
    if not bot:
        logger.error("❌ _bot_ref is None — не могу отправить штраф!")
        return

    db = get_db()
    penalty_applied = 0

    async for session in db.get_session():
        try:
            rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
            robber = rr.scalar_one_or_none()
            if robber:
                penalty_applied = min(INACTIVITY_PENALTY, max(0, robber.balance_vv))
                robber.balance_vv -= penalty_applied
                robber.is_robbing_now = False
                robber.robbing_started_at = None

            vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
            victim = vr.scalar_one_or_none()
            if victim:
                victim.is_being_robbed = False
                victim.robbery_started_at = None

            await session.commit()
            logger.info(f"✅ Штраф {penalty_applied} 🪙 списан с {rid}, жертва {vid} свободна")

        except Exception as e:
            await session.rollback()
            logger.error(f"❌ inactivity DB error: {e}", exc_info=True)
        finally:
            await session.close()

    await _safe_edit_text(
        bot, inline_message_id=iid,
        text=(
            f"⚠️ <b>Штраф за бездействие!</b>\n\n"
            f"Вы взяли игрока на понт, но не грабили его.\n"
            f"💸 Списано: <b>{penalty_applied:,} 🪙</b>\n\n"
            f"⏱ Лимит: {INACTIVITY_TIMEOUT_SECONDS} сек без действий.\n"
            f"<i>Не блокируйте чужие ограбления!</i>"
        ),
        parse_mode="HTML",
        reply_markup=None,
    )


# ============================================================================
# УТИЛИТЫ
# ============================================================================


def _clean(name: str | None) -> str:
    if not name:
        return "Игрок"
    return name.lstrip("@")


def _display_name(user: User) -> str:
    if user.username:
        return _clean(user.username)
    return f"ID:{user.tg_id}"


def _check_robber(call, robber_id):
    return call.from_user.id == robber_id


def _get_chat_id(call):
    if call.message and call.message.chat and call.message.chat.type in ("group", "supergroup"):
        return call.message.chat.id
    return None


async def _track_both(session, call, rid, vid):
    cid = _get_chat_id(call)
    if cid:
        await track_chat_activity(session, cid, rid)
        await track_chat_activity(session, cid, vid)


async def _add_pot_tax(session, chat_id, tax):
    if not chat_id or tax <= 0:
        return 0
    gc_r = await session.execute(select(GroupChat).where(GroupChat.chat_id == chat_id))
    group = gc_r.scalar_one_or_none()
    if not group:
        group = GroupChat(chat_id=chat_id, common_pot=0)
        session.add(group)
        await session.flush()
    group.common_pot += tax
    return tax


def _has_item(inventory, item_name):
    return any(inv.item.name == item_name and inv.quantity > 0 for inv in inventory)


async def _consume_item(session, user_id, item_name):
    inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
    for inv in inv_r.scalars().all():
        if inv.item.name == item_name and inv.quantity > 0:
            if inv.quantity <= 1:
                await session.delete(inv)
            else:
                inv.quantity -= 1
            return True
    return False


def _mask_code(code, revealed):
    return " ".join(code[i] if i in revealed else "_" for i in range(len(code)))


def _get_total_balance(user) -> int:
    return user.balance_vv + (user.hidden_coins or 0)


def _calc_money_bail(total_balance: int, target_amount: int) -> tuple[int, float, str]:
    high_bail = int(target_amount * 0.75)
    low_bail = int(target_amount * 0.50)
    if total_balance >= high_bail:
        return (high_bail, 0.0, "")
    elif total_balance >= low_bail:
        return (low_bail, 0.20, "")
    else:
        return (0, 0.0,
            f"❌ Недостаточно средств для залога!\n"
            f"Нужно минимум <b>50%</b> от суммы куша "
            f"(<b>{low_bail:,} 🪙</b>).\n"
            f"🚫 <i>Инструменты в залог не принимаются.</i>")


def _deduct_bail(user, amount: int) -> None:
    if user.balance_vv >= amount:
        user.balance_vv -= amount
    else:
        remainder = amount - user.balance_vv
        user.balance_vv = 0
        user.hidden_coins = max(0, (user.hidden_coins or 0) - remainder)


async def _has_lawyer(session, user_id: int) -> tuple[bool, int]:
    inv_r = await session.execute(select(Inventory).where(Inventory.user_id == user_id))
    inv_all = inv_r.scalars().all()
    count = sum(inv.quantity for inv in inv_all if inv.item.name == "Адвокат" and inv.quantity > 0)
    return count > 0, count


async def _find_victim(session, target_input: str) -> User | None:
    if target_input.isdigit():
        tg_id = int(target_input)
        vr = await session.execute(select(User).where(User.tg_id == tg_id))
        victim = vr.scalar_one_or_none()
        if victim:
            return victim
    vr = await session.execute(select(User).where(User.username == target_input))
    return vr.scalar_one_or_none()


def _cleanup_session(iid: str, rid: int = None, vid: int = None) -> None:
    _cancel_inactivity_timer(iid)
    sess = _robbery_sessions.pop(iid, None)
    if sess:
        s_rid = sess.get("robber_id", rid)
        s_vid = sess.get("victim_id", vid)
        if s_rid and s_vid:
            _unlock_victim(s_vid, s_rid)
    elif rid and vid:
        _unlock_victim(vid, rid)
    _cleanup_lock(iid)


@router.callback_query(lambda c: c.data == "noop")
async def noop_handler(call: CallbackQuery) -> None:
    await _safe_answer(call)


# ============================================================================
# INLINE
# ============================================================================


async def robbery_inline_handler(inline_query: InlineQuery) -> None:
    query_text = inline_query.query.strip()

    if query_text.startswith("@"):
        target_input = query_text[1:].strip()
    else:
        target_input = query_text.strip()

    if not target_input:
        return

    robber_id = inline_query.from_user.id
    robber_name = inline_query.from_user.first_name or "Грабитель"

    _set_bot_ref(inline_query.bot)

    db = get_db()
    victim = None
    robber = None

    async for session in db.get_session():
        try:
            victim = await _find_victim(session, target_input)
            rr = await session.execute(select(User).where(User.tg_id == robber_id).with_for_update())
            robber = rr.scalar_one_or_none()
        except Exception as e:
            logger.error(f"❌: {e}")
        finally:
            await session.close()

    if not victim:
        label = f"@{target_input}" if not target_input.isdigit() else f"ID:{target_input}"
        await _inline_error(inline_query, "rob_not_found", f"❌ {label} не найден", "Не зарегистрирован")
        return

    if victim.tg_id == robber_id:
        await _inline_error(inline_query, "rob_self", "❌ Нельзя грабить самого себя!", "Выберите другую цель")
        return

    if not robber:
        await _inline_error(inline_query, "rob_no_reg", "❌ Не зарегистрированы!", "/start")
        return

    if robber.balance_vv < MIN_BALANCE_TO_ROB:
        await _inline_error(
            inline_query, "rob_poor",
            f"❌ Минимум {MIN_BALANCE_TO_ROB} 🪙",
            f"Баланс: {robber.balance_vv} 🪙 — недостаточно")
        return

    if robber.jail_until and robber.jail_until > datetime.utcnow():
        mins = max(1, int((robber.jail_until - datetime.utcnow()).total_seconds() // 60))

        has_law = False
        law_count = 0
        async for session in db.get_session():
            try:
                has_law, law_count = await _has_lawyer(session, robber_id)
            except Exception:
                pass
            finally:
                await session.close()

        if has_law:
            result = InlineQueryResultArticle(
                id="rob_jail_lawyer",
                title=f"🔒 Тюрьма {mins}мин — 💼 Есть адвокат!",
                description="Нажмите чтобы использовать адвоката",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"🔒 <b>{robber_name}</b> в тюрьме ещё <b>{mins} мин</b>.\n\n"
                        f"💼 <i>Адвокат может помочь...</i>"
                    ), parse_mode="HTML"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="💼 Вызвать адвоката",
                        callback_data=f"rob_lawyer_{robber_id}_{victim.tg_id}")]
                ]))
            await inline_query.bot.answer_inline_query(
                inline_query.id, [result], cache_time=1, is_personal=True)
        else:
            await _inline_error(inline_query, "rob_jail",
                f"🔒 Тюрьма {mins}мин", "Нет адвоката — ждите")
        return

    v_clean = _display_name(victim)
    result_id = f"rob_{robber_id}_{victim.tg_id}"
    result = InlineQueryResultArticle(
        id=result_id, title=f"🔫 Ограбить {v_clean}",
        description=f"Мин. {MIN_BALANCE_TO_ROB} 🪙 | ⏱ {INACTIVITY_TIMEOUT_SECONDS}с на действие",
        input_message_content=InputTextMessageContent(
            message_text=f"🔫 <b>{robber_name}</b> планирует ограбление <b>{v_clean}</b>...\n\n⏳ Загрузка...",
            parse_mode="HTML"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔍 Начать", callback_data=f"rob_start_{robber_id}_{victim.tg_id}")]]))
    _pending_robberies[result_id] = {
        "robber_id": robber_id,
        "victim_id": victim.tg_id,
        "victim_username": v_clean,
    }
    await inline_query.bot.answer_inline_query(inline_query.id, [result], cache_time=1, is_personal=True)


async def _inline_error(iq, rid, title, desc):
    r = InlineQueryResultArticle(id=rid, title=title, description=desc,
        input_message_content=InputTextMessageContent(message_text=f"<b>{title}</b>", parse_mode="HTML"))
    await iq.bot.answer_inline_query(iq.id, [r], cache_time=1, is_personal=True)


async def robbery_chosen_result(chosen: ChosenInlineResult) -> None:
    _set_bot_ref(chosen.bot)
    rid = chosen.result_id
    if rid in _pending_robberies and chosen.inline_message_id:
        _robbery_sessions[chosen.inline_message_id] = _pending_robberies.pop(rid)


# ============================================================================
# АДВОКАТ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_lawyer_"))
async def rob_lawyer_handler(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ Telegram
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid = int(parts[2])
    vid = int(parts[3]) if len(parts) > 3 else None

    if call.from_user.id != rid:
        return

    iid = call.inline_message_id
    db = get_db()

    async for session in db.get_session():
        try:
            ok, msg = await use_lawyer(session, rid)
            if ok:
                await session.commit()
                btns = []
                if vid:
                    btns.append([InlineKeyboardButton(
                        text="🔫 Продолжить ограбление",
                        callback_data=f"rob_start_{rid}_{vid}")])
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(
                        f"💼 <b>Адвокат вытащил вас из тюрьмы!</b> ✅\n\n"
                        f"🟢 Вы снова на свободе."
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None)
            else:
                await _safe_answer(call, msg, show_alert=True)
        except Exception as e:
            await session.rollback()
            logger.error(f"❌ rob_lawyer: {e}", exc_info=True)
        finally:
            await session.close()


# ============================================================================
# РАЗВЕДКА — ВЫБОР ЦЕЛИ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_start_"))
async def rob_start(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return
    if rid == vid:
        return
    await _show_target_choice(call, rid, vid)


@router.callback_query(lambda c: c.data and c.data.startswith("rob_back_"))
async def rob_back(call: CallbackQuery) -> None:
    await _safe_answer(call)
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id

    sess = _robbery_sessions.get(iid)
    if not sess:
        await _safe_edit_text(call.bot, inline_message_id=iid,
            text="❌ <b>Сессия ограбления истекла.</b>", parse_mode="HTML")
        return

    _reset_timer(iid, rid, vid)
    await _show_back_to_targets(call, rid, vid)


async def _show_back_to_targets(call, robber_id, victim_id):
    iid = call.inline_message_id
    db = get_db()
    async for session in db.get_session():
        try:
            rr = await session.execute(select(User).where(User.tg_id == robber_id).with_for_update())
            robber = rr.scalar_one_or_none()
            vr = await session.execute(select(User).where(User.tg_id == victim_id).with_for_update())
            victim = vr.scalar_one_or_none()
            if not robber or not victim:
                _cleanup_session(iid, robber_id, victim_id)
                return

            if robber.jail_until and robber.jail_until > datetime.utcnow():
                mins = max(1, int((robber.jail_until - datetime.utcnow()).total_seconds() // 60))
                has_law, law_count = await _has_lawyer(session, robber_id)
                btns = []
                if has_law:
                    btns.append([InlineKeyboardButton(
                        text=f"💼 Адвокат ({law_count} шт.) — Освободиться",
                        callback_data=f"rob_lawyer_{robber_id}_{victim_id}")])
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(
                        f"🔒 <b>Вы в тюрьме!</b>\n\n"
                        f"⏳ Осталось: <b>{mins} мин</b>\n\n"
                        + ("💼 <i>Адвокат может вытащить вас прямо сейчас!</i>" if has_law
                           else "❌ <i>Адвоката нет. Ждите или купите в /shop.</i>")
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None)
                return

            if robber.balance_vv < MIN_BALANCE_TO_ROB:
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(
                        f"❌ <b>Минимум {MIN_BALANCE_TO_ROB} 🪙 для ограбления!</b>\n\n"
                        f"💼 Ваш баланс: <b>{robber.balance_vv:,} 🪙</b>"
                    ),
                    parse_mode="HTML")
                return

            v_name = _display_name(victim)

            btns = []
            wallet_amount = victim.balance_vv
            btns.append([InlineKeyboardButton(
                text=f"👤 Личный баланс ({wallet_amount:,} 🪙)",
                callback_data=f"rob_wallet_{robber_id}_{victim_id}")])

            if victim.has_active_safe():
                se = "🏦" if victim.safe_type == "elite" else "🧰"
                sn = "Элитный" if victim.safe_type == "elite" else "Ржавый"
                safe_items = len(victim.hidden_item_ids) if victim.hidden_item_ids else 0
                safe_coins = victim.hidden_coins or 0
                level = victim.get_safe_level()
                btns.append([InlineKeyboardButton(
                    text=f"📦 {se} {sn} ур.{level} ({safe_items} генов, {safe_coins:,} 🪙)",
                    callback_data=f"rob_safe_target_{robber_id}_{victim_id}")])

            vi_r = await session.execute(select(Inventory).where(Inventory.user_id == victim_id))
            vi_items = vi_r.scalars().all()
            gene_items = [inv for inv in vi_items if inv.item.drop_chance > 0 and inv.quantity > 0]
            total_genes = sum(inv.quantity for inv in gene_items)
            if total_genes > 0:
                genes_value = sum(inv.item.price * inv.quantity for inv in gene_items)
                btns.append([InlineKeyboardButton(
                    text=f"🧬 Инвентарь ({total_genes} генов, ≈{genes_value:,} 🪙)",
                    callback_data=f"rob_genes_{robber_id}_{victim_id}")])

            btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"rob_cancel_{robber_id}_{victim_id}")])

            timer_note = f"\n\n⏱ <i>{INACTIVITY_TIMEOUT_SECONDS} сек на действие, иначе штраф {INACTIVITY_PENALTY} 🪙</i>"

            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=f"🔍 <b>{v_name}</b>\n\n<b>Выберите цель ограбления:</b>{timer_note}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        except Exception as e:
            logger.error(f"❌ rob_back: {e}", exc_info=True)
        finally:
            await session.close()


async def _show_target_choice(call, robber_id, victim_id):
    iid = call.inline_message_id
    db = get_db()
    async for session in db.get_session():
        try:
            cid = _get_chat_id(call)
            if cid:
                await track_chat_activity(session, cid, robber_id)
                await track_chat_activity(session, cid, victim_id)

            rr = await session.execute(select(User).where(User.tg_id == robber_id).with_for_update())
            robber = rr.scalar_one_or_none()
            if not robber:
                return

            if robber.balance_vv < MIN_BALANCE_TO_ROB:
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(
                        f"❌ <b>Минимум {MIN_BALANCE_TO_ROB} 🪙 для ограбления!</b>\n\n"
                        f"💼 Ваш баланс: <b>{robber.balance_vv:,} 🪙</b>"
                    ),
                    parse_mode="HTML")
                return

            if robber.jail_until and robber.jail_until > datetime.utcnow():
                mins = max(1, int((robber.jail_until - datetime.utcnow()).total_seconds() // 60))
                has_law, law_count = await _has_lawyer(session, robber_id)
                btns = []
                if has_law:
                    btns.append([InlineKeyboardButton(
                        text=f"💼 Адвокат ({law_count} шт.) — Освободиться",
                        callback_data=f"rob_lawyer_{robber_id}_{victim_id}")])
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(
                        f"🔒 <b>Вы в тюрьме!</b>\n\n"
                        f"⏳ Осталось: <b>{mins} мин</b>\n\n"
                        + ("💼 <i>Адвокат может вытащить вас прямо сейчас!</i>" if has_law
                           else "❌ <i>Адвоката не��. Ждите или купите в /shop.</i>")
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None)
                return

            vr = await session.execute(select(User).where(User.tg_id == victim_id).with_for_update())
            victim = vr.scalar_one_or_none()
            if not victim:
                return
            v_name = _display_name(victim)

            if _is_victim_locked(victim_id, robber_id):
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=f"🛑 <b>{v_name}</b> уже кто-то грабит!\n\n<i>Подождите или выберите другую цель.</i>",
                    parse_mode="HTML")
                return

            if not _lock_victim(victim_id, robber_id):
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=f"🛑 <b>{v_name}</b> уже кто-то грабит!",
                    parse_mode="HTML")
                return

            _robbery_sessions[iid] = {
                "robber_id": robber_id,
                "victim_id": victim_id,
                "victim_username": v_name,
                "used_bouncer": False,
            }

            _start_inactivity_timer(iid, robber_id, victim_id)

            # Вышибала
            ri_r = await session.execute(select(Inventory).where(Inventory.user_id == robber_id))
            ri = ri_r.scalars().all()
            has_bouncer = _has_item(ri, ITEM_BOUNCER)

            victim_has_hazbik = victim.is_hazbik_active()

            # ── Авто-активация Охраны ──
            victim_has_security = victim.is_security_active()
            if not victim_has_security:
                vi_def_r = await session.execute(select(Inventory).where(Inventory.user_id == victim_id))
                vi_def_all = vi_def_r.scalars().all()
                for inv in vi_def_all:
                    if inv.item.name == "Охрана" and inv.quantity > 0:
                        if inv.quantity <= 1:
                            await session.delete(inv)
                        else:
                            inv.quantity -= 1
                        from utils.inventory_helpers import activate_security
                        await activate_security(session, victim)
                        await session.commit()
                        victim_has_security = True
                        logger.info(f"🛡 Авто-активация Охраны: victim={victim_id}")
                        break
            else:
                vi_def_all = []

            # ── Авто-активация Крыши ──
            victim_has_roof = victim.is_roof_active()
            if not victim_has_roof:
                if not vi_def_all:
                    vi_def_r2 = await session.execute(select(Inventory).where(Inventory.user_id == victim_id))
                    vi_def_all = vi_def_r2.scalars().all()
                for inv in vi_def_all:
                    if inv.item.name == "Крыша" and inv.quantity > 0:
                        if inv.quantity <= 1:
                            await session.delete(inv)
                        else:
                            inv.quantity -= 1
                        from utils.inventory_helpers import activate_roof
                        await activate_roof(session, victim)
                        await session.commit()
                        victim_has_roof = True
                        logger.info(f"🕴 Авто-активация Крыши: victim={victim_id}")
                        break

            has_any_protection = victim_has_hazbik or victim_has_security or victim_has_roof

            bouncer_text = ""

            if has_any_protection:
                if has_bouncer:
                    await _consume_item(session, robber_id, ITEM_BOUNCER)
                    _robbery_sessions[iid]["used_bouncer"] = True
                    bypassed = []
                    if victim_has_hazbik:
                        bypassed.append("🛡 Хазбик")
                    if victim_has_security:
                        bypassed.append("💂 Охрана")
                    if victim_has_roof:
                        bypassed.append("🕴 Крыша")
                    bypassed_list = ", ".join(bypassed)
                    bouncer_text = (
                        f"\n\n👊 <b>Вышибала в деле!</b> Обошёл: {bypassed_list}\n"
                    )
                    await session.commit()
                else:
                    if victim_has_hazbik:
                        mins = victim.hazbik_remaining_minutes()
                        _cleanup_session(iid, robber_id, victim_id)
                        await _safe_edit_text(
                            call.bot, inline_message_id=iid,
                            text=(f"🛡 <b>Охрана Хазбика:</b> Этот бро уже пострадал, "
                                  f"Хасбик не подпустит тебя к нему ещё <b>{mins} мин</b>!"),
                            parse_mode="HTML")
                        return

                    if victim_has_security:
                        _cleanup_session(iid, robber_id, victim_id)
                        await session.commit()
                        await _safe_edit_text(
                            call.bot, inline_message_id=iid,
                            text=f"💂 <b>{v_name} под охраной!</b>",
                            parse_mode="HTML")
                        return

                    if victim_has_roof:
                        _cleanup_session(iid, robber_id, victim_id)
                        rc = random.random() < ROOF_COUNTER_CHANCE
                        t = f"🕴 <b>{v_name} под крышей!</b>"
                        if rc:
                            t += "\n\n💀 Тебя избили!"
                        await session.commit()
                        await _safe_edit_text(
                            call.bot, inline_message_id=iid,
                            text=t, parse_mode="HTML")
                        return

            # ── Кнопки выбора цели ──
            btns = []
            wallet_amount = victim.balance_vv
            btns.append([InlineKeyboardButton(
                text=f"👤 Личный баланс ({wallet_amount:,} 🪙)",
                callback_data=f"rob_wallet_{robber_id}_{victim_id}")])

            if victim.has_active_safe():
                se = "🏦" if victim.safe_type == "elite" else "🧰"
                sn = "Элитный" if victim.safe_type == "elite" else "Ржавый"
                safe_items = len(victim.hidden_item_ids) if victim.hidden_item_ids else 0
                safe_coins = victim.hidden_coins or 0
                level = victim.get_safe_level()
                btns.append([InlineKeyboardButton(
                    text=f"📦 {se} {sn} ур.{level} ({safe_items} генов, {safe_coins:,} 🪙)",
                    callback_data=f"rob_safe_target_{robber_id}_{victim_id}")])

            # ── Гены из инвентаря ──
            vi_r = await session.execute(select(Inventory).where(Inventory.user_id == victim_id))
            vi_items = vi_r.scalars().all()
            gene_items = [inv for inv in vi_items if inv.item.drop_chance > 0 and inv.quantity > 0]
            total_genes = sum(inv.quantity for inv in gene_items)
            if total_genes > 0:
                genes_value = sum(inv.item.price * inv.quantity for inv in gene_items)
                btns.append([InlineKeyboardButton(
                    text=f"🧬 Инвентарь ({total_genes} генов, ≈{genes_value:,} 🪙)",
                    callback_data=f"rob_genes_{robber_id}_{victim_id}")])

            btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"rob_cancel_{robber_id}_{victim_id}")])

            timer_note = f"\n\n⏱ <i>{INACTIVITY_TIMEOUT_SECONDS} сек на действие, иначе штраф {INACTIVITY_PENALTY} 🪙</i>"

            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=f"🔍 <b>{v_name}</b>{bouncer_text}\n\n<b>Выберите цель ограбления:</b>{timer_note}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        except Exception as e:
            logger.error(f"❌ target_choice: {e}", exc_info=True)
            _cleanup_session(iid, robber_id, victim_id)
        finally:
            await session.close()


# ============================================================================
# ОГРАБЛЕНИЕ БАЛАНСА — выбор %
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_wallet_"))
async def rob_wallet_percent(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id

    _reset_timer(iid, rid, vid)

    db = get_db()
    async for session in db.get_session():
        try:
            vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
            victim = vr.scalar_one_or_none()
            rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
            robber = rr.scalar_one_or_none()
            if not victim or not robber:
                return
            v_name = _display_name(victim)
            balance = victim.balance_vv
            robber_total = _get_total_balance(robber)

            btns = []
            for pct in HEIST_PERCENT_OPTIONS:
                amount = int(balance * pct / 100)
                if amount <= 0:
                    continue
                bail_amount, penalty, err = _calc_money_bail(robber_total, amount)
                if err or bail_amount <= 0:
                    btns.append([InlineKeyboardButton(
                        text=f"❌ {pct}% = {amount:,} 🪙 (нет денег на залог)",
                        callback_data="noop")])
                else:
                    penalty_tag = " ⚠️-20%" if penalty > 0 else ""
                    btns.append([InlineKeyboardButton(
                        text=f"💰 {pct}% = {amount:,} 🪙 (залог {bail_amount:,}){penalty_tag}",
                        callback_data=f"rob_wpick_{rid}_{vid}_{pct}")])

            if not btns:
                _cleanup_session(iid, rid, vid)
                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(
                        f"❌ У <b>{v_name}</b> нет денег на руках!\n\n"
                        f"Или у вас не хватает на залог.\n"
                        f"🚫 <i>Инструменты в залог не принимаются.</i>"
                    ),
                    parse_mode="HTML")
                return

            safe_note = ""
            if robber.hidden_coins and robber.hidden_coins > 0:
                safe_note = f"\n🔐 В сейфе: <b>{robber.hidden_coins:,} 🪙</b>"

            btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rob_back_{rid}_{vid}")])
            btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"rob_cancel_{rid}_{vid}")])

            small_target_warning = ""
            sample_chance = calculate_rob_chance(robber.balance_vv, victim.balance_vv, MAX_CHANCE_MONEY)
            if sample_chance < MAX_CHANCE_MONEY:
                small_target_warning = (
                    f"\n⚠️ <b>Цель слишком мелкая для тебя!</b> Из-за разницы в весовых категориях "
                    f"твой шанс на успех снижен до <b>{sample_chance:.1f}%</b>, "
                    f"но залог за провал остается прежним.\n"
                )

            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=(
                    f"💰 <b>{v_name}</b> — баланс <b>{balance:,} 🪙</b>\n\n"
                    f"💼 Наличные: <b>{robber.balance_vv:,} 🪙</b>{safe_note}\n"
                    f"💰 <b>Доступный залог: {robber_total:,} 🪙</b> <i>(включая сейф)</i>\n\n"
                    f"⚠️ Залог — <b>только наличные + сейф</b>\n"
                    f"✅ При успехе залог не списывается\n"
                    f"❌ При провале — сначала с баланса, потом из сейфа\n"
                    f"{small_target_warning}\n"
                    f"Выбери размер куша 👇"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        except Exception as e:
            logger.error(f"❌ wallet_percent: {e}", exc_info=True)
        finally:
            await session.close()


# ============================================================================
# ИСПОЛНЕНИЕ ОГРАБЛЕНИЯ БАЛАНСА
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_wpick_"))
async def rob_wallet_execute(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid, pct = int(parts[2]), int(parts[3]), int(parts[4])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id
    chat_id = _get_chat_id(call)
    rn = call.from_user.first_name or "Грабитель"

    # ── Антиспам Lock ──
    lock = _get_lock(f"wpick_{iid}")
    if lock.locked():
        return
    async with lock:
        _reset_timer(iid, rid, vid)

        db = get_db()
        async for session in db.get_session():
            try:
                await _track_both(session, call, rid, vid)
                rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
                robber = rr.scalar_one_or_none()
                vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
                victim = vr.scalar_one_or_none()
                if not robber or not victim:
                    _cleanup_session(iid, rid, vid)
                    return

                if robber.jail_until and robber.jail_until > datetime.utcnow():
                    mins = max(1, int((robber.jail_until - datetime.utcnow()).total_seconds() // 60))
                    has_law, law_count = await _has_lawyer(session, rid)
                    btns = []
                    if has_law:
                        btns.append([InlineKeyboardButton(
                            text=f"💼 Адвокат ({law_count} шт.)",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=f"🔒 <b>Вы в тюрьме!</b> ({mins} мин)\n\n"
                             + ("💼 <i>Используйте адвоката!</i>" if has_law else ""),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None)
                    return

                v_name = _display_name(victim)
                target_amount = int(victim.balance_vv * pct / 100)
                if target_amount <= 0:
                    _cleanup_session(iid, rid, vid)
                    return

                robber_total = _get_total_balance(robber)
                bail_amount, penalty, err = _calc_money_bail(robber_total, target_amount)
                if err or bail_amount <= 0:
                    _cleanup_session(iid, rid, vid)
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"❌ <b>Недостаточно средств для залога!</b>\n\n"
                            f"💰 Куш: <b>{target_amount:,} 🪙</b> ({pct}%)\n"
                            f"💸 Нужно минимум: <b>{int(target_amount * 0.50):,} 🪙</b> (50%)\n"
                            f"💼 Наличные: <b>{robber.balance_vv:,} 🪙</b>\n"
                            f"🔐 В сейфе: <b>{robber.hidden_coins or 0:,} 🪙</b>\n"
                            f"💰 Итого: <b>{robber_total:,} 🪙</b>\n\n"
                            f"🚫 <i>Инструменты в залог не принимаются.</i>"
                        ),
                        parse_mode="HTML")
                    return

                # ── Свидетель 5% ──
                if random.random() <= WITNESS_CHANCE:
                    _deduct_bail(robber, bail_amount)
                    victim.balance_vv += bail_amount
                    robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_DURATION_MINUTES)
                    apply_hazbik_protection(victim)
                    await session.commit()
                    _cleanup_session(iid, rid, vid)

                    has_law, law_count = await _has_lawyer(session, rid)
                    result_btns = []
                    if has_law:
                        result_btns.append([InlineKeyboardButton(
                            text=f"💼 Адвокат ({law_count} шт.)",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])

                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"👵 <b>Вас заметила бдительная бабушка!</b>\n\n"
                            f"Она вызвала полицию, заметив ваши подозрительные действия.\n\n"
                            f"👤 <b>{rn}</b> пойман на месте!\n\n"
                            f"💸 Залог: <b>{bail_amount:,} 🪙</b> (списан)\n"
                            f"💰 Компенсация {v_name}: <b>+{bail_amount:,} 🪙</b> (100%)\n"
                            f"🛡 {v_name} получает защиту Хазбика\n"
                            f"🔒 Тюрьма: <b>{JAIL_DURATION_MINUTES} мин</b>\n\n"
                            f"<i>Шанс: {int(WITNESS_CHANCE * 100)}% — не повезло!</i>"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)
                    return

                ri_r = await session.execute(select(Inventory).where(Inventory.user_id == rid))
                ri = ri_r.scalars().all()
                hg = _has_item(ri, ITEM_GLOVES)

                ch = min(MAX_CHANCE_MONEY, (bail_amount / target_amount) * 100)
                if penalty > 0:
                    ch = ch * (1.0 - penalty)
                if hg:
                    ch = min(MAX_CHANCE_MONEY, ch * GLOVES_MULTIPLIER)
                    await _consume_item(session, rid, ITEM_GLOVES)

                current_chance = calculate_rob_chance(robber.balance_vv, victim.balance_vv, ch)

                robber.increment_action()
                roll = random.uniform(0, 100)
                ok = roll <= current_chance

                used_bouncer = (_robbery_sessions.get(iid) or {}).get("used_bouncer", False)

                if ok:
                    actual = min(target_amount, victim.balance_vv)
                    victim.balance_vv -= actual
                    robber.balance_vv += actual
                    old_level = robber.level
                    new_levels = add_xp(robber, actual)
                    apply_hazbik_protection(victim)
                    await session.commit()
                    if new_levels:
                        await grant_level_rewards(call.bot, session, robber, old_level, new_levels)
                        await session.commit()

                    await _send_robbery_notification(call.bot, victim, rn, amount=actual, used_bouncer=used_bouncer)

                    gloves_text = "\n🧤 Перчатки использованы!" if hg else ""
                    lvl_text = _build_lvl_text(new_levels)
                    rt = (
                        f"🔫 <b>УСПЕХ!</b> ✅\n\n"
                        f"👤 <b>{rn}</b> ограбил {v_name}!\n\n"
                        f"💰 Куш: <b>{actual:,} 🪙</b> ({pct}%)\n"
                        f"✅ Залог: <b>{bail_amount:,} 🪙</b> (не списан)"
                        f"{gloves_text}\n\n"
                        f"🎲 {roll:.1f} / ≤{current_chance:.1f}"
                        f"{lvl_text}"
                    )
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=rt, parse_mode="HTML")
                else:
                    balance_before = robber.balance_vv
                    _deduct_bail(robber, bail_amount)
                    from_safe = max(0, bail_amount - balance_before) if balance_before < bail_amount else 0

                    comp = int(bail_amount * COMPENSATION_PERCENT)
                    ptax = int(bail_amount * POT_TAX_PERCENT)
                    victim.balance_vv += comp
                    robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_DURATION_MINUTES)
                    pa = await _add_pot_tax(session, chat_id, ptax)
                    if pa > 0 and chat_id:
                        await check_pot_explosion(session, chat_id, call.bot)
                    await session.commit()

                    deduct_text = ""
                    if from_safe > 0:
                        deduct_text = f"\n💸 <i>Из них {from_safe:,} 🪙 списано из сейфа</i>"

                    pt = f"\n🏦 Налог: <b>+{pa:,} 🪙</b>" if pa > 0 else ""
                    gloves_text = "\n🧤 Перчатки использованы!" if hg else ""
                    penalty_text = "\n⚠️ Штраф за низкий залог: -20% к шансу" if penalty > 0 else ""

                    has_law, law_count = await _has_lawyer(session, rid)
                    lawyer_hint = ""
                    if has_law:
                        lawyer_hint = f"\n\n💼 <i>У вас есть Адвокат ({law_count} шт.)!</i>"

                    rt = (
                        f"🔫 <b>ПРОВАЛ!</b> ❌\n\n"
                        f"👤 <b>{rn}</b> попался!\n\n"
                        f"💸 Залог: <b>{bail_amount:,} 🪙</b> (списан){deduct_text}\n"
                        f"💰 Компенсация {v_name}: <b>+{comp:,} 🪙</b>\n"
                        f"🔒 Тюрьма: {JAIL_DURATION_MINUTES}мин"
                        f"{pt}{gloves_text}{penalty_text}\n\n"
                        f"🎲 {roll:.1f} / ≤{current_chance:.1f}"
                        f"{lawyer_hint}"
                    )

                    result_btns = []
                    if has_law:
                        result_btns.append([InlineKeyboardButton(
                            text="💼 Вызвать адвоката",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])

                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=rt, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)

                _cleanup_session(iid, rid, vid)

            except Exception as e:
                await session.rollback()
                _cleanup_session(iid, rid, vid)
                logger.error(f"❌ rob_wallet: {e}", exc_info=True)
            finally:
                await session.close()


# ============================================================================
# ОГРАБЛЕНИЕ ГЕНОВ ИЗ ИНВЕНТАРЯ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_genes_"))
async def rob_genes_list(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id

    _reset_timer(iid, rid, vid)

    db = get_db()
    async for session in db.get_session():
        try:
            vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
            victim = vr.scalar_one_or_none()
            rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
            robber = rr.scalar_one_or_none()
            if not victim or not robber:
                return

            v_name = _display_name(victim)
            robber_total = _get_total_balance(robber)

            vi_r = await session.execute(select(Inventory).where(Inventory.user_id == vid))
            vi_items = vi_r.scalars().all()
            gene_items = [inv for inv in vi_items if inv.item.drop_chance > 0 and inv.quantity > 0]
            gene_items.sort(key=lambda inv: inv.item.price, reverse=True)

            if not gene_items:
                await _safe_answer(call, "❌ У жертвы нет генов!", show_alert=True)
                return

            btns = []
            for inv in gene_items[:8]:
                item = inv.item
                bail_needed = int(item.price * GENE_BAIL_PERCENT)
                if robber_total >= bail_needed:
                    btns.append([InlineKeyboardButton(
                        text=f"🧬 {item.name} ({inv.quantity}шт) — {item.price:,}🪙 [залог {bail_needed:,}]",
                        callback_data=f"rob_gpick_{rid}_{vid}_{item.id}")])
                else:
                    btns.append([InlineKeyboardButton(
                        text=f"❌ {item.name} — нет денег на залог ({bail_needed:,}🪙)",
                        callback_data="noop")])

            btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rob_back_{rid}_{vid}")])
            btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"rob_cancel_{rid}_{vid}")])

            safe_note = ""
            if robber.hidden_coins and robber.hidden_coins > 0:
                safe_note = f"\n🔐 В сейфе: <b>{robber.hidden_coins:,} 🪙</b>"

            gene_current_chance = calculate_rob_chance(robber.balance_vv, victim.balance_vv, GENE_STEAL_CHANCE)
            small_target_warning = ""
            if gene_current_chance < GENE_STEAL_CHANCE:
                small_target_warning = (
                    f"\n⚠️ <b>Цель слишком мелкая для тебя!</b> Из-за разницы в весовых категориях "
                    f"твой шанс на успех снижен до <b>{gene_current_chance:.1f}%</b>, "
                    f"но залог за провал остается прежним.\n"
                )

            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=(
                    f"🧬 <b>Инвентарь {v_name}</b>\n\n"
                    f"💼 Наличные: <b>{robber.balance_vv:,} 🪙</b>{safe_note}\n"
                    f"💰 Доступный залог: <b>{robber_total:,} 🪙</b>\n\n"
                    f"🎯 Шанс кражи: <b>{gene_current_chance:.1f}%</b>\n"
                    f"⚠️ Залог = 50% цены гена\n"
                    f"❌ Провал = тюрьма {JAIL_DURATION_MINUTES}мин + залог списан\n"
                    f"{small_target_warning}\n"
                    f"Выбери ген 👇"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        except Exception as e:
            logger.error(f"❌ rob_genes_list: {e}", exc_info=True)
        finally:
            await session.close()


@router.callback_query(lambda c: c.data and c.data.startswith("rob_gpick_"))
async def rob_gene_execute(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid, item_id = int(parts[2]), int(parts[3]), int(parts[4])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id
    chat_id = _get_chat_id(call)
    rn = call.from_user.first_name or "Грабитель"

    # ── Антиспам Lock ──
    lock = _get_lock(f"gpick_{iid}")
    if lock.locked():
        return
    async with lock:
        _reset_timer(iid, rid, vid)

        db = get_db()
        async for session in db.get_session():
            try:
                await _track_both(session, call, rid, vid)
                rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
                robber = rr.scalar_one_or_none()
                vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
                victim = vr.scalar_one_or_none()
                if not robber or not victim:
                    _cleanup_session(iid, rid, vid)
                    return

                v_name = _display_name(victim)

                if robber.jail_until and robber.jail_until > datetime.utcnow():
                    mins = max(1, int((robber.jail_until - datetime.utcnow()).total_seconds() // 60))
                    has_law, law_count = await _has_lawyer(session, rid)
                    btns = []
                    if has_law:
                        btns.append([InlineKeyboardButton(
                            text=f"💼 Адвокат ({law_count} шт.)",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=f"🔒 <b>Вы в тюрьме!</b> ({mins} мин)",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None)
                    return

                vi_r = await session.execute(
                    select(Inventory).where(Inventory.user_id == vid, Inventory.item_id == item_id))
                victim_inv = vi_r.scalar_one_or_none()
                if not victim_inv or victim_inv.quantity <= 0:
                    _cleanup_session(iid, rid, vid)
                    return

                item = victim_inv.item
                if item.drop_chance <= 0:
                    _cleanup_session(iid, rid, vid)
                    return

                bail_amount = int(item.price * GENE_BAIL_PERCENT)
                robber_total = _get_total_balance(robber)
                if robber_total < bail_amount:
                    _cleanup_session(iid, rid, vid)
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"❌ <b>Недостаточно для залога!</b>\n\n"
                            f"🧬 {item.name} — {item.price:,} 🪙\n"
                            f"💸 Залог: <b>{bail_amount:,} 🪙</b>\n"
                            f"💰 Ваши средства: <b>{robber_total:,} 🪙</b>"
                        ),
                        parse_mode="HTML")
                    return

                # ── Свидетель 5% ──
                if random.random() <= WITNESS_CHANCE:
                    _deduct_bail(robber, bail_amount)
                    victim.balance_vv += bail_amount
                    robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_DURATION_MINUTES)
                    apply_hazbik_protection(victim)
                    await session.commit()
                    _cleanup_session(iid, rid, vid)

                    has_law, law_count = await _has_lawyer(session, rid)
                    result_btns = []
                    if has_law:
                        result_btns.append([InlineKeyboardButton(
                            text=f"💼 Адвокат ({law_count} шт.)",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])

                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"👵 <b>Бабушка заметила!</b>\n\n"
                            f"Вы пытались стащить {item.name}, но вас сдали!\n\n"
                            f"💸 Залог: <b>{bail_amount:,} 🪙</b> (списан)\n"
                            f"💰 Компенсация {v_name}: <b>+{bail_amount:,} 🪙</b>\n"
                            f"🔒 Тюрьма: <b>{JAIL_DURATION_MINUTES} мин</b>"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)
                    return

                ri_r = await session.execute(select(Inventory).where(Inventory.user_id == rid))
                ri = ri_r.scalars().all()
                hg = _has_item(ri, ITEM_GLOVES)

                chance = GENE_STEAL_CHANCE
                if hg:
                    chance = min(MAX_CHANCE_MONEY, chance * GENE_GLOVES_BONUS)
                    await _consume_item(session, rid, ITEM_GLOVES)

                current_chance = calculate_rob_chance(robber.balance_vv, victim.balance_vv, chance)

                robber.increment_action()
                roll = random.uniform(0, 100)
                ok = roll <= current_chance

                used_bouncer = (_robbery_sessions.get(iid) or {}).get("used_bouncer", False)

                if ok:
                    if victim_inv.quantity <= 1:
                        await session.delete(victim_inv)
                    else:
                        victim_inv.quantity -= 1

                    await add_item_to_inventory(session, rid, item_id, 1)

                    old_level = robber.level
                    new_levels = add_xp(robber, item.price)
                    apply_hazbik_protection(victim)
                    await session.commit()
                    _cleanup_session(iid, rid, vid)
                    if new_levels:
                        await grant_level_rewards(call.bot, session, robber, old_level, new_levels)
                        await session.commit()

                    await _send_robbery_notification(call.bot, victim, rn, amount=item.price, used_bouncer=used_bouncer)

                    gloves_text = "\n🧤 Перчатки использованы!" if hg else ""
                    lvl_text = _build_lvl_text(new_levels)

                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"🧬 <b>ГЕН УКРАДЕН!</b> ✅\n\n"
                            f"👤 <b>{rn}</b> стащил у {v_name}:\n"
                            f"  • 🧬 <b>{item.name}</b> ({item.price:,} 🪙)\n\n"
                            f"✅ Залог: <b>{bail_amount:,} 🪙</b> (не списан)"
                            f"{gloves_text}\n\n"
                            f"🎲 {roll:.1f} / ≤{current_chance:.1f}"
                            f"{lvl_text}"
                        ),
                        parse_mode="HTML")
                else:
                    balance_before = robber.balance_vv
                    _deduct_bail(robber, bail_amount)
                    from_safe = max(0, bail_amount - balance_before) if balance_before < bail_amount else 0

                    comp = int(bail_amount * COMPENSATION_PERCENT)
                    ptax = int(bail_amount * POT_TAX_PERCENT)
                    victim.balance_vv += comp
                    robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_DURATION_MINUTES)
                    pa = await _add_pot_tax(session, chat_id, ptax)
                    if pa > 0 and chat_id:
                        await check_pot_explosion(session, chat_id, call.bot)
                    await session.commit()

                    deduct_text = ""
                    if from_safe > 0:
                        deduct_text = f"\n💸 <i>Из них {from_safe:,} 🪙 из сейфа</i>"

                    pt = f"\n🏦 Нал��г: <b>+{pa:,} 🪙</b>" if pa > 0 else ""
                    gloves_text = "\n🧤 Перчатки использованы!" if hg else ""

                    has_law, law_count = await _has_lawyer(session, rid)
                    lawyer_hint = ""
                    result_btns = []
                    if has_law:
                        result_btns.append([InlineKeyboardButton(
                            text="💼 Вызвать адвоката",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])
                        lawyer_hint = f"\n\n💼 <i>Адвокат ({law_count} шт.) может освободить!</i>"

                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"🧬 <b>ПРОВАЛ!</b> ❌\n\n"
                            f"👤 <b>{rn}</b> попался при краже {item.name}!\n\n"
                            f"💸 Залог: <b>{bail_amount:,} 🪙</b> (списан){deduct_text}\n"
                            f"💰 Компенсация {v_name}: <b>+{comp:,} 🪙</b>\n"
                            f"🔒 Тюрьма: {JAIL_DURATION_MINUTES}мин"
                            f"{pt}{gloves_text}\n\n"
                            f"🎲 {roll:.1f} / ≤{current_chance:.1f}"
                            f"{lawyer_hint}"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)

                _cleanup_session(iid, rid, vid)

            except Exception as e:
                await session.rollback()
                _cleanup_session(iid, rid, vid)
                logger.error(f"❌ rob_gene: {e}", exc_info=True)
            finally:
                await session.close()


# ============================================================================
# ОГРАБЛЕНИЕ СЕЙФА — разведка
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_safe_target_"))
async def rob_safe_recon(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[3]), int(parts[4])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id

    _reset_timer(iid, rid, vid)

    db = get_db()
    async for session in db.get_session():
        try:
            vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
            victim = vr.scalar_one_or_none()
            if not victim or not victim.has_active_safe():
                return
            rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
            robber = rr.scalar_one_or_none()
            if not robber:
                return
            v_name = _display_name(victim)
            se = "🏦" if victim.safe_type == "elite" else "🧰"
            sn = "Элитный" if victim.safe_type == "elite" else "Ржавый"
            level = victim.get_safe_level()

            if victim.safe_type == "elite":
                health = victim.elite_safe_health
                if health >= 2:
                    durability_line = f"🛡 Состояние: 🟢 <b>Новое (2/2)</b> | ⭐ ур.{level}"
                elif health == 1:
                    durability_line = f"🛡 Состояние: 🟡 <b>Повреждено (1/2)</b> | ⭐ ур.{level}"
                else:
                    durability_line = f"🛡 Состояние: 🔴 <b>Сломано (0/2)</b> | ⭐ ур.{level}"
            else:
                durability_line = f"❤️ Прочность: <b>{victim.safe_health}/3</b> | ⭐ ур.{level}"

            attacker_total = robber.balance_vv + (robber.hidden_coins or 0)
            victim_total = victim.balance_vv + (victim.hidden_coins or 0)
            loot_percent = calculate_loot_percent(attacker_total, victim_total)

            ht = ""
            if victim.hidden_item_ids:
                for hid in victim.hidden_item_ids:
                    ir = await session.execute(select(Item).where(Item.id == hid))
                    io = ir.scalar_one_or_none()
                    if io:
                        ht += f"  • 🧬 {io.name} ({io.price:,} 🪙)\n"
            if victim.hidden_coins and victim.hidden_coins > 0:
                ht += f"  • 💰 {victim.hidden_coins:,} 🪙 (заберёте ~{loot_percent * 100:.0f}%)\n"
            if not ht:
                ht = "  <i>Пуст</i>"

            crowbar_chance = calculate_rob_chance(
                robber.balance_vv, victim.balance_vv, CROWBAR_SUCCESS_CHANCE * 100
            )

            small_target_warning = ""
            if crowbar_chance < CROWBAR_SUCCESS_CHANCE * 100:
                small_target_warning = (
                    f"\n⚠️ <b>Цель слишком мелкая для тебя!</b> Из-за разницы в весовых категориях "
                    f"твой шанс на успех снижен до <b>{crowbar_chance:.1f}%</b>, "
                    f"но залог за провал остается прежним.\n"
                )

            btns = [[InlineKeyboardButton(text="🔓 Взломать код", callback_data=f"rob_safe_{rid}_{vid}")]]
            ri_r = await session.execute(select(Inventory).where(Inventory.user_id == rid))
            ri = ri_r.scalars().all()
            if victim.safe_type == "rusty" and _has_item(ri, ITEM_CROWBAR):
                btns.append([InlineKeyboardButton(
                    text=f"🔨 Лом ({crowbar_chance:.0f}%)",
                    callback_data=f"rob_crowbar_{rid}_{vid}")])
            btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rob_back_{rid}_{vid}")])
            btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"rob_cancel_{rid}_{vid}")])
            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=(f"🔍 <b>{v_name}</b>\n\n"
                      f"{se} <b>{sn}</b> {durability_line}\n\n"
                      f"<b>В сейфе:</b>\n{ht}\n"
                      f"📈 <i>Ожидаемая добыча: ~{loot_percent * 100:.0f}%</i>\n"
                      f"{small_target_warning}\n"
                      f"⚠️ <i>Провал = тюрьма {JAIL_SAFE_FAIL_MINUTES}мин</i>"),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        except Exception as e:
            logger.error(f"❌ safe_recon: {e}", exc_info=True)
        finally:
            await session.close()


# ============================================================================
# ЛОМ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_crowbar_"))
async def rob_crowbar(call: CallbackQuery) -> None:
    global _failed_crowbar_attempts
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)

    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id
    rn = call.from_user.first_name or "Грабитель"

    # ── Антиспам Lock ──
    lock = _get_lock(f"crowbar_{iid}")
    if lock.locked():
        return
    async with lock:
        _reset_timer(iid, rid, vid)

        db = get_db()
        async for session in db.get_session():
            try:
                await _track_both(session, call, rid, vid)
                rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
                robber = rr.scalar_one_or_none()
                vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
                victim = vr.scalar_one_or_none()
                if not victim or not victim.has_active_safe():
                    _cleanup_session(iid, rid, vid)
                    return
                if victim.safe_type != "rusty":
                    return
                if not await _consume_item(session, rid, ITEM_CROWBAR):
                    return

                crowbar_current_chance = calculate_rob_chance(
                    robber.balance_vv, victim.balance_vv, CROWBAR_SUCCESS_CHANCE * 100
                )
                success = random.random() <= crowbar_current_chance / 100
                if not success:
                    _failed_crowbar_attempts += 1
                    if _failed_crowbar_attempts % 3 == 0:
                        success = True
                        _failed_crowbar_attempts = 0

                used_bouncer = (_robbery_sessions.get(iid) or {}).get("used_bouncer", False)

                if success:
                    _failed_crowbar_attempts = 0
                    safe_coins_before = victim.hidden_coins or 0
                    safe_type_before = victim.safe_type
                    loot, loot_rob, loot_old_level, loot_new_levels, loot_percent, safe_destroyed = await _loot_safe(session, rid, victim)
                    apply_hazbik_protection(victim)
                    await session.commit()
                    _cleanup_session(iid, rid, vid)
                    if loot_rob and loot_new_levels:
                        await grant_level_rewards(call.bot, session, loot_rob, loot_old_level, loot_new_levels)
                        await session.commit()
                    elite_damaged = (not safe_destroyed and safe_type_before == "elite")
                    elite_destroyed = (safe_destroyed and safe_type_before == "elite")
                    await _send_robbery_notification(call.bot, victim, rn, amount=int(safe_coins_before * loot_percent), used_bouncer=used_bouncer, elite_safe_damaged=elite_damaged, elite_safe_destroyed=elite_destroyed)
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"🔨💥 <b>Сейф вскрыт!</b> Учитывая ваш статус и обороты жертвы, вы смогли вынести "
                            f"<b>{loot_percent * 100:.0f}%</b> содержимого. ✅\n\n"
                            f"Ржавый сейф не выдержал удара!\n"
                            f"🔨 Лом сломался после использования.\n\n"
                            f"{loot}"
                        ),
                        parse_mode="HTML")
                else:
                    robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_SAFE_FAIL_MINUTES)
                    victim.safe_health = max(0, victim.safe_health - 1)
                    await session.commit()
                    _cleanup_session(iid, rid, vid)

                    has_law, law_count = await _has_lawyer(session, rid)
                    result_btns = []
                    lawyer_hint = ""
                    if has_law:
                        result_btns.append([InlineKeyboardButton(
                            text=f"💼 Адвокат ({law_count} шт.)",
                            callback_data=f"rob_lawyer_{rid}_{vid}")])
                        lawyer_hint = f"\n\n💼 <i>Адвокат ({law_count} шт.) может освободить!</i>"

                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"🔨💥 <b>Лом сломался!</b> ❌\n\n"
                            f"Взлом провален. Лом уничтожен.\n"
                            f"❤️ Прочность сейфа: {victim.safe_health}/3\n"
                            f"🔒 Тюрьма: {JAIL_SAFE_FAIL_MINUTES}мин{lawyer_hint}"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)

            except Exception as e:
                await session.rollback()
                _cleanup_session(iid, rid, vid)
                logger.error(f"❌ crowbar: {e}", exc_info=True)
            finally:
                await session.close()


# ============================================================================
# СЕЙФ МИНИ-ИГРА (ОПТИМИЗИРОВАНО: answer() первым, Lock, MessageNotModified)
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_safe_") and "_target_" not in c.data)
async def rob_safe_start(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id

    _reset_timer(iid, rid, vid)

    db = get_db()
    async for session in db.get_session():
        try:
            await _track_both(session, call, rid, vid)
            vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
            victim = vr.scalar_one_or_none()
            if not victim or not victim.has_active_safe():
                _cleanup_session(iid, rid, vid)
                return
            ri_r = await session.execute(select(Inventory).where(Inventory.user_id == rid))
            ri = ri_r.scalars().all()
            code = victim.safe_code

            hidden_pos = random.randint(0, 3)
            revealed = {i for i in range(4) if i != hidden_pos}

            lockpick_count = sum(
                inv.quantity for inv in ri
                if inv.item.name == ITEM_LOCKPICK and inv.quantity > 0
            )

            v_name = _display_name(victim)
            prev_sess = _robbery_sessions.get(iid) or {}
            _robbery_sessions[iid] = {
                "robber_id": rid,
                "victim_id": vid,
                "victim_username": v_name,
                "mode": "safe_crack",
                "code": code,
                "original_code": code,
                "revealed": list(revealed),
                "hidden_pos": hidden_pos,
                "attempts_left": SAFE_MAX_ATTEMPTS,
                "max_attempts": SAFE_MAX_ATTEMPTS,
                "lockpicks_available": lockpick_count,
                "used_bouncer": prev_sess.get("used_bouncer", False),
            }

            masked = _mask_code(code, revealed)
            tt = ""
            if lockpick_count > 0:
                tt += f"🗝 Отмычек: <b>{lockpick_count}</b>\n"
            tt += f"🔓 Угадай цифру (позиция {hidden_pos + 1})\n"
            tt += f"⚠️ Провал = тюрьма {JAIL_SAFE_FAIL_MINUTES}мин\n"
            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=(f"🔓 <b>Взлом {v_name}</b>\n\n"
                      f"Код: <code>{masked}</code>\n"
                      f"Попыток: <b>{SAFE_MAX_ATTEMPTS}</b>\n\n{tt}"
                      f"Набери код 👇"),
                parse_mode="HTML",
                reply_markup=_build_code_keyboard(rid, vid, revealed, code, ""))
        except Exception as e:
            logger.error(f"❌ safe_start: {e}", exc_info=True)
            _cleanup_session(iid, rid, vid)
        finally:
            await session.close()


def _build_code_keyboard(rid, vid, revealed, code, ci, show_lockpick=False, lockpick_count=0):
    rows = []
    d = ci + "_" * (4 - len(ci))
    rows.append([InlineKeyboardButton(text=f"[ {' '.join(d)} ]", callback_data="noop")])

    if show_lockpick:
        rows.append([InlineKeyboardButton(
            text=f"🗝 Использовать отмычку ({lockpick_count} шт.)",
            callback_data=f"safe_lockpick_{rid}_{vid}")])
        rows.append([InlineKeyboardButton(text="❌ Сдаться", callback_data=f"safe_giveup_{rid}_{vid}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    for rs in range(1, 10, 3):
        row = [InlineKeyboardButton(text=str(x), callback_data=f"safe_digit_{rid}_{vid}_{ci}{x}")
            for x in range(rs, min(rs + 3, 10))]
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="⬅️", callback_data=f"safe_digit_{rid}_{vid}_{ci[:-1] if ci else ''}"),
        InlineKeyboardButton(text="0", callback_data=f"safe_digit_{rid}_{vid}_{ci}0"),
        InlineKeyboardButton(text="✅", callback_data=f"safe_submit_{rid}_{vid}_{ci}")])
    rows.append([InlineKeyboardButton(text="❌", callback_data=f"rob_cancel_{rid}_{vid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data and c.data.startswith("safe_digit_"))
async def safe_digit(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ (критично для скорости набора кода!)
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    ci = parts[4] if len(parts) > 4 else ""
    if not _check_robber(call, rid):
        return
    if len(ci) > 4:
        ci = ci[:4]
    iid = call.inline_message_id
    sess = _robbery_sessions.get(iid)
    if not sess:
        return
    if sess.get("attempts_left", 0) <= 0:
        return

    _reset_timer(iid, rid, vid)

    # ── Обновляем клавиатуру без запроса к БД (быстро!) ──
    await _safe_edit_reply_markup(
        call.bot, inline_message_id=iid,
        reply_markup=_build_code_keyboard(rid, vid, set(sess.get("revealed", [])), sess["code"], ci))


@router.callback_query(lambda c: c.data and c.data.startswith("safe_submit_"))
async def safe_submit(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    guess = parts[4] if len(parts) > 4 else ""
    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id
    sess = _robbery_sessions.get(iid)
    if not sess:
        return
    if len(guess) != 4 or not guess.isdigit():
        return
    if sess.get("attempts_left", 0) <= 0:
        return

    # ── Антиспам Lock (предотвращает двойной submit) ──
    lock = _get_lock(f"submit_{iid}")
    if lock.locked():
        return
    async with lock:
        _reset_timer(iid, rid, vid)

        code = sess["code"]
        sess["attempts_left"] -= 1
        al = sess["attempts_left"]
        v_name = sess.get("victim_username", str(vid))
        db = get_db()

        async for session in db.get_session():
            try:
                rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
                robber = rr.scalar_one_or_none()
                vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
                victim = vr.scalar_one_or_none()
                if not victim or not victim.has_active_safe():
                    _cleanup_session(iid, rid, vid)
                    return

                if guess == code:
                    rn = call.from_user.first_name or "Грабитель"
                    used_bouncer = sess.get("used_bouncer", False)
                    safe_coins_before = victim.hidden_coins or 0
                    safe_type_before = victim.safe_type
                    loot, loot_rob, loot_old_level, loot_new_levels, loot_percent, safe_destroyed = await _loot_safe(session, rid, victim)
                    apply_hazbik_protection(victim)
                    await session.commit()
                    _cleanup_session(iid, rid, vid)
                    if loot_rob and loot_new_levels:
                        await grant_level_rewards(call.bot, session, loot_rob, loot_old_level, loot_new_levels)
                        await session.commit()
                    elite_damaged = (not safe_destroyed and safe_type_before == "elite")
                    elite_destroyed = (safe_destroyed and safe_type_before == "elite")
                    await _send_robbery_notification(call.bot, victim, rn, amount=int(safe_coins_before * loot_percent), used_bouncer=used_bouncer, elite_safe_damaged=elite_damaged, elite_safe_destroyed=elite_destroyed)
                    await _safe_edit_text(
                        call.bot, inline_message_id=iid,
                        text=(
                            f"🔓 <b>Сейф вскрыт!</b> Учитывая ваш статус и обороты жертвы, вы смогли вынести "
                            f"<b>{loot_percent * 100:.0f}%</b> содержимого. ✅\n\n"
                            f"Код <code>{code}</code>\n\n{loot}"
                        ),
                        parse_mode="HTML")
                    return

                if victim.safe_type == "rusty":
                    victim.safe_health = max(0, victim.safe_health - 1)

                new_code = generate_safe_code()
                victim.safe_code = new_code
                sess["code"] = new_code

                hidden_pos = random.randint(0, 3)
                revealed = {i for i in range(4) if i != hidden_pos}
                sess["revealed"] = list(revealed)
                sess["hidden_pos"] = hidden_pos

                await session.commit()

                if al <= 0:
                    lp_r = await session.execute(select(Inventory).where(Inventory.user_id == rid))
                    lp_inv = lp_r.scalars().all()
                    lockpick_count = sum(
                        inv.quantity for inv in lp_inv
                        if inv.item.name == ITEM_LOCKPICK and inv.quantity > 0
                    )
                    sess["lockpicks_available"] = lockpick_count

                    if lockpick_count > 0:
                        health_line = f"❤️ Прочность: {victim.safe_health}/3\n" if victim.safe_type == "rusty" else ""
                        masked = _mask_code(new_code, revealed)
                        await _safe_edit_text(
                            call.bot, inline_message_id=iid,
                            text=(f"🔓 <b>{v_name}</b>\n\n"
                                  f"❌ <code>{guess}</code> — неверно!\n"
                                  f"🔄 Код перекодирован!\n"
                                  f"{health_line}\n"
                                  f"Новый код: <code>{masked}</code>\n"
                                  f"⚠️ <b>Попытки закончились!</b>\n\n"
                                  f"🗝 У вас есть <b>{lockpick_count}</b> отмычек.\n"
                                  f"Использовать отмычку для +1 попытки?"),
                            parse_mode="HTML",
                            reply_markup=_build_code_keyboard(
                                rid, vid, revealed, new_code, "",
                                show_lockpick=True, lockpick_count=lockpick_count))
                        return
                    else:
                        robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_SAFE_FAIL_MINUTES)
                        await session.commit()
                        _cleanup_session(iid, rid, vid)

                        health_line = f"❤️ Прочность сейфа: {victim.safe_health}/3\n" if victim.safe_type == "rusty" else ""
                        has_law, law_count = await _has_lawyer(session, rid)
                        result_btns = []
                        lawyer_hint = ""
                        if has_law:
                            result_btns.append([InlineKeyboardButton(
                                text=f"💼 Адвокат ({law_count} шт.)",
                                callback_data=f"rob_lawyer_{rid}_{vid}")])
                            lawyer_hint = f"\n\n💼 <i>Адвокат ({law_count} шт.) может освободить!</i>"

                        await _safe_edit_text(
                            call.bot, inline_message_id=iid,
                            text=(f"🔒 <b>Взлом провален!</b> ❌\n\n"
                                  f"❌ <code>{guess}</code> — неверно!\n"
                                  f"{health_line}"
                                  f"🗝 Отмычек нет.\n\n"
                                  f"🔒 Тюрьма: {JAIL_SAFE_FAIL_MINUTES}мин{lawyer_hint}"),
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)
                        return

                health_line = f"❤️ Прочность: {victim.safe_health}/3\n" if victim.safe_type == "rusty" else ""
                masked = _mask_code(new_code, revealed)
                lockpick_count = sess.get("lockpicks_available", 0)
                lp_text = f"🗝 Отмычек: <b>{lockpick_count}</b>\n" if lockpick_count > 0 else ""

                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(f"🔓 <b>{v_name}</b>\n\n"
                          f"❌ <code>{guess}</code> — неверно!\n"
                          f"🔄 Код перекодирован!\n"
                          f"{health_line}\n"
                          f"Новый код: <code>{masked}</code>\n"
                          f"Осталось попыток: <b>{al}</b>\n"
                          f"{lp_text}\nНабери 👇"),
                    parse_mode="HTML",
                    reply_markup=_build_code_keyboard(rid, vid, revealed, new_code, ""))

            except Exception as e:
                await session.rollback()
                _cleanup_session(iid, rid, vid)
                logger.error(f"❌ submit: {e}", exc_info=True)
            finally:
                await session.close()


# ============================================================================
# ОТМЫЧКА
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safe_lockpick_"))
async def safe_use_lockpick(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return

    iid = call.inline_message_id
    sess = _robbery_sessions.get(iid)
    if not sess:
        return

    # ── Антиспам Lock ──
    lock = _get_lock(f"lockpick_{iid}")
    if lock.locked():
        return
    async with lock:
        _reset_timer(iid, rid, vid)

        v_name = sess.get("victim_username", str(vid))
        db = get_db()

        async for session in db.get_session():
            try:
                consumed = await _consume_item(session, rid, ITEM_LOCKPICK)
                if not consumed:
                    await _safe_answer(call, "❌ Нет отмычек!", show_alert=True)
                    return

                lp_r = await session.execute(select(Inventory).where(Inventory.user_id == rid))
                lp_inv = lp_r.scalars().all()
                lockpick_count = sum(
                    inv.quantity for inv in lp_inv
                    if inv.item.name == ITEM_LOCKPICK and inv.quantity > 0
                )

                await session.commit()

                sess["attempts_left"] = 1
                sess["lockpicks_available"] = lockpick_count

                code = sess["code"]
                revealed = set(sess.get("revealed", []))
                masked = _mask_code(code, revealed)

                vr = await session.execute(select(User).where(User.tg_id == vid).with_for_update())
                victim = vr.scalar_one_or_none()

                health_line = ""
                if victim and victim.safe_type == "rusty":
                    health_line = f"❤️ Прочность: {victim.safe_health}/3\n"

                lp_text = f"🗝 Отмычек осталось: <b>{lockpick_count}</b>\n" if lockpick_count > 0 else ""

                await _safe_edit_text(
                    call.bot, inline_message_id=iid,
                    text=(f"🔓 <b>{v_name}</b>\n\n"
                          f"🗝 <b>Отмычка использована!</b>\n"
                          f"Замок поддался — ещё <b>1 попытка</b>!\n\n"
                          f"{health_line}"
                          f"Код: <code>{masked}</code>\n"
                          f"Попыток: <b>1</b>\n"
                          f"{lp_text}\nНабери 👇"),
                    parse_mode="HTML",
                    reply_markup=_build_code_keyboard(rid, vid, revealed, code, ""))

            except Exception as e:
                await session.rollback()
                logger.error(f"❌ lockpick: {e}", exc_info=True)
            finally:
                await session.close()


# ============================================================================
# СДАТЬСЯ
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("safe_giveup_"))
async def safe_giveup(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    parts = call.data.split("_")
    rid, vid = int(parts[2]), int(parts[3])
    if not _check_robber(call, rid):
        return

    iid = call.inline_message_id

    db = get_db()
    async for session in db.get_session():
        try:
            rr = await session.execute(select(User).where(User.tg_id == rid).with_for_update())
            robber = rr.scalar_one_or_none()
            if robber:
                robber.jail_until = datetime.utcnow() + timedelta(minutes=JAIL_SAFE_FAIL_MINUTES)
            await session.commit()

            _cleanup_session(iid, rid, vid)

            has_law, law_count = await _has_lawyer(session, rid)
            result_btns = []
            lawyer_hint = ""
            if has_law:
                result_btns.append([InlineKeyboardButton(
                    text=f"💼 Адвокат ({law_count} шт.)",
                    callback_data=f"rob_lawyer_{rid}_{vid}")])
                lawyer_hint = f"\n\n💼 <i>Адвокат может освободить!</i>"

            await _safe_edit_text(
                call.bot, inline_message_id=iid,
                text=(f"🏳️ <b>Сдался!</b>\n\n"
                      f"Взлом отменён.\n"
                      f"🔒 Тюрьма: {JAIL_SAFE_FAIL_MINUTES}мин{lawyer_hint}"),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=result_btns) if result_btns else None)

        except Exception as e:
            await session.rollback()
            _cleanup_session(iid, rid, vid)
            logger.error(f"❌ giveup: {e}", exc_info=True)
        finally:
            await session.close()


# ============================================================================
# ЛУТАНИЕ СЕЙФА
# ============================================================================


async def _loot_safe(session, robber_id, victim):
    lines = []
    rob = None
    old_level = -1  # -1 означает «не инициализировано»
    new_levels: list[int] = []
    loot_percent = SAFE_LOOT_COIN_PERCENT  # default fallback
    safe_destroyed = True  # По умолчанию сейф уничтожается

    is_elite_first_crack = (
        victim.safe_type == "elite"
        and victim.elite_safe_health >= 2
    )

    if victim.hidden_item_ids:
        for iid in list(victim.hidden_item_ids):
            ir = await session.execute(select(Item).where(Item.id == iid))
            io = ir.scalar_one_or_none()
            if io and is_gene_item(io):
                await add_item_to_inventory(session, robber_id, iid, 1)
                lines.append(f"🧬 {io.name} ({io.price:,}🪙)")
            elif io:
                await add_item_to_inventory(session, victim.tg_id, iid, 1)
        victim.hidden_item_ids = []

    if victim.hidden_coins and victim.hidden_coins > 0:
        rr = await session.execute(select(User).where(User.tg_id == robber_id).with_for_update())
        rob = rr.scalar_one_or_none()
        attacker_total = (rob.balance_vv + (rob.hidden_coins or 0)) if rob else 0
        victim_total = victim.balance_vv + (victim.hidden_coins or 0)
        loot_percent = calculate_loot_percent(attacker_total, victim_total)
        stolen = int(victim.hidden_coins * loot_percent)
        returned = victim.hidden_coins - stolen
        if rob and stolen > 0:
            rob.balance_vv += stolen
            old_level = rob.level
            new_levels = add_xp(rob, stolen)
            lines.append(f"💰 {stolen:,}🪙 ({loot_percent * 100:.0f}%)")
        if is_elite_first_crack:
            # Первый взлом элитного сейфа: остаток остаётся в сейфе
            if returned > 0:
                victim.hidden_coins = returned
                lines.append(f"↩️ {returned:,}🪙 остались в повреждённом сейфе ({100 - loot_percent * 100:.0f}%)")
            else:
                victim.hidden_coins = 0
        else:
            if returned > 0:
                victim.balance_vv += returned
                lines.append(f"↩️ {returned:,}🪙 возвращено владельцу ({100 - loot_percent * 100:.0f}%)")
            victim.hidden_coins = 0

    if is_elite_first_crack:
        # Первый взлом: сейф повреждён, но не уничтожен
        victim.elite_safe_health = 1
        victim.safe_code = generate_safe_code()
        safe_destroyed = False
    else:
        # Второй взлом элитного сейфа или любой взлом ржавого: уничтожаем
        if victim.safe_type == "elite":
            victim.elite_safe_health = 0
        destroy_safe(victim)
        safe_destroyed = True

    _safe_fail_tracker.pop(victim.tg_id, None)

    loot_text = "<b>Добыча:</b>\n" + "\n".join(f"  • {l}" for l in lines) if lines else "<b>Добыча:</b>\n  <i>Пуст!</i>"
    return loot_text, rob, old_level, new_levels, loot_percent, safe_destroyed


# ============================================================================
# ОТМЕНА
# ============================================================================


@router.callback_query(lambda c: c.data and c.data.startswith("rob_cancel_"))
async def rob_cancel(call: CallbackQuery) -> None:
    await _safe_answer(call)  # ← Мгновенный ответ
    _set_bot_ref(call.bot)
    raw = call.data.split("rob_cancel_")[1]
    parts = raw.split("_")
    rid = int(parts[0])
    vid = int(parts[1]) if len(parts) > 1 else None

    if not _check_robber(call, rid):
        return
    iid = call.inline_message_id

    _cleanup_session(iid, rid, vid)

    await _safe_edit_text(
        call.bot, inline_message_id=iid,
        text="❌ <b>Отменено.</b>", parse_mode="HTML")
