import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_pypi_release.sh"
WHEEL = "oncolab-0.2.0-py3-none-any.whl"
SDIST = "oncolab-0.2.0.tar.gz"
MOCK_GH = """#!/bin/sh
set -eu
case "$1 $2" in
    "api repos/progbeat/oncolab/git/ref/tags/v0.2.0")
        test "$*" = "api repos/progbeat/oncolab/git/ref/tags/v0.2.0 --jq .object.sha"
        printf '%s\\n' "$MOCK_REF";;
    "api repos/progbeat/oncolab/releases/tags/v0.2.0")
        case "$MOCK_STATE" in
            absent) printf 'gh: Not Found (HTTP 404)\\n' >&2; exit 1;;
            offline) printf 'gh: network request timed out\\n' >&2; exit 1;;
            *) printf '%s\\n' "$MOCK_STATE";;
        esac;;
    "release view")
        test "$*" = 'release view v0.2.0 --repo progbeat/oncolab --json isDraft,isImmutable --jq .isImmutable and (.isDraft | not)'
        printf '%s\\n' "$MOCK_IMMUTABLE";;
    "release download")
        test "$*" = 'release download v0.2.0 --repo progbeat/oncolab --dir dist --pattern oncolab-0.2.0-py3-none-any.whl --pattern oncolab-0.2.0.tar.gz --pattern sha256.sum'
        cp fixtures/* dist/;;
    *) exit 2;;
esac
"""


def manifest(files):
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                   for name, data in files.items()).encode()


def run_verifier(root, *args, ref="tested", immutable="true", state="immutable"):
    gh = root / "gh"
    gh.write_text(MOCK_GH)
    gh.chmod(0o700)
    env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", GITHUB_SHA="tested",
               GITHUB_REPOSITORY="progbeat/oncolab", MOCK_REF=ref, MOCK_IMMUTABLE=immutable,
               MOCK_STATE=state)
    return subprocess.run(["bash", str(SCRIPT), "0.2.0", *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=10)


class PyPIRelease(unittest.TestCase):
    def test_download_and_verify(self):
        for name, error, downloads in (
            ("verified", None, True),
            ("wrong ref", "does not point at the tested commit", False),
            ("mutable", "not a published immutable release", False),
            ("corrupt", "distribution checksums do not match", True),
            ("substituted pair", "manifest does not match the smoke-tested distributions", True),
            ("missing digest", "Missing or invalid checksum manifest digest", False),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixtures = root / "fixtures"
                fixtures.mkdir()
                files = {WHEEL: b"wheel contents", SDIST: b"source contents"}
                expected_digest = hashlib.sha256(manifest(files)).hexdigest()
                if name == "substituted pair":
                    files[SDIST] = b"other distribution with a valid matching manifest"
                (fixtures / "sha256.sum").write_bytes(manifest(files))
                if name == "corrupt":
                    files[SDIST] = b"altered source contents"
                for filename, data in files.items():
                    (fixtures / filename).write_bytes(data)
                args = () if name == "missing digest" else (expected_digest,)
                result = run_verifier(root, *args, ref="another" if name == "wrong ref" else "tested",
                                      immutable="false" if name == "mutable" else "true")
                self.assertEqual(downloads, (root / "dist").is_dir())
                if error:
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(error, result.stderr)
                else:
                    self.assertEqual(0, result.returncode, result.stderr)
                    for filename, data in files.items():
                        self.assertEqual(data, (root / "dist" / filename).read_bytes())

    def test_recover_original_distributions_not_rebuilt_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            original = {WHEEL: b"wheel made by old uv", SDIST: b"original sdist"}
            rebuilt_digest = hashlib.sha256(manifest({**original, WHEEL: b"wheel made by new uv"})).hexdigest()
            (fixtures / "sha256.sum").write_bytes(manifest(original))
            for filename, data in original.items():
                (fixtures / filename).write_bytes(data)

            result = run_verifier(root, "--recover")
            self.assertEqual(0, result.returncode, result.stderr)
            recovered = root / "dist"
            for filename, data in original.items():
                self.assertEqual(data, (recovered / filename).read_bytes())
            recovered_digest = hashlib.sha256((recovered / "sha256.sum").read_bytes()).hexdigest()
            self.assertNotEqual(rebuilt_digest, recovered_digest)
            # A separate PyPI job downloads the same published pair and must
            # accept the recovered manifest proof rather than the rebuilt one.
            recovered.rename(root / "release-job-dist")
            self.assertEqual(0, run_verifier(root, recovered_digest).returncode)

    def test_recover_builds_only_when_unpublished(self):
        for state, status in (("absent", 3), ("draft", 3), ("offline", 1), ("mutable", 1)):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = run_verifier(root, "--recover", state=state)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse((root / "dist").exists())
