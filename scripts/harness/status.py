"""Состояние практики по данным GitHub — единый источник для /practice, /practice-status и отчёта.

  python3 scripts/harness/status.py p3            — сводка в markdown
  python3 scripts/harness/status.py p3 --json     — всё в JSON
  python3 scripts/harness/status.py p3 --contrib  — раздел «Вклад участников» для отчёта

PR относится к практике, если у него метка practice:pN, а без метки — если он влит (или создан, если не влит)
после срока прошлой практики и не позже срока этой. Review считается содержательным, если в нём есть
комментарии к строкам, запрошены изменения или текст длиннее 40 символов и это не «LGTM».
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import gh_json, load_config, now, zadania_dir  # noqa: E402
from zadania import brief  # noqa: E402

PR_FIELDS = "number,title,url,author,state,isDraft,createdAt,mergedAt,labels,reviewRequests,reviews,closingIssuesReferences,headRefName,reviewDecision"
TRIVIAL = re.compile(r"^\s*(lgtm|ok|ок|👍|\+1|test|норм\S*)\s*[.!]*\s*$", re.I)


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def window(zdir: Path, n: int, cfg: dict) -> tuple[datetime, datetime]:
    end = datetime.fromisoformat(brief(zdir, n, cfg)["deadline"])
    prev = zdir / "tasks" / f"p{n - 1}.md"
    start = datetime.fromisoformat(brief(zdir, n - 1, cfg)["deadline"]) if prev.exists() else end - timedelta(days=9)
    return start, end


def in_practice(pr: dict, label: str, start: datetime, end: datetime) -> bool:
    labels = {l["name"] for l in pr.get("labels", [])}
    if any(l.startswith("practice:") for l in labels):
        return label in labels
    moment = parse_time(pr.get("mergedAt")) or parse_time(pr.get("createdAt"))
    return bool(moment and start < moment <= end)


def substantive(review: dict, inline: int) -> bool:
    body = (review.get("body") or "").strip()
    if inline > 0 or review.get("state") == "CHANGES_REQUESTED":
        return True
    return len(body) >= 40 and not TRIVIAL.match(body)


def inline_counts(repo: str, number: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in gh_json("api", f"repos/{repo}/pulls/{number}/comments", "--paginate") or []:
        login = c["user"]["login"]
        counts[login] = counts.get(login, 0) + 1
    return counts


def team_prs(cfg: dict, label: str, start: datetime, end: datetime) -> list[dict]:
    repo = cfg["team"]["repo"]
    prs = gh_json("pr", "list", "-R", repo, "--state", "all", "--limit", "200", "--json", PR_FIELDS)
    result = []
    for pr in prs:
        if not in_practice(pr, label, start, end):
            continue
        inline = inline_counts(repo, pr["number"])
        pr["substantive_reviewers"] = sorted({r["author"]["login"] for r in pr["reviews"]
                                              if r["author"]["login"] != pr["author"]["login"]
                                              and substantive(r, inline.get(r["author"]["login"], 0))})
        result.append(pr)
    return result


def cross_reviews(target: str, logins: set[str], start: datetime, end: datetime) -> dict[str, list[str]]:
    """Ссылки на PR проверяемой команды, где наши участники оставили содержательное review."""
    found: dict[str, set[str]] = {}
    prs = gh_json("pr", "list", "-R", target, "--state", "all", "--limit", "100",
                  "--json", "number,url,createdAt,reviews")
    for pr in prs:
        ours = [r for r in pr["reviews"] if r["author"]["login"] in logins
                and start < (parse_time(r.get("submittedAt")) or start) <= end]
        if not ours:
            continue
        inline = inline_counts(target, pr["number"])
        for r in ours:
            login = r["author"]["login"]
            if substantive(r, inline.get(login, 0)):
                found.setdefault(login, set()).add(pr["url"])
    return {k: sorted(v) for k, v in found.items()}


def collect(n: int) -> dict:
    cfg = load_config()
    zdir = zadania_dir(cfg)
    b = brief(zdir, n, cfg)
    label = f"{cfg['labels']['practice_prefix']}p{n}"
    start, end = window(zdir, n, cfg)
    repo = cfg["team"]["repo"]
    logins = {m["login"] for m in cfg["members"]}

    issues = gh_json("issue", "list", "-R", repo, "--label", label, "--state", "all", "--limit", "200",
                     "--json", "number,title,state,url,assignees,labels")
    tracking = [i for i in issues if any(l["name"] == cfg["labels"]["tracking"] for l in i["labels"])]
    questions = gh_json("issue", "list", "-R", repo, "--label", cfg["labels"]["question"], "--state", "open",
                        "--limit", "100", "--json", "number,title,url,labels,createdAt")
    prs = team_prs(cfg, label, start, end)

    cross: dict[str, list[str]] = {}
    target = b["review"]["target"] if b["review"] else None
    if target:
        try:
            cross = cross_reviews(target, logins, start, end)
        except RuntimeError as err:
            cross = {"_error": [str(err)]}

    report_prs = gh_json("pr", "list", "-R", cfg["course"]["repo"], "--state", "all", "--limit", "20",
                         "--search", f"p{n} {cfg['team']['slug']} in:title",
                         "--json", "number,title,url,state,labels,author")
    report_prs = [p for p in report_prs if re.match(rf"^p{n}\s*[—-]\s*{cfg['team']['slug']}\b", p["title"])]

    contrib = {}
    for m in cfg["members"]:
        login = m["login"]
        contrib[login] = {
            "student": m["student"],
            "authored_merged": [p["url"] for p in prs if p["author"]["login"] == login and p["mergedAt"]],
            "authored_open": [p["url"] for p in prs if p["author"]["login"] == login and p["state"] == "OPEN"],
            "reviews": [p["url"] for p in prs if login in p["substantive_reviewers"]],
            "review_requested": [p["url"] for p in prs if p["state"] == "OPEN"
                                 and login in {r.get("login") for r in p["reviewRequests"]}],
            "cross_reviews": cross.get(login, []),
            "issues": [i["url"] for i in issues if login in {a["login"] for a in i["assignees"]}
                       and i not in tracking],
        }
        c = contrib[login]
        c["has_contribution"] = bool(c["authored_merged"] or c["reviews"] or c["cross_reviews"])

    return {
        "practice": f"p{n}", "title": b["title"], "deadline": b["deadline"], "hours_left": b["hours_left"],
        "now": now().isoformat(timespec="minutes"), "window_start": start.isoformat(),
        "checklist": b["checklist"], "review": b["review"], "reviewed_by": b["reviewed_by"],
        "tracking_issue": tracking[0]["url"] if tracking else None,
        "issues": [{"number": i["number"], "title": i["title"], "state": i["state"], "url": i["url"],
                    "assignees": [a["login"] for a in i["assignees"]],
                    "labels": [l["name"] for l in i["labels"]]} for i in issues if i not in tracking],
        "prs": [{"number": p["number"], "title": p["title"], "url": p["url"], "author": p["author"]["login"],
                 "state": p["state"], "draft": p["isDraft"], "review_decision": p["reviewDecision"],
                 "requested": [r.get("login") for r in p["reviewRequests"]],
                 "substantive_reviewers": p["substantive_reviewers"],
                 "closes": [i["number"] for i in p["closingIssuesReferences"]]} for p in prs],
        "open_questions": [{"number": q["number"], "title": q["title"], "url": q["url"]} for q in questions],
        "report_prs": report_prs,
        "contributions": contrib,
    }


def render_contrib(data: dict) -> str:
    lines = []
    for login, c in data["contributions"].items():
        parts = []
        if c["authored_merged"]:
            parts.append("PR: " + ", ".join(c["authored_merged"]))
        if c["reviews"]:
            parts.append("review: " + ", ".join(c["reviews"]))
        if c["cross_reviews"]:
            parts.append("перекрёстное review: " + ", ".join(c["cross_reviews"]))
        lines.append(f"- **{c['student']}** (@{login}) — " + ("; ".join(parts) if parts else "вклада в этой практике нет"))
    return "\n".join(lines)


def render(data: dict) -> str:
    out = [f"# {data['practice']}: {data['title']}",
           f"Срок: {data['deadline']}, осталось {data['hours_left']} ч. Сейчас {data['now']}.",
           f"Трекинг: {data['tracking_issue'] or 'не создан'}", "", "## Чек-лист"]
    out += [f"- C{i}. {c}" for i, c in enumerate(data["checklist"], 1)]
    out += ["", "## Задачи"]
    out += [f"- #{i['number']} [{i['state']}] {i['title']} — {', '.join(i['assignees']) or 'без исполнителя'}"
            for i in data["issues"]] or ["- нет"]
    out += ["", "## PR"]
    out += [f"- #{p['number']} [{p['state']}{', draft' if p['draft'] else ''}] {p['title']} — @{p['author']}; "
            f"review: {p['review_decision'] or '—'}; ждут: {', '.join(p['requested']) or '—'}"
            for p in data["prs"]] or ["- нет"]
    out += ["", "## Вклад участников", render_contrib(data), "", "## Вопросы к команде"]
    out += [f"- #{q['number']} {q['title']}" for q in data["open_questions"]] or ["- нет"]
    out += ["", "## Отчёт в Zadania"]
    out += [f"- {p['url']} [{p['state']}] {', '.join(l['name'] for l in p['labels'])}" for p in data["report_prs"]] \
        or ["- PR не открыт"]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("practice")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--contrib", action="store_true")
    args = parser.parse_args()
    data = collect(int(args.practice.lstrip("pP")))
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    elif args.contrib:
        print(render_contrib(data))
    else:
        print(render(data))


if __name__ == "__main__":
    main()
