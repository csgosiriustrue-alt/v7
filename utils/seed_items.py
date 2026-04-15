"""Сид предметов — все цены после дефляции x10."""
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import Item, RarityEnum

logger = logging.getLogger(__name__)

# ============================================================================
# ГОЛОВАСТИКИ
# ============================================================================

TADPOLE_ITEMS = [
    # ── ЛЕГЕНДЫ (УЛЬТРА-ТИР) ──
    ("Ген Бога",                      "👁", 250_000, 0.01, RarityEnum.LEGENDARY),
    ("Ген Тайного Правительства",      "🔺", 150_000, 0.03, RarityEnum.LEGENDARY),
    ("Ген Иллюмината",                "👁‍🗨", 100_000, 0.09, RarityEnum.LEGENDARY),
    ("Гены Павла Дурова",             "🧬",  60_000, 0.18, RarityEnum.LEGENDARY),
    ("Гены Президента",               "🧬",  13_500, 0.17, RarityEnum.LEGENDARY),

    # ── ТОП-ТИР (ЭПИК) ──
    ("Ген Роналдо",                   "⚽️",   7_777, 1.50, RarityEnum.EPIC),
    ("Гены Криминального Авторитета",  "🧬",   6_000, 2.50, RarityEnum.EPIC),
    ("Ген Тун-Тун Сахура",            "🗿",   5_000, 2.25, RarityEnum.EPIC),
    ("Гены Меллстроя",                "🧬",   5_000, 2.25, RarityEnum.EPIC),
    ("Гены Программиста",             "🧬",   3_000, 2.50, RarityEnum.EPIC),
    ("Гены Бизнесмена",               "🧬",   1_800, 2.40, RarityEnum.EPIC),

    # ── МИД-ТИР (РЕДКИЕ) ──
    ("Ген Австрийского Художника",    "🎨",   1_488, 3.20, RarityEnum.RARE),
    ("Гены Артиста",                  "🧬",   1_250, 3.50, RarityEnum.RARE),
    ("Ген Задрота",                   "🤓",     888, 3.40, RarityEnum.RARE),
    ("Гены Саши Ноткоин",             "🧬",     800, 3.50, RarityEnum.RARE),
    ("Ген Доброго Спермоеда",         "🍼",     596, 3.45, RarityEnum.RARE),
    ("Гены Инфоцигана",               "🧬",     800, 3.25, RarityEnum.RARE),
    ("Гены Онлифанщицы",              "🧬",     800, 3.25, RarityEnum.RARE),

    # ── ЛОУ-ТИР (ОБЫЧНЫЕ) ──
    ("Гены Скамера",                  "🧬",     550, 3.00, RarityEnum.COMMON),
    ("Гены Альтушки",                 "🧬",     550, 3.00, RarityEnum.COMMON),
    ("Ген Оффника",                   "👊",     500, 3.10, RarityEnum.COMMON),
    ("Гены Лудомана",                 "🧬",     550, 2.75, RarityEnum.COMMON),
    ("Гены Инцела",                   "🧬",     550, 2.75, RarityEnum.COMMON),
    ("Гены Холдера TON",              "🧬",     400, 5.50, RarityEnum.COMMON),
    ("Гены Холдера Подарков",         "🧬",     400, 5.50, RarityEnum.COMMON),
    ("Гены Холдера Стикеров",         "🧬",     350, 5.00, RarityEnum.COMMON),
    ("Гены Холдера NFT",              "🧬",     350, 5.00, RarityEnum.COMMON),
    ("Гены Доставщика",               "🧬",     350, 5.00, RarityEnum.COMMON),
    ("Гены Ивана Золо",               "🧬",     280, 5.00, RarityEnum.COMMON),
    ("Гены Фурри",                    "🧬",     275, 5.00, RarityEnum.COMMON),
    ("Гены Фитоняши",                 "🧬",     275, 4.50, RarityEnum.COMMON),
    ("Гены Тиктокера",                "🧬",     275, 4.50, RarityEnum.COMMON),
    ("Гены Карлика",                  "🧬",     200, 5.00, RarityEnum.COMMON),
    ("Гены Результата инцеста",       "🧬",     170, 4.00, RarityEnum.COMMON),
    ("Воздухан",                      "💨",     155, 4.00, RarityEnum.COMMON),
    ("Урод",                          "🤮",     100, 4.00, RarityEnum.COMMON),
    ("Нищета",                        "🪣",     125, 3.50, RarityEnum.COMMON),
    ("Пустышка",                      "❌",     100, 3.50, RarityEnum.COMMON),
]

