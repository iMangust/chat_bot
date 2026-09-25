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
   CREATE DATABASE tamabot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;  -- ВАЖНО: utf8mb4 (эмодзи)
   CREATE USER 'tamabot'@'localhost' IDENTIFIED BY 'СИЛЬНЫЙ_ПАРОЛЬ';
   GRANT ALL PRIVILEGES ON tamabot.* TO 'tamabot'@'localhost';
   FLUSH PRIVILEGES;
   ```

### 1.3 Redis (опционально, но рекомендуется)
- **Memurai Developer** (бесплатно, https://memurai.dev) — ставится как сервис Windows.
- Без Redis бот запустится, но кулдауны/FSM будут сбрасываться при рестарте.

### 1.4 Зависимости проекта

**Быстрый способ — BAT-скрипты (рекомендуется):** в корне проекта (рядом с папкой `bot\`) лежат три скрипта:

| Скрипт | Назначение |
|---|---|
| `install.bat` | Один раз: создаёт venv, ставит зависимости, копирует `.env.example → .env` и открывает его в блокноте |
| `run.bat` | Запуск бота с живыми логами в реальном времени (двойной клик → консоль с цветными логами) |
| `console.bat` | TamaConsole — полноэкранный интерфейс: вкладки Логи / Дашборд / БД / Настройки / Пользователи, кнопки Пуск/Стоп/Рестарт |

> ⚠️ BAT-файлы сохранены в кодировке **cp1251 (ANSI Russian) с CRLF** + `chcp 1251` — это обязательное
> требование cmd.exe. При редактировании сохраняйте их именно в cp1251/Windows-1251
> (Notepad++: Кодировки → Кириллица → Windows-1251), иначе вместо русских строк cmd
> начнёт «выполнять» мусор (`'ый' is not recognized...`). Корректность форматов
> контролируется тестами `tests/test_bat_files.py`. Логи Python идут в UTF-8 и читаются
> корректно: run.bat/console.bat выставляют `PYTHONUTF8=0` и `PYTHONIOENCODING=utf-8`.

**Ручной способ:**
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

Все остальные настройки имеют разумные значения по умолчанию (см. `app/config.py`):

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `MIN_MESSAGE_LENGTH` / `ACTIVITY_COOLDOWN_SEC` | 5 / 10 | антифрод активности |
| `REACTIONS_CAP_PER_DAY` | 20 | максимум засчитанных реакций A→B в сутки |
| `XP_PER_MESSAGE` / `XP_LEVEL_BASE` | 2 / 50 | экономика XP (`xp_needed = 50·L^1.5`) |
| `POLLING_TIMEOUT` / `POLLING_LIMIT` | 30 / 50 | long polling |
| `DAILY_REPORT_HOUR_UTC` | 17 (≈20 МСК) | время ежедневного отчёта |
| `EVENING_REMINDER_HOUR_UTC` | 16 (≈19 МСК) | предупреждение «стрик сгорит» |
| `PET_WARNING_MIN_HOURS` | 6 | не чаще раза в N ч «питомец скучает» |
| `INVITE_REWARD_COINS` | 50 | награда за приглашённого друга |
| `WEATHER_ENABLED` | true | сезонная деградация статов (зима/лето) |
| `TZ_OFFSET_HOURS` | 3 | смещение для «ночной совы» и праздников |

Таблицы создаются автоматически при первом старте (`Base.metadata.create_all`),
справочники ачивок (18 шт.) и магазина (8 товаров) сеидятся идемпотентно в `on_startup`.

---

## 2. Запуск

### BAT-скрипты (двойной клик — основной способ)
- `run.bat` — бот + живые цветные логи в реальном времени, uptime в заголовке окна; остановка Ctrl+C или закрытием окна.
- `console.bat` — TamaConsole: управление запуском/остановкой, логи, дашборд, просмотр БД, редактирование настроек `.env` без ручного открытия файла.

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
python -m pytest tests -q        # ожидаем: 51 passed (включая проверки формата .bat)
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
  | `Incorrect string value: '\xF0\x9F...' (1366)` | база создана в utf8mb3. С v1.2.6 бот чинит это сам при старте (авто-конвертация в utf8mb4). Вручную: `ALTER DATABASE tamabot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;` + перезапуск |
  | Логи остановились на «bot started», окно живёт, но бот молчит | штатно: long polling не пишет ничего до первого апдейта (с v1.2.7 — строка «📡 слушаю обновления» и DEBUG aiogram-ретраев). Если бот реально не отвечает: 1) нет доступа к api.telegram.org с сервера (в РФ — прокси/блокировка, проверьте `curl https://api.telegram.org/bot<TOKEN>/getMe`); 2) второй экземпляр бота уже слушает поллинг (409 Conflict — виден в DEBUG-логах, убейте старый процесс/NSSM-сервис); 3) неверный токен — с v1.2.7 старт падает сразу с явной ошибкой |
  | Бот молчит в группе | не админ / `TRACKED_CHAT_IDS` не совпадает / сообщение короче `MIN_MESSAGE_LENGTH` или в кулдауне |
  | Реакции не считаются | в группе выключен «Выбор реакции» |
  | Предупреждение про Redis | Redis недоступен — ok, работает fallback; для прод-стабильности поставьте Memurai |
  | `DataError: ex must be datetime.timedelta or int` | redis-py>=5 принимает TTL только целым числом. В v1.3.3 исправлено: все кулдауны/локи нормализуются через `_norm_ttl()` (дробные округляются вверх). Просто обновитесь. |
  | `'ConnectionPool' object has no attribute 'get'` | В v1.3.3 исправлено: FSM-хранилище строится из экземпляра Redis, а не пула; перед поллингом выполняется PING и при несовместимости — автоматический переход на MemoryStorage. |
  | `unknown command 'HELLO'` | Ваш Redis/Memurai < 6.0 не понимает RESP3. В v1.3.0 исправлено: бот принудительно использует протокол RESP2 (`protocol=2`) — просто обновитесь. Если ошибка осталась — проверьте, что pull действительно применился (`findstr /c:"protocol=2" bot\app\main.py`) |
  | Логи/текст BAT «иероглифами» | Файлы .bat повреждены при копировании (UTF-8 вместо cp1251). Перекачайте через `git clone` или ZIP-архив релиза, НЕ через copy-paste из браузера/RDP-буфера |

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
✅ Статистика, экран достижений с пагинацией
✅ Лидерборды с вкладками День/Неделя/Всё (💬 болтуны, 💖 реакции, 🔥 серии, 🐾 питомцы, 🏅 уровни)
   + еженедельный снапшот и призы топ-3 (🪙 500/250/100, пон-к 00:30 UTC, идемпотентно)
