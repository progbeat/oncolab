import asyncio
import io
import subprocess
import tarfile
import sys
from concurrent.futures import Future
import tempfile
import unittest
from pathlib import Path

from oncolab.cli import call_with_upload
from oncolab.config import ConfigError, plan
from oncolab.copy import copy_plan, receive_archive, send_archive
from oncolab.remote import setup_all


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
