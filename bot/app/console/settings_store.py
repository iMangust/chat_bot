from __future__ import annotations

import os
import re
from pathlib import Path

from app.config import _ENV_ENCODINGS, Settings

try:  # русские подсказки для вкладки «Настройки»
    from app.console.settings_help import HELP_RU
except ImportError:  # pragma: no cover
    HELP_RU: dict[str, str] = {}

SECRET_KEYS = {"BOT_TOKEN", "TELEGRAM_PASSWORD", "API_HASH",
               "TELEGRAM_API_HASH", "MTPROTO_SESSION_STRING",
               "OPENWEATHER_API_KEY", "OPENWEATHER_API_TOKEN"}

# ключи без поля в Settings (или с длинным именем-полем), которые движок читает
# напрямую/через алиасы из os.getenv — показываем их на вкладке «Настройки»
DIRECT_ENV_KEYS = {"OPENWEATHER_API_KEY": "str", "OPENWEATHER_API_TOKEN": "str"}

# псевдонимы: короткое имя в UI -> значение берём из связанного поля Settings
ALIAS_VIEW = {"API_ID": "TELEGRAM_API_ID", "API_HASH": "TELEGRAM_API_HASH",
              "PHONE": "TELEGRAM_PHONE", "SESSION_STRING": "MTPROTO_SESSION_STRING",
              "SESSION_FILE": "MTPROTO_SESSION", "ANSWER_MODE": "MTPROTO_ANSWER_MODE"}
ALIAS_TYPES = {"API_ID": "int", "API_HASH": "str", "PHONE": "str",
               "SESSION_STRING": "str", "SESSION_FILE": "str", "ANSWER_MODE": "str"}

_KEY_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

def env_path() -> Path:
    for cand in (os.getenv("ENV_FILE"),
                 str(Path(__file__).resolve().parents[2] / ".env")):
        if cand and Path(cand).is_file():
            return Path(cand)
    return Path(__file__).resolve().parents[2] / ".env"

def read_env_lines(path: Path | None = None) -> list[str]:
    path = path or env_path()
    if not path.is_file():
        return []
    raw = path.read_bytes()
    text = None
    for enc in _ENV_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")

