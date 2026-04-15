"""Статистика по балансу + сейф + уровни, /top для групп."""
import logging
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from sqlalchemy import select

from database import get_db
from models import User, ChatActivity
from utils.keyboards import get_main_keyboard
from utils.levels import get_required_xp, MAX_LEVEL

logger = logging.getLogger(__name__)
router = Router()


def _clean_name(user) -> str:
    name = user.username or f"Игрок #{user.tg_id}"
    return name.lstrip("@")


def _total_coins(user: User) -> int:
    return user.balance_vv + (user.hidden_coins or 0)


def _level_badge(level: int) -> str:
    if level >= 70:
        return "💎"
    elif level >= 50:
        return "👑"
    elif level >= 30:
        return "⭐"
    elif level >= 15:
        return "🔥"
    elif level >= 5:
        return "✨"
    return "🌱"


def _format_player_line(idx: int, user: User, total: int) -> str:
    name = _clean_name(user)

    medal = ""
    if idx == 1:
        medal = "🥇 "
    elif idx == 2:
        medal = "🥈 "
    elif idx == 3:
        medal = "🥉 "

    safe_part = ""
    if user.hidden_coins and user.hidden_coins > 0:
        safe_part = f" (🔐 {user.hidden_coins:,})"

    badge = _level_badge(user.level)

    req = get_required_xp(user.level)
    if user.level >= MAX_LEVEL:
        xp_info = "MAX"
    else:
        pct = int(min(1.0, user.xp / req) * 100) if req > 0 else 100
        xp_info = f"{pct}%"

    return (
        f"{medal}<b>{idx}.</b> {name}\n"
        f"     💰 {total:,} 🪙{safe_part}\n"
        f"     {badge} Ур. <b>{user.level}</b> [{xp_info}]\n"
    )


async def _get_top_by_balance(session, user_ids: list[int] | None = None, limit: int = 10):
    if user_ids is not None:
        users_r = await session.execute(
            select(User).where(User.tg_id.in_(user_ids))
        )
    else:
        users_r = await session.execute(select(User))
    users = users_r.scalars().all()

    ranked = [(user, _total_coins(user)) for user in users]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:limit]


# ============================================================================
# /stats (только ЛС)
# ============================================================================


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.chat.type != "private":
        return
    await handle_stats(message)


@router.message(lambda m: m.text == "📊 Статистика" and m.chat.type == "private")
async def button_stats(message: Message) -> None:
    await handle_stats(message)


async def handle_stats(message: Message) -> None:
    db = get_db()
    async for session in db.get_session():
        try:
            count_r = await session.execute(select(User))
            all_users = count_r.scalars().all()
            total_users = len(all_users)

            top = await _get_top_by_balance(session, limit=10)

            text = (
                f"<b>📊 Статистика Cum Gen</b>\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"👥 Всего игроков: <code>{total_users}</code>\n\n"
                f"<b>💰 Топ-10 по богатству</b>\n"
                f"<i>наличные + сейф</i>\n"
                f"─────────────────\n"
            )
            if top:
                for idx, (user, total) in enumerate(top, 1):
                    text += _format_player_line(idx, user, total)
                    if idx <= 3:
                        text += "─────────────────\n"
            else:
                text += "<i>Пока нет игроков.</i>\n"

            text += f"\n━━━━━━━━━━━━━━━━━"

            await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())
        except Exception as e:
            logger.error(f"❌ stats: {e}", exc_info=True)
            await message.answer("❌ Ошибка", reply_markup=get_main_keyboard())
        finally:
            await session.close()


# ============================================================================
# /top (только группы)
# ============================================================================


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("❌ Эта команда доступна только в групповых чатах!")
        return

    chat_id = message.chat.id
    db = get_db()
    async for session in db.get_session():
        try:
            activity_r = await session.execute(
                select(ChatActivity.user_id).where(ChatActivity.chat_id == chat_id))
            user_ids = [row[0] for row in activity_r.all()]

            if not user_ids:
                await message.answer(
                    "🏆 В этом чате пока никто не играл...\n\n"
                    "<i>Играйте в казино, теребите или грабьте — и попадёте в топ!</i>",
                    parse_mode="HTML")
                return

            top = await _get_top_by_balance(session, user_ids=user_ids, limit=10)

            if not top:
                await message.answer("🏆 В этом чате пока никто не играл...")
                return

            text = (
                f"<b>🏆 Топ-10 богачей чата</b>\n"
                f"━━━━━━━━━━━━━━━━━\n\n"
            )
            for idx, (user, total) in enumerate(top, 1):
                text += _format_player_line(idx, user, total)
                if idx <= 3:
                    text += "─────────────────\n"

            text += (
                f"\n━━━━━━━━━━━━━━━━━\n"
                f"<i>Тереби, грабь, играй — попадай в топ!</i>"
            )

            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"❌ /top: {e}", exc_info=True)
            await message.answer("❌ Ошибка")
        finally:
            await session.close()
