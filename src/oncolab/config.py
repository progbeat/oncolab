import graphlib
import os
import re
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
            raise ConfigError("--mount requires source=REMOTE:PATH,target=PATH[,readonly]")
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
    if not isinstance(root.get("mounts", {}), dict):
        raise ConfigError("mounts must be a table")
    if root.get("mounts"):
        raise ConfigError("Legacy [mounts.*] entries must be migrated to [setup.<name>].mount. "
                          "Select a dedicated [rclone].config: oncolab transfers the whole file.")
    workdir = PurePosixPath(string(root.get("workdir", "/content"), "workdir"))
    if workdir.anchor != "/" or ".." in workdir.parts:
        raise ConfigError("workdir must be an absolute VM path without '..'")
    env = parse_config_env(root.get("env", {}))
    copies = profile_copies(root.get("copy", []), base)
    copy_requested = copy_requested or bool(copies)
    rclone = table(root.get("rclone", {}), ("config",), "rclone")
    rclone_path = local_path(rclone["config"], base, "rclone.config").resolve() if "config" in rclone else None
    forwards = ports([*strings(root.get("ports", []), "ports"), *cli_ports])
    ssh = table(root.get("ssh", {}), ("known_hosts",), "ssh")
    known_hosts = local_path(ssh["known_hosts"], base, "ssh.known_hosts") if "known_hosts" in ssh else None
    if known_hosts is not None and not known_hosts.is_file():
        raise ConfigError(f"Known hosts file not found: {known_hosts}")

    mounts, targets, env_names = {}, [], set()
    def add_mount(name, entry, where, *, alternate=False):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(f"Invalid mount name: {name!r}")
        key = f"mounts.{name}"
        table(entry, ("source", "target", "readonly"), where)
        source = string(entry.get("source"), where + ".source")
        remote, separator, _ = source.partition(":")
        if not separator or not re.fullmatch(r"[\w. -]+", remote) or not remote.strip():
            raise ConfigError(f"{where}.source must be REMOTE:PATH")
        target = PurePosixPath(string(entry.get("target"), where + ".target"))
        if not target.is_absolute():
            target = workdir / target
        if (not target.is_absolute() or target.anchor != "/" or ".." in target.parts
                or not any(target.is_relative_to(p) and target != PurePosixPath(p)
                           for p in ("/content", "/mnt", "/home", "/root"))):
            raise ConfigError(f"{where}.target must be beneath /content, /mnt, /home or /root")
        mount = {"source": source, "target": str(target),
                 "read_only": boolean(entry.get("readonly", False), f"{where}.readonly")}
        if alternate and any(old == mount for old in mounts.values()):
            return None
        mount_env = name.upper().replace("-", "_")
        if mount_env in env_names:
            raise ConfigError(f"Mount {name!r} conflicts with another mount's environment variable")
        env_names.add(mount_env)
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in targets):
            raise ConfigError(f"{where}.target overlaps another mount")
        targets.append(target)
        mounts[key] = mount
        return key

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
            step["mount"] = add_mount(name, entry["mount"], f"{where}.mount")
        setup[name] = step
    for index, value in enumerate(cli_mounts, 1):
        name = f"cli-{index}"
        while name.upper().replace("-", "_") in env_names:
            name = "cli-" + name
        add_mount(name, mount_option(value), "--mount", alternate=True)
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
    if mounts and rclone_path is None:
        raise ConfigError("Mounts require an explicit [rclone].config file")
    return config, rclone_path if mounts else None, known_hosts


def read_rclone_config(path):
    if not path.is_file():
        raise ConfigError(f"Rclone config file not found or not a regular file: {path}")
    return path.read_bytes()