# ── Стетоскоп и Рентген УДАЛЕНЫ из TOOL_ITEMS ──
TOOL_ITEMS = [
    # name, emoji, price, price_stars, rarity, max_inv, monthly_limit, desc
    ("Ржавый Сейф",      "🧰",  5_000, 7,  RarityEnum.RARE,      1, 1,  "Спрячь 1 предмет или 100К монет."),
    ("Элитный Сейф",     "🏦", 15_000, 15, RarityEnum.LEGENDARY, 1, 1,  "Спрячь 3 предмета или 700К монет."),
    ("Охрана",           "💂",  0, 15,  RarityEnum.RARE,      15, 5,  "Блокирует ограбление на 6ч."),
    ("Крыша",            "🕴",  25_500, 25,  RarityEnum.EPIC,      3, 5,  "Блокирует + 15% залог грабителя. 8ч."),
    ("Отмычка",          "🗝",    0, 2,   RarityEnum.COMMON,   30, 0,  "+1 попытка ввода кода сейфа."),
    ("Лом",              "🔨",  1_200, 7,  RarityEnum.RARE,      10, 0,  "70% шанс вскрыть Ржавый сейф."),
    ("Адвокат",          "💼",  0, 1,  RarityEnum.RARE,      30, 0,  "Мгновенно из тюрьмы."),
    ("Липкие Перчатки",  "🧤",  1_000, 3,   RarityEnum.COMMON,   10, 0,  "x1.25 к шансу ограбления."),
    ("Durov's Figure",   "🗿",100_000,1000, RarityEnum.LEGENDARY, 1, 0,  "Коллекционная фигурка Дурова. Бесценна."),
    ("Вышибала",         "👊",  5_000, 10,  RarityEnum.EPIC,      3, 0,  "Одноразовый. Игнорирует охрану жертвы при ограблении."),
]

BOOST_ITEMS = [
    ("Журнал для взрослых", "🔞", 1_500, 3,  RarityEnum.RARE,      5, 5,
     "КД теребления: 4ч → 2ч на 24 часа."),
    ("Резиновая кукла",     "🫦", 2_500, 3,  RarityEnum.EPIC,      3, 3,
     "Шанс топ-тир + легенд x2 на 24ч."),
    ("Путана",              "💋", 10_069, 20, RarityEnum.LEGENDARY,  2, 2,
     "Шанс Rare + Epic + Legendary x7 на 24ч."),
]

CHARGE_ITEM = ("Заряд теребления", "⚡", 1_000, 3, RarityEnum.COMMON, 6, 15,
    "+1 заряд теребления. Макс 6.")

# Предметы для деактивации (удалённые из магазина)
DEPRECATED_ITEMS = {"Рентген", "Стетоскоп"}


