# 🐾 TamaBot v1.4.9 — развлекательный Telegram-бот (тамагочи + достижения + топы)

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-green.svg)](https://docs.aiogram.dev/)
[![MySQL](https://img.shields.io/badge/MySQL-8+-4479A1.svg)](https://www.mysql.com/)
[![Textual](https://img.shields.io/badge/TamaConsole-Textual%208-orange.svg)](https://textual.textualize.io/)

Многофункциональный развлекательный Telegram-бот с системой достижений, геймификацией
и полноценным виртуальным питомцем (тамагочи). Работает в связке **«Канал + привязанная
группа обсуждения»**, всё взаимодействие — через **личные сообщения** с интерактивными
кнопками. Запуск и управление — на **Windows Server** через BAT-скрипты и встроенную
консоль управления **TamaConsole**.

> 📂 **Основной код проекта — в папке [`bot/`](bot/).**
> Полная документация по установке, настройке и эксплуатации: **[bot/README.md](bot/README.md)**.
> Этот файл — быстрый старт.

---

## 🚀 Быстрый старт (Windows Server)

```bat
:: 1. Установить Python 3.12 x64 (галочка "Add to PATH") и MySQL 8 Community
:: 2. Распаковать проект, например в C:\tamabot\
cd C:\tamabot

install.bat    :: 3. Один раз: venv + зависимости + создание .env (откроется блокнот)
               ::    заполнить BOT_TOKEN, DATABASE_URL, TRACKED_CHAT_IDS, ADMIN_IDS
run.bat        :: 4. Запуск бота с живыми цветными логами в реальном времени
console.bat    :: 5. (альтернатива) TamaConsole — полноэкранное управление:
               ::    вкладки Логи / Дашборд / БД / Настройки / Пользователи,
               ::    кнопки Пуск / Стоп / Рестарт, редактирование .env из UI
```

⚠️ **Важно:** `install.bat`, `run.bat`, `console.bat` сохранены в кодировке
**cp1251 (ANSI Russian) с CRLF** + переключение `chcp 1251` — обязательное требование `cmd.exe`. При редактировании
сохраняйте именно в cp1251/«Кириллица Windows» (Notepad++ → Кодировки → Кириллица → Windows-1251), иначе cmd
начнёт «выполнять» русские строки (`'ый' is not recognized...`). Корректность формата
контролируется тестами `bot/tests/test_bat_files.py`. Логи Python пишутся в UTF-8 и
корректно отображаются в окне chcp 1251 (переменные `PYTHONUTF8=0`, `PYTHONIOENCODING=utf-8`
выставляются в run.bat/console.bat).

---

## 🧩 Возможности (реализовано, v1.0–v1.4)

| Блок | Что внутри |
|---|---|
| 👋 Приветствие | Онбординг в ЛС при `new_chat_members`, fallback-упоминание в группе, выбор имени/вида питомца |
| 📊 Активность | Сообщения (день/неделя/всё), реакции, streak, replies/упоминания, медиа; антифрод: кулдаун 10 с, мин. длина 5 симв., кап реакций/сутки |
| 🎯 XP/уровни | `xp_needed = 50·L^1.5`, награды за активность и действия питомца |
| 🏆 Достижения | 18 ачивок (активность / стрики / реакции / тамагочи / социальные / секретные), прогресс-бары ▰▰▱, пуш при разблокировке |
| 🐣 Тамагочи | 5 видов с разными характеристиками, предпочтениями и ценами; стадии яйцо→легендарный; оффлайн-деградация (ленивая); кормление/сон/мытьё/лечение/тренировки/прогулки с рандом-ивентами; настроение; сезонная погода и праздники |
| 🎮 Мини-игры | Угадайка (🧠 сужает подсказку), КНБ (🏃 даёт доп. время), реакция; победы → XP/монеты/ачивки |
| 🛒 Магазин | 8 товаров (еда/игрушки/лекарства + смена вида питомца), инвентарь, монетная экономика |
| 📈 Топы | Вкладки День/Неделя/Всё: болтуны, реакции, стрики, питомцы, уровни; еженедельный снапшот + призы топ-3 (🪙 500/250/100) |
| 🖼 Профиль | Карточка PNG через Pillow (авто-кэш), кнопка «🖼 Карточка» и /card |
| 🤝 Социальное | Дружба питомцев (до 5, +счастье), рекомендации, рефералы `t.me/bot?start=invite_<id>` (+50 🪙 и ачивка) |
| 🔔 Уведомления | Очередь пушей: «питомец скучает», «стрик сгорит», ежедневный отчёт; экран ⚙️ настроек (4 тумблера) |
| 🛡 Инфраструктура | Long polling, rate-limit middleware, graceful shutdown, APScheduler (decay/очередь/стрики/отчёты/топ недели), loguru с ротацией, опциональный Redis/Memurai с in-memory fallback |

Команды: `/start /top /stats /achievements /shop /award /card /settings /help` (дублируют кнопки).

---

## 🗂 Структура репозитория

```
├── install.bat / run.bat / console.bat   # BAT-оболочки запуска (cp1251+CRLF!)
├── README.md                             # этот файл (быстрый старт)
└── bot/
    ├── app/
    │   ├── main.py                       # точка входа, long polling, on_startup-сиды
    │   ├── config.py                     # pydantic-settings (.env), __version__
    │   ├── handlers/                     # start, welcome, tamagotchi, games, shop,
    │   │                                 # achievements, stats, top, profile, settings, admin
    │   ├── services/                     # activity, xp, achievements, pet, economy,
    │   │                                 # leaderboard, friends, notifications, card (Pillow)
    │   ├── db/                           # models.py (SQLAlchemy 2.1 async) + repositories/
    │   ├── keyboards/                    # inline/reply-клавиатуры
    │   ├── middlewares/                  # throttle, user, session
    │   ├── console/                      # TamaConsole (Textual): логи/дашборд/БД/настройки
    │   └── tasks/scheduler.py            # APScheduler-джобы
    ├── tests/                            # pytest: 48 passed (SQLite in-memory)
    ├── requirements.txt / pytest.ini
    └── README.md                         # ПОЛНАЯ документация (установка, NSSM, чек-лист Telegram, диагностика)
```

---

## 🧪 Тесты

```powershell
cd bot
venv\Scripts\activate
python -m pytest tests -q      # ожидаем: 48 passed
```

Тесты не требуют MySQL/Redis (SQLite in-memory) и включают проверки формата BAT-файлов.

---

## 📌 Требования окружения

| Компонент | Версия | Комментарий |
|---|---|---|
| Windows Server | 2016+ | запуск под обычным пользователем или сервисом |
| Python | 3.11+ x64 (проверено на 3.13) | готовые wheels для aiomysql/cryptography |
| MySQL Community | 8.x | БД `tamabot`, utf8mb4 |
| Memurai / Redis | опционально | кулдауны/FSM переживают рестарт |
| NSSM | опционально | автостарт как сервис Windows |

---

## 🔜 План v1.4+

⏳ Alembic-миграции (сейчас `create_all` + идемпотентные сиды)
⏳ Ежемесячные лиги и косметика (окрас/аксессуары; поле `settings_extra` уже в схеме)
✅ Webhook-режим починен в 1.4.0 (SimpleRequestHandler + секрет из .env; для Windows по-прежнему не требуется — polling стабилен)

---

## 📄 Лицензия

MIT. Полная инструкция по эксплуатации, чек-лист настройки Telegram и таблица
диагностики частых проблем — в [bot/README.md](bot/README.md).
