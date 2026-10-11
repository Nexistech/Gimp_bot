"""Plugin loggers that stay silent unless the logging cog is loaded.

Cogs should use get_logger("folder_name") and must not print. Those loggers
do not propagate to the root logger, so they never write bot.log. The logging
cog attaches logs/<folder>.log and sets the level.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

LEVELS = {
    "fatal": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE,
}
LEVEL_NAMES = ("fatal", "error", "warning", "info", "debug", "trace")
LOG_DIR = "logs"

_file_handlers = {}


class _PluginFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno >= logging.CRITICAL:
            record.levelname = "FATAL"
        elif record.levelno <= TRACE:
            record.levelname = "TRACE"
        return super().format(record)


def _trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


def get_logger(name):
    logger = logging.getLogger(f"plugin.{name}")
    logger.propagate = False
    if not getattr(logger, "_plugin_ready", False):
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        logger.trace = _trace.__get__(logger, logging.Logger)
        logger._plugin_ready = True
    return logger


def normalize_level(level_name):
    key = str(level_name or "warning").strip().lower()
    if key not in LEVELS:
        return "warning"
    return key


def configure(name, level_name="warning"):
    """Attach logs/<name>.log and apply a level. Safe to call again."""
    level_name = normalize_level(level_name)
    logger = get_logger(name)
    logger.setLevel(LEVELS[level_name])
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = _file_handlers.get(name)
    if handler is None:
        path = os.path.join(LOG_DIR, f"{name}.log")
        handler = RotatingFileHandler(path, maxBytes=500_000, backupCount=3)
        handler.setLevel(TRACE)
        handler.setFormatter(_PluginFormatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        _file_handlers[name] = handler
    _attach_related(name, handler, LEVELS[level_name])
    return logger


def silence(name=None):
    """Drop file handlers so plugin logs stop. Main bot.log is untouched."""
    names = list(_file_handlers) if name is None else [name]
    for plugin_name in names:
        handler = _file_handlers.pop(plugin_name, None)
        logger = get_logger(plugin_name)
        if handler is not None:
            logger.removeHandler(handler)
            _detach_related(plugin_name, handler)
            handler.close()
        logger.setLevel(logging.CRITICAL + 1)


def _attach_related(name, handler, level):
    if name != "web_config":
        return
    for related in ("aiohttp.access", "aiohttp.server"):
        extra = logging.getLogger(related)
        extra.propagate = False
        extra.setLevel(level)
        if handler not in extra.handlers:
            extra.addHandler(handler)


def _detach_related(name, handler):
    if name != "web_config":
        return
    for related in ("aiohttp.access", "aiohttp.server"):
        extra = logging.getLogger(related)
        if handler in extra.handlers:
            extra.removeHandler(handler)