async def seed_items(session: AsyncSession) -> None:
    existing_r = await session.execute(select(Item.name))
    existing_names = {row[0] for row in existing_r.all()}
    added = 0

    for name, emoji, price, drop_chance, rarity in TADPOLE_ITEMS:
        if name not in existing_names:
            session.add(Item(name=name, emoji=emoji, price=price, price_stars=0,
                drop_chance=drop_chance, rarity=rarity, is_starter=False,
                max_in_inventory=0, monthly_coin_limit=0,
                description=f"Генетический материал: {name}"))
            added += 1
        else:
            item_r = await session.execute(
                select(Item).where(Item.name == name).limit(1))
            item = item_r.scalars().first()
            if item:
                item.drop_chance = drop_chance
                item.price = price

    for name, emoji, price, price_stars, rarity, max_inv, monthly_limit, desc in TOOL_ITEMS:
        if name not in existing_names:
            session.add(Item(name=name, emoji=emoji, price=price, price_stars=price_stars,
                drop_chance=0, rarity=rarity, is_starter=False,
                max_in_inventory=max_inv, monthly_coin_limit=monthly_limit, description=desc))
            added += 1
        else:
            item_r = await session.execute(
                select(Item).where(Item.name == name).limit(1))
            item = item_r.scalars().first()
            if item:
                item.price = price
                item.price_stars = price_stars
                item.description = desc

    for name, emoji, price, price_stars, rarity, max_inv, monthly_limit, desc in BOOST_ITEMS:
        if name not in existing_names:
            session.add(Item(name=name, emoji=emoji, price=price, price_stars=price_stars,
                drop_chance=0, rarity=rarity, is_starter=False,
                max_in_inventory=max_inv, monthly_coin_limit=monthly_limit, description=desc))
            added += 1
        else:
            item_r = await session.execute(
                select(Item).where(Item.name == name).limit(1))
            item = item_r.scalars().first()
            if item:
                item.price = price
                item.price_stars = price_stars

    cn, ce, cp, cs, cr, cmi, cml, cd = CHARGE_ITEM
    if cn not in existing_names:
        session.add(Item(name=cn, emoji=ce, price=cp, price_stars=cs,
            drop_chance=0, rarity=cr, is_starter=False,
            max_in_inventory=cmi, monthly_coin_limit=cml, description=cd))
        added += 1
    else:
        item_r = await session.execute(
            select(Item).where(Item.name == cn).limit(1))
        item = item_r.scalars().first()
        if item:
            item.price = cp
            item.price_stars = cs

    if added > 0:
        await session.flush()
        logger.info(f"🧬 Добавлено {added} предметов")
    else:
        logger.info("🧬 Все предметы уже в БД (цены обновлены)")

    # Деактивируем удалённые предметы
    for dep_name in DEPRECATED_ITEMS:
        dep_r = await session.execute(
            select(Item).where(Item.name == dep_name).limit(1))
        dep = dep_r.scalars().first()
        if dep:
            dep.drop_chance = 0
            dep.price = 0
            dep.price_stars = 0
            dep.max_in_inventory = 0
            logger.info(f"🗑 Деактивирован: {dep_name}")

    tadpole_names = {t[0] for t in TADPOLE_ITEMS}
    old_r = await session.execute(select(Item).where(Item.drop_chance > 0))
    disabled = 0
    for item in old_r.scalars().all():
        if item.name not in tadpole_names:
            item.drop_chance = 0
            disabled += 1
    if disabled > 0:
        logger.info(f"🧬 Отключено {disabled} старых")

    # ── Удаляем дубликаты по имени (оставляем первый, мигрируем Inventory и hidden_item_ids) ──
    from models import Inventory, User
    all_items_r = await session.execute(select(Item).order_by(Item.id.asc()))
    all_items = all_items_r.scalars().all()
    seen_names: dict[str, int] = {}
    dupes_removed = 0
    for item in all_items:
        if item.name in seen_names:
            canonical_id = seen_names[item.name]
            dupe_id = item.id

            # 1. Мигрируем Inventory: переносим количества с дубликата на canonical item_id
            dupe_invs = (await session.execute(
                select(Inventory).where(Inventory.item_id == dupe_id))).scalars().all()
            if dupe_invs:
                dupe_user_ids = [inv.user_id for inv in dupe_invs]
                canon_invs_r = await session.execute(
                    select(Inventory).where(
                        Inventory.item_id == canonical_id,
                        Inventory.user_id.in_(dupe_user_ids)))
                canon_invs_by_user = {inv.user_id: inv for inv in canon_invs_r.scalars().all()}
                for dupe_inv in dupe_invs:
                    canon_inv = canon_invs_by_user.get(dupe_inv.user_id)
                    if canon_inv:
                        canon_inv.quantity += dupe_inv.quantity
                        await session.delete(dupe_inv)
                    else:
                        dupe_inv.item_id = canonical_id

            # 2. Мигрируем hidden_item_ids в сейфах (JSON поле User)
            users_r = await session.execute(select(User).where(User.hidden_item_ids.isnot(None)))
            for user in users_r.scalars().all():
                if user.hidden_item_ids and dupe_id in user.hidden_item_ids:
                    user.hidden_item_ids = [
                        canonical_id if x == dupe_id else x for x in user.hidden_item_ids
                    ]

            await session.flush()

            # 3. Теперь безопасно удаляем дубликат Item
            await session.delete(item)
            dupes_removed += 1
            logger.info(f"🗑 Дубликат '{item.name}' (id={dupe_id}) → каноничный id={canonical_id}")
        else:
            seen_names[item.name] = item.id
    if dupes_removed > 0:
        await session.flush()
        logger.info(f"🗑 Удалено {dupes_removed} дубликатов предметов (inventory мигрирован)")
