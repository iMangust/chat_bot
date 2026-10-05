#!/usr/bin/env python3
"""ОДНОКРАТНАЯ (легаси) миграция меток времени: UTC -> локальное камчатское.

ВНИМАНИЕ: текущая версия приложения хранит created_at/last_seen в ЛОКАЛЬНОМ
(камчатском) времени — «в базе то же, что на часах». Скрипт нужен только для
баз, созданных промежуточными сборками эпохи UTC-хранения (коммиты с
«унификация Камчатка <-> UTC»): он сдвигает метки, записанные в период
UTC-хранения, на +TZ_OFFSET_HOURS (по умолчанию 12 ч).

--direction to_local  (по умолчанию): сдвиг +hours (эпоха UTC -> локальное).
--direction to_utc    : обратный ход (-hours), если снова решите хранить UTC.

Сдвигается только всё строго раньше --cutover — момента перехода между
режимами хранения, измеренного в ЛОКАЛЬНОМ времени; более новые записи
(уже локальные) не трогаются.

Использование (из каталога bot/):
    python scripts/migrate_tz_to_utc.py --dry-run \
        --cutover "2026-10-01T00:00:00"
    python scripts/migrate_tz_to_utc.py --cutover "2026-10-01T00:00:00"
    python scripts/migrate_tz_to_utc.py --rollback --cutover "2026-10-01T00:00:00"
    # обратный режим (если снова захотите хранить UTC):
    python scripts/migrate_tz_to_utc.py --direction to_utc --cutover "..."

Требует DATABASE_URL в окружении/.env (см. app.config.get_settings).
Перед запуском сделайте резервную копию БД!
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.config import get_settings

# таблица -> список колонок с метками времени, писавшихся по-старому
TZ_COLUMNS: dict[str, list[str]] = {
    "users": ["last_active_date", "created_at", "updated_at"],
    "chat_messages_log": ["created_at"],
    "reactions_log": ["created_at"],
    "user_achievements": ["unlocked_at"],
    "pets": [
        "unlocked_at", "sleep_until", "walk_until", "sick_since",
        "last_update", "sleep_started_at", "walk_start_at", "born_at",
        "archived_at",
    ],
    "pet_actions_log": ["created_at"],
    "pet_friends": ["since"],
    "channel_subscribers": ["first_seen", "last_seen_at", "last_contact_at"],
    "notifications_queue": ["send_at", "created_at"],
    "leaderboards_snapshot": ["created_at"],
    "pet_duels": ["created_at", "updated_at"],
    "user_stats": ["updated_at"],
    "notification_settings": ["updated_at"],
    "merch_variants": ["reserved_at", "updated_at"],
    "events": ["created_at"],
}


def build_statements(hours: int, direction: int) -> list[tuple[str, str]]:
    """direction=-1 — прямой переход в UTC (-hours), +1 — откат (+hours)."""
    interval = f"{hours * direction} hours"  # direction=-1 -> "-12 hours"
    stmts: list[tuple[str, str]] = []
    for table, cols in TZ_COLUMNS.items():
        for col in cols:
            sql = (
                f"UPDATE {table} SET {col} = {col} + INTERVAL '{interval}' "
                f"WHERE {col} IS NOT NULL AND {col} < :cutover"
            )
            stmts.append((f"{table}.{col}", sql))
    return stmts


async def run_migration(url: str, stmts, cutover: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            total = 0
            for name, sql in stmts:
                res = await conn.execute(text(sql), {"cutover": cutover})
                n = res.rowcount or 0
                total += n
                print(f"{name}: {n} rows")
            print(f"Итого затронуто строк: {total}")
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description="Time-storage migration (UTC <-> local Kamchatka)")
    ap.add_argument("--cutover", required=True,
                    help="Момент перехода режима хранения меток, измеренный в "
                         "ЛОКАЛЬНОМ камчатском времени, ISO "
                         "(например '2026-10-01T00:00:00'). Метки раньше него "
                         "считаются записанными в старом режиме и сдвигаются.")
    ap.add_argument("--hours", type=int, default=None,
                    help="Сдвиг в часах (по умолчанию TZ_OFFSET_HOURS из настроек)")
    ap.add_argument("--direction", choices=["to_local", "to_utc"], default="to_local",
                    help="to_local (по умолчанию): эпоха UTC-хранения -> локальное "
                         "камчатское (+часы). to_utc: локальное -> UTC (-часы).")
    ap.add_argument("--rollback", action="store_true",
                    help="Обратный ход относительно --direction")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    hours = args.hours if args.hours is not None else get_settings().tz_offset_hours
    base = 1 if args.direction == "to_local" else -1
    direction = -base if args.rollback else base
    stmts = build_statements(hours, direction)
    tag = ("ROLLBACK-" if args.rollback else "") + args.direction

    if args.dry_run:
        print(f"DRY RUN ({tag}, shift={hours * direction:+d} h, "
              f"cutover={args.cutover!r}) — SQL:")
        for name, sql in stmts:
            print(f"-- {name}\n{sql.replace(':cutover', repr(args.cutover))};")
        return 0

    url = get_settings().database_url
    asyncio.run(run_migration(url, stmts, args.cutover))
    print("Готово. Перезапустите приложение.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
