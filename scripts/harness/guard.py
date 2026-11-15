"""PreToolUse-хук: запрещает агенту необратимые и запрещённые курсом действия.

Получает JSON вызова на stdin. Запрет — код выхода 2, причина — в stderr (её видит агент).
Запрещено без человека: force-push, push в main, удаление веток и ресурсов, сброс незакоммиченных изменений,
rm -r вне временных каталогов, вывод токенов, merge не через squash, правка Zadania вне teams/<команда>/.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

PROTECTED = {"main", "master"}
SAFE_RM = re.compile(r"(^|/)(tmp|\.harness/state|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache|"
                     r"node_modules|dist|build|htmlcov|\.coverage)(/|$)|^/tmp/|^/private/tmp/|^\$TMPDIR|"
                     r"^\$CLAUDE_JOB_DIR|\.claude/jobs/")
SECRET_VAR = re.compile(r"\$\{?[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*\}?")
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def segments(command: str) -> list[list[str]]:
    """Команда → список простых команд (по ; && || | и переводам строк), без присваиваний VAR=x в начале."""
    result = []
    for part in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if words and words[0] in {"sudo", "command", "exec", "time"}:
            words = words[1:]
        if words:
            result.append(words)
    return result


def git_parts(words: list[str]) -> tuple[str | None, list[str], str | None]:
    """git [-C dir] [-c k=v] <sub> args → (sub, args, dir)."""
    i, cwd = 1, None
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        if opt in GIT_GLOBAL_WITH_VALUE and i + 1 < len(words):
            if opt == "-C":
                cwd = words[i + 1]
            i += 2
        else:
            i += 1
    return (words[i], words[i + 1:], cwd) if i < len(words) else (None, [], cwd)


def current_branch(cwd: str | None) -> str | None:
    out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or None, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def check_git(sub: str, args: list[str], cwd: str | None) -> str | None:
    if "--no-verify" in args:
        return "git --no-verify запрещён: проверки и хуки обходить нельзя"
    if sub == "push":
        flags = [a for a in args if a.startswith("-")]
        if any(f in {"-f", "--force", "--mirror", "--delete", "-d"} or f.startswith("--force") for f in flags):
            return "force-push, --mirror и удаление веток на сервере делает только человек (CONTRIBUTING, правила компании)"
        positional = [a for a in args if not a.startswith("-")]
        refspecs = positional[1:]
        if any(r.startswith("+") for r in refspecs):
            return "push с «+» в refspec — это force-push; запрещено"
        for r in refspecs:
            if r.split(":")[-1].removeprefix("refs/heads/") in PROTECTED:
                return "push в main запрещён: изменения попадают в main только через PR (CONTRIBUTING)"
        pushes_head = not refspecs or any(r in {"HEAD", "@"} for r in refspecs)
        if pushes_head and current_branch(cwd) in PROTECTED:
            return "push из ветки main запрещён: создайте ветку и откройте PR"
    if sub == "reset" and "--hard" in args:
        return "git reset --hard удаляет незакоммиченные изменения — только с подтверждением человека"
    if sub == "clean":
        return "git clean удаляет неотслеживаемые файлы — только с подтверждением человека"
    if sub == "checkout" and "--" in args and args.index("--") + 1 < len(args):
        return "git checkout -- <пути> отбрасывает изменения — только с подтверждением человека"
    if sub == "restore" and "--staged" not in args:
        return "git restore отбрасывает изменения — только с подтверждением человека (разрешён git restore --staged)"
    if sub == "branch" and any(a in {"-D", "--delete", "-d"} for a in args):
        return "удаление веток делает только человек"
    if sub == "stash" and args[:1] and args[0] in {"drop", "clear"}:
        return "git stash drop/clear удаляет данные — только с подтверждением человека"
    if sub == "worktree" and args[:1] == ["remove"] and any(a in {"-f", "--force"} for a in args):
        return "git worktree remove --force теряет незакоммиченные изменения"
    return None


def check_gh(args: list[str]) -> str | None:
    if args[:2] in (["auth", "token"],) or args[:1] == ["secret"]:
        return "вывод токенов и работа с секретами запрещены"
    if len(args) >= 2 and args[1] in {"delete", "archive"} and args[0] in {"repo", "release", "issue", "label", "run", "cache"}:
        return f"gh {args[0]} {args[1]} — удаление делает только человек"
    if args[:1] == ["api"]:
        joined = " ".join(args)
        if re.search(r"(-X|--method)\s*=?\s*DELETE", joined, re.I):
            return "DELETE через gh api — удаление делает только человек"
    if args[:2] == ["pr", "merge"]:
        if "--admin" in args:
            return "gh pr merge --admin обходит правила ветки — запрещено"
        if "--squash" not in args and "-s" not in args:
            return "вливать только Squash and merge: gh pr merge <N> --squash (CONTRIBUTING)"
    return None


def check_bash(command: str, cwd: str | None) -> str | None:
    if SECRET_VAR.search(command) and re.search(r"\b(echo|printf|cat|print)\b", command):
        return "значения токенов и ключей не выводятся — используйте только имя переменной"
    for words in segments(command):
        name = Path(words[0]).name
        if name in {"printenv"} or (name == "env" and len(words) == 1):
            return "вывод окружения запрещён: в нём токены"
        if name == "cat" and any(Path(w).name.startswith(".env") for w in words[1:]):
            return "чтение .env запрещено: значения секретов не выводятся"
        if name == "git":
            sub, args, git_cwd = git_parts(words)
            reason = sub and check_git(sub, args, git_cwd or cwd)
            if reason:
                return reason
        if name == "gh":
            reason = check_gh(words[1:])
            if reason:
                return reason
        if name == "rm" and any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--recursive", w) for w in words[1:]):
            targets = [w for w in words[1:] if not w.startswith("-")]
            if not targets or not all(SAFE_RM.search(t) for t in targets):
                return "rm -r вне временных каталогов — только с подтверждением человека; используйте git rm или временный каталог"
    return None


def check_write(file_path: str, cwd: str | None) -> str | None:
    path = Path(file_path)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    path = path.resolve()
    if ".git" in path.parts:
        return "прямая правка .git запрещена"
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from common import load_config, zadania_dir
        cfg = load_config()
        zdir = zadania_dir(cfg, clone=False).resolve()
    except (Exception, SystemExit):
        return None
    if zdir in path.parents:
        allowed = zdir / "teams" / cfg["team"]["slug"]
        if allowed != path and allowed not in path.parents:
            return f"в Zadania можно менять только teams/{cfg['team']['slug']}/ — иначе проверка PR его отклонит"
    return None


def decide(payload: dict) -> str | None:
    tool, data, cwd = payload.get("tool_name"), payload.get("tool_input") or {}, payload.get("cwd")
    if tool == "Bash":
        return check_bash(data.get("command", ""), cwd)
    if tool in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        return check_write(data.get("file_path") or data.get("notebook_path") or "", cwd)
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)
    reason = decide(payload)
    if reason:
        print(f"[harness guard] {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
