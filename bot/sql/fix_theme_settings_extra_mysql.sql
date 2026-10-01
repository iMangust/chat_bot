-- ============================================================================
-- Фикс текущей базы (MySQL / MariaDB) под обновление «Темы оформления»
-- (☀️ Стандарт / 🦇 Готика).
--
-- Тема хранится в существующей JSON-колонке users.settings_extra по ключу
-- "theme" — новых таблиц/колонок НЕ требуется. Однако SQLAlchemy записывает
-- в колонку JSON значение NULL у строк, созданных до появления дефолта
-- default=dict. Бот такие строки обрабатывает (NULL трактуется как {}), но
-- для чистоты данных и корректной работы JSON-выражений этот скрипт:
--   1. гарантирует наличие колонки users.settings_extra (JSON, nullable);
--   2. приводит все NULL-значения к пустому объекту {};
--   3. показывает, как массово выдать тему всем юзерам (закомментировано).
--
-- Идемпотентен: можно запускать повторно.
--
-- Запуск (из директории bot/sql):
--   mysql -h <хост> -u <пользователь> -p <имя_базы> < fix_theme_settings_extra_mysql.sql
-- ============================================================================

-- 1. Колонка settings_extra (если база очень старая и колонки нет).
--    В MySQL нельзя "ADD COLUMN IF NOT EXISTS", поэтому используем
--    процедуру-обёртку; при существовании колонки ничего не произойдёт.
DROP PROCEDURE IF EXISTS fix_theme_add_column;
DELIMITER $$
CREATE PROCEDURE fix_theme_add_column()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'users'
          AND COLUMN_NAME  = 'settings_extra'
    ) THEN
        ALTER TABLE users ADD COLUMN settings_extra JSON NULL;
    END IF;
END$$
DELIMITER ;
CALL fix_theme_add_column();
DROP PROCEDURE IF EXISTS fix_theme_add_column;

-- 2. NULL -> {} во всех строках (безопасно: бот читает NULL как {}).
UPDATE users
SET    settings_extra = JSON_OBJECT()
WHERE  settings_extra IS NULL
   OR  CAST(settings_extra AS CHAR) = 'null';

-- 3. Диагностика: сколько юзеров с какой темой (после внедрения).
SELECT COALESCE(JSON_UNQUOTE(JSON_EXTRACT(settings_extra, '$.theme')), 'standard') AS theme,
       COUNT(*) AS users_count
FROM   users
GROUP  BY theme;

-- 4. (Опционально) принудительно назначить готическую тему всем юзерам —
--    снять комментарий, если нужен такой массовый перевод:
-- UPDATE users
-- SET settings_extra = JSON_SET(COALESCE(NULLIF(settings_extra, CAST('null' AS JSON)), JSON_OBJECT()),
--                               '$.theme', 'gothic');

-- 5. (Опционально) точечная выдача темы конкретному юзеру:
-- UPDATE users
-- SET settings_extra = JSON_SET(COALESCE(NULLIF(settings_extra, CAST('null' AS JSON)), JSON_OBJECT()),
--                               '$.theme', 'gothic')
-- WHERE user_id = 123456789;
