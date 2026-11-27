import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_pypi_release.sh"
WHEEL = "oncolab-0.2.0-py3-none-any.whl"
SDIST = "oncolab-0.2.0.tar.gz"


class PyPIRelease(unittest.TestCase):
    def test_download_and_verify(self):
        for name, ref, immutable, corrupt, error in (
            ("verified", "tested", "true", False, None),
            ("wrong ref", "another", "true", False, "does not point at the tested commit"),
            ("mutable", "tested", "false", False, "not a published immutable release"),
            ("corrupt", "tested", "true", True, "distribution checksums do not match"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixtures = root / "fixtures"
                fixtures.mkdir()
                files = {WHEEL: b"wheel contents", SDIST: b"source contents"}
                (fixtures / "sha256.sum").write_text(
                    "".join(f"{hashlib.sha256(data).hexdigest()}  {filename}\n"
                            for filename, data in files.items())
                )
                if corrupt:
                    files[SDIST] = b"altered source contents"
                for filename, data in files.items():
                    (fixtures / filename).write_bytes(data)
                gh = root / "gh"
                gh.write_text("""#!/bin/sh
set -eu
case "$1 $2" in
    "api repos/progbeat/oncolab/git/ref/tags/v0.2.0")
        test "$*" = "api repos/progbeat/oncolab/git/ref/tags/v0.2.0 --jq .object.sha"
        printf '%s\\n' "$MOCK_REF";;
    "release view")
        test "$*" = 'release view v0.2.0 --repo progbeat/oncolab --json isDraft,isImmutable --jq .isImmutable and (.isDraft | not)'
        printf '%s\\n' "$MOCK_IMMUTABLE";;
    "release download")
        test "$*" = 'release download v0.2.0 --repo progbeat/oncolab --dir dist --pattern oncolab-0.2.0-py3-none-any.whl --pattern oncolab-0.2.0.tar.gz --pattern sha256.sum'
        cp fixtures/* dist/;;
    *) exit 2;;
esac
""")
                gh.chmod(0o700)
                env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", GITHUB_SHA="tested",
                           GITHUB_REPOSITORY="progbeat/oncolab", MOCK_REF=ref, MOCK_IMMUTABLE=immutable)
                result = subprocess.run(["bash", str(SCRIPT), "0.2.0"], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=10)
                if error:
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(error, result.stderr)
                    self.assertEqual(corrupt, (root / "dist").is_dir())
                else:
                    self.assertEqual(0, result.returncode, result.stderr)
                    for filename, data in files.items():
                        self.assertEqual(data, (root / "dist" / filename).read_bytes())
