```bash
# New machine: clone the branch containing these changes.
git clone --branch experiments/harness-arc https://github.com/JuheonYi/prime-agent.git
cd prime-agent

export DATA_ROOT="$HOME/.local/share/primeagent-experiments"

# Install and run offline smoke checks. No live model calls.
bash prime-agent-runtime/experiments/setup.sh --root "$DATA_ROOT"

# /login -> GitHub Copilot -> browser authorization -> exit without a task.
bash prime-agent-runtime/experiments/setup.sh --root "$DATA_ROOT" login
```

```bash
# Optional 60-second live smoke. Uses model quota.
"$DATA_ROOT/envs/arc/bin/python" prime-agent-runtime/experiments/run.py \
  --task ft09-0d8bbf25 --seconds 60 \
  --output "$DATA_ROOT/runs/ft09-smoke-001"
```

```bash
# LS20: up to two hours or 5,000 game actions, whichever comes first.
"$DATA_ROOT/envs/arc/bin/python" prime-agent-runtime/experiments/run.py \
  --task ls20-9607627b --seconds 7200 --max-actions 5000 \
  --output "$DATA_ROOT/runs/ls20-2h-001"
```

```bash
# FT09: same budget, independent state. Omit --max-actions for time-only.
"$DATA_ROOT/envs/arc/bin/python" prime-agent-runtime/experiments/run.py \
  --task ft09-0d8bbf25 --seconds 7200 --max-actions 5000 \
  --output "$DATA_ROOT/runs/ft09-2h-001"
```
