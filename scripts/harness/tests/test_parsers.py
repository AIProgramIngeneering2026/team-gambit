import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_check  # noqa: E402
import zadania  # noqa: E402
from common import EKB  # noqa: E402

TASK = """# Практика 4. API и хранение

**Срок:** воскресенье, 29 ноября, 23:59 (по Екатеринбургу). **Баллы:** 4 личных + 4 командных.

## Чек-лист командной оценки

- [ ] API соответствует контракту
- [ ] Хранилище с миграциями

Всё — 4 балла.
"""


class ZadaniaParsing(unittest.TestCase):
    def test_deadline_points_checklist(self):
        today = datetime(2026, 11, 14, tzinfo=EKB)
        self.assertEqual(zadania.parse_deadline(TASK, today), datetime(2026, 11, 29, 23, 59, tzinfo=EKB))
        self.assertEqual(zadania.parse_points(TASK), {"personal": 4, "team": 4})
        self.assertEqual(zadania.parse_checklist(TASK), ["API соответствует контракту", "Хранилище с миграциями"])

    def test_deadline_crosses_new_year(self):
        text = "**Срок:** воскресенье, 10 января, 23:59"
        self.assertEqual(zadania.parse_deadline(text, datetime(2026, 12, 20, tzinfo=EKB)).year, 2027)

    def test_review_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks"
            tasks.mkdir()
            (tasks / "review_p4.md").write_text(
                "| Команда | Проверяет | Запасная |\n|---|---|---|\n"
                "| gambit | [team-a](https://github.com/Org/team-a/pulls) | [team-b](https://github.com/Org/team-b/pulls) |\n"
                "| vlm | [team-gambit](https://github.com/Org/team-gambit/pulls) | [team-c](https://github.com/Org/team-c/pulls) |\n",
                encoding="utf-8")
            self.assertEqual(zadania.review_pair(Path(tmp), 4, "gambit"), {"target": "Org/team-a", "backup": "Org/team-b"})
            self.assertEqual(zadania.reviewed_by(Path(tmp), 4, "gambit"), "team-vlm")


class ReportCheck(unittest.TestCase):
    TEMPLATE = "# Практика 4\n\n## Репозиторий\n<!-- ссылка -->\n\n## Вклад участников\n<!-- ссылки -->\n"
    CFG = {"team": {"slug": "gambit", "repo": "Org/team-gambit"},
           "members": [{"login": "a", "student": "ИвановИИ"}, {"login": "b", "student": "ПетровПП"}]}

    def run_check(self, report_text: str, changed=None):
        with tempfile.TemporaryDirectory() as tmp:
            zdir = Path(tmp)
            (zdir / "templates").mkdir()
            (zdir / "templates" / "p4.md").write_text(self.TEMPLATE, encoding="utf-8")
            report = zdir / "p4.md"
            report.write_text(report_text, encoding="utf-8")
            return report_check.check(report, changed or ["teams/gambit/p4.md"], self.CFG, zdir)

    def test_comment_only_section_is_empty(self):
        problems = self.run_check("## Репозиторий\n<!-- todo -->\n\n## Вклад участников\n- ИвановИИ\n- ПетровПП "
                                  "https://github.com/Org/team-gambit/pull/1\n")
        self.assertTrue(any("Репозиторий" in p for p in problems))

    def test_complete_report_passes(self):
        problems = self.run_check("## Репозиторий\nhttps://github.com/Org/team-gambit\n\n## Вклад участников\n"
                                  "- ИвановИИ — https://github.com/Org/team-gambit/pull/1\n- ПетровПП — review "
                                  "https://github.com/Org/team-gambit/pull/2\n")
        self.assertEqual(problems, [])

    def test_missing_member_and_foreign_file(self):
        problems = self.run_check("## Репозиторий\nx\n\n## Вклад участников\n- ИвановИИ "
                                  "https://github.com/Org/team-gambit/pull/1\n",
                                  changed=["teams/gambit/p4.md", "students/ИвановИИ/README.md"])
        self.assertTrue(any("ПетровПП" in p for p in problems))
        self.assertTrue(any("students/" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
