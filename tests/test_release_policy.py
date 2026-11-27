import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_policy", ROOT / "scripts/release_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def metadata(version, deps=()):
    return {"project": {"version": version, "name": "oncolab", "dependencies": list(deps)}}


def lock(version):
    return {"package": [{"name": "oncolab", "version": version, "source": {"editable": "."}}]}


class Policy(unittest.TestCase):
    def check(self, paths, after="0.1.0", notes="", dependencies=()):
        return policy.check(metadata("0.1.0"), metadata(after, dependencies),
                            lock("0.1.0"), lock(after), paths, "", notes)

    def test_docs_and_tests_keep_current_version(self):
        self.assertIsNone(self.check(["README.md", "tests/test_example.py", "docs/releases.md"]))
        with self.assertRaisesRegex(ValueError, "No package changes"):
            self.check(["README.md"], "0.2.0", "# 0.2.0\n<!-- Bump: minor -->\nNotes")

    def test_source_and_runtime_dependencies_require_one_bump(self):
        self.assertEqual(self.check(["src/oncolab/cli.py"], "0.2.0",
                                    "# 0.2.0\n<!-- Bump: minor -->\nAdded a flag"), "0.2.0")
        self.assertEqual(self.check(["pyproject.toml"], "0.1.1",
                                    "# 0.1.1\n<!-- Bump: patch -->\nDependency fix", ["top"]), "0.1.1")
        for after in ("0.1.0", "0.3.0", "1.2.0"):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.check(["src/oncolab/cli.py"], after, f"# {after}\n<!-- Bump: minor -->\nNotes")

    def test_manifest_lock_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "uv.lock"):
            policy.check(metadata("0.1.0"), metadata("0.2.0"), lock("0.1.0"),
                         lock("0.1.0"), ["src/tools.py"], "", "# 0.2.0\n<!-- Bump: minor -->\nNotes")


class Publisher(unittest.TestCase):
    def test_publish_no_force_no_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "RELEASES.md").write_text("# 0.2.0\n<!-- Bump: minor -->\nNotes")
            (tmp / "dist").mkdir()
            for name in ["oncolab-0.2.0-py3-none-any.whl", "oncolab-0.2.0.tar.gz", "sha256.sum"]:
                (tmp / "dist" / name).write_text("test")
            gh = tmp / "gh"
            gh.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> commands
case "$1 $2" in
  'api repos/progbeat/oncolab/git/ref/tags/v0.2.0') cat tag ;;
  'api --method') echo "$GITHUB_SHA" > tag ;;
  'release create') echo true > draft ;;
  'release upload') test "$(cat draft)" = true ;;
  'release edit') echo false > draft ;;
  'release view')
    test -f draft || exit 1
    case "$*" in
      *'--json isDraft --jq'*) cat draft ;;
      *'--json isImmutable --jq'*) test "$(cat draft)" = false && echo true ;;
      *'--json assets --jq'*) echo oncolab-0.2.0-py3-none-any.whl; echo oncolab-0.2.0.tar.gz; echo sha256.sum ;;
    esac ;;
  *) exit 1 ;;
esac
""")
            gh.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GITHUB_REPOSITORY="progbeat/oncolab",
                       GITHUB_SHA="a" * 40)
            command = ["bash", str(ROOT / "scripts/publish_release.sh"), "0.2.0"]
            subprocess.run(command, cwd=tmp, env=env, check=True, capture_output=True)
            subprocess.run(command, cwd=tmp, env=env, check=True, capture_output=True)
            trace = (tmp / "commands").read_text()
            self.assertEqual(trace.count("api --method POST"), 1)
            self.assertEqual(trace.count("release create"), 1)
            self.assertEqual(trace.count("release upload"), 1)
            self.assertEqual(trace.count("release edit"), 1)
            (tmp / "tag").write_text("b" * 40)
            failed = subprocess.run(command, cwd=tmp, env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b"refusing to move", failed.stderr)
