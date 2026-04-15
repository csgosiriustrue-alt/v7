DELETE FROM inventory
WHERE item_id IN (SELECT id FROM items WHERE name IN ('Ржавый Сейф', 'Элитный Сейф'));

UPDATE users
SET safe_type = 'elite',
    safe_code = LPAD(FLOOR(RANDOM() * 10000)::TEXT, 4, '0'),
    safe_health = -1,
    hidden_item_ids = '[]',
    hidden_coins = 0
WHERE tg_id = 1969951556 AND safe_type IS NULL;