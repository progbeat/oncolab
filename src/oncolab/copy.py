"""One-way file staging over a separate SSH channel."""
import errno
import os
import select
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def literal_exists(path):
    try:
        Path(os.path.expanduser(path)).lstat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        # LOCAL:REMOTE can be too long as one component but valid separately.
        return False


def copy_plan(specs, workdir, mounts, *, explicit=()):
    pairs = []
    for spec in specs:
        local, sep, remote = spec.partition(":")
        if sep and literal_exists(spec):
            raise ValueError("Ambiguous --copy path: use --copy-to LOCAL REMOTE for literal names")
        pairs.append((local, remote if sep else None))

    sources, targets = [], []
    for local, remote in [*pairs, *explicit]:
        if not local or remote == "":
            raise ValueError("--copy expects LOCAL or LOCAL:REMOTE; --copy-to expects two paths")
        try:
            source = Path(local).expanduser().absolute()
        except RuntimeError:
            raise ValueError(f"Cannot expand --copy home directory: {local}") from None
        if source.is_symlink() or not (source.is_file() or source.is_dir()):
            raise ValueError(f"--copy source must be a regular file or directory: {local}")
        source = source.resolve()
        if not source.name:
            raise ValueError("--copy cannot upload the local filesystem root")
        target = PurePosixPath(remote) if remote is not None else PurePosixPath(source.name)
        if remote is not None and remote.endswith("/"):
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
    return list(zip(sources, map(str, targets)))


def regular(member):
    if not (member.isreg() or member.isdir()):
        raise ValueError(f"--copy does not follow or transfer links or special files: {member.name}")
    return member


class CopyTar(tarfile.TarFile):
    def gettarinfo(self, name=None, arcname=None, fileobj=None):
        # Treat each directory entry as a separate file without dereferencing symlinks.
        self.inodes.clear()
        member = super().gettarinfo(name, arcname, fileobj)
        if member is None:  # tarfile would silently skip sockets.
            raise ValueError(f"--copy does not transfer special files: {name}")
        return regular(member)


def send_archive(stream, sources):
    with CopyTar.open(fileobj=stream, mode="w|") as archive:
        for i, source in enumerate(sources):
            archive.add(source, arcname=str(i))
        # Distinguish a complete archive from a stream truncated between members.
        archive.addfile(tarfile.TarInfo("complete"))


def publish(source, target, directory_modes, mode=None):
    if source.is_dir():
        target.mkdir(mode=0o700)
        for entry in source.iterdir():
            publish(entry, target / entry.name, directory_modes)
        target.chmod(directory_modes.get(source, 0o700) if mode is None else mode)
    else:
        if mode is None:
            try:
                os.link(source, target)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
        with source.open("rb") as reader:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    if mode is not None:
                        timestamps = source.stat()
                        os.utime(target, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))
                        os.fchmod(writer.fileno(), mode)
                    else:
                        shutil.copystat(source, target)
            except BaseException:
                target.unlink(missing_ok=True)
                raise


def receive_archive(stream, targets, modes=None):
    with tempfile.TemporaryDirectory(prefix="oncolab-copy-") as staging:
        root = Path(staging)
        names = {str(i) for i in range(len(targets))}
        seen = set()
        directory_modes = {}
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
                if member.isdir():
                    # The data filter ignores directory modes during extraction.
                    # Restore safe access only after all children are published.
                    directory_modes[root / path] = (member.mode & 0o755) | 0o700
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
            source = root / str(i)
            mode = modes[i] if modes else None
            if mode is None:
                publish(source, target, directory_modes)
            else:
                publish(source, target, directory_modes, mode)


COPY_FAILED = 42  # Receiver reported failure; distinct from SSH transport failure.
INPUT_CLOSED = 43  # The consumer exited or closed stdin without reading to EOF.


