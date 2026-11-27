"""Check that changes to the distributed tool declare exactly one release."""
import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def version_tuple(text):
    if not VERSION.fullmatch(text):
        raise ValueError(f"Expected a stable X.Y.Z version, got {text!r}")
    return tuple(map(int, text.split(".")))


def increment(old, bump):
    a, b, c = version_tuple(old)
    return {"major": (a + 1, 0, 0), "minor": (a, b + 1, 0), "patch": (a, b, c + 1)}[bump]


def manifest_inputs(manifest):
    project = {k: v for k, v in manifest["project"].items() if k != "version"}
    return manifest | {"project": project}


def lock_inputs(lock, name):
    entries = [
        {k: v for k, v in package.items() if k != "version"}
        if package["name"] == name and "editable" in package["source"] else package
        for package in lock["package"]
    ]
    return lock | {"package": entries}


def check(old, new, old_lock, new_lock, paths, old_notes, notes):
    before, after = old["project"]["version"], new["project"]["version"]
    version_tuple(before)
    version_tuple(after)
    name = new["project"]["name"]
    root = [p for p in new_lock["package"] if p["name"] == name and "editable" in p["source"]]
    if len(root) != 1 or root[0]["version"] != after:
        raise ValueError("Project version and uv.lock disagree; run uv lock")
    changed = (any(path.startswith("src/") for path in paths)
               or manifest_inputs(old) != manifest_inputs(new)
               or lock_inputs(old_lock, old["project"]["name"]) != lock_inputs(new_lock, name))
    if not changed:
        if before != after or old_notes != notes:
            raise ValueError("No package changes: keep the current version and RELEASES.md")
        return None
    lines = notes.splitlines()
    if len(lines) < 3 or lines[0] != f"# {after}" or not any(line.strip() for line in lines[2:]):
        raise ValueError(f"RELEASES.md must start with '# {after}', a bump directive, and release notes")
    match = re.fullmatch(r"<!-- Bump: (patch|minor|major) -->", lines[1])
    if not match or version_tuple(after) != increment(before, match[1]):
        raise ValueError(f"Set exactly one patch, minor or major increment over {before} in RELEASES.md and pyproject.toml")
    return after


def git(*args):
    return subprocess.check_output(["git", *args], text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    try:
        base = git("rev-parse", "--verify", "--end-of-options", args.base + "^{commit}").strip()
        old_notes = git("show", f"{base}:RELEASES.md") if "RELEASES.md" in git(
            "ls-tree", "--name-only", base, "--", "RELEASES.md").splitlines() else ""
        path = Path("RELEASES.md")
        paths = git("diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--").split("\0")
        version = check(
            tomllib.loads(git("show", f"{base}:pyproject.toml")),
            tomllib.loads(Path("pyproject.toml").read_text()),
            tomllib.loads(git("show", f"{base}:uv.lock")),
            tomllib.loads(Path("uv.lock").read_text()),
            paths, old_notes, path.read_text() if path.exists() else "",
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Release policy: {exc}")
    if target := os.environ.get("GITHUB_OUTPUT"):
        with open(target, "a") as file:
            print(f"version={version or ''}", file=file)
    print(f"Release v{version}" if version else "No release needed")


if __name__ == "__main__":
    main()
