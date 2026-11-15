"""Общие функции скриптов харнеса: конфиг, пути, вызовы gh. Только стандартная библиотека Python 3.11+."""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Срок курса — по Екатеринбургу; zoneinfo может не знать зону на голой системе, поэтому фиксированный сдвиг.
EKB = timezone(timedelta(hours=5), "YEKT")


def repo_root() -> Path:
    """Корень текущего рабочего дерева team-gambit (в worktree — корень worktree)."""
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else HERE.parents[1]


def main_root() -> Path:
    """Корень основного клона: у worktree общий .git лежит в основном клоне."""
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         cwd=HERE, capture_output=True, text=True)
    if out.returncode == 0:
        return Path(out.stdout.strip()).parent
    return repo_root()


def load_config() -> dict:
    with open(repo_root() / ".harness" / "config.toml", "rb") as f:
        return tomllib.load(f)


def state_dir() -> Path:
    path = main_root() / ".harness" / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def zadania_dir(cfg: dict | None = None, clone: bool = True) -> Path:
    """Локальный клон Zadania: HARNESS_ZADANIA_DIR → course.local_path от основного клона → .harness/state/Zadania."""
    cfg = cfg or load_config()
    candidates = []
    if os.environ.get("HARNESS_ZADANIA_DIR"):
        candidates.append(Path(os.environ["HARNESS_ZADANIA_DIR"]).expanduser())
    candidates.append((main_root() / cfg["course"]["local_path"]).resolve())
    candidates.append(state_dir() / "Zadania")
    for path in candidates:
        if (path / "tasks").is_dir():
            return path
    if not clone:
        raise SystemExit("Клон Zadania не найден: задайте HARNESS_ZADANIA_DIR или course.local_path")
    target = state_dir() / "Zadania"
    subprocess.run(["gh", "repo", "clone", cfg["course"]["repo"], str(target), "--", "--quiet"], check=True)
    return target


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {out.stderr.strip()}")
    return json.loads(out.stdout or "null")


def actor() -> str:
    """Логин, от имени которого сейчас работает gh (учитывает GH_TOKEN)."""
    return os.environ.get("HARNESS_ACTOR") or gh_json("api", "user")["login"]


def members(cfg: dict) -> list[dict]:
    return cfg["members"]


def member(cfg: dict, login: str) -> dict | None:
    return next((m for m in cfg["members"] if m["login"].lower() == login.lower()), None)


def now() -> datetime:
    return datetime.now(EKB)
