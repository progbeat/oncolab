import asyncio
import io
import os
import shlex
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
from oncolab.copy import copy_plan, notify_failure, publish_result, receive_archive, send_archive
from oncolab.remote import setup_all, wait_copy


class Copy(unittest.TestCase):
    def test_destinations_and_overlaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "train with spaces.py"
            source.touch()
            _, targets = copy_plan([str(source), f"{source}:other/"], "/content/workspace", [])
            self.assertEqual(targets, ["/content/workspace/train with spaces.py",
                                       "/content/workspace/other/train with spaces.py"])
            for specs, mounts in [
                ([f"{source}:../outside"], []),
                ([f"{source}:/mnt/drive/f"], [Path("/mnt/drive")]),
                ([f"{source}:dir", f"{source}:dir/child"], []),
            ]:
                with self.assertRaises(ValueError):
                    copy_plan(specs, "/content", mounts)

    def test_round_trip_and_never_replaces_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            source.mkdir()
            (source / "a b").write_bytes(b"bytes")
            (source / ".hidden").write_bytes(b"hidden")
            stream = io.BytesIO()
            send_archive(stream, [source, source / ".hidden"])
            destination = root / "dest"
            file = root / "renamed"
            receive_archive(io.BytesIO(stream.getvalue()), [destination, file])
            self.assertEqual((destination / "a b").read_bytes(), b"bytes")
            self.assertEqual((destination / ".hidden").read_bytes(), b"hidden")
            self.assertEqual(file.read_bytes(), b"hidden")
            with self.assertRaisesRegex(ValueError, "already exists"):
                receive_archive(io.BytesIO(stream.getvalue()), [destination, file])

    def test_hard_links_are_transferred_as_separate_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            source.mkdir()
            (source / "first").write_text("contents")
            os.link(source / "first", source / "second")
            stream = io.BytesIO()
            send_archive(stream, [source, source / "first"])
            target = root / "target"
            receive_archive(io.BytesIO(stream.getvalue()), [target, root / "extra"])
            for path in (target / "first", target / "second", root / "extra"):
                self.assertEqual(path.read_text(), "contents")
            self.assertNotEqual((target / "first").stat().st_ino, (target / "second").stat().st_ino)

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
                                return await pending
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
