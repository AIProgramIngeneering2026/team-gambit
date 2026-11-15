import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guard  # noqa: E402


def bash(command: str):
    return guard.decide({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"})


class BashRules(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(guard, "current_branch", return_value="feat/x")
        self.branch = patcher.start()
        self.addCleanup(patcher.stop)

    def test_blocks_force_push_in_any_form(self):
        for cmd in ["git push --force", "git push -f origin feat/x", "git push --force-with-lease",
                    "git push origin +feat/x", "git -C repo push --force origin feat/x", "git push --mirror"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bash(cmd))

    def test_blocks_push_to_main(self):
        for cmd in ["git push origin main", "git push origin HEAD:main", "git push origin feat/x:refs/heads/master"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bash(cmd))

    def test_blocks_bare_push_from_main_only(self):
        self.branch.return_value = "main"
        self.assertIsNotNone(bash("git push"))
        self.assertIsNotNone(bash("git push origin HEAD"))
        self.branch.return_value = "docs/15-api"
        self.assertIsNone(bash("git push"))

    def test_allows_normal_branch_work(self):
        for cmd in ["git push -u origin docs/15-api-contract", "git commit -m 'docs: x'", "git restore --staged a.md",
                    "git status && git diff", "gh pr merge 20 --squash", "gh pr create --title t --body b",
                    "rm -rf /tmp/harness-x", "rm -r .pytest_cache", "git worktree remove .claude/worktrees/x"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd))

    def test_blocks_data_loss(self):
        for cmd in ["git reset --hard origin/main", "git clean -fd", "git checkout -- .", "git restore src/a.py",
                    "git branch -D feat/x", "git stash drop", "rm -rf src", "rm -r docs/adr",
                    "git worktree remove --force .claude/worktrees/x"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bash(cmd))

    def test_blocks_gh_deletes_and_non_squash_merge(self):
        for cmd in ["gh repo delete x", "gh issue delete 5", "gh api -X DELETE repos/o/r/git/refs/heads/x",
                    "gh api --method=DELETE repos/o/r/issues/1/labels/x", "gh pr merge 20 --merge",
                    "gh pr merge 20 --squash --admin", "gh auth token"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bash(cmd))

    def test_blocks_secret_output(self):
        for cmd in ["printenv", "env | grep GH", "echo $GH_TOKEN", 'printf "%s" "${GH_TOKEN_SHAALDY}"', "cat .env"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(bash(cmd))

    def test_env_assignment_prefix_is_not_a_bypass(self):
        self.assertIsNotNone(bash("GIT_TRACE=1 git push --force origin feat/x"))
        self.assertIsNotNone(bash("cd repo && git push origin main"))


class WriteRules(unittest.TestCase):
    def test_zadania_only_team_folder(self):
        zdir = Path("/x/Zadania")
        cfg = {"team": {"slug": "gambit"}}
        with mock.patch("common.zadania_dir", return_value=zdir), mock.patch("common.load_config", return_value=cfg):
            allowed = guard.decide({"tool_name": "Write", "tool_input": {"file_path": "/x/Zadania/teams/gambit/p3.md"}})
            foreign = guard.decide({"tool_name": "Edit", "tool_input": {"file_path": "/x/Zadania/teams/vlm/p3.md"}})
            tasks = guard.decide({"tool_name": "Write", "tool_input": {"file_path": "/x/Zadania/tasks/p3.md"}})
        self.assertIsNone(allowed)
        self.assertIsNotNone(foreign)
        self.assertIsNotNone(tasks)

    def test_git_internals(self):
        self.assertIsNotNone(guard.decide({"tool_name": "Write", "tool_input": {"file_path": "/r/.git/config"}}))


if __name__ == "__main__":
    unittest.main()
