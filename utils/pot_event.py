"""Ивент 'Грабёж Общака' — взлом банка чата."""
import logging
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, GroupChat, ChatActivity

logger = logging.getLogger(__name__)

POT_EXPLOSION_THRESHOLD = 100_000
MAX_LUCKY_PLAYERS = 50
MIN_REWARD = 1_500
MAX_REWARD = 7_777


async def ensure_group_chat(session: AsyncSession, chat_id: int) -> GroupChat:
    gc_r = await session.execute(
        select(GroupChat).where(GroupChat.chat_id == chat_id)
    )
    group = gc_r.scalar_one_or_none()
    if group is None:
        group = GroupChat(chat_id=chat_id, common_pot=0)
        session.add(group)
        await session.flush()
    return group


async def track_chat_activity(session: AsyncSession, chat_id: int, user_id: int) -> None:
    try:
        await ensure_group_chat(session, chat_id)
        existing = await session.execute(
            select(ChatActivity).where(
                ChatActivity.user_id == user_id,
                ChatActivity.chat_id == chat_id,
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(ChatActivity(user_id=user_id, chat_id=chat_id))
            await session.flush()
            logger.debug(f"📌 Активность: user={user_id} chat={chat_id}")
    except Exception as e:
        err = str(e).lower()
        if "unique" in err or "duplicate" in err:
            pass
        else:
            logger.error(f"❌ track_activity: {e}")


async def check_pot_explosion(session: AsyncSession, chat_id: int, bot) -> bool:
    gc_r = await session.execute(
        select(GroupChat).where(GroupChat.chat_id == chat_id)
    )
    group = gc_r.scalar_one_or_none()

    if not group:
        return False
    if group.common_pot < POT_EXPLOSION_THRESHOLD:
        return False
    if group.is_event_active:
        return False

    group.is_event_active = True
    pot_total = group.common_pot

    logger.info(f"🚨 POT EXPLOSION: chat={chat_id}, pot={pot_total}")

    activity_r = await session.execute(
        select(ChatActivity).where(ChatActivity.chat_id == chat_id)
    )
    activities = activity_r.scalars().all()

    if not activities:
        group.common_pot = 0
        group.is_event_active = False
        await session.flush()
        logger.warning(f"⚠️ Ивент: нет игроков в {chat_id}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚨 <b>ОБЩАК ВЗЛОМАН!</b> 🚨\n\n"
                    f"Банк превысил <b>{POT_EXPLOSION_THRESHOLD:,} 🪙</b>...\n"
                    f"Но в списке активности никого не было! 💨\n\n"
                    f"💡 <i>Играйте в казино или грабьте — тогда попадёте в список!</i>\n"
                    f"🏦 Общак обнулён."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"❌ Уведомление (пусто): {e}")
        return True

    user_ids = [a.user_id for a in activities]
    random.shuffle(user_ids)
    lucky_ids = user_ids[:MAX_LUCKY_PLAYERS]

    users_r = await session.execute(
        select(User).where(User.tg_id.in_(lucky_ids))
    )
    users_map = {u.tg_id: u for u in users_r.scalars().all()}

    remaining_pot = pot_total
    winners_count = 0
    total_distributed = 0

    for uid in lucky_ids:
        if remaining_pot <= 0:
            break
        user = users_map.get(uid)
        if not user:
            continue
        reward = random.randint(MIN_REWARD, MAX_REWARD)
        if remaining_pot < reward:
            reward = remaining_pot
        user.balance_vv += reward
        remaining_pot -= reward
        total_distributed += reward
        winners_count += 1

    # CHANGED: Только обнуляем общак. НЕ удаляем ChatActivity!
    group.common_pot = 0
    group.is_event_active = False

    # REMOVED: Удаление записей ChatActivity
    # for activity in activities:
    #     await session.delete(activity)

    await session.flush()

    logger.info(f"🚨 DONE: chat={chat_id}, distributed={total_distributed}, winners={winners_count}")

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚨 <b>ОБЩАК ВЗЛОМАН!</b> 🚨\n\n"
                f"Банк чата превысил <b>{POT_EXPLOSION_THRESHOLD:,} 🪙</b> "
                f"и не выдержал напряжения!\n\n"
                f"💥 Система безопасности дала сбой, и деньги "
                f"разлетелись по карманам самых активных игроков.\n\n"
                f"💰 Было в общаке: <b>{pot_total:,} 🪙</b>\n"
                f"💸 <b>{winners_count}</b> игроков успели набить карманы!\n"
                f"💵 Раздано: <b>{total_distributed:,} 🪙</b>\n"
                f"🏦 Банк снова пуст.\n\n"
                f"<i>Играйте дальше и копите новый общак!</i>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"❌ Уведомление: {e}")

    return True
