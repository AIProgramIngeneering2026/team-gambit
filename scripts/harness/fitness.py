"""Проверки соответствия архитектурным правилам и спецификации, которые иначе проверяют глазами.

  python3 scripts/harness/fitness.py core-imports  — ядро не импортирует веб-фреймворк, БД и HTTP-клиенты
  python3 scripts/harness/fitness.py doc-links     — относительные ссылки в *.md ведут на существующие файлы
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, repo_root  # noqa: E402

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def core_imports(root: Path, cfg: dict) -> list[str]:
    forbidden = set(cfg["checks"]["forbidden_core_imports"])
    problems = []
    for core in cfg["checks"]["core_paths"]:
        for path in sorted((root / core).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in forbidden:
                        problems.append(f"{path.relative_to(root)}:{node.lineno}: ядро импортирует {name}")
    return problems


def doc_links(root: Path) -> list[str]:
    problems = []
    for path in sorted(root.rglob("*.md")):
        if any(part in {".git", "node_modules", ".claude", ".harness"} for part in path.relative_to(root).parts):
            continue
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"^[a-z]+:|^#|^mailto:", target):
                continue
            file_part = target.split("#", 1)[0]
            if file_part and not (path.parent / file_part).exists():
                problems.append(f"{path.relative_to(root)}: ссылка на несуществующий {file_part}")
    return problems


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    root, cfg = repo_root(), load_config()
    if command == "core-imports":
        problems = core_imports(root, cfg)
    elif command == "doc-links":
        problems = doc_links(root)
    else:
        raise SystemExit(__doc__)
    for p in problems:
        print(f"FAIL {p}")
    print("OK" if not problems else f"{len(problems)} проблем(ы)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
