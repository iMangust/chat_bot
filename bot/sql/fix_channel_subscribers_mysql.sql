-- ============================================================================
-- Исправление БД (MySQL / MariaDB) после ошибки 1064:
-- «INSERT OR IGNORE INTO channel_subscribers ...» — синтаксис SQLite,
-- MySQL его не понимает. Ошибка возникала в миграции v2.0.2 (бэкфилл
-- колонки chats из истории сообщений/реакций). В коде это уже исправлено
-- (dialect-aware upsert), этот скрипт приводит текущую базу к ожидаемой
-- схеме и восстанавливает данные.
--
-- Идемпотентен: можно запускать повторно, безопасен при отсутствии старых
-- колонок/таблиц.
--
-- Запуск (из директории bot/sql):
--   mysql -h <хост> -u <пользователь> -p <имя_базы> < fix_channel_subscribers_mysql.sql
-- ============================================================================

-- 1. Новая таблица с корректной схемой (PK = user_id, chats — JSON).
CREATE TABLE IF NOT EXISTS channel_subscribers_new (
    user_id         BIGINT       NOT NULL PRIMARY KEY,
    username        VARCHAR(64)  NULL,
    first_name      VARCHAR(128) NOT NULL DEFAULT '',
    first_seen      DATETIME(6)  NOT NULL,
    last_seen_at    DATETIME(6)  NOT NULL,
    -- LONGTEXT вместо JSON: совместимо с MySQL 5.6+/MariaDB (JSON там псевдоним TEXT);
    -- приложение читает колонку через TypeDecorator, который парсит строку как JSON.
    chats           LONGTEXT     NOT NULL,
    ever_contacted  TINYINT(1)   NOT NULL DEFAULT 0,
    last_contact_at DATETIME(6)  NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. Перенос данных из старой таблицы (если она есть и отличается по
--    составу колонок от новой). Колонки, отсутствующие в старой таблице,
--    подставляются значениями по умолчанию — запрос всегда валиден.
SET @old_exists := (SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                      AND table_name = 'channel_subscribers');

SET @has_first_name     := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'first_name');
SET @has_first_seen     := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'first_seen');
SET @has_joined_at      := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'joined_at');
SET @has_last_seen_at   := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'last_seen_at');
SET @has_last_seen      := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'last_seen');
SET @has_chats          := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'chats');
SET @has_ever_contacted := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'ever_contacted');
SET @has_last_contact   := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'channel_subscribers' AND column_name = 'last_contact_at');

SET @sql := IF(@old_exists > 0, CONCAT(
    'INSERT IGNORE INTO channel_subscribers_new ',
    '(user_id, username, first_name, first_seen, last_seen_at, chats, ever_contacted, last_contact_at) ',
    'SELECT o.user_id, MAX(o.username), ',
    IF(@has_first_name > 0, "MAX(o.first_name)", "''"), ', ',
    IF(@has_first_seen > 0, 'MIN(o.first_seen)',
       IF(@has_joined_at > 0, 'MIN(o.joined_at)', 'NOW(6)')), ', ',
    IF(@has_last_seen_at > 0, 'MAX(o.last_seen_at)',
       IF(@has_last_seen > 0, 'MAX(o.last_seen)', 'NOW(6)')), ', ',
    -- если chats нет или она пустая — список чатов соберём на шаге 3
    IF(@has_chats > 0, "COALESCE(NULLIF(MAX(o.chats), ''), '[]')",
       "'[]'" ), ', ',
    IF(@has_ever_contacted > 0, 'COALESCE(CAST(MAX(o.ever_contacted) AS UNSIGNED), 0)', '0'), ', ',
    IF(@has_last_contact > 0, 'MAX(o.last_contact_at)', 'NULL'),
    ' FROM channel_subscribers o GROUP BY o.user_id'),
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3. Восстановление chats из истории сообщений и реакций (аналог бэкфилла
--    v2.0.2). Пропускается, если таблиц истории нет — тогда оставляем '[]'.
SET @has_history := (
    (SELECT COUNT(*) FROM information_schema.tables
      WHERE table_schema = DATABASE() AND table_name = 'chat_messages_log') +
    (SELECT COUNT(*) FROM information_schema.tables
      WHERE table_schema = DATABASE() AND table_name = 'reactions_log'));

-- 3a. Строкам, у которых chats пустой, проставляем чаты из истории.
SET @sql := IF(@has_history > 0, "
    UPDATE channel_subscribers_new cs
    JOIN (
        SELECT user_id, JSON_ARRAYAGG(chat_id) AS chats FROM (
            SELECT DISTINCT user_id, chat_id FROM chat_messages_log
            UNION
            SELECT DISTINCT from_user AS user_id, chat_id FROM reactions_log
        ) t
        GROUP BY user_id
    ) h ON h.user_id = cs.user_id
    SET cs.chats = h.chats
    WHERE cs.chats IS NULL OR cs.chats = '' OR cs.chats = '[]'",
    "DO 0");
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3b. Подписчики, найденные только в истории, но отсутствующие в реестре.
SET @sql := IF(@has_history > 0, "
    INSERT IGNORE INTO channel_subscribers_new
        (user_id, username, first_name, first_seen, last_seen_at, chats,
         ever_contacted, last_contact_at)
    SELECT h.user_id, NULL, '', NOW(6), NOW(6), h.chats, 0, NULL
    FROM (
        SELECT user_id, JSON_ARRAYAGG(chat_id) AS chats FROM (
            SELECT DISTINCT user_id, chat_id FROM chat_messages_log
            UNION
            SELECT DISTINCT from_user AS user_id, chat_id FROM reactions_log
        ) t
        GROUP BY user_id
    ) h
    LEFT JOIN channel_subscribers_new cs ON cs.user_id = h.user_id
    WHERE cs.user_id IS NULL",
    "DO 0");
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 4. Замена старой таблицы новой.
DROP TABLE IF EXISTS channel_subscribers;
ALTER TABLE channel_subscribers_new RENAME TO channel_subscribers;

-- 5. Индекс по first_seen (MariaDB 10.1.4+/MySQL 8.0.29+ не имеют
--    CREATE INDEX IF NOT EXISTS — проверяем через information_schema).
SET @idx_exists := (SELECT COUNT(*) FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = 'channel_subscribers'
                      AND index_name = 'ix_channel_subscribers_first_seen');
SET @sql := IF(@idx_exists = 0,
    'CREATE INDEX ix_channel_subscribers_first_seen ON channel_subscribers (first_seen)',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 6. Проверка результата.
SELECT COUNT(*) AS total_subscribers,
       SUM(CASE WHEN chats IS NULL OR chats IN ('', '[]')
                THEN 1 ELSE 0 END) AS empty_chats
FROM channel_subscribers;
