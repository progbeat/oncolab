import configparser
import graphlib
import io
import os
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath


class ConfigError(ValueError):
    pass


def table(value, allowed, where):
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    if extra := value.keys() - set(allowed):
        raise ConfigError(f"{where}: unknown key {min(extra)!r}")
    return value


def string(value, where):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ConfigError(f"{where} must be a nonempty string")
    return value


def strings(value, where):
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be an array")
    return [string(item, where) for item in value]


def boolean(value, where):
    if type(value) is not bool:
        raise ConfigError(f"{where} must be true or false")
    return value


def local_path(value, base, where):
    path = Path(string(value, where)).expanduser()
    return base / path


def plan(path, *, copy_requested=False):
    from .env import parse_config_env
    if path is None:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "oncolab/config.toml"
        if not path.exists():
            return {"mounts": {}, "setup": {}, "entrypoint": [], "workdir": "/content", "env": {}}, {}, None
    path = path.expanduser()
    with path.open("rb") as file:
        root = tomllib.load(file)
    table(root, ("rclone", "mounts", "setup", "entrypoint", "ssh", "workdir", "env"), str(path))
    workdir = PurePosixPath(string(root.get("workdir", "/content"), "workdir"))
    if workdir.anchor != "/" or ".." in workdir.parts:
        raise ConfigError("workdir must be an absolute VM path without '..'")
    env = parse_config_env(root.get("env", {}))
    base = path.absolute().parent
    rclone = table(root.get("rclone", {}), ("config", "allow_full_drive"), "rclone")
    allow_full = boolean(rclone.get("allow_full_drive", False), "rclone.allow_full_drive")
    ssh = table(root.get("ssh", {}), ("known_hosts",), "ssh")
    known_hosts = local_path(ssh["known_hosts"], base, "ssh.known_hosts") if "known_hosts" in ssh else None
    if known_hosts is not None and not known_hosts.is_file():
        raise ConfigError(f"Known hosts file not found: {known_hosts}")

    mounts, origins, targets, env_names = {}, {}, [], set()
    if not isinstance(root.get("mounts", {}), dict):
        raise ConfigError("mounts must be a table")
    for name, entry in root.get("mounts", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(f"Invalid mount name: {name!r}")
        mount_env = name.upper().replace("-", "_")
        if mount_env in env_names:
            raise ConfigError(f"Mount {name!r} conflicts with another mount's environment variable")
        env_names.add(mount_env)
        key = f"mounts.{name}"
        table(entry, ("source", "target", "read_only", "config"), key)
        source = string(entry.get("source"), key + ".source")
        remote, separator, _ = source.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", remote):
            raise ConfigError(f"{key}.source must name a configured rclone remote")
        target = PurePosixPath(string(entry.get("target"), key + ".target"))
        if (not target.is_absolute() or target.anchor != "/" or ".." in target.parts
                or not any(target.is_relative_to(p) and target != PurePosixPath(p)
                           for p in ("/content", "/mnt", "/home", "/root"))):
            raise ConfigError(f"{key}.target must be beneath /content, /mnt, /home or /root")
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in targets):
            raise ConfigError(f"{key}.target overlaps another mount")
        targets.append(target)
        origin = entry.get("config", rclone.get("config"))
        origin = local_path(origin, base, key + ".config") if origin is not None else None
        if remote in origins and origins[remote][0] != origin:
            raise ConfigError(f"Remote {remote!r} refers to multiple configs; give the remotes distinct names")
        origins[remote] = (origin, allow_full)
        mounts[key] = {"source": source, "target": str(target),
                       "read_only": boolean(entry.get("read_only", False), key + ".read_only")}

    setup = {}
    if not isinstance(root.get("setup", {}), dict):
        raise ConfigError("setup must be a table")
    for name, entry in root.get("setup", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(f"Invalid setup step name: {name!r}")
        table(entry, ("run", "script", "description", "depends"), f"setup.{name}")
        if ("run" in entry) == ("script" in entry):
            raise ConfigError(f"setup.{name} requires exactly one of run and script")
        if "script" in entry:
            run = local_path(entry["script"], base, f"setup.{name}.script").read_text()
        else:
            run = entry["run"]
        argv = (["bash", "-eo", "pipefail", "-c", string(run, f"setup.{name}.run")] if isinstance(run, str)
                else strings(run, f"setup.{name}.run"))
        if not argv:
            raise ConfigError(f"setup.{name}.run cannot be empty")
        setup[name] = {"description": string(entry.get("description", name), f"setup.{name}.description"),
                       "argv": argv, "depends": strings(entry.get("depends", []), f"setup.{name}.depends")}
    if any(workdir.is_relative_to(target) for target in targets):
        raise ConfigError("workdir must be on the VM filesystem, outside Drive mounts")
    if copy_requested and "copy" in setup:
        raise ConfigError("setup.copy conflicts with the --copy dependency")
    for name, step in setup.items():
        for dependency in step["depends"]:
            if dependency not in setup and dependency not in mounts and not (dependency == "copy" and copy_requested):
                raise ConfigError(f"setup.{name}: unknown dependency {dependency!r}")
    try:
        order = graphlib.TopologicalSorter({name: step["depends"] for name, step in setup.items()}).static_order()
        setup = {name: setup[name] for name in order if name in setup}
    except graphlib.CycleError as exc:
        raise ConfigError("Setup dependencies contain a cycle") from exc
    entrypoint = strings(root.get("entrypoint", []), "entrypoint")
    return {"mounts": mounts, "setup": setup, "entrypoint": entrypoint,
            "workdir": str(workdir), "env": env}, origins, known_hosts


def credentials(origins):
    out = configparser.ConfigParser(interpolation=None, default_section="\0")
    for remote, (path, allow_full) in origins.items():
        flags = ["--config", str(path)] if path is not None else []
        result = subprocess.run(["rclone", *flags, "--ask-password=false", "config", "show", remote],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ConfigError(f"Could not read rclone remote {remote!r}")
        try:
            section = configparser.ConfigParser(interpolation=None, default_section="\0")
            section.read_string(result.stdout.decode())
        except (ValueError, configparser.Error):
            raise ConfigError(f"Invalid rclone config for {remote!r}") from None
        if section.sections() != [remote] or section.get(remote, "type", fallback="") != "drive":
            raise ConfigError(f"{remote!r}: expected a direct Google Drive remote")
        if section.has_option(remote, "service_account_file") or section.has_option(remote, "service_account_credentials"):
            raise ConfigError(f"{remote!r}: use OAuth; service-account credentials are not forwarded")
        scope = section.get(remote, "scope", fallback="")
        if not allow_full and not {part.strip() for part in scope.split(",")} <= {
            "drive.file", "drive.readonly", "drive.metadata.readonly",
        }:
            raise ConfigError(f"{remote!r}: configure a restricted Drive scope, or opt in to allow_full_drive")
        out[remote] = dict(section[remote])
    stream = io.StringIO()
    out.write(stream)
    return stream.getvalue().encode()
