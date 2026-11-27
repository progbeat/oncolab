"""One-way file staging over a separate SSH channel."""
import errno
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def copy_plan(specs, workdir, mounts):
    sources, targets = [], []
    for spec in specs:
        local, sep, remote = spec.partition(":")
        if not local or sep and not remote:
            raise ValueError("--copy expects LOCAL or LOCAL:REMOTE")
        source = Path(local).expanduser().absolute()
        if not source.name or source.is_symlink() or not (source.is_file() or source.is_dir()):
            raise ValueError(f"--copy source must be a regular file or directory: {local}")
        source = source.resolve()
        target = PurePosixPath(remote) if sep else PurePosixPath(source.name)
        if sep and remote.endswith("/"):
            target /= source.name
        target = PurePosixPath(workdir) / target
        if target.anchor != "/" or target == PurePosixPath("/") or ".." in target.parts:
            raise ValueError(f"Invalid --copy destination: {target}")
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in targets):
            raise ValueError(f"Overlapping --copy destination: {target}")
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in mounts):
            raise ValueError(f"--copy destination overlaps a Drive mount: {target}")
        sources.append(source)
        targets.append(target)
    return sources, [str(target) for target in targets]


def regular(member):
    if not (member.isreg() or member.isdir()):
        raise ValueError(f"--copy does not follow or transfer links or special files: {member.name}")
    return member


class CopyTar(tarfile.TarFile):
    def gettarinfo(self, *args, **kwargs):
        # Treat each directory entry as a separate file without dereferencing symlinks.
        self.inodes.clear()
        return super().gettarinfo(*args, **kwargs)


def send_archive(stream, sources):
    with CopyTar.open(fileobj=stream, mode="w|") as archive:
        for i, source in enumerate(sources):
            archive.add(source, arcname=str(i), filter=regular)
        # Distinguish a complete archive from a stream truncated between members.
        archive.addfile(tarfile.TarInfo("complete"))


def publish(source, target):
    if source.is_dir():
        target.mkdir(mode=0o700)
        for entry in source.iterdir():
            publish(entry, target / entry.name)
        shutil.copystat(source, target)
    else:
        try:
            os.link(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            shutil.copystat(source, target)


def receive_archive(stream, targets):
    with tempfile.TemporaryDirectory(prefix="oncolab-copy-") as staging:
        root = Path(staging)
        names = {str(i) for i in range(len(targets))}
        seen = set()
        complete = False
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                path = PurePosixPath(regular(member).name)
                if path == PurePosixPath("complete") and member.isreg() and not member.size:
                    complete = True
                    break
                if not path.parts or ".." in path.parts or path.parts[0] not in names:
                    raise ValueError("Invalid path in copy stream")
                if path in seen:
                    raise ValueError("Duplicate path in copy stream")
                seen.add(path)
                archive.extract(member, root, filter="data")
        while stream.read(1024 * 1024):
            pass
        if not complete or not all(PurePosixPath(str(i)) in seen for i in range(len(targets))):
            raise ValueError("Incomplete copy stream")
        paths = [Path(target) for target in targets]
        for target in paths:
            if target.resolve() != target or target.exists():
                raise ValueError(f"--copy destination already exists or contains a symlink: {target}")
        for i, target in enumerate(paths):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.resolve() != target:
                raise ValueError(f"--copy destination contains a symlink: {target}")
            publish(root / str(i), target)


COPY_FAILED = 42  # Receiver reported failure; distinct from SSH transport failure.


def publish_result(result, status):
    # The complete markers are reserved before transfer, so reporting needs no data writes.
    result = Path(result)
    result.with_name("copy-" + status).replace(result)


def notify_failure(result):
    result = Path(result)
    try:
        if result.read_text() in ("ok", "failed"):
            return
    except FileNotFoundError:
        pass
    try:
        publish_result(result, "failed")
    except FileNotFoundError:
        if result.parent.exists():
            raise
        # The runner already left setup and cleaned its private staging directory.


def copy_receiver(result, targets):
    status = "failed"
    try:
        receive_archive(sys.stdin.buffer, targets)
        status = "ok"
    except (OSError, ValueError, EOFError, tarfile.TarError) as exc:
        print(exc, file=sys.stderr)
    publish_result(result, status)
    return COPY_FAILED if status == "failed" else 0


if __name__ == "__main__":
    import json

    if sys.argv[2] == "--failed":
        notify_failure(sys.argv[1])
    else:
        raise SystemExit(copy_receiver(sys.argv[1], json.loads(sys.argv[2])))
