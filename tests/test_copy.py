import asyncio
import errno
import io
import os
import shlex
import stat
import subprocess
import tarfile
import sys
from concurrent.futures import Future
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oncolab.cli import call_with_upload, upload
from oncolab.config import ConfigError, plan
from oncolab.copy import copy_plan, notify_failure, publish, publish_result, receive_archive, send_archive
from oncolab.remote import setup_all, wait_copy


class Copy(unittest.TestCase):
    def test_destinations_and_overlaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "train with spaces.py"
            source.touch()
            copies = copy_plan([str(source), f"{source}:other/"], "/content/workspace", [])
            targets = [target for _, target in copies]
            self.assertEqual(targets, ["/content/workspace/train with spaces.py",
                                       "/content/workspace/other/train with spaces.py"])
            for specs, mounts in [
                ([f"{source}:../outside"], []),
                ([f"{source}:/mnt/drive/f"], [Path("/mnt/drive")]),
                ([f"{source}:dir", f"{source}:dir/child"], []),
            ]:
                with self.assertRaises(ValueError):
                    copy_plan(specs, "/content", mounts)

    def test_local_filename_with_colon_cannot_select_another_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            plain = root / "train"
            literal = root / "train:today"
            plain.touch()
            literal.touch()
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                copy_plan([str(literal)], "/content", [])
            escaped = str(literal).replace(":", r"\:")
            self.assertEqual(copy_plan([escaped, f"{escaped}:other:file"], "/content", []),
                             [(literal, "/content/train:today"), (literal, "/content/other:file")])

    def test_round_trip_and_never_replaces_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            source.mkdir(mode=0o700)
            nested = source / "nested"
            nested.mkdir()
            nested.chmod(0o751)
            (source / "a b").write_bytes(b"bytes")
            (source / ".hidden").write_bytes(b"hidden")
            os.link(source / "a b", source / "hardlink")
            stream = io.BytesIO()
            send_archive(stream, [source, source / ".hidden"])
            destination = root / "dest"
            file = root / "renamed"
            receive_archive(io.BytesIO(stream.getvalue()), [destination, file])
            self.assertEqual((destination / "a b").read_bytes(), b"bytes")
            self.assertEqual((destination / ".hidden").read_bytes(), b"hidden")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((destination / "nested").stat().st_mode), 0o751)
            self.assertEqual(file.read_bytes(), b"hidden")
            self.assertEqual((destination / "hardlink").read_bytes(), b"bytes")
            self.assertNotEqual((destination / "a b").stat().st_ino, (destination / "hardlink").stat().st_ino)
            with self.assertRaisesRegex(ValueError, "already exists"):
                receive_archive(io.BytesIO(stream.getvalue()), [destination, file])

    def test_cross_filesystem_copy_is_private_until_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source", root / "target"
            source.write_text("content")
            source.chmod(0o644)

            def copy(reader, writer):
                self.assertEqual(stat.S_IMODE(os.fstat(writer.fileno()).st_mode), 0o600)
                writer.write(reader.read())
                if fail:
                    raise OSError(errno.ENOSPC, "out of space")

            with patch("oncolab.copy.os.link", side_effect=OSError(errno.EXDEV, "cross-device")), \
                 patch("oncolab.copy.shutil.copyfileobj", side_effect=copy):
                fail = True
                with self.assertRaises(OSError) as caught:
                    publish(source, target, {})
                self.assertEqual(caught.exception.errno, errno.ENOSPC)
                self.assertFalse(target.exists())
                fail = False
                publish(source, target, {})
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                self.assertEqual(target.read_text(), "content")
                with self.assertRaises(FileExistsError):
                    publish(source, target, {})
                self.assertEqual(target.read_text(), "content")

    def test_notification_replaces_empty_marker_without_writing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job"
            job.mkdir()
            marker = job / "copy-result"
            (job / "copy-failed").write_text("failed")
            marker.touch()
            with patch.object(Path, "write_text", side_effect=OSError("full")):
                notify_failure(marker)
            self.assertEqual(marker.read_text(), "failed")
            notify_failure(marker)
            marker.unlink()
            job.rmdir()
            notify_failure(marker)

    def test_receiver_failure_keeps_ssh_alive_for_runner_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            local_ssh = root / "ssh"
            local_ssh.write_text(f"#!/bin/sh\nfor arg; do command=$arg; done\n"
                                 f"export PATH={shlex.quote(str(Path(sys.executable).parent))}:$PATH\n"
                                 'exec sh -c "$command"\n')
            local_ssh.chmod(0o755)
            result = root / "copy-result"
            (root / "copy-failed").write_text("failed")
            source = root / "local"
            source.touch()
            occupied = root / "remote"
            occupied.touch()
            # This must not open a fallback SSH channel after failure was reported.
            with patch("oncolab.cli.sp.run", side_effect=AssertionError("fallback attempted")):
                error, notified = upload([str(local_ssh)], "target", str(result), [source], [str(occupied)])
            self.assertIn("already exists", error)
            self.assertTrue(notified)
            self.assertEqual(result.read_text(), "failed")
            future = Future()
            future.set_result((error, notified))
            cleanup = root / "cleanup"
            status = call_with_upload([sys.executable, "-c",
                                       f"from pathlib import Path; Path({str(cleanup)!r}).touch(); raise SystemExit(1)"],
                                      future)
            self.assertEqual(status, 1)
            self.assertTrue(cleanup.exists())

    def test_reject_links_and_truncated_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "dir"
            source.mkdir()
            (source / "link").symlink_to(root / "outside")
            with self.assertRaises(ValueError):
                send_archive(io.BytesIO(), [source])
            (source / "link").unlink()
            stream = io.BytesIO()
            send_archive(stream, [source])
            target = root / "target"
            with self.assertRaises((ValueError, EOFError, tarfile.TarError)):
                receive_archive(io.BytesIO(stream.getvalue()[:512]), [target])
            self.assertFalse(target.exists())

    def test_failed_copy_notification_does_not_leave_local_ssh_waiting(self):
        future = Future()
        future.set_result(("transfer failed", False))
        self.assertEqual(call_with_upload([sys.executable, "-c", "import time; time.sleep(30)"], future), 255)

    def test_profile_and_copy_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('workdir = "/app"\n[env]\nMODE = "test"\n'
                              '[setup.task]\nrun = "true"\ndepends = ["copy"]\n'
                              '[mounts.work]\nsource = "gdrive:work"\ntarget = "/content/work"\n')
            with self.assertRaises(ConfigError):
                plan(config)
            parsed, _, _ = plan(config, copy_requested=True)
            self.assertEqual(parsed["workdir"], "/app")
            self.assertEqual(parsed["env"], {"MODE": "test"})


