"""Verify reconstructed packages, game data, and isolation without model calls."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import logging
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

EXPERIMENTS = Path(__file__).resolve().parents[1]
REPO = EXPERIMENTS.parents[1]
SPEC = json.loads((EXPERIMENTS / "repro/environment.lock.json").read_text())


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def expected_packages(kind: str) -> dict[str, str]:
    if kind == "arc":
        return SPEC["arc_packages"]
    lines = (EXPERIMENTS / "repro/kernel.requirements.in").read_text().splitlines()
    packages = dict(line.split("==") for line in lines if line and not line.startswith("#"))
    packages.update({package["name"]: package["version"] for package in SPEC["local_kernel_packages"]})
    return packages


def check_packages(kind: str) -> dict:
    expected = {normalized(name): version for name, version in expected_packages(kind).items()}
    actual = {normalized(d.metadata["Name"]): d.version for d in metadata.distributions()}
    if actual != expected:
        differences = {
            name: {"expected": expected.get(name), "installed": actual.get(name)}
            for name in sorted(expected.keys() | actual.keys()) if expected.get(name) != actual.get(name)
        }
        raise RuntimeError(f"{kind} package snapshot mismatch: {json.dumps(differences)}")
    if platform.python_version() != SPEC["python"][kind]:
        raise RuntimeError(f"{kind} Python must be {SPEC['python'][kind]}, found {platform.python_version()}")
    if kind == "arc":
        provenance = json.loads(metadata.distribution("prolong-agent").read_text("direct_url.json"))
        if provenance.get("vcs_info", {}).get("commit_id") != SPEC["prolong_revision"]:
            raise RuntimeError("PRO-LONG source revision mismatch")
    else:
        if digest(Path(sys.executable)) != SPEC["kernel_interpreter_sha256"]:
            raise RuntimeError("Kernel interpreter binary differs from the captured Python build")
        for package in SPEC["local_kernel_packages"]:
            distribution = metadata.distribution(package["name"])
            provenance = json.loads(distribution.read_text("direct_url.json"))
            source = REPO / package["path"]
            recorded_source = Path(unquote(urlsplit(provenance["url"]).path))
            if recorded_source != source or provenance.get("dir_info", {}).get("editable", False) != package["editable"]:
                raise RuntimeError(f"Wrong source/editable install for {package['name']}")
            module = importlib.import_module(package["module"])
            if package["editable"]:
                origin = Path(importlib.util.find_spec(package["module"]).origin).resolve()
                if not origin.is_relative_to(source):
                    raise RuntimeError(f"{package['module']} imports from the wrong checkout: {origin}")
            else:
                installed = Path(module.__file__).parent
                expected_files = {
                    str(path.relative_to(source / "src/rlm")): digest(path)
                    for path in (source / "src/rlm").rglob("*.py")
                }
                installed_files = {
                    str(path.relative_to(installed)): digest(path) for path in installed.rglob("*.py")
                }
                if installed_files != expected_files:
                    raise RuntimeError("Installed Python runtime differs from this checkout")
        for module in SPEC["unavailable_kernel_modules"]:
            if importlib.util.find_spec(module) is not None:
                raise RuntimeError(f"Unexpected skill enabled: {module}")
    return {"kind": kind, "python": platform.python_version(), "packages": actual}


def check_game_files(data: Path, game: dict) -> Path:
    name, version = game["id"].split("-", 1)
    directory = data / name / version
    info = json.loads((directory / "metadata.json").read_text())
    if info["game_id"] != game["id"] or digest(directory / game["source"]) != game["sha256"]:
        raise RuntimeError(f"Game version or source hash mismatch: {game['id']}")
    if any(info.get(key) != value for key, value in game.get("metadata", {}).items()):
        raise RuntimeError(f"Game scoring/display metadata mismatch: {game['id']}")
    return directory


def prepare_games(root: Path, download: bool) -> dict:
    from arc_agi import Arcade, OperationMode

    data = root / "benchmarks/arc-agi-3"
    logger = logging.getLogger("prime-agent-reproduce")
    logger.setLevel(logging.WARNING)
    logging.getLogger("arc_agi.scorecard").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="game-check-", dir=root / "cache") as temporary:
        temporary = Path(temporary)
        for game in SPEC["games"]:
            name, version = game["id"].split("-", 1)
            directory = data / name / version
            if not directory.exists() and download:
                staging = temporary / name
                arcade = Arcade(operation_mode=OperationMode.NORMAL, environments_dir=str(staging),
                                recordings_dir=str(temporary / "download-recordings"), logger=logger)
                environment = arcade.make(game["id"])
                if environment is None:
                    raise RuntimeError(f"Official game download failed: {game['id']}")
                source = check_game_files(staging, game)
                directory.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, directory)
                info = json.loads((directory / "metadata.json").read_text())
                info["local_dir"] = str(directory)
                (directory / "metadata.json").write_text(json.dumps(info, indent=2) + "\n")
            check_game_files(data, game)
        with patch("requests.sessions.Session.request", side_effect=RuntimeError("network access in offline game check")):
            arcade = Arcade(operation_mode=OperationMode.OFFLINE, environments_dir=str(data),
                            recordings_dir=str(temporary / "offline-recordings"), logger=logger)
            for game in SPEC["games"]:
                card = arcade.open_scorecard()
                try:
                    environment = arcade.make(game["id"], scorecard_id=card)
                    if environment is None:
                        raise RuntimeError(f"Offline game unavailable: {game['id']}")
                    observation = environment.reset()
                    action = environment.action_space[0]
                    observation = environment.step(action, data={"x": 32, "y": 32} if action.is_complex() else {})
                    if observation is None or tuple(observation.frame[-1].shape) != (64, 64):
                        raise RuntimeError(f"Invalid action/observation roundtrip: {game['id']}")
                finally:
                    arcade.close_scorecard(card)
    return {"games": [game["id"] for game in SPEC["games"]], "offline_action_roundtrips": True}


def check_sandbox(root: Path) -> dict:
    sys.path.insert(0, str(EXPERIMENTS))
    from run import DEFAULT_LIMITS, configure_agent
    from sandbox import command

    with tempfile.TemporaryDirectory(prefix="isolation-check-", dir=root / "cache") as temporary:
        temporary = Path(temporary)
        for name in ("workspace", "agent", "io"):
            (temporary / name).mkdir()
        private_file = temporary / "host-only"
        private_file.write_text("isolation sentinel")
        configure_agent(temporary / "agent", DEFAULT_LIMITS)
        code = f"""
from pathlib import Path
import socket
import rlm, agent_message, attach_image, compact, edit, goal, refine
assert not Path({str(private_file)!r}).exists()
assert not Path({str(root / 'benchmarks')!r}).exists()
assert not Path({str(root / 'copilot-native')!r}).exists()
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.2)
except OSError:
    pass
else:
    raise RuntimeError('unexpected external network access')
print('SANDBOX_PREFLIGHT_PASSED')
"""
        kernel = root / "envs/kernel"
        arguments = command(root / "build/target/debug/prime-agent", REPO, kernel, root / "envs/python",
                            temporary, temporary / "io", [str(kernel / "bin/python"), "-c", code])
        completed = subprocess.run(arguments, text=True, capture_output=True, timeout=30, check=True)
        if "SANDBOX_PREFLIGHT_PASSED" not in completed.stdout:
            raise RuntimeError("Sandbox readiness marker missing")
    return {"sandbox": "passed", "inference_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("kernel", "arc", "games", "sandbox"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.kind in ("kernel", "arc"):
        result = check_packages(args.kind)
    elif args.kind == "games":
        result = prepare_games(root, args.download)
    else:
        result = check_sandbox(root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
