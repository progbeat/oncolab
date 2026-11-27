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
    try:
        return base / Path(string(value, where)).expanduser()
    except RuntimeError:
        raise ConfigError(f"{where}: could not expand home directory") from None


def profile_copies(value, base):
    if not isinstance(value, list):
        raise ConfigError("copy must be an array of tables ([[copy]])")
    copies = []
    for index, entry in enumerate(value, 1):
        where = f"copy entry {index}"
        table(entry, ("local", "remote"), where)
        local = str(local_path(entry.get("local"), base, f"{where}.local"))
        remote = string(entry["remote"], f"{where}.remote") if "remote" in entry else None
        copies.append((local, remote))
    return copies


def argv(value, where):
    if isinstance(value, str):
        return ["bash", "-eo", "pipefail", "-c", string(value, where)]
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be an array")
    if not value:
        raise ConfigError(f"{where} cannot be empty")
    string(value[0], where)
    if any(not isinstance(arg, str) or "\0" in arg for arg in value[1:]):
        raise ConfigError(f"{where} arguments must be strings without NUL bytes")
    return value


def ports(values):
    result = {}
    for value in strings(values, "ports"):
        if not re.fullmatch(r"[0-9]{1,5}:[0-9]{1,5}", value):
            raise ConfigError("ports and --publish require LOCAL_PORT:REMOTE_PORT")
        local, remote = map(int, value.split(":"))
        if not (1 <= local <= 65535 and 1 <= remote <= 65535):
            raise ConfigError("Port numbers must be between 1 and 65535")
        if local in result and result[local] != remote:
            raise ConfigError(f"Conflicting remote ports for local port {local}")
        result[local] = remote
    return list(result.items())


def mount_option(value):
    options = {}
    for option in value.split(","):
        key, sep, val = option.partition("=")
        if key in options or (not sep and key != "readonly"):
            raise ConfigError("--mount requires type=gdrive,source=PATH,target=PATH[,readonly]")
        if key == "readonly":
            if sep and val not in ("true", "false"):
                raise ConfigError("--mount readonly must be true or false")
            options[key] = not sep or val == "true"
        else:
            options[key] = val
    return options


def task_copies(value, base, where):
    entries = value if isinstance(value, list) else [value]
    if not entries:
        raise ConfigError(f"{where} cannot be empty")
    copies = []
    for entry in entries:
        mode = None
        if isinstance(entry, str):
            local, sep, remote = string(entry, where).partition(":")
            if sep:
                from .copy import literal_exists
                whole = local_path(entry, base, where)
                if literal_exists(str(whole)):
                    raise ConfigError(f"{where}: use a table for literal paths containing ':'")
            else:
                remote = None
        else:
            table(entry, ("source", "target", "chmod"), where)
            local = entry.get("source")
            remote = entry["target"] if "target" in entry else None
            if "chmod" in entry:
                mode = entry["chmod"]
                if type(mode) is not int or not 0 <= mode <= 0o777:
                    raise ConfigError(f"{where}.chmod must be an integer between 0o000 and 0o777")
        if remote is not None:
            remote = string(remote, f"{where}.target")
        if remote == "-" and mode is not None:
            raise ConfigError(f"{where}: chmod does not apply to stdin")
        copies.append((str(local_path(local, base, f"{where}.source")), remote, mode))
    if sum(remote == "-" for _, remote, _ in copies) > 1:
        raise ConfigError(f"{where}: only one source may stream into stdin")
    return copies


