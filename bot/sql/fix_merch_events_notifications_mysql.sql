-- ============================================================================
-- Исправление БД (MySQL / MariaDB) под текущую версию бота.
--
-- Покрывает изменения схемы, появившиеся в обновлениях:
--   1. МЕРЧ / БРОНИ: «снимок» покупателя на момент брони — колонки
--      merch_variants.buyer_name и merch_variants.buyer_username.
--      Нужны для кликабельного уведомления «🛒 НОВАЯ БРОНЬ МЕРЧА»
--      (имя + @username покупателя, ссылка «Написать покупателю»).
--      Таблица merch_variants создаётся автоматически (create_all), но
--      существующие строки в старых базах этих колонок не имеют.
--   2. УВЕДОМЛЕНИЯ: notifications_queue.payload_json (TEXT) — сериализованная
--      inline-клавиатура уведомления. Без неё дневная сводка «🌅 Твой день»
--      отправлялась без обещанной кнопки «Продолжить день».
--   3. МЕРОПРИЯТИЯ: таблица events (если старой базы нет вовсе) и колонка
--      events.image_url VARCHAR(512) — афиша; теперь туда пишется не только
--      URL картинки, но и Telegram file_id загруженного фото (как в мерче).
--
-- Приложение при старте выполняет эти же правки автоматически
-- (_LIGHT_COLUMNS в app/main.py), поэтому скрипт нужен, если вы хотите
-- подготовить/починить базу заранее вручную или у БД-пользователя бота нет
-- прав ALTER/CREATE.
--
-- Идемпотентен: можно запускать повторно — лишние колонки/таблицы не
-- создаются, ошибки не будет.
--
-- Запуск (из директории bot/sql):
--   mysql -h <хост> -u <пользователь> -p <имя_базы> < fix_merch_events_notifications_mysql.sql
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 0. Процедура-обёртка «добавить колонку, если её ещё нет» (в MySQL нет
--    ADD COLUMN IF NOT EXISTS).
-- ---------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS fix_add_column;
DELIMITER $$
CREATE PROCEDURE fix_add_column(
    IN p_table  VARCHAR(64),
    IN p_column VARCHAR(64),
    IN p_ddl    VARCHAR(512)
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name   = p_table
          AND column_name  = p_column
    ) AND EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name   = p_table
    ) THEN
        SET @ddl := CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `', p_column, '` ', p_ddl);
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- 1. merch_variants: снимок покупателя для уведомлений о брони.
-- ---------------------------------------------------------------------------
CALL fix_add_column('merch_variants', 'buyer_name',     "VARCHAR(128) NOT NULL DEFAULT ''");
CALL fix_add_column('merch_variants', 'buyer_username', "VARCHAR(64) NULL");

-- Подстраховка: если фото товара хранится в photo_file_id, а колонки нет
-- (совсем старые базы) — добавим и её (в модели она объявлена).
CALL fix_add_column('merch_variants', 'photo_file_id',  "VARCHAR(256) NULL");

-- ---------------------------------------------------------------------------
-- 2. notifications_queue: клавиатура уведомления («Продолжить день»).
-- ---------------------------------------------------------------------------
CALL fix_add_column('notifications_queue', 'payload_json', "TEXT NULL");

-- ---------------------------------------------------------------------------
-- 3. events: таблица целиком (для очень старых баз) + image_url под афишу.
--    DDL полностью совпадает с моделью Event (app/db/models.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
    title       VARCHAR(128)  NOT NULL,
    date        VARCHAR(10)   NOT NULL DEFAULT '',
    time        VARCHAR(16)   NOT NULL DEFAULT '',
    place       VARCHAR(256)  NOT NULL DEFAULT '',
    meet        VARCHAR(256)  NOT NULL DEFAULT '',
    description TEXT          NOT NULL,
    image_url   VARCHAR(512)  NULL,
    url         VARCHAR(512)  NULL,
    icon        VARCHAR(16)   NOT NULL DEFAULT '🎪',
    going       JSON          NULL,
    created_at  DATETIME(6)   NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CALL fix_add_column('events', 'image_url', "VARCHAR(512) NULL");

-- Примечание: meet («Сбор») теперь опционален на уровне приложения
-- (пустая строка = шаг пропущен); отдельной миграции не требует.

DROP PROCEDURE IF EXISTS fix_add_column;

-- ---------------------------------------------------------------------------
-- Проверка результата:
--   SHOW COLUMNS FROM merch_variants LIKE 'buyer%';
--   SHOW COLUMNS FROM notifications_queue LIKE 'payload_json';
--   SHOW COLUMNS FROM events LIKE 'image_url';
-- ---------------------------------------------------------------------------
