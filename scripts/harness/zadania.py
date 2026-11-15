"""Разбор заданий курса из клона Zadania.

  python3 scripts/harness/zadania.py sync              — git pull --ff-only клона Zadania
  python3 scripts/harness/zadania.py current [--json]  — активные практики: срок не прошёл, отчёта в main нет
  python3 scripts/harness/zadania.py brief p3 [--json] — срок, баллы, чек-лист, шаблон отчёта, пара review
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EKB, load_config, now, zadania_dir  # noqa: E402

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
DEADLINE = re.compile(r"\*\*Срок:\*\*[^0-9]*(\d{1,2})\s+([а-я]+),?\s*(\d{1,2}):(\d{2})")
POINTS = re.compile(r"\*\*Баллы:\*\*\s*(\d+)\s+личн\S*\s*\+\s*(\d+)\s+командн")
LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
REVIEW_ROW = re.compile(r"^\|\s*{slug}\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|", re.M)


def parse_deadline(text: str, today: datetime) -> datetime | None:
    m = DEADLINE.search(text)
    if not m or m.group(2) not in MONTHS:
        return None
    day, month, hour, minute = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)), int(m.group(4))
    year = today.year
    # Учебный год: осенний семестр переходит через Новый год.
    if today.month >= 9 and month < 6:
        year += 1
    elif today.month < 6 and month >= 9:
        year -= 1
    return datetime(year, month, day, hour, minute, tzinfo=EKB)


def parse_points(text: str) -> dict | None:
    m = POINTS.search(text)
    return {"personal": int(m.group(1)), "team": int(m.group(2))} if m else None


def parse_checklist(text: str) -> list[str]:
    """Пункты «- [ ] …» из раздела чек-листа командной оценки."""
    start = text.find("## Чек-лист")
    if start < 0:
        return []
    items = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("## "):
            break
        m = re.match(r"^\s*- \[ \]\s+(.*\S)", line)
        if m:
            items.append(m.group(1))
    return items


def template_sections(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def review_pair(zdir: Path, n: int, slug: str) -> dict | None:
    path = zdir / "tasks" / f"review_p{n}.md"
    if not path.exists():
        return None
    m = re.compile(REVIEW_ROW.pattern.format(slug=re.escape(slug)), re.M).search(path.read_text(encoding="utf-8"))
    if not m:
        return None
    to_repo = lambda url: re.sub(r"^https://github.com/([^/]+/[^/]+).*$", r"\1", url)  # noqa: E731
    return {"target": to_repo(m.group(2)), "backup": to_repo(m.group(4))}


def reviewed_by(zdir: Path, n: int, slug: str) -> str | None:
    """Какая команда проверяет нас в практике n."""
    path = zdir / "tasks" / f"review_p{n}.md"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and f"team-{slug}/" in cells[1]:
            return f"team-{cells[0]}"
    return None


def practice_numbers(zdir: Path) -> list[int]:
    return sorted(int(p.stem[1:]) for p in (zdir / "tasks").glob("p[0-9].md"))


def brief(zdir: Path, n: int, cfg: dict) -> dict:
    slug = cfg["team"]["slug"]
    task_path = zdir / "tasks" / f"p{n}.md"
    text = task_path.read_text(encoding="utf-8")
    template = zdir / "templates" / f"p{n}.md"
    deadline = parse_deadline(text, now())
    report = zdir / "teams" / slug / f"p{n}.md"
    links = sorted({l for l in LINK.findall(text) if not l.startswith("http")})
    return {
        "practice": f"p{n}",
        "title": text.splitlines()[0].lstrip("# ").strip(),
        "task": str(task_path),
        "deadline": deadline.isoformat() if deadline else None,
        "hours_left": round((deadline - now()).total_seconds() / 3600, 1) if deadline else None,
        "points": parse_points(text),
        "checklist": parse_checklist(text),
        "template": str(template) if template.exists() else None,
        "report_sections": template_sections(template.read_text(encoding="utf-8")) if template.exists() else [],
        "report_path": f"teams/{slug}/p{n}.md",
        "report_in_main": report.exists(),
        "review": review_pair(zdir, n, slug),
        "reviewed_by": reviewed_by(zdir, n, slug),
        "linked_files": [str((task_path.parent / l).resolve().relative_to(zdir)) for l in links
                         if (task_path.parent / l).exists()],
    }


def current(zdir: Path, cfg: dict) -> list[dict]:
    """Практики, по которым ещё можно работать: срок не прошёл и отчёт не влит; самая срочная — первой."""
    active = []
    for n in practice_numbers(zdir):
        b = brief(zdir, n, cfg)
        if b["hours_left"] is not None and b["hours_left"] > 0 and not b["report_in_main"]:
            active.append(b)
    return sorted(active, key=lambda b: b["deadline"])


def sync(zdir: Path) -> None:
    dirty = subprocess.run(["git", "-C", str(zdir), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "-C", str(zdir), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    if dirty or branch != "main":
        print(f"Zadania: пропускаю pull — ветка {branch}, изменения: {'есть' if dirty else 'нет'}; читаю origin/main")
        subprocess.run(["git", "-C", str(zdir), "fetch", "--quiet", "origin"], check=True)
        return
    subprocess.run(["git", "-C", str(zdir), "pull", "--ff-only", "--quiet"], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sync", "current", "brief"])
    parser.add_argument("practice", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cfg = load_config()
    zdir = zadania_dir(cfg)
    if args.command == "sync":
        sync(zdir)
        print(f"Zadania: {zdir}")
        return
    if args.command == "current":
        data = current(zdir, cfg)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=1))
        else:
            for b in data:
                print(f"{b['practice']}: {b['title']} — срок {b['deadline']}, осталось {b['hours_left']} ч")
            if not data:
                print("Активных практик нет")
        return
    n = int((args.practice or "").lstrip("pP") or 0)
    if not n:
        parser.error("укажите практику, например p3")
    b = brief(zdir, n, cfg)
    if args.json:
        print(json.dumps(b, ensure_ascii=False, indent=1))
        return
    print(f"{b['practice']}: {b['title']}\nСрок: {b['deadline']} (осталось {b['hours_left']} ч), баллы: {b['points']}")
    print("Чек-лист:\n" + "\n".join(f"  C{i}. {c}" for i, c in enumerate(b["checklist"], 1)))
    print("Разделы отчёта: " + ", ".join(b["report_sections"]))
    print(f"Отчёт: {b['report_path']} (в main: {'да' if b['report_in_main'] else 'нет'})")
    if b["review"]:
        print(f"Перекрёстное review: проверяем {b['review']['target']} (запасная {b['review']['backup']}), "
              f"нас проверяет {b['reviewed_by']}")
    print("Связанные файлы: " + ", ".join(b["linked_files"]))


if __name__ == "__main__":
    main()
