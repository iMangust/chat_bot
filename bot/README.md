# 🐾 TamaBot — развлекательный Telegram-бот (тамагочи + достижения + топы)

Стек: **Python 3.12 · aiogram 3.x (long polling) · MySQL 8 · SQLAlchemy 2.0 async · APScheduler · loguru**.
Redis опционален (на Windows — Memurai); без него работает in-memory fallback для кулдаунов/FSM.

---

## 1. Установка на Windows Server

### 1.1 Python
1. Скачать и установить **Python 3.12.x** (галочка *Add python.exe to PATH*).
2. Проверка: `python --version`.

### 1.2 MySQL
1. Установить **MySQL Community Server 8.x** (https://dev.mysql.com/downloads/installer/),
   сервис автозапуска, порт 3306.
2. В `mysql -u root -p` выполнить:
   ```sql
   CREATE DATABASE tamabot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER 'tamabot'@'localhost' IDENTIFIED BY 'СИЛЬНЫЙ_ПАРОЛЬ';
   GRANT ALL PRIVILEGES ON tamabot.* TO 'tamabot'@'localhost';
   FLUSH PRIVILEGES;
   ```

### 1.3 Redis (опционально, но рекомендуется)
- **Memurai Developer** (бесплатно, https://memurai.dev) — ставится как сервис Windows.
- Без Redis бот запустится, но кулдауны/FSM будут сбрасываться при рестарте.

### 1.4 Зависимости проекта
```powershell
cd C:\tamabot\bot
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
> Если установка `aiomysql`/`cryptography` падает — запускать бота нужно на **Python 3.11/3.12 x64**
> (пакеты имеют готовые wheels для Windows). Драйвер `asyncmy` не требуется.

### 1.5 Конфигурация
```powershell
copy .env.example .env
notepad .env
```
Заполнить: `BOT_TOKEN`, `DATABASE_URL` (пароль!), `TRACKED_CHAT_IDS`, `ADMIN_IDS`, `IS_DEV=false`.

Таблицы создаются автоматически при первом старте (`Base.metadata.create_all`),
справочники ачивок (18 шт.) и магазина (8 товаров) сеидятся идемпотентно в `on_startup`.

---

## 2. Запуск

### Ручной запуск (для проверки)
```powershell
venv\Scripts\activate
python -m app.main
```
Ожидаем в консоли: `✅ bot started` и `starting long polling…`.

### Автостарт как сервис Windows (NSSM — рекомендовано)
1. Скачать NSSM: https://nssm.cc/download, распаковать `nssm.exe` (win64) в `C:\tamabot\`.
2. Установить сервис от администратора:
   ```powershell
   cd C:\tamabot\bot
   C:\tamabot\nssm.exe install TamaBot "C:\tamabot\bot\venv\Scripts\python.exe" "-m app.main"
   C:\tamabot\nssm.exe set TamaBot AppDirectory "C:\tamabot\bot"
   C:\tamabot\nssm.exe set TamaBot AppStdout "C:\tamabot\bot\logs\service.out.log"
   C:\tamabot\nssm.exe set TamaBot AppStderr "C:\tamabot\bot\logs\service.err.log"
   C:\tamabot\nssm.exe set TamaBot AppExit Default Restart
   C:\tamabot\nssm.exe set TamaBot AppRestartDelay 10000
   C:\tamabot\nssm.exe start TamaBot
   ```
3. Управление: `net start TamaBot` / `net stop TamaBot`, статус: `sc query TamaBot`.
   Graceful shutdown обрабатывается ботом (Ctrl-C эквивалент — остановка сервиса).

### Альтернатива без NSSM — Планировщик задач
`taskschd.msc` → создать задачу «При запуске», пользователь SYSTEM, действие:
`C:\tamabot\bot\venv\Scripts\python.exe -m app.main`, рабочая папка `C:\tamabot\bot`.

---

## 3. Настройка Telegram (чек-лист)

- [ ] @BotFather: токен в `.env`; `/setdescription`, `/setuserpic`.
- [ ] Бот — **админ** группы обсуждения с правами: *видеть сообщения*, *реакции* (нужен
      включённый в группе выбор реакций), *приглашать пользователей*.
- [ ] Группа привязана к каналу (Обсуждение → выбрать группу).
- [ ] В настройках группы включить **«Выбор реакции»** (иначе `message_reaction` не приходит).
- [ ] Проверить `TRACKED_CHAT_IDS`: ID супергруппы начинается с `-100…` (см. логи бота при первом сообщении).
- [ ] Права бота в канале — только чтение (он там не пишет).

---

## 4. Тесты

```powershell
cd C:\tamabot\bot
venv\Scripts\activate
python -m pytest tests -q        # ожидаем: 17 passed
```
Тесты идут на SQLite-in-memory, Redis/MySQL не нужны.

---

## 5. Логи и диагностика

- Файловые логи: `bot\logs\bot_YYYY-MM-DD.log` (ротация ежедневно, хранение 14 дней, уровень DEBUG).
- Консоль/сервисные: `logs\service.out.log` / `service.err.log` (если настроен NSSM).
- Частые проблемы:
  | Симптом | Причина / решение |
  |---|---|
  | `RuntimeError: BOT_TOKEN не задан` | нет `.env` или токен пуст |
  | `Can't connect to MySQL server` | MySQL не запущен / неверный `DATABASE_URL` |
  | Бот молчит в группе | не админ / `TRACKED_CHAT_IDS` не совпадает / сообщение короче `MIN_MESSAGE_LENGTH` или в кулдауне |
  | Реакции не считаются | в группе выключен «Выбор реакции» |
  | Предупреждение про Redis | Redis недоступен — ok, работает fallback; для прод-стабильности поставьте Memurai |

---

## 6. Что уже реализовано (готово к запуску)

✅ Регистрация/онбординг, приветствие новичков в ЛС (fallback — упоминание в группе)
✅ Трекинг активности с антифродом (кулдаун 10 с, мин. длина 5 симв., кап реакций, игнор команд/дублей/флуда)
✅ XP/уровни (`xp_needed = 50·L^1.5`), стрики дней
✅ 18 достижений (активность/стрики/реакции/питомец/социальные/секретные) с прогрессом и пуш-уведомлением
✅ Тамагочи: 5 видов с разными характеристиками/предпочтениями/ценами, стадии эволюции,
   оффлайн-деградация (ленивая, по `last_update`), кормление/игра/сон/мытьё/лечение/тренировки/прогулки,
   настроение и спрайты
✅ Магазин и инвентарь (8 товаров, покупка/использование), монетная экономика
✅ Статистика, экран достижений с пагинацией, топы (болтуны/стрики/питомцы)
✅ Rate-limit действий (throttle-middleware), graceful shutdown, APScheduler-задачи
✅ Тесты бизнес-логики: 17 passed

## 7. Отложено на v1.1+ (не блокирует запуск)

⏳ Мини-игры (угадай число / КНБ / реакция) — сейчас игра упрощённая
⏳ Карточка профиля картинкой (Pillow), недельные снапшоты лидербордов
⏳ Дружба питомцев, погода/сезоны, праздничные события
⏳ Alembic-миграции (пока `create_all`; для эволюции схемы добавить `alembic init`)
⏳ Экран настроек уведомлений пользователя
