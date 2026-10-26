"""Bubblewrap launch boundary shared by benchmark adapters."""

from pathlib import Path


def command(
    binary: Path,
    repo: Path,
    kernel: Path,
    python_home: Path,
    run: Path,
    io_directory: Path,
    arguments: list[str],
    *,
    model_socket: str = "model.sock",
    readonly_mounts: tuple[tuple[Path, str], ...] = (),
) -> list[str]:
    mounts = [item for source, target in readonly_mounts for item in ("--ro-bind", str(source), target)]
    return [
        "bwrap", "--die-with-parent", "--new-session",
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
        "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--dir", "/app",
        "--ro-bind", str(binary), "/app/prime-agent",
        "--ro-bind", str(repo / "prime-agent-runtime"), "/app/prime-agent-runtime",
        "--ro-bind", str(repo / "skills"), "/app/skills",
        # Existing kernel skills are editable installs pointing at this exact path.
        "--ro-bind", str(repo / "skills"), str(repo / "skills"),
        "--ro-bind", str(kernel), str(kernel),
        "--ro-bind", str(python_home), str(python_home),
        "--ro-bind", str(io_directory), "/io",
        "--bind", str(run / "workspace"), "/work",
        "--symlink", "work", "/workspace",
        "--bind", str(run / "agent"), "/run/agent",
        "--ro-bind", str(run / "agent/models.json"), "/run/agent/models.json",
        "--ro-bind", str(run / "agent/settings.json"), "/run/agent/settings.json",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--clearenv", "--setenv", "PATH", f"{kernel}/bin:/app:/usr/bin:/bin",
        "--setenv", "HOME", "/work", "--setenv", "TMPDIR", "/tmp",
        "--setenv", "DO_NOT_TRACK", "1",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PRIME_AGENT_CODING_AGENT_DIR", "/run/agent",
        "--setenv", "PRIME_AGENT_KERNEL_PYTHON", str(kernel / "bin/python"),
        "--setenv", "PRIME_AGENT_HARNESS_SNAPSHOTS", "1",
        "--setenv", "PI_PACKAGE_DIR", "/app", "--chdir", "/work",
        *mounts,
        "--", str(kernel / "bin/python"),
        "/app/prime-agent-runtime/experiments/local_relay.py",
        "--model-socket", f"/io/{model_socket}",
        "--", *arguments,
    ]
