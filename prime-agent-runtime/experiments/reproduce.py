"""Reconstruct the observed Linux experiment environment; never change agent policy."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

EXPERIMENTS = Path(__file__).resolve().parent
REPO = EXPERIMENTS.parents[1]
SPEC_PATH = EXPERIMENTS / "repro/environment.lock.json"
MARKER = ".primeagent-repro.json"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments, *, env=None, capture=False, cwd=None, timeout=None):
    arguments = [str(argument) for argument in arguments]
    if not capture:
        print("+ " + shlex.join(arguments), flush=True)
    result = subprocess.run(arguments, cwd=cwd, env=env, check=True, text=True,
                            capture_output=capture, timeout=timeout)
    return result.stdout.strip() if capture else None


def source_fingerprint(repo: Path = REPO) -> str:
    paths = run(["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture=True)
    fingerprint = hashlib.sha256()
    for name in sorted(set(paths.split("\0")) - {""}):
        path = repo / name
        fingerprint.update(name.encode() + b"\0")
        fingerprint.update(path.read_bytes() if path.is_file() else b"<missing>")
        fingerprint.update(b"\0")
    return fingerprint.hexdigest()


def environment(root: Path) -> dict[str, str]:
    prefixes = ("UV_", "PIP_", "PYTHON", "CARGO_", "RUSTUP_", "PRIME_AGENT_", "RLM_", "ARC_")
    excluded = {
        "VIRTUAL_ENV", "OPERATION_MODE", "ENVIRONMENTS_DIR", "RECORDINGS_DIR",
        "RUSTFLAGS", "RUSTDOCFLAGS", "RUSTC", "RUSTDOC", "CC", "CXX", "AR",
        "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LD_PRELOAD", "LD_LIBRARY_PATH",
    }
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(prefixes) and key not in excluded}
    env.update({
        "PATH": f"{root}/toolchain/bin:{root}/toolchain/cargo/bin:/usr/bin:/bin",
        "CARGO_HOME": str(root / "toolchain/cargo"),
        "RUSTUP_HOME": str(root / "toolchain/rustup"),
        "CARGO_TARGET_DIR": str(root / "build/target"),
        "CARGO_BUILD_JOBS": "2",
        "RUSTUP_TOOLCHAIN": json.loads(SPEC_PATH.read_text())["rust"]["version"],
        "UV_CACHE_DIR": str(root / "cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "envs/python"),
        "UV_PROJECT_ENVIRONMENT": str(root / "envs/arc"),
        "UV_NO_CONFIG": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
        "DO_NOT_TRACK": "1",
    })
    return env


def validate_root(root: Path, repo: Path = REPO) -> None:
    if root == Path.home().resolve() or root == Path("/") or root.is_relative_to(repo) or repo.is_relative_to(root):
        raise RuntimeError("Use a dedicated data directory outside the source checkout and home root")
    if root.exists() and not (root / MARKER).is_file() and any(root.iterdir()):
        raise RuntimeError(f"Refusing to modify unmanaged non-empty directory: {root}; choose a NEW data root")


def check_system(spec: dict, install_missing: bool) -> None:
    host = spec["platform"]
    release = platform.freedesktop_os_release()
    if (platform.system(), platform.machine(), release.get("ID"), release.get("VERSION_ID")) != (
        host["system"], host["machine"], host["distribution"], host["version"],
    ):
        raise RuntimeError("This snapshot requires Ubuntu 24.04 on Linux x86_64; no alternate sandbox is substituted")
    if platform.libc_ver()[1] != host["glibc"]:
        raise RuntimeError(f"Expected glibc {host['glibc']}, found {platform.libc_ver()[1]}")
    missing = []
    for name, expected in spec["system_packages"].items():
        result = subprocess.run(["dpkg-query", "-W", "-f=${Status} ${Version}", name],
                                capture_output=True, text=True)
        if result.returncode or not result.stdout.startswith("install ok installed "):
            missing.append(f"{name}={expected}")
        elif result.stdout.split()[-1] != expected:
            raise RuntimeError(f"System package {name}: expected {expected}, found {result.stdout.split()[-1]}; "
                               "use a matching host image, not an implicit upgrade/downgrade")
    if missing:
        if not install_missing:
            raise RuntimeError(f"Missing system packages: {', '.join(missing)}")
        privilege = [] if os.geteuid() == 0 else ["sudo"]
        run([*privilege, "apt-get", "update"])
        run([*privilege, "apt-get", "install", "--no-install-recommends", "-y", *missing])
        check_system(spec, False)


def download(url: str, destination: Path, expected: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest(destination) != expected:
            raise RuntimeError(f"Cached installer hash mismatch: {destination}")
        return destination
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if digest(temporary) != expected:
            raise RuntimeError(f"Downloaded installer hash mismatch: {url}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def install_tools(root: Path, spec: dict, env: dict) -> Path:
    uv = root / "toolchain/bin/uv"
    if not uv.exists():
        archive = download(spec["uv"]["url"], root / "cache/downloads/uv.tar.gz", spec["uv"]["sha256"])
        uv.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as package:
            member = package.getmember("uv-x86_64-unknown-linux-gnu/uv")
            if not member.isfile():
                raise RuntimeError("uv archive does not contain a regular executable")
            with package.extractfile(member) as source, uv.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        uv.chmod(0o755)
    if digest(uv) != spec["uv"]["binary_sha256"]:
        raise RuntimeError("Installed uv binary differs from the captured release")
    rustup = root / "toolchain/cargo/bin/rustup"
    if not rustup.exists():
        installer = download(spec["rust"]["rustup_url"], root / "cache/downloads/rustup-init",
                             spec["rust"]["rustup_sha256"])
        installer.chmod(0o755)
        run([installer, "-y", "--no-modify-path", "--profile", "minimal",
             "--default-toolchain", spec["rust"]["version"]], env=env)
    run([rustup, "toolchain", "install", spec["rust"]["version"], "--profile", "minimal"], env=env)
    version = run([root / "toolchain/cargo/bin/rustc", "--version"], env=env, capture=True)
    if version.split()[1] != spec["rust"]["version"]:
        raise RuntimeError(f"Wrong compiler version: {version}")
    return uv


def probe(root: Path, kind: str, env: dict, *, download_games=False) -> dict:
    python = root / ("envs/kernel/bin/python" if kind == "kernel" else "envs/arc/bin/python")
    arguments = [python, "-B", EXPERIMENTS / "repro/probe.py", kind, "--root", root]
    if download_games:
        arguments.append("--download")
    return json.loads(run(arguments, env=env, capture=True, timeout=180))


def install_environments(root: Path, spec: dict, env: dict, uv: Path) -> None:
    kernel = root / "envs/kernel/bin/python"
    run([uv, "python", "install", "--no-bin", spec["python"]["kernel"]], env=env)
    if not kernel.exists():
        run([uv, "venv", root / "envs/kernel", "--python", spec["python"]["kernel"]], env=env)
    run([uv, "pip", "sync", "--python", kernel, "--require-hashes",
         EXPERIMENTS / "repro/kernel.requirements.lock"], env=env)
    arguments = [uv, "pip", "install", "--python", kernel, "--no-deps",
                 "--build-constraints", EXPERIMENTS / "repro/build.constraints"]
    for package in spec["local_kernel_packages"]:
        if package["editable"]:
            arguments.append("--editable")
        arguments.append(REPO / package["path"])
    run(arguments, env=env)
    arc = root / "envs/arc/bin/python"
    if not arc.exists():
        run([uv, "venv", root / "envs/arc", "--python", "/usr/bin/python3.12"], env=env)
    exported = root / "cache/arc.requirements.txt"
    run([uv, "export", "--quiet", "--locked", "--no-emit-project", "--project", EXPERIMENTS,
         "--output-file", exported], env=env)
    run([uv, "pip", "sync", "--python", arc, "--build-constraints",
         EXPERIMENTS / "repro/build.constraints", exported], env=env)
    probe(root, "kernel", env)
    probe(root, "arc", env)


def check_ready(root: Path) -> dict:
    record = json.loads((root / MARKER).read_text())
    if record.get("status") != "ready":
        raise RuntimeError("Installation is incomplete; rerun setup")
    if record["repo"] != str(REPO) or record["source_sha256"] != source_fingerprint():
        raise RuntimeError("Checkout location or content changed after setup; use a fresh data root for that revision")
    for relative, expected in record["files_sha256"].items():
        if digest(root / relative) != expected:
            raise RuntimeError(f"Installed executable changed: {relative}")
    return record


def check(root: Path, spec: dict, env: dict) -> dict:
    check_system(spec, False)
    return {
        "kernel": probe(root, "kernel", env),
        "arc": probe(root, "arc", env),
        "games": probe(root, "games", env),
        "sandbox": probe(root, "sandbox", env),
    }


def setup(root: Path, spec: dict, env: dict) -> None:
    validate_root(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if marker.exists():
        record = json.loads(marker.read_text())
        if record["repo"] != str(REPO) or record["source_sha256"] != source_fingerprint():
            raise RuntimeError("Existing installation belongs to a different checkout/snapshot; choose a new root")
        if record.get("status") == "ready":
            check_ready(root)
            check(root, spec, env)
            print("Existing installation verified; no packages, games, runs, or credentials replaced.")
            return
    else:
        marker.write_text(json.dumps({
            "schema": 1, "status": "installing", "repo": str(REPO),
            "source_sha256": source_fingerprint(),
        }, indent=2) + "\n")
    check_system(spec, True)
    for name in ("cache", "runs", "copilot-native/agent"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "copilot-native/agent").chmod(0o700)
    uv = install_tools(root, spec, env)
    install_environments(root, spec, env, uv)
    cargo = root / "toolchain/cargo/bin/cargo"
    run([cargo, "build", "--locked", "-p", "pa-cli", "--bin", "prime-agent"], env=env, cwd=REPO)
    run([cargo, "build", "--locked", "-p", "pa-core", "--example", "copilot_credentials"], env=env, cwd=REPO)
    probe(root, "games", env, download_games=True)
    verified = check(root, spec, env)
    files = ("toolchain/bin/uv", "build/target/debug/prime-agent",
             "build/target/debug/examples/copilot_credentials")
    marker.write_text(json.dumps({
        "schema": 1, "status": "ready", "repo": str(REPO),
        "source_sha256": source_fingerprint(),
        "git_commit": run(["git", "-C", REPO, "rev-parse", "HEAD"], capture=True),
        "git_dirty": bool(run(["git", "-C", REPO, "status", "--porcelain"], capture=True)),
        "files_sha256": {path: digest(root / path) for path in files},
        "verified": verified,
    }, indent=2) + "\n")
    print("Installation and offline checks passed. No runs or credentials were imported.")
    print("Authenticate on this machine with the same setup.sh command followed by 'login'.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Dedicated data directory outside the checkout")
    parser.add_argument("command", choices=("setup", "check", "login"), nargs="?", default="setup")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    spec = json.loads(SPEC_PATH.read_text())
    env = environment(root)
    try:
        validate_root(root)
        if args.command == "setup":
            root.mkdir(parents=True, exist_ok=True)
            # A marker, not a lock file alone, establishes ownership of this directory.
            if not (root / MARKER).exists():
                (root / MARKER).write_text(json.dumps({
                    "schema": 1, "status": "installing", "repo": str(REPO),
                    "source_sha256": source_fingerprint(),
                }) + "\n")
            with (root / ".setup.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                setup(root, spec, env)
        else:
            check_ready(root)
            if args.command == "check":
                check(root, spec, env)
                print("All offline checks passed; authentication and live inference were not tested.")
            elif args.command == "login":
                env["PRIME_AGENT_CODING_AGENT_DIR"] = str(root / "copilot-native/agent")
                env["PRIME_AGENT_KERNEL_PYTHON"] = str(root / "envs/kernel/bin/python")
                env["PI_PACKAGE_DIR"] = str(REPO)
                print("Use /login, select GitHub Copilot, complete browser authorization, then exit. Do not submit a task.")
                run([root / "build/target/debug/prime-agent", "--provider", "github-copilot"], env=env, cwd=root)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Experiment setup failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
