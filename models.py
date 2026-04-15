from datetime import datetime, date, timedelta
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, Float,
    ForeignKey, Integer, String, JSON, UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class RarityEnum(PyEnum):
    COMMON = "Common"
    RARE = "Rare"
    EPIC = "Epic"
    LEGENDARY = "Legendary"


class SafeTypeEnum(PyEnum):
    RUSTY = "rusty"
    ELITE = "elite"

MAX_BOX_COUNT = 10
BOX_REFILL_HOURS = 2
MAX_DAILY_BETS = 25
MAX_DAILY_BJ = 15

SECURITY_DURATION_HOURS = 6
ROOF_DURATION_HOURS = 8

RUSTY_BASE_UPGRADE_COST = 1_500
RUSTY_UPGRADE_STEP = 300
RUSTY_MAX_LEVEL = 20
RUSTY_BASE_COIN_CAPACITY = 100_000
RUSTY_BASE_ITEM_CAPACITY = 1

ELITE_BASE_UPGRADE_COST = 4_000
ELITE_UPGRADE_STEP = 800
ELITE_MAX_LEVEL = 0
ELITE_BASE_COIN_CAPACITY = 700_000
ELITE_BASE_ITEM_CAPACITY = 3

SAFE_CAPACITY_MULTIPLIER = 0.30