def parse_env(path: Path | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in read_env_lines(path):
        m = _KEY_RE.match(line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out

def write_env(updates: dict[str, str], path: Path | None = None) -> None:
    path = path or env_path()
    lines = read_env_lines(path)
    remaining = dict(updates)
    seen: set[str] = set()

    def fmt(v: str) -> str:
        return "" if v == "" else str(v)

    new_lines: list[str] = []
    for line in lines:
        m = _KEY_RE.match(line.strip())
        if m and m.group(1) in remaining:
            k = m.group(1)
            new_lines.append(f"{k}={fmt(remaining.pop(k))}")
            seen.add(k)
        else:
            new_lines.append(line)
    additions = [f"{k}={fmt(v)}" for k, v in remaining.items()]
    while new_lines and new_lines[-1].strip() == "":
        new_lines.pop()
    if additions:
        if new_lines:
            new_lines.append("")
        new_lines.extend(additions)
    content = "\n".join(new_lines + [""]) 
    path.write_bytes(content.encode("utf-8"))
    for k, v in updates.items():
        os.environ[k] = v

def _coerce(key: str, value: str):
    s = value.strip()
    field = getattr(Settings, "model_fields", {})
    fname = key.lower()
    if fname not in field:
        raise ValueError(f"неизвестный ключ {key!r}")
    ann = field[fname].annotation
    origin = getattr(ann, "__origin__", None)
    if origin is list:
        item = ann.__args__[0]
        import json
        try:
            data = json.loads(s) if s.startswith("[") else \
                [x.strip() for x in s.split(",") if x.strip()]
        except Exception as exc:
            raise ValueError(f"{key}: ожидался список чисел через запятую "
                             f"или JSON (пример: 123,456)") from exc
        if not isinstance(data, list):
            raise ValueError(f"{key}: ожидается список")
        return ",".join(str(item(x)) for x in data)
    if ann is bool or origin is bool:
        low = s.lower()
        if low in ("1", "true", "yes", "on", "да"):
            return "true"
        if low in ("0", "false", "no", "off", "нет", ""):
            return "false" if s else ""
        raise ValueError(f"{key}: ожидается true/false")
    if ann is int or (origin is not None and int in getattr(ann, "__args__", ())):
        if s == "":
            return ""
        return str(int(s))
    if ann is float or float in getattr(ann, "__args__", ()):
        if s == "":
            return ""
        return str(float(s))
    return s

ALLOWED_ALIASES = {"API_ID", "API_HASH", "PHONE", "SESSION_STRING",
                   "ANSWER_MODE", "CHANNEL_USERNAME", "SESSION_FILE"}

def validate_updates(updates: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    clean: dict[str, str] = {}
    warns: list[str] = []
    fields = set(getattr(Settings, "model_fields", {}))
    # UI может слать длинные имена полей Settings — приводим к каноничным именам .env
    to_env_name = {"TELEGRAM_API_ID": "API_ID", "TELEGRAM_API_HASH": "API_HASH",
                   "TELEGRAM_PHONE": "PHONE", "MTPROTO_SESSION_STRING": "SESSION_STRING",
                   "MTPROTO_SESSION": "SESSION_FILE", "MTPROTO_ANSWER_MODE": "ANSWER_MODE"}
    normalized: dict[str, str] = {}
    for k, v in updates.items():
        ku = k.strip().upper()
        env_key = to_env_name.get(ku)
        if env_key and ku.lower() not in fields:
            normalized[env_key] = v
        elif env_key and ku.lower() in fields:
            normalized[env_key] = v  # поле есть, но в .env принято короткое имя
        else:
            normalized[k] = v
    updates = normalized
    for key, val in updates.items():
        key = key.strip().upper()
        if not key:
            continue
        if key.lower() not in fields:
            if key in ALLOWED_ALIASES or key in DIRECT_ENV_KEYS \
                    or key.startswith("OPENWEATHER_"):
                clean[key] = str(val).strip()
                continue
            raise ValueError(f"ключ {key!r} не поддерживается")
        elif key in DIRECT_ENV_KEYS:
            v = str(val).strip()
            if DIRECT_ENV_KEYS[key] == "int" and v:
                try:
                    int(v)
                except ValueError:
                    raise ValueError(f"{key}: ожидается целое число")
            clean[key] = v
        else:
            clean[key] = _coerce(key, "" if val is None else str(val))
    tok = clean.get("BOT_TOKEN")
    if tok is not None and tok and ":" not in tok:
        warns.append("BOT_TOKEN похож на неполный: формат «123456:ABC...» (от @BotFather)")
    db = clean.get("DATABASE_URL", "")
    if db and not re.match(r"^(mysql|sqlite|postgresql)(\+\w+)?://", db):
        warns.append("DATABASE_URL должен начинаться с mysql+aiomysql:// или sqlite+aiosqlite://…")
    mode = clean.get("MTPROTO_ANSWER_MODE")
    if mode not in (None, "") and mode not in ("off", "user", "hybrid"):
        raise ValueError("MTPROTO_ANSWER_MODE: допускается off | user | hybrid")
    lvl = clean.get("LOG_LEVEL")
    if lvl and lvl.upper() not in ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError(f"LOG_LEVEL: неизвестный уровень {lvl!r}")
    return clean, warns

def settings_view() -> dict:
    raw = parse_env()
    fields = Settings.model_fields
    # длинные имена полей, для которых в .env принято короткое имя — не показываем дублями
    to_env_name = {"TELEGRAM_API_ID": "API_ID", "TELEGRAM_API_HASH": "API_HASH",
                   "TELEGRAM_PHONE": "PHONE", "MTPROTO_SESSION_STRING": "SESSION_STRING",
                   "MTPROTO_SESSION": "SESSION_FILE", "MTPROTO_ANSWER_MODE": "ANSWER_MODE"}
    items = []
    for name, f in fields.items():
        key = name.upper()
        if key in to_env_name:
            continue
        default = f.default if f.default is not None else f.default_factory() \
            if callable(f.default_factory) else None
        cur = raw.get(key, "")
        is_secret = key in SECRET_KEYS or key == "TELEGRAM_PASSWORD" or key == "TELEGRAM_PASSWORD"
        comment = HELP_RU.get(key) or _line_comment(key)
        items.append({
            "key": key,
            "field": name,
            "title": (f.description or "").splitlines()[0] if f.description else key,
            "type": _type_name(f.annotation),
            "default": _as_str(default),
            "value": ("•" * 12 if is_secret and cur else cur),
            "isSecret": is_secret,
            "isSet": bool(cur),
            "comment": comment,
        })
    by_key = {i["key"]: i for i in items}
    for short, long_key in ALIAS_VIEW.items():
        base = by_key.get(long_key) or raw.get(short, "")
        cur = raw.get(short, "") or (base if isinstance(base, str) else "")
        is_secret = short in SECRET_KEYS or short == "SESSION_STRING"
        items.append({
            "key": short, "field": short.lower(), "title": short,
            "type": ALIAS_TYPES[short], "default": "",
            "value": ("•" * 12 if is_secret and cur else cur),
            "isSecret": is_secret, "isSet": bool(cur),
            "comment": HELP_RU.get(short) or _line_comment(short),
        })
    for key, typ in DIRECT_ENV_KEYS.items():
        cur = raw.get(key, "")
        secret = key in SECRET_KEYS
        items.append({
            "key": key, "field": key.lower(), "title": key,
            "type": typ, "default": "",
            "value": ("•" * 12 if secret and cur else cur),
            "isSecret": secret, "isSet": bool(cur),
            "comment": HELP_RU.get(key) or _line_comment(key),
        })
    return {
        "path": str(env_path()),
        "exists": env_path().is_file(),
        "version": _version(),
        "items": items,
    }

def _version() -> str:
    from app.config import __version__
    return __version__

def _type_name(ann) -> str:
    origin = getattr(ann, "__origin__", None)
    if origin is list:
        return "list"
    if origin is not None and type(None) in getattr(ann, "__args__", ()):
        inner = [a for a in ann.__args__ if a is not type(None)]
        return _type_name(inner[0]) if inner else "str"
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    return "str"

def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)

_COMMENT_CACHE: dict[str, str] = {}

def _line_comment(key: str) -> str:
    if key in _COMMENT_CACHE:
        return _COMMENT_CACHE[key]
    example = env_path().with_name(".env.example")
    text = ""
    if example.is_file():
        prev = ""
        for line in read_env_lines(example):
            m = _KEY_RE.match(line.strip())
            if m and m.group(1) == key:
                inline = line.split("#", 1)
                if "#" in line and inline[1].strip():
                    text = inline[1].strip()
                else:
                    text = prev.lstrip("# ").strip()
                break
            if line.strip():
                prev = line
    _COMMENT_CACHE[key] = text
    return text
