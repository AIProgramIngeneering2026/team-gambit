#!/usr/bin/env bash
# Разовая подготовка участника к работе с харнесом. Повторный запуск безопасен.
#   scripts/harness/bootstrap.sh
# Что делает: проверяет gh и Claude Code, создаёт метки харнеса в командном репозитории,
# находит или клонирует Zadania, создаёт ваш fork Zadania и remote «fork» на него.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

need() { command -v "$1" >/dev/null || { echo "Нет $1: $2"; exit 1; }; }
need gh "https://cli.github.com, затем gh auth login"
need python3 "нужен Python 3.11+"
need claude "https://docs.claude.com/claude-code"
need uvx "https://docs.astral.sh/uv/ — нужен для валидатора OpenAPI и тестов"

py() { python3 -c "import sys; sys.path.insert(0, 'scripts/harness'); from common import *; $1"; }
repo=$(py 'print(load_config()["team"]["repo"])')
course=$(py 'print(load_config()["course"]["repo"])')
me=$(gh api user --jq .login)
echo "Вы: $me; командный репозиторий: $repo"

label() { gh label create "$1" -R "$repo" --color "$2" --description "$3" --force >/dev/null && echo "  метка $1"; }
for n in 1 2 3 4 5 6 7 8; do label "practice:p$n" "1d76db" "Практика $n"; done
label "harness:tracking" "5319e7" "План и статус практики (ведёт харнес)"
label "question"         "d93f0b" "Вопрос команде: нужно решение человека"
label "assumption"       "fbca04" "Агент принял решение сам — подтвердите или поправьте"
label "change-request"   "0e8a16" "Изменение или уточнение требований от команды"
label "answered"         "c2e0c6" "Ответ получен и перенесён в решения"
label "triaged"          "c5def5" "Запрос разобран харнесом"
label "blocked"          "b60205" "Задача заблокирована открытым вопросом"
label "needs-human"      "e99695" "Агент остановился: нужен человек"

zadania=$(py 'print(zadania_dir())')
echo "Zadania: $zadania"
gh repo fork "$course" --clone=false >/dev/null 2>&1 || true
if ! git -C "$zadania" remote get-url fork >/dev/null 2>&1; then
  git -C "$zadania" remote add fork "https://github.com/$me/$(basename "$course").git"
fi
echo "  remote fork → $(git -C "$zadania" remote get-url fork)"
echo "Готово. Дальше: /practice-status или scripts/harness/run.sh practice"
