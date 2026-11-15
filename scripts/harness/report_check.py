"""Проверка отчёта pN.md до открытия PR — те же правила, что у tools/check_pr.py в Zadania.

  python3 scripts/harness/report_check.py <путь к отчёту> [--changed teams/gambit/p3.md ...]

Правила: файлы только в teams/<команда>/ с разрешёнными именами; все разделы «## …» шаблона есть и не пустые
(HTML-комментарии содержимым не считаются); в «Вклад участников» упомянут каждый участник из конфига
и есть ссылки на PR командного репозитория.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, zadania_dir  # noqa: E402

TEAM_FILES = re.compile(r"team\.md|p[1-8]\.md|assets/[^/]+")
COMMENT = re.compile(r"<!--.*?-->", re.S)


def sections(text: str) -> dict[str, bool]:
    """{заголовок: есть ли под ним содержимое} — как в check_pr.py."""
    result, current, body = {}, None, []
    for line in COMMENT.sub("", text).splitlines():
        if line.startswith("## "):
            if current is not None:
                result[current] = bool("".join(body).strip())
            current, body = line[3:].strip(), []
        elif current is not None:
            body.append(line)
    if current is not None:
        result[current] = bool("".join(body).strip())
    return result


def check(report: Path, changed: list[str], cfg: dict, zdir: Path) -> list[str]:
    slug = cfg["team"]["slug"]
    problems = []
    for path in changed:
        parts = path.split("/", 2)
        if len(parts) < 3 or parts[0] != "teams" or parts[1] != slug or not TEAM_FILES.fullmatch(parts[2]):
            problems.append(f"`{path}`: можно менять только teams/{slug}/ (team.md, p1–p8.md, assets/)")
    m = re.fullmatch(r"p([1-8])\.md", report.name)
    if not m:
        return problems + [f"`{report.name}`: имя отчёта должно быть pN.md"]
    template = zdir / "templates" / report.name
    text = report.read_text(encoding="utf-8")
    filled = sections(text)
    for heading in sections(template.read_text(encoding="utf-8")):
        if heading not in filled:
            problems.append(f"нет раздела «## {heading}» из шаблона")
        elif not filled[heading]:
            problems.append(f"раздел «## {heading}» пустой (комментарии не считаются)")
    contrib = COMMENT.sub("", text).split("## Вклад участников", 1)
    if len(contrib) == 2:
        block = contrib[1].split("\n## ", 1)[0]
        for member in cfg["members"]:
            login = re.escape(member["login"])
            mentioned = member["student"] in block or re.search(rf"(@|github\.com/){login}\b", block, re.I)
            if not mentioned:
                problems.append(f"в «Вкладе участников» нет {member['student']} (@{member['login']})")
        if f"github.com/{cfg['team']['repo']}/pull/" not in block:
            problems.append("в «Вкладе участников» нет ни одной полной ссылки на PR командного репозитория")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--changed", nargs="*", help="пути изменённых файлов относительно корня Zadania")
    args = parser.parse_args()
    cfg = load_config()
    zdir = zadania_dir(cfg)
    changed = args.changed if args.changed is not None else [f"teams/{cfg['team']['slug']}/{args.report.name}"]
    problems = check(args.report, changed, cfg, zdir)
    for p in problems:
        print(f"FAIL {p}")
    print("OK" if not problems else f"{len(problems)} проблем(ы)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