ROBBERY_LOCK_TIMEOUT_SECONDS = 120
ROBBER_LOCK_TIMEOUT_SECONDS = 120


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    balance_vv: Mapped[int] = mapped_column(BigInteger, default=0)
    balance_stars: Mapped[int] = mapped_column(Integer, default=0)
    jail_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    safety_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_masked: Mapped[bool] = mapped_column(Boolean, default=False)

    box_count: Mapped[int] = mapped_column(Integer, nullable=False, default=MAX_BOX_COUNT)
    last_refill_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    black_market_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_market_check: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    safe_type: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    safe_code: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    safe_health: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    elite_safe_health: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    hidden_item_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    hidden_coins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    safe_level_rusty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    safe_level_elite: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    security_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    roof_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    roof_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    magazine_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    doll_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    putana_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    casino_bets_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_casino_reset: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    bj_games_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_bj_reset: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    hazbik_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Уровень и опыт ──
    xp: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    last_safe_coin_purchase: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    purchase_cooldowns: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Блокировка: цель ограбления ──
    is_being_robbed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    robbery_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Блокировка: вор (мульти-грабёж) ──
    is_robbing_now: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    robbing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Уведомления ──
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    inventory: Mapped[list["Inventory"]] = relationship(
        "Inventory", back_populates="user", cascade="all, delete-orphan", lazy="selectin")
    purchases: Mapped[list["PurchaseLog"]] = relationship(
        "PurchaseLog", back_populates="user", cascade="all, delete-orphan", lazy="selectin")

    def has_active_safe(self) -> bool:
        return self.safe_type is not None and self.safe_code is not None

    def get_safe_level(self) -> int:
        if self.safe_type == "elite":
            return self.safe_level_elite
        if self.safe_type == "rusty":
            return self.safe_level_rusty
        return 1

    def safe_item_limit(self) -> int:
        if self.safe_type == "elite":
            base = ELITE_BASE_ITEM_CAPACITY
            level = self.safe_level_elite
        elif self.safe_type == "rusty":
            base = RUSTY_BASE_ITEM_CAPACITY
            level = self.safe_level_rusty
        else:
            return 0
        return int(base * (1 + SAFE_CAPACITY_MULTIPLIER * (level - 1)))

    def safe_coin_limit(self) -> int:
        if self.safe_type == "elite":
            base = ELITE_BASE_COIN_CAPACITY
            level = self.safe_level_elite
        elif self.safe_type == "rusty":
            base = RUSTY_BASE_COIN_CAPACITY
            level = self.safe_level_rusty
        else:
            return 0
        return int(base * (1 + SAFE_CAPACITY_MULTIPLIER * (level - 1)))

    def get_upgrade_cost(self) -> int | None:
        if self.safe_type == "rusty":
            level = self.safe_level_rusty
            if level >= RUSTY_MAX_LEVEL:
                return None
            return RUSTY_BASE_UPGRADE_COST + RUSTY_UPGRADE_STEP * (level - 1)
        elif self.safe_type == "elite":
            level = self.safe_level_elite
            return ELITE_BASE_UPGRADE_COST + ELITE_UPGRADE_STEP * (level - 1)
        return None

    def can_upgrade_safe(self) -> tuple[bool, str]:
        if not self.has_active_safe():
            return False, "Нет активного сейфа!"
        cost = self.get_upgrade_cost()
        if cost is None:
            return False, "🏆 Достигнут максимальный уровень!"
        if self.balance_vv < cost:
            return False, f"Недостаточно! Нужно {cost:,} 🪙, у вас {self.balance_vv:,} 🪙"
        return True, ""

    def do_upgrade_safe(self) -> tuple[bool, str, int]:
        can, reason = self.can_upgrade_safe()
        if not can:
            return False, reason, 0
        cost = self.get_upgrade_cost()
        self.balance_vv -= cost
        old_level = self.get_safe_level()
        if self.safe_type == "rusty":
            self.safe_level_rusty += 1
            new_level = self.safe_level_rusty
        elif self.safe_type == "elite":
            self.safe_level_elite += 1
            new_level = self.safe_level_elite
        else:
            return False, "Ошибка типа сейфа", 0
        return True, f"Уровень {old_level} → {new_level}", cost

    def hidden_items_count(self) -> int:
        return len(self.hidden_item_ids) if self.hidden_item_ids else 0

    def is_security_active(self) -> bool:
        if not self.security_active:
            return False
        if self.security_until and self.security_until > datetime.utcnow():
            return True
        self.security_active = False
        self.security_until = None
        return False

    def is_roof_active(self) -> bool:
        if not self.roof_active:
            return False
        if self.roof_until and self.roof_until > datetime.utcnow():
            return True
        self.roof_active = False
        self.roof_until = None
        return False

    def is_hazbik_active(self) -> bool:
        return self.hazbik_until is not None and self.hazbik_until > datetime.utcnow()

    def hazbik_remaining_minutes(self) -> int:
        if not self.hazbik_until:
            return 0
        remaining = (self.hazbik_until - datetime.utcnow()).total_seconds()
        return max(0, int(remaining // 60))

    # ── Блокировка цели ──

    def check_robbery_lock(self) -> bool:
        if not self.is_being_robbed:
            return False
        if self.robbery_started_at:
            elapsed = (datetime.utcnow() - self.robbery_started_at).total_seconds()
            if elapsed > ROBBERY_LOCK_TIMEOUT_SECONDS:
                self.is_being_robbed = False
                self.robbery_started_at = None
                return False
        else:
            self.is_being_robbed = False
            return False
        return True

    def lock_robbery(self) -> None:
        self.is_being_robbed = True
        self.robbery_started_at = datetime.utcnow()

    def unlock_robbery(self) -> None:
        self.is_being_robbed = False
        self.robbery_started_at = None

    # ── Блокировка вора (мульти-грабёж) ──

    def check_robber_lock(self) -> bool:
        if not self.is_robbing_now:
            return False
        if self.robbing_started_at:
            elapsed = (datetime.utcnow() - self.robbing_started_at).total_seconds()
            if elapsed > ROBBER_LOCK_TIMEOUT_SECONDS:
                self.is_robbing_now = False
                self.robbing_started_at = None
                return False
        else:
            self.is_robbing_now = False
            return False
        return True

    def lock_robber(self) -> None:
        self.is_robbing_now = True
        self.robbing_started_at = datetime.utcnow()

    def unlock_robber(self) -> None:
        self.is_robbing_now = False
        self.robbing_started_at = None

    def is_magazine_active(self) -> bool:
        return self.magazine_until is not None and self.magazine_until > datetime.utcnow()

    def is_doll_active(self) -> bool:
        return self.doll_until is not None and self.doll_until > datetime.utcnow()

    def is_putana_active(self) -> bool:
        return self.putana_until is not None and self.putana_until > datetime.utcnow()

    def get_refill_hours(self) -> int:
        return 2 if self.is_magazine_active() else BOX_REFILL_HOURS

    def get_drop_multiplier(self) -> float:
        if self.is_putana_active():
            return 7.0
        if self.is_doll_active():
            return 2.0
        return 1.0

    def _reset_daily_bets_if_needed(self) -> None:
        today = date.today()
        if self.last_casino_reset is None or self.last_casino_reset < today:
            self.casino_bets_today = 0
            self.last_casino_reset = today

    def check_casino_limit(self) -> tuple[bool, int]:
        self._reset_daily_bets_if_needed()
        remaining = max(0, MAX_DAILY_BETS - self.casino_bets_today)
        return (remaining > 0, remaining)

    def use_casino_bet(self) -> bool:
        self._reset_daily_bets_if_needed()
        if self.casino_bets_today >= MAX_DAILY_BETS:
            return False
        self.casino_bets_today += 1
        return True

    def _reset_daily_bj_if_needed(self) -> None:
        today = date.today()
        if self.last_bj_reset is None or self.last_bj_reset < today:
            self.bj_games_today = 0
            self.last_bj_reset = today

    def check_bj_limit(self) -> tuple[bool, int]:
        self._reset_daily_bj_if_needed()
        remaining = max(0, MAX_DAILY_BJ - self.bj_games_today)
        return (remaining > 0, remaining)

    def use_bj_game(self) -> bool:
        self._reset_daily_bj_if_needed()
        if self.bj_games_today >= MAX_DAILY_BJ:
            return False
        self.bj_games_today += 1
        return True

    def increment_action(self) -> None:
        pass

    def get_purchase_cooldown_info(self, item_name: str) -> dict:
        from utils.cooldown_config import COIN_PURCHASE_COOLDOWNS
        config = COIN_PURCHASE_COOLDOWNS.get(item_name)
        if not config:
            return {'can_buy': True, 'bought_in_window': 0, 'limit': 999, 'next_available': None, 'cooldown_hours': 0}
        limit = config['limit']
        cooldown_hours = config['cooldown_hours']
        now = datetime.utcnow()
        cooldowns = self.purchase_cooldowns or {}
        key = f"{item_name}_purchases"
        records = cooldowns.get(key, [])
        cutoff = now - timedelta(hours=cooldown_hours)
        valid_records = [r for r in records if datetime.fromisoformat(r) > cutoff]
        bought_in_window = len(valid_records)
        can_buy = bought_in_window < limit
        next_available = None
        if not can_buy and valid_records:
            oldest = min(datetime.fromisoformat(r) for r in valid_records)
            next_available = oldest + timedelta(hours=cooldown_hours)
        return {
            'can_buy': can_buy, 'bought_in_window': bought_in_window,
            'limit': limit, 'next_available': next_available, 'cooldown_hours': cooldown_hours,
        }

    def record_coin_purchase(self, item_name: str) -> None:
        from utils.cooldown_config import COIN_PURCHASE_COOLDOWNS
        if item_name not in COIN_PURCHASE_COOLDOWNS:
            return
        now = datetime.utcnow()
        config = COIN_PURCHASE_COOLDOWNS[item_name]
        cooldown_hours = config['cooldown_hours']
        cooldowns = dict(self.purchase_cooldowns or {})
        key = f"{item_name}_purchases"
        records = cooldowns.get(key, [])
        cutoff = now - timedelta(hours=cooldown_hours)
        records = [r for r in records if datetime.fromisoformat(r) > cutoff]
        records.append(now.isoformat())
        cooldowns[key] = records
        self.purchase_cooldowns = cooldowns

    def active_boosts_text(self) -> str:
        now = datetime.utcnow()
        boosts = []
        if self.is_magazine_active():
            h = max(0, int((self.magazine_until - now).total_seconds() // 3600))
            m = max(0, int(((self.magazine_until - now).total_seconds() % 3600) // 60))
            boosts.append(f"🔞 Журнал ({h}ч {m}мин)")
        if self.is_doll_active():
            h = max(0, int((self.doll_until - now).total_seconds() // 3600))
            m = max(0, int(((self.doll_until - now).total_seconds() % 3600) // 60))
            boosts.append(f"🫦 Кукла x2 ({h}ч {m}мин)")
        if self.is_putana_active():
            h = max(0, int((self.putana_until - now).total_seconds() // 3600))
            m = max(0, int(((self.putana_until - now).total_seconds() % 3600) // 60))
            boosts.append(f"💋 Путана x7 ({h}ч {m}мин)")
        return "\n".join(boosts) if boosts else "<b>Без бустов</b>"


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    emoji: Mapped[str] = mapped_column(String(50), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drop_chance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rarity: Mapped[RarityEnum] = mapped_column(Enum(RarityEnum), nullable=False, default=RarityEnum.COMMON)
    is_starter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_in_inventory: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monthly_coin_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    inventory: Mapped[list["Inventory"]] = relationship(
        "Inventory", back_populates="item", cascade="all, delete-orphan", lazy="selectin")


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    user: Mapped[User] = relationship("User", back_populates="inventory", lazy="selectin")
    item: Mapped[Item] = relationship("Item", back_populates="inventory", lazy="selectin")


class PurchaseLog(Base):
    __tablename__ = "purchase_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    purchased_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="purchases")


class GroupChat(Base):
    __tablename__ = "group_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    common_pot: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_event_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ChatActivity(Base):
    __tablename__ = "chat_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("group_chats.chat_id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_chat_activity_user_chat"),)