✅ Мини-игры с питомцем: угадайка (🧠 сужает подсказку), КНБ, реакция (🏃 даёт доп. время);
   победы → счётчик ачивки «Игумен»
✅ Магазин товаров для смены вида питомца (cat/dog/fox/owl/dragon)
✅ Дружба питомцев (до 5, взаимная, +1 счастье/сутки за друга) с рекомендациями «познакомиться»
✅ Карточка профиля PNG (Pillow, авто-кэш по версии статов) — кнопка «🖼 Карточка», /card
✅ Погода/сезоны: сезонная модификация деградации + праздничные события в рендере питомца
✅ Очередь уведомлений (NotificationQueue): «питомец скучает», «стрик сгорит», ежедневный отчёт
✅ ⚙️ Экран настроек уведомлений (4 тумблера) — всё учитывается планировщиком
✅ Рефералы: deep-link `t.me/bot?start=invite_<id>` → +50 🪙 и ачивка «Знакомый»
✅ Rate-limit действий (throttle-middleware), graceful shutdown, APScheduler-задачи
   (decay 30 мин, очередь уведомлений 1 мин, стрики 00:15, отчёт/вечерний хинт/топ недели)
✅ TamaConsole (Textual 8): вкладки Логи / Дашборд / БД / Настройки / Пользователи,
   кнопки Пуск/Стоп/Рестарт, live-хвост логов, редактор .env из UI (`console.bat`)
✅ BAT-оболочки install/run/console в кодировке cp1251+CRLF (тестируются в CI-тестах)
✅ Тесты бизнес-логики + форматные проверки: 51 passed

Команды бота: `/start /top /stats /achievements /shop /award /card /settings /help`

## 7. История исправлений v1.3.x (продовые фиксы Windows Server)

