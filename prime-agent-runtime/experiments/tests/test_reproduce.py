import hashlib
import io
import json
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import reproduce
from repro import probe
from run import DEFAULT_LIMITS, parse_arguments


class ReproductionTests(unittest.TestCase):
    def test_kernel_snapshot_and_hash_lock_pin_the_same_packages(self):
        source = (reproduce.EXPERIMENTS / "repro/kernel.requirements.in").read_text()
        locked = (reproduce.EXPERIMENTS / "repro/kernel.requirements.lock").read_text()
        pattern = r"^([a-z0-9-]+)==([^\s;\\]+)"
        self.assertEqual(dict(re.findall(pattern, source, re.MULTILINE)),
                         dict(re.findall(pattern, locked, re.MULTILINE)))
        for requirement in re.split(r"\n(?=[a-z0-9-]+==)", locked):
            if re.search(pattern, requirement, re.MULTILINE):
                self.assertIn("--hash=sha256:", requirement)

    def test_arc_snapshot_matches_the_existing_lock_and_fixed_game_versions(self):
        spec = json.loads(reproduce.SPEC_PATH.read_text())
        locked = tomllib.loads((reproduce.EXPERIMENTS / "uv.lock").read_text())
        versions = {(package["name"], package["version"]) for package in locked["package"]}
        self.assertTrue(set(spec["arc_packages"].items()).issubset(versions))
        self.assertEqual([game["id"] for game in spec["games"]], ["ls20-9607627b", "ft09-0d8bbf25"])
        self.assertEqual(spec["unavailable_kernel_modules"], ["agent_observe", "rlm_heartbeat"])

    def test_existing_unmanaged_data_and_checkout_paths_are_never_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            repo = parent / "repo"
            repo.mkdir()
            data = parent / "data"
            data.mkdir()
            sentinel = data / "existing-run"
            sentinel.write_text("keep")
            for root in (data, repo, repo / "environment", parent):
                with self.subTest(root=root), self.assertRaises(RuntimeError):
                    reproduce.validate_root(root, repo)
            self.assertEqual(sentinel.read_text(), "keep")
            reproduce.validate_root(parent / "fresh", repo)

    def test_source_fingerprint_includes_untracked_files_and_content_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            source = repo / "runtime.py"
            source.write_text("original\n")
            first = reproduce.source_fingerprint(repo)
            subprocess.run(["git", "-C", str(repo), "add", "runtime.py"], check=True)
            self.assertEqual(reproduce.source_fingerprint(repo), first)
            source.write_text("changed\n")
            self.assertNotEqual(reproduce.source_fingerprint(repo), first)
            source.write_text("original\n")
            (repo / "new.py").write_text("untracked\n")
            self.assertNotEqual(reproduce.source_fingerprint(repo), first)

    def test_installer_cache_hash_failure_never_replaces_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive"
            destination.write_bytes(b"existing")
            with patch("reproduce.urllib.request.urlopen") as request:
                with self.assertRaisesRegex(RuntimeError, "Cached installer hash mismatch"):
                    reproduce.download("https://example.invalid/archive", destination, "wrong")
                request.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"existing")

    def test_download_is_verified_before_publication_and_failed_partial_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive"
            with patch("reproduce.urllib.request.urlopen", return_value=io.BytesIO(b"incorrect")):
                with self.assertRaisesRegex(RuntimeError, "Downloaded installer hash mismatch"):
                    reproduce.download("https://example.invalid/archive", destination, "wrong")
            self.assertEqual(list(destination.parent.iterdir()), [])
            content = b"verified"
            with patch("reproduce.urllib.request.urlopen", return_value=io.BytesIO(content)):
                reproduce.download("https://example.invalid/archive", destination, hashlib.sha256(content).hexdigest())
            self.assertEqual(destination.read_bytes(), content)

    def test_run_wrapper_keeps_original_defaults_and_forwards_only_managed_paths(self):
        root = Path("/new machine/primeagent")
        arguments = reproduce.experiment_arguments(root, ["--output", str(root / "runs/test")])
        parsed, limits = parse_arguments(arguments[3:])
        self.assertEqual(limits, DEFAULT_LIMITS)
        self.assertEqual(parsed.binary, root / "build/target/debug/prime-agent")
        self.assertEqual(parsed.auth_dir, root / "copilot-native/agent")
        self.assertEqual(parsed.data, root / "benchmarks/arc-agi-3")
        for override in ("--binary", "--data=/different"):
            with self.assertRaisesRegex(ValueError, "managed"):
                reproduce.experiment_arguments(root, [override])

    def test_package_version_drift_fails_explicitly(self):
        with patch("repro.probe.metadata.distributions", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "package snapshot mismatch"):
                probe.check_packages("kernel")

    def test_wrong_game_hash_or_id_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            directory = data / "ft09/version"
            directory.mkdir(parents=True)
            source = directory / "ft09.py"
            source.write_text("game")
            game = {"id": "ft09-version", "source": "ft09.py", "sha256": probe.digest(source)}
            info = directory / "metadata.json"
            info.write_text(json.dumps({"game_id": "ft09-version"}))
            self.assertEqual(probe.check_game_files(data, game), directory)
            source.write_text("different game")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                probe.check_game_files(data, game)

    def test_game_scoring_metadata_is_part_of_the_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            directory = data / "game/version"
            directory.mkdir(parents=True)
            source = directory / "game.py"
            source.write_text("game")
            (directory / "metadata.json").write_text(json.dumps({
                "game_id": "game-version", "baseline_actions": [2],
            }))
            game = {
                "id": "game-version", "source": "game.py", "sha256": probe.digest(source),
                "metadata": {"baseline_actions": [1]},
            }
            with self.assertRaisesRegex(RuntimeError, "metadata mismatch"):
                probe.check_game_files(data, game)

    def test_environment_does_not_inherit_foreign_build_or_python_settings(self):
        with patch.dict("os.environ", {
            "PYTHONPATH": "/foreign", "RUSTFLAGS": "--cfg foreign", "LD_PRELOAD": "/foreign",
            "UV_INDEX_URL": "https://example.invalid", "PRIME_AGENT_KERNEL_PYTHON": "/foreign",
        }):
            env = reproduce.environment(Path("/new/root"))
        for name in ("PYTHONPATH", "RUSTFLAGS", "LD_PRELOAD", "UV_INDEX_URL", "PRIME_AGENT_KERNEL_PYTHON"):
            self.assertNotIn(name, env)
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], "/new/root/envs/python")

    def test_completed_setup_is_read_only_and_does_not_install_or_authenticate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = {"schema": 1, "status": "ready", "repo": str(reproduce.REPO), "source_sha256": "fixture"}
            (root / reproduce.MARKER).write_text(json.dumps(record))
            sentinel = root / "runs/do-not-replace"
            sentinel.parent.mkdir()
            sentinel.write_text("saved")
            with (
                patch("reproduce.source_fingerprint", return_value="fixture"),
                patch("reproduce.check_ready"),
                patch("reproduce.check"),
                patch("reproduce.install_tools") as tools,
                patch("reproduce.install_environments") as environments,
                patch("reproduce.run") as command,
            ):
                reproduce.setup(root, {}, {})
                tools.assert_not_called()
                environments.assert_not_called()
                command.assert_not_called()
            self.assertEqual(sentinel.read_text(), "saved")
            self.assertEqual(json.loads((root / reproduce.MARKER).read_text()), record)

    def test_system_version_mismatch_does_not_upgrade_or_downgrade_the_host(self):
        spec = json.loads(reproduce.SPEC_PATH.read_text())
        with (
            patch("reproduce.platform.system", return_value="Linux"),
            patch("reproduce.platform.machine", return_value="x86_64"),
            patch("reproduce.platform.freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}),
            patch("reproduce.platform.libc_ver", return_value=("glibc", "2.39")),
            patch("reproduce.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="install ok installed wrong-version",
            )),
            patch("reproduce.run") as command,
        ):
            with self.assertRaisesRegex(RuntimeError, "implicit upgrade/downgrade"):
                reproduce.check_system(spec, True)
            command.assert_not_called()

    def test_login_uses_new_root_auth_without_a_task_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("reproduce.check_ready"), patch("reproduce.run") as command:
                self.assertEqual(reproduce.main(["--root", str(root), "login"]), 0)
            args, kwargs = command.call_args
            self.assertEqual(args[0], [root / "build/target/debug/prime-agent", "--provider", "github-copilot"])
            self.assertEqual(kwargs["env"]["PRIME_AGENT_CODING_AGENT_DIR"], str(root / "copilot-native/agent"))
            self.assertEqual(kwargs["cwd"], root)


if __name__ == "__main__":
    unittest.main()