def plan(path, *, copy_requested=False, cli_mounts=(), cli_ports=()):
    from .env import parse_config_env
    if path is None:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "oncolab/config.toml"
        if not path.exists():
            path = None
    if path is None:
        root, base = {}, Path.cwd()
    else:
        path = path.expanduser()
        with path.open("rb") as file:
            root = tomllib.load(file)
        base = path.absolute().parent
    table(root, ("rclone", "mounts", "setup", "entrypoint", "ssh", "workdir", "env", "copy", "ports"), str(path))
    workdir = PurePosixPath(string(root.get("workdir", "/content"), "workdir"))
    if workdir.anchor != "/" or ".." in workdir.parts:
        raise ConfigError("workdir must be an absolute VM path without '..'")
    env = parse_config_env(root.get("env", {}))
    copies = profile_copies(root.get("copy", []), base)
    copy_requested = copy_requested or bool(copies)
    rclone = table(root.get("rclone", {}), ("config", "allow_full_drive", "remote"), "rclone")
    default_remote = string(rclone.get("remote", "gdrive"), "rclone.remote")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", default_remote):
        raise ConfigError("rclone.remote must name a configured rclone remote")
    forwards = ports([*strings(root.get("ports", []), "ports"), *cli_ports])
    allow_full = boolean(rclone.get("allow_full_drive", False), "rclone.allow_full_drive")
    ssh = table(root.get("ssh", {}), ("known_hosts",), "ssh")
    known_hosts = local_path(ssh["known_hosts"], base, "ssh.known_hosts") if "known_hosts" in ssh else None
    if known_hosts is not None and not known_hosts.is_file():
        raise ConfigError(f"Known hosts file not found: {known_hosts}")

    mounts, origins, targets, env_names = {}, {}, [], set()
    def add_mount(name, entry, where, *, explicit=False, alternate=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(f"Invalid mount name: {name!r}")
        key = f"mounts.{name}"
        if explicit:
            table(entry, ("type", "source", "target", "readonly"), where)
            if entry.get("type") != "gdrive":
                raise ConfigError(f"{where} requires type=gdrive (local binds are not supported)")
        else:
            table(entry, ("source", "target", "read_only", "config"), where)
        source = string(entry.get("source"), where + ".source")
        if explicit:
            source = f"{default_remote}:{source}"
        remote, separator, _ = source.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", remote):
            raise ConfigError(f"{where}.source must name a configured rclone remote")
        target = PurePosixPath(string(entry.get("target"), where + ".target"))
        if explicit and not target.is_absolute():
            target = workdir / target
        if (not target.is_absolute() or target.anchor != "/" or ".." in target.parts
                or not any(target.is_relative_to(p) and target != PurePosixPath(p)
                           for p in ("/content", "/mnt", "/home", "/root"))):
            raise ConfigError(f"{where}.target must be beneath /content, /mnt, /home or /root")
        read_key = "readonly" if explicit else "read_only"
        mount = {"source": source, "target": str(target),
                 "read_only": boolean(entry.get(read_key, False), f"{where}.{read_key}")}
        origin = entry.get("config", rclone.get("config"))
        origin = local_path(origin, base, where + ".config") if origin is not None else None
        if alternate and any(old == mount for old in mounts.values()) and origins.get(remote) == (origin, allow_full):
            return None
        mount_env = name.upper().replace("-", "_")
        if mount_env in env_names:
            raise ConfigError(f"Mount {name!r} conflicts with another mount's environment variable")
        env_names.add(mount_env)
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in targets):
            raise ConfigError(f"{where}.target overlaps another mount")
        targets.append(target)
        if remote in origins and origins[remote][0] != origin:
            raise ConfigError(f"Remote {remote!r} refers to multiple configs; give the remotes distinct names")
        origins[remote] = (origin, allow_full)
        mounts[key] = mount
        return key

    if not isinstance(root.get("mounts", {}), dict):
        raise ConfigError("mounts must be a table")
    for name, entry in root.get("mounts", {}).items():
        add_mount(name, entry, f"mounts.{name}")
    setup = {}
    if not isinstance(root.get("setup", {}), dict):
        raise ConfigError("setup must be a table")
    for name, entry in root.get("setup", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(f"Invalid setup step name: {name!r}")
        where = f"setup.{name}"
        table(entry, ("run", "script", "description", "depends", "needs", "start", "ready",
                      "teardown", "copy", "mount"), where)
        if "needs" in entry and "depends" in entry:
            raise ConfigError(f"{where}: specify needs or legacy depends, not both")
        if len({"run", "script", "start", "mount"} & entry.keys()) > 1:
            raise ConfigError(f"{where}: run, script, start and mount are mutually exclusive")
        if "mount" in entry and "copy" in entry:
            raise ConfigError(f"{where}: define copies and mounts as separate tasks")
        if "ready" in entry and "start" not in entry:
            raise ConfigError(f"{where}.ready requires start")
        if not {"run", "script", "start", "copy", "mount"} & entry.keys():
            raise ConfigError(f"{where}: expected run, script, start, copy or mount")
        step = {"description": string(entry.get("description", name), f"{where}.description"),
                "depends": strings(entry.get("needs", entry.get("depends", [])), f"{where}.needs")}
        if "script" in entry:
            step["argv"] = argv(local_path(entry["script"], base, f"{where}.script").read_text(), f"{where}.run")
        for field in ("run", "start", "ready", "teardown"):
            if field in entry:
                step["argv" if field == "run" else field] = argv(entry[field], f"{where}.{field}")
        if "copy" in entry:
            step["copies"] = task_copies(entry["copy"], base, f"{where}.copy")
            if any(remote == "-" for _, remote, _ in step["copies"]) and not {"argv", "start"} & step.keys():
                raise ConfigError(f"{where}.copy: target '-' requires run or start")
        if "mount" in entry:
            step["mount"] = add_mount(name, entry["mount"], f"{where}.mount", explicit=True)
        setup[name] = step
    for index, value in enumerate(cli_mounts, 1):
        name = f"cli-{index}"
        while name.upper().replace("-", "_") in env_names:
            name = "cli-" + name
        add_mount(name, mount_option(value), "--mount", explicit=True, alternate=True)
    if any(workdir.is_relative_to(target) for target in targets):
        raise ConfigError("workdir must be on the VM filesystem, outside Drive mounts")
    if copy_requested and "copy" in setup:
        raise ConfigError("setup.copy conflicts with the copy dependency")
    for name, step in setup.items():
        for dependency in step["depends"]:
            if dependency not in setup and dependency not in mounts and not (dependency == "copy" and copy_requested):
                raise ConfigError(f"setup.{name}: unknown dependency {dependency!r}")
            if any(step.get("mount") == dependency for step in setup.values()):
                raise ConfigError(f"setup.{name}: refer to a setup mount by its task name, not {dependency!r}")
    try:
        order = graphlib.TopologicalSorter({name: step["depends"] for name, step in setup.items()}).static_order()
        setup = {name: setup[name] for name in order if name in setup}
    except graphlib.CycleError as exc:
        raise ConfigError("Setup dependencies contain a cycle") from exc
    entrypoint = strings(root.get("entrypoint", []), "entrypoint")
    config = {"mounts": mounts, "setup": setup, "entrypoint": entrypoint,
              "workdir": str(workdir), "env": env}
    if copies:
        config["copies"] = copies
    if forwards:
        config["ports"] = forwards
    return config, origins, known_hosts


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