| Версия | Исправление |
|---|---|
| 1.3.0 | Redis: `protocol=2` (RESP2) вместо HELLO/RESP3; BAT → cp1251 (`chcp 1251`) |
| 1.3.1 | README в UTF-8, таймауты сокетов Redis, RESP2 для FSM |
| 1.3.2 | console-runtime использовал `RedisStorage.from_url` (RESP3) — переведён на общий конструктор + probe/PING с fallback на MemoryStorage |
| 1.3.3 | нормализация TTL (`_norm_ttl`: float→int), активный PING-probe до поллинга |
| 1.3.4–1.3.6 | **краш live-режима** `BotRuntime.start() missing 'self'` — метод был ошибочно помечен `@staticmethod`; убран, добавлены регресс-тесты связки livelog→runtime и AST-проверка всего пакета app |
| 1.3.7 | **краш кнопки «🖼 Карточка»**: `getset(..., ex=...)` — GETSET не принимает kwarg ex (TTL через отдельную EXPIRE) + защита от future-поломки: если redis-py>=6 уберёт `ex` из `set()`, кулдауны/локи автоматически переключатся на `setnx`+`expire` |
| 1.3.8 | **IntegrityError 1452 в еженедельном снапшоте** (`/awards`, джоб scheduler): маркер недели писался в `user_stats` с `user_id=0`, нарушая FK к `users.tg_id` — перенесён в `leaderboards_snapshot` (category=`last_week_award:<дата>`); **«Сова» срабатывала вне 3–5 ночи**: время бралось с сервера при обработке апдейта — теперь используется `message.date` (время отправки сообщения в Telegram) + смещение `TZ_OFFSET_HOURS` из `.env` |
| 1.3.9 | **Bad Request «there is no text in the message to edit»** — безопасное редактирование сообщений (`safe_edit_or_answer`: fallback на отправку нового сообщения/answer на callback); **кириллица квадратами на PNG-карточке** — системные шрифты + очистка глифов; **текст карточки вылезал за рамки** — перенос/усечение по ширине; **реакции не учитывались** — обработчик `message_reaction` (+XP автору и получателю, дневной лимит); **типы сообщений** — голосовое/кружок/фото/стикер различаются с бонусными XP; **трекинг каналов** — активность засчитывается и в каналах (автор из `sender_chat`); **реферальная система** — deep-link на канал, единовременная награда при первой активности приглашённого (+30 XP, монеты), защита от накрутки; **HTML в текстах** — экранирование (`<b>` больше не виден как текст); **магазин** — атомарное списание монет (защита от двойного клика), починка сида товаров; **🧢 Мерч канала** — отдельный раздел с категориями (футболки/худи/аксессуары), карточками, размерами и журналом заявок (`logs/merch_orders.jsonl`), витрина через `MERCH_ITEMS` в `.env`; мерч выведен из магазина питомца |
| 1.4.1 | **HTML-рендеринг приведён в порядок**: сырые теги `<b>...</b>` в топах/меню больше не видны — `safe_edit_or_answer`/`answer_cb` теперь всегда передают `parse_mode="HTML"` (aiogram не наследует дефолт `DefaultBotProperties`, если аргумент передан как `None`); явный `parse_mode="HTML"` добавлен во все прямые `message.answer`/`bot.send_message` (DM-пуши планировщика, рефералка, левелапы, «Сова», приветствие в чате, мерч, /pet, /top, /ach, /award); все динамические имена (юзеры, питомцы, usernames) экранируются через новый `app.utils.html_text.esc` — защита от поломки разметки и инъекции тегов; зачёт реакций подтверждён тестами: `message_reaction` доходит до `process_reaction` (добавление засчитывается, снятие/само-реакция — нет), кастомные emoji-реакции Premium тоже считаются; устойчивость хендлера реакций к mock-объектам (`getattr` вместо жёсткого `isinstance(update.user, User)`). Тесты: 75 passed |
| 1.4.0 | **Полный рефакторинг точки входа и консоли**: вебхук починен (`SimpleRequestAiohttpHandler` не существует в aiogram 3.x — падение с ImportError при включении webhook; теперь `SimpleRequestHandler`, секрет вебхука берётся из `WEBHOOK_SECRET_TOKEN`, а не хардкодом); краш постов от имени канала (`sender_chat` без `username`) в трекере; гарантированный graceful shutdown (polling, остановленный извне, больше не оставляет висящие scheduler/Redis/engine) + страховка `dp.errors.register` и подключение ранее неиспользуемого `ErrorNotifyMiddleware`; добавлены текстовые команды `/help` и `/pet` (были заявлены в меню Telegram, но хендлеров не было — команда «не работала»); консоль (TamaConsole) теперь регистрирует `error_router`, роутер 🧢 Мерча и полный список команд идентичный `app.main`; `.env.example` приведён в соответствие с `config.py` (вебхук, антифрод, канал, мерч); версии синхронизированы: `__version__` = README = changelog = **1.4.0** |

## 8. Отложено на v1.4+ (не блокирует запуск)

⏳ Alembic-миграции (пока `create_all` + идемпотентные сиды; при первой смене схемы — `alembic init`)
⏳ Webhook-режим (для Windows Server не нужен — polling стабилен)
⏳ Ежемесячные лиги и косметические предметы (окрас/аксессуары — поле `settings_extra` уже есть)
