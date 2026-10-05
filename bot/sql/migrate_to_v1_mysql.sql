-- ============================================================================
-- Миграция существующей MySQL/MariaDB БД бота до актуальной схемы (v1.0.0)
-- ----------------------------------------------------------------------------
-- Что делает:
--   1. Создаёт отсутствующие таблицы полной актуальной схемы (IF NOT EXISTS —
--      существующие таблицы и данные НЕ трогает).
--   2. Добавляет недостающие колонки в старые таблицы (pets, users,
--      merch_variants, notifications_queue) — тоже идемпотентно.
--   3. Помечает БД как актуальную для Alembic (таблица alembic_version),
--      чтобы при следующем запуске бота миграции не попытались пересоздать
--      то, что уже есть.
--
-- Свойства: идемпотентен (можно запускать повторно), безопасен для живых
-- данных, только DDL без DELETE/UPDATE строк.
--
-- Запуск (Windows, из папки bot\sql):
--   mysql -h <хост> -u <пользователь> -p <имя_базы> < migrate_to_v1_mysql.sql
-- или через любой SQL-клиент (HeidiSQL, Workbench, Navicat): открыть файл и
-- выполнить целиком.
--
-- ПЕРЕ ЗАПУСКОМ СДЕЛАЙТЕ БЭКАП:
--   mysqldump -h <хост> -u <пользователь> -p <имя_базы> > backup_before_migration.sql
-- ============================================================================

SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ==== table: users ====
CREATE TABLE IF NOT EXISTS users (
	tg_id BIGINT NOT NULL, 
	username VARCHAR(64), 
	first_name VARCHAR(128) NOT NULL, 
	last_name VARCHAR(128), 
	lang VARCHAR(8) NOT NULL, 
	level INTEGER NOT NULL, 
	xp INTEGER NOT NULL, 
	coins INTEGER NOT NULL, 
	streak_days INTEGER NOT NULL, 
	best_streak INTEGER NOT NULL, 
	last_active_date DATETIME, 
	onboarded BOOL NOT NULL, 
	welcome_shown BOOL NOT NULL, 
	pet_name VARCHAR(64), 
	is_banned BOOL NOT NULL, 
	referrer_id BIGINT, 
	settings_extra JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	messages_count INTEGER NOT NULL, 
	reactions_given INTEGER NOT NULL, 
	reactions_received INTEGER NOT NULL, 
	PRIMARY KEY (tg_id)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: pets ====
CREATE TABLE IF NOT EXISTS pets (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id BIGINT NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	species VARCHAR(10) NOT NULL, 
	level INTEGER NOT NULL, 
	xp INTEGER NOT NULL, 
	stage VARCHAR(9) NOT NULL, 
	hunger FLOAT NOT NULL, 
	happiness FLOAT NOT NULL, 
	energy FLOAT NOT NULL, 
	hygiene FLOAT NOT NULL, 
	health FLOAT NOT NULL, 
	strength INTEGER NOT NULL, 
	agility INTEGER NOT NULL, 
	intellect INTEGER NOT NULL, 
	is_sleeping BOOL NOT NULL, 
	sleep_until DATETIME, 
	walk_until DATETIME, 
	sick_since DATETIME, 
	last_update DATETIME NOT NULL, 
	sleep_started_at DATETIME, 
	walk_start_at DATETIME, 
	born_at DATETIME NOT NULL, 
	settings_extra JSON NOT NULL, 
	generation INTEGER NOT NULL, 
	is_archived BOOL NOT NULL, 
	archived_at DATETIME, 
	archive_reason VARCHAR(32), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (tg_id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: achievements ====
CREATE TABLE IF NOT EXISTS achievements (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(64) NOT NULL, 
	title VARCHAR(128) NOT NULL, 
	description TEXT NOT NULL, 
	icon VARCHAR(16) NOT NULL, 
	category VARCHAR(9) NOT NULL, 
	condition_type VARCHAR(18) NOT NULL, 
	condition_value INTEGER NOT NULL, 
	reward_xp INTEGER NOT NULL, 
	reward_coins INTEGER NOT NULL, 
	is_hidden BOOL NOT NULL, 
	rarity VARCHAR(9) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: user_achievements ====
CREATE TABLE IF NOT EXISTS user_achievements (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id BIGINT NOT NULL, 
	achievement_id INTEGER NOT NULL, 
	progress INTEGER NOT NULL, 
	unlocked_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_ach UNIQUE (user_id, achievement_id), 
	FOREIGN KEY(user_id) REFERENCES users (tg_id) ON DELETE CASCADE, 
	FOREIGN KEY(achievement_id) REFERENCES achievements (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: items ====
CREATE TABLE IF NOT EXISTS items (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	icon VARCHAR(16) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	price INTEGER NOT NULL, 
	effect JSON NOT NULL, 
	description TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: pet_inventory ====
CREATE TABLE IF NOT EXISTS pet_inventory (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	pet_id INTEGER NOT NULL, 
	item_id INTEGER NOT NULL, 
	quantity INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pet_item UNIQUE (pet_id, item_id), 
	FOREIGN KEY(pet_id) REFERENCES pets (id) ON DELETE CASCADE, 
	FOREIGN KEY(item_id) REFERENCES items (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: chat_settings ====
CREATE TABLE IF NOT EXISTS chat_settings (
	chat_id BIGINT NOT NULL, 
	cooldown_sec INTEGER NOT NULL, 
	min_length INTEGER NOT NULL, 
	config JSON NOT NULL, 
	PRIMARY KEY (chat_id)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: notifications_queue ====
CREATE TABLE IF NOT EXISTS notifications_queue (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id BIGINT NOT NULL, 
	text TEXT NOT NULL, 
	send_at DATETIME NOT NULL, 
	sent BOOL NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	payload_json TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (tg_id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: notification_settings ====
CREATE TABLE IF NOT EXISTS notification_settings (
	user_id BIGINT NOT NULL, 
	pet_reminders BOOL NOT NULL, 
	streak_reminders BOOL NOT NULL, 
	achievement_notifications BOOL NOT NULL, 
	daily_report BOOL NOT NULL, 
	PRIMARY KEY (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (tg_id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: channel_subscribers ====
CREATE TABLE IF NOT EXISTS channel_subscribers (
	user_id BIGINT NOT NULL, 
	username VARCHAR(64), 
	first_name VARCHAR(128) NOT NULL, 
	first_seen DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	chats JSON NOT NULL, 
	ever_contacted BOOL NOT NULL, 
	last_contact_at DATETIME, 
	PRIMARY KEY (user_id)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

CREATE INDEX ix_channel_subscribers_first_seen ON channel_subscribers (first_seen);

-- ==== table: leaderboards_snapshot ====
CREATE TABLE IF NOT EXISTS leaderboards_snapshot (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	period VARCHAR(16) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	data JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: merch_categories ====
CREATE TABLE IF NOT EXISTS merch_categories (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(32) NOT NULL, 
	title VARCHAR(64) NOT NULL, 
	icon VARCHAR(16) NOT NULL, 
	position INTEGER NOT NULL, 
	PRIMARY KEY (id)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: merch_products ====
CREATE TABLE IF NOT EXISTS merch_products (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	category_id INTEGER NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	description TEXT NOT NULL, 
	image_url VARCHAR(512), 
	sizes JSON, 
	colors JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES merch_categories (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: merch_variants ====
CREATE TABLE IF NOT EXISTS merch_variants (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	product_id INTEGER NOT NULL, 
	size VARCHAR(16) NOT NULL, 
	color VARCHAR(32) NOT NULL, 
	price_rub INTEGER NOT NULL, 
	stock INTEGER NOT NULL, 
	photo_file_id VARCHAR(256), 
	reserved_by BIGINT, 
	reserved_at DATETIME, 
	buyer_name VARCHAR(128) NOT NULL, 
	buyer_username VARCHAR(64), 
	sold_count INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES merch_products (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: events ====
CREATE TABLE IF NOT EXISTS events (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	title VARCHAR(128) NOT NULL, 
	date VARCHAR(10) NOT NULL, 
	time VARCHAR(16) NOT NULL, 
	place VARCHAR(256) NOT NULL, 
	meet VARCHAR(256) NOT NULL, 
	description TEXT NOT NULL, 
	image_url VARCHAR(512), 
	url VARCHAR(512), 
	icon VARCHAR(16) NOT NULL, 
	going JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: chat_messages_log ====
CREATE TABLE IF NOT EXISTS chat_messages_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id BIGINT NOT NULL, 
	chat_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	length INTEGER NOT NULL, 
	has_media BOOL NOT NULL, 
	media_type VARCHAR(32), 
	is_reply BOOL NOT NULL, 
	mentions_count INTEGER NOT NULL, 
	is_counted BOOL NOT NULL, 
	skip_reason VARCHAR(32), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_cml_chat_msg UNIQUE (chat_id, message_id), 
	FOREIGN KEY(user_id) REFERENCES users (tg_id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

CREATE INDEX ix_cml_chat_created ON chat_messages_log (chat_id, created_at);

CREATE INDEX ix_cml_user_created ON chat_messages_log (user_id, created_at);

-- ==== table: reactions_log ====
CREATE TABLE IF NOT EXISTS reactions_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	from_user BIGINT NOT NULL, 
	to_user BIGINT NOT NULL, 
	chat_id BIGINT NOT NULL, 
	message_id BIGINT NOT NULL, 
	emoji VARCHAR(16) NOT NULL, 
	is_counted BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_reaction_once UNIQUE (from_user, message_id, emoji), 
	FOREIGN KEY(from_user) REFERENCES users (tg_id) ON DELETE CASCADE, 
	FOREIGN KEY(to_user) REFERENCES users (tg_id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

CREATE INDEX ix_rl_to_created ON reactions_log (to_user, created_at);

-- ==== table: pet_actions_log ====
CREATE TABLE IF NOT EXISTS pet_actions_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	pet_id INTEGER NOT NULL, 
	action VARCHAR(32) NOT NULL, 
	value INTEGER NOT NULL, 
	meta JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(pet_id) REFERENCES pets (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

CREATE INDEX ix_pal_pet_created ON pet_actions_log (pet_id, created_at);

-- ==== table: pet_friends ====
CREATE TABLE IF NOT EXISTS pet_friends (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	pet_id INTEGER NOT NULL, 
	friend_pet_id INTEGER NOT NULL, 
	since DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pet_friend UNIQUE (pet_id, friend_pet_id), 
	FOREIGN KEY(pet_id) REFERENCES pets (id) ON DELETE CASCADE, 
	FOREIGN KEY(friend_pet_id) REFERENCES pets (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== table: pet_duels ====
CREATE TABLE IF NOT EXISTS pet_duels (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	pet_id INTEGER NOT NULL, 
	week_key VARCHAR(12) NOT NULL, 
	wins INTEGER NOT NULL, 
	losses INTEGER NOT NULL, 
	score INTEGER NOT NULL, 
	fights INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pet_duel_week UNIQUE (pet_id, week_key), 
	FOREIGN KEY(pet_id) REFERENCES pets (id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

CREATE INDEX ix_pd_week_score ON pet_duels (week_key, score);

-- ==== table: user_stats ====
CREATE TABLE IF NOT EXISTS user_stats (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id BIGINT NOT NULL, 
	`key` VARCHAR(32) NOT NULL, 
	value INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_stat UNIQUE (user_id, `key`), 
	FOREIGN KEY(user_id) REFERENCES users (tg_id) ON DELETE CASCADE
)
CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci ENGINE=InnoDB;

-- ==== conditional ADD COLUMN for legacy tables (idempotent) ====
DROP PROCEDURE IF EXISTS _migrate_add_columns;
DELIMITER //
CREATE PROCEDURE _migrate_add_columns()
BEGIN
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'pets' AND column_name = 'is_archived';
    IF @c = 0 THEN
        ALTER TABLE `pets` ADD COLUMN `is_archived` TINYINT(1) NOT NULL DEFAULT 0;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'pets' AND column_name = 'generation';
    IF @c = 0 THEN
        ALTER TABLE `pets` ADD COLUMN `generation` INTEGER NOT NULL DEFAULT 1;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'pets' AND column_name = 'archived_at';
    IF @c = 0 THEN
        ALTER TABLE `pets` ADD COLUMN `archived_at` DATETIME NULL;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'pets' AND column_name = 'archive_reason';
    IF @c = 0 THEN
        ALTER TABLE `pets` ADD COLUMN `archive_reason` VARCHAR(32) NULL;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'pets' AND column_name = 'sleep_started_at';
    IF @c = 0 THEN
        ALTER TABLE `pets` ADD COLUMN `sleep_started_at` DATETIME NULL;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'pets' AND column_name = 'walk_start_at';
    IF @c = 0 THEN
        ALTER TABLE `pets` ADD COLUMN `walk_start_at` DATETIME NULL;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'merch_variants' AND column_name = 'photo_file_id';
    IF @c = 0 THEN
        ALTER TABLE `merch_variants` ADD COLUMN `photo_file_id` VARCHAR(256) NULL;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'merch_variants' AND column_name = 'buyer_name';
    IF @c = 0 THEN
        ALTER TABLE `merch_variants` ADD COLUMN `buyer_name` VARCHAR(128) NOT NULL DEFAULT '';
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'merch_variants' AND column_name = 'buyer_username';
    IF @c = 0 THEN
        ALTER TABLE `merch_variants` ADD COLUMN `buyer_username` VARCHAR(64) NULL;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'welcome_shown';
    IF @c = 0 THEN
        ALTER TABLE `users` ADD COLUMN `welcome_shown` TINYINT(1) NOT NULL DEFAULT 0;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'created_at';
    IF @c = 0 THEN
        ALTER TABLE `users` ADD COLUMN `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'updated_at';
    IF @c = 0 THEN
        ALTER TABLE `users` ADD COLUMN `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'messages_count';
    IF @c = 0 THEN
        ALTER TABLE `users` ADD COLUMN `messages_count` INTEGER NOT NULL DEFAULT 0;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'reactions_given';
    IF @c = 0 THEN
        ALTER TABLE `users` ADD COLUMN `reactions_given` INTEGER NOT NULL DEFAULT 0;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'reactions_received';
    IF @c = 0 THEN
        ALTER TABLE `users` ADD COLUMN `reactions_received` INTEGER NOT NULL DEFAULT 0;
    END IF;
    SELECT COUNT(*) INTO @c FROM information_schema.columns
      WHERE table_schema = DATABASE() AND table_name = 'notifications_queue' AND column_name = 'payload_json';
    IF @c = 0 THEN
        ALTER TABLE `notifications_queue` ADD COLUMN `payload_json` TEXT NULL;
    END IF;
END//
DELIMITER ;
CALL _migrate_add_columns();
DROP PROCEDURE _migrate_add_columns;

-- ==== alembic stamp head =========================================
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT IGNORE INTO alembic_version (version_num) VALUES ('8b8860099c46');

-- Готово. Проверка: SELECT * FROM alembic_version; -> 8b8860099c46