def publish_result(result, status):
    # The complete markers are reserved before transfer, so reporting needs no data writes.
    result = Path(result)
    result.with_name("copy-" + status).replace(result)


def notify_failure(result):
    result = Path(result)
    try:
        if result.read_text() in ("ok", "failed", "short"):
            return
    except FileNotFoundError:
        pass
    try:
        publish_result(result, "failed")
    except FileNotFoundError:
        if result.parent.exists():
            raise
        # The runner already left setup and cleaned its private staging directory.


def copy_receiver(result, targets, modes=None):
    Path(result).with_name("copy-pid").write_text(f"{os.getpid()}\n")
    status = "failed"
    try:
        if modes is None:
            receive_archive(sys.stdin.buffer, targets)
        else:
            receive_archive(sys.stdin.buffer, targets, modes)
        status = "ok"
    except (OSError, ValueError, EOFError, tarfile.TarError) as exc:
        print(exc, file=sys.stderr)
    publish_result(result, status)
    return COPY_FAILED if status == "failed" else 0


INPUT_CHUNK = 64 * 1024


def send_input(stream, source):
    # A zero-length frame means successful EOF. A short read, closed channel,
    # or local source error cannot be mistaken for a complete input stream.
    with source.open("rb") as reader:
        while chunk := reader.read(INPUT_CHUNK):
            stream.write(len(chunk).to_bytes(4, "big"))
            stream.write(chunk)
            stream.flush()
    stream.write(b"\0" * 4)


def input_receiver(result):
    result = Path(result)
    try:
        result.with_name("copy-pid").write_text(f"{os.getpid()}\n")
    except FileNotFoundError:
        return INPUT_CLOSED  # Session cleanup beat a queued producer.
    cancelled = result.with_name("cancelled")
    fifo = result.with_name("stdin")
    status = "failed"
    stdin = sys.stdin.fileno()
    writer = None

    def check_cancelled():
        if cancelled.exists() or not result.parent.is_dir():
            raise BrokenPipeError("Task stopped before its stdin stream completed")

    def read_exact(count):
        data = bytearray()
        while len(data) < count:
            check_cancelled()
            if not select.select([stdin], [], [], .1)[0]:
                continue
            chunk = os.read(stdin, count - len(data))
            if not chunk:
                raise ValueError("Incomplete task stdin stream")
            data.extend(chunk)
        return data

    try:
        check_cancelled()
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        fifo.unlink()
        result.with_name("writer-ready").touch()
        while size := int.from_bytes(read_exact(4), "big"):
            if size > INPUT_CHUNK:
                raise ValueError("Invalid task stdin frame size")
            data = memoryview(read_exact(size))
            while data:
                check_cancelled()
                if not select.select([], [writer], [], .1)[1]:
                    continue
                try:
                    data = data[os.write(writer, data):]
                except BlockingIOError:
                    continue
        status = "ok"
    except OSError as exc:
        if (isinstance(exc, BrokenPipeError) or exc.errno == errno.ENXIO
                or (exc.errno == errno.ENOENT and (cancelled.exists() or not result.parent.is_dir()))):
            status = "short"
        else:
            print(exc, file=sys.stderr)
    except ValueError as exc:
        print(exc, file=sys.stderr)
    finally:
        if writer is not None:
            os.close(writer)
    if result.parent.is_dir():
        try:
            publish_result(result, status)
        except FileNotFoundError:
            if result.parent.is_dir():
                raise
    return {"failed": COPY_FAILED, "short": INPUT_CLOSED, "ok": 0}[status]


if __name__ == "__main__":
    import json

    if sys.argv[2] == "--failed":
        notify_failure(sys.argv[1])
    elif sys.argv[2] == "--stdin":
        raise SystemExit(input_receiver(sys.argv[1]))
    else:
        raise SystemExit(copy_receiver(sys.argv[1], json.loads(sys.argv[2]),
                                       json.loads(sys.argv[3]) if len(sys.argv) > 3 else None))