class CopySetup(unittest.IsolatedAsyncioTestCase):
    async def test_abandoned_receiver_fails_without_waiting_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "copy-result"
            marker.touch()
            result = await wait_copy(marker, start_timeout=0)
            self.assertEqual(result.returncode, 1)
            marker.with_name("copy-pid").write_text("10000\n")
            with patch("oncolab.remote.os.kill", side_effect=ProcessLookupError):
                result = await wait_copy(marker)
            self.assertEqual(result.returncode, 1)

    async def test_independent_setup_overlaps_copy_and_dependent_setup_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "result"
            steps = {
                "independent": {"description": "independent", "argv": ["true"], "depends": []},
                "dependent": {"description": "dependent", "argv": ["true"], "depends": ["copy"]},
            }
            seen = []
            copy_running = asyncio.Event()

            async def run(label, command):
                seen.append(label)
                if label == "Copying files…":
                    copy_running.set()
                    return await command()
                if label == "independent":
                    await copy_running.wait()
                    self.assertFalse(marker.exists())
                    marker.write_text("ok")
                if label == "dependent":
                    self.assertEqual(marker.read_text(), "ok")
                return subprocess.CompletedProcess([], 0)

            self.assertEqual(await asyncio.wait_for(
                setup_all(run, {"setup": steps, "copy_result": str(marker)}, None), 2), 0)
            self.assertEqual(seen, ["Copying files…", "independent", "dependent"])
            marker.write_text("failed")

            async def failed(label, command):
                self.assertNotEqual(label, "dependent")
                if label == "Copying files…":
                    return await command()
                return subprocess.CompletedProcess([], 0)

            self.assertEqual(await asyncio.wait_for(
                setup_all(failed, {"setup": steps, "copy_result": str(marker)}, None), 2), 1)

    async def test_copy_creates_the_workdir_or_its_parent_before_setup(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            try:
                for relative in ("project", "project/subdir"):
                    with self.subTest(workdir=relative):
                        destination = root / relative.replace("/", "-") / "project"
                        workdir = destination if relative == "project" else destination / "subdir"
                        marker = destination.with_name("copy-result")
                        marker.parent.mkdir()
                        marker.touch()
                        marker.with_name("copy-ok").write_text("ok")
                        steps = {"pwd": {"description": "checking cwd", "argv": ["pwd"], "depends": []}}

                        async def run(label, command):
                            if label == "Copying files…":
                                self.assertFalse(workdir.exists())
                                pending = asyncio.create_task(wait_copy(marker))
                                await asyncio.sleep(0)
                                self.assertFalse(pending.done(), "empty marker must not complete the transfer")
                                destination.mkdir()
                                publish_result(marker, "ok")
                                await pending
                                return await command()
                            if label == "checking cwd":
                                self.assertEqual(Path.cwd(), workdir)
                                return subprocess.CompletedProcess([], 0)
                            return await command()

                        self.assertEqual(await asyncio.wait_for(setup_all(run, {
                            "setup": steps, "copy_result": str(marker), "workdir": str(workdir),
                            "copy_supplies_workdir": True,
                        }, None), 2), 0)
            finally:
                os.chdir(original)
