import re

from .config import ConfigError


def _validate(name, value, where):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ConfigError(f"{where} requires a valid variable name")
    if not isinstance(value, str):
        raise ConfigError(f"{where} values must be strings")
    if "\0" in value:
        raise ConfigError(f"{where} values cannot contain NUL bytes")


def parse_env(assignments):
    """Parse explicit NAME=VALUE arguments without expanding local variables."""
    environment = {}
    for assignment in assignments:
        name, separator, value = assignment.partition("=")
        if not separator:
            raise ConfigError("--env expects NAME=VALUE")
        _validate(name, value, "--env")
        environment[name] = value
    return environment


def parse_config_env(value):
    """Validate an [env] table, keeping values literal."""
    if not isinstance(value, dict):
        raise ConfigError("env must be a table")
    for name, entry in value.items():
        _validate(name, entry, "env")
    return dict(value)
