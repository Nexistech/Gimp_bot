import json
import os
from copy import deepcopy

ROOT_SCHEMA = [
    {"key": "DEV_GUILD_ID", "type": "int", "label": "Dev guild ID", "default": 0},
    {"key": "ALLOWED_ROLE_NAME", "type": "str", "label": "Allowed Discord role", "default": "Concierge", "widget": "discord_role"},
    {"key": "SPAM_CHANNEL_ID", "type": "int", "label": "Spam / report channel ID", "default": 0},
    {"key": "FREELOADER_CHANNEL_ID", "type": "int", "label": "Freeloader channel ID", "default": 0},
    {"key": "SHOPLIFTING_CHANNEL_ID", "type": "int", "label": "Shoplifting channel ID", "default": 0},
    {
        "key": "ROLES_TO_TAG",
        "type": "str_list",
        "label": "Roles to tag",
        "default": ["Leadership", "Concierge"],
        "widget": "discord_roles",
    },
    {"key": "WEB_HOST", "type": "str", "label": "Config web host", "default": "127.0.0.1"},
    {"key": "WEB_PORT", "type": "int", "label": "Config web port", "default": 8080},
    {"key": "WEB_PASSWORD", "type": "str", "label": "Config web password", "default": "change-me"},
]


def settings_path(plugin_dir, filename="settings.json"):
    return os.path.join(plugin_dir, filename)


def _read_json(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def merge_defaults(defaults, stored):
    merged = deepcopy(defaults)
    if not stored:
        return merged
    for key, value in stored.items():
        if key in merged:
            merged[key] = value
        else:
            merged[key] = value
    return merged


def load_settings(plugin_dir, defaults, filename="settings.json"):
    """Load settings.json, creating it from defaults if missing/empty/invalid."""
    path = settings_path(plugin_dir, filename)
    stored = None
    try:
        stored = _read_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[Settings] Could not read {path}: {exc}")
    data = merge_defaults(defaults, stored)
    if stored != data:
        try:
            _write_json(path, data)
        except OSError as exc:
            print(f"[Settings] Could not write {path}: {exc}")
    return data


def save_settings(plugin_dir, data, filename="settings.json"):
    path = settings_path(plugin_dir, filename)
    _write_json(path, data)
    return path


def schema_defaults(schema):
    return {item["key"]: deepcopy(item.get("default")) for item in schema}


def coerce_value(field, raw):
    ftype = field.get("type", "str")
    if raw is None:
        return deepcopy(field.get("default"))
    if ftype == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if ftype == "int":
        return int(raw)
    if ftype == "float":
        return float(raw)
    if ftype == "int_list":
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw).replace(",", " ").split()
        out = []
        for item in values:
            item = str(item).strip()
            if item:
                out.append(int(item))
        return out
    if ftype == "str_list":
        if isinstance(raw, list):
            values = raw
        else:
            values = [part.strip() for part in str(raw).split(",")]
        return [part for part in values if part]
    return str(raw)


def load_root_settings():
    defaults = schema_defaults(ROOT_SCHEMA)
    try:
        import config as cfg
        for key in list(defaults):
            if hasattr(cfg, key):
                defaults[key] = getattr(cfg, key)
    except Exception:
        pass
    return load_settings(os.getcwd(), defaults)
