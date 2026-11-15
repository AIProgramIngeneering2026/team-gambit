#!/usr/bin/env bash
# Запуск харнеса без человека (claude -p). Агент не задаёт вопросов: неясное решает по протоколу решений
# и оставляет вопросы команде в issues.
#
#   scripts/harness/run.sh practice [pN]                    — от своего имени (текущий gh auth)
#   scripts/harness/run.sh practice p3 --as Shaaldy         — от имени участника: токен из переменной token_env
#   scripts/harness/run.sh practice p3 --as all [--passes 2] — по очереди за всех участников
#   scripts/harness/run.sh inbox                             — разобрать ответы и запросы команды
#
# Переменные: HARNESS_PERMISSION_MODE (по умолчанию dontAsk), HARNESS_MAX_TURNS, HARNESS_MODEL.
# Журнал каждого запуска — .harness/state/runs/. Значения токенов не печатаются и не пишутся в журнал.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

skill="${1:?usage: run.sh <skill> [args] [--as login|all] [--passes N]}"; shift
args=(); as=""; passes=1
while [ $# -gt 0 ]; do
  case "$1" in
    --as) as="$2"; shift 2 ;;
    --passes) passes="$2"; shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done

py() { python3 -c "import sys; sys.path.insert(0, 'scripts/harness'); from common import *; $1"; }
STATE=$(py 'print(state_dir())')
ZADANIA=$(py 'print(zadania_dir())')
mkdir -p "$STATE/runs"
default_login=$(gh api user --jq .login)

run_as() {
  local login="$1"
  (
    set +x
    if [ "$login" != "$default_login" ]; then
      local var
      var=$(py "m = member(load_config(), '$login'); print(m['token_env'] if m else '')")
      [ -n "$var" ] || { echo "Участника $login нет в .harness/config.toml"; exit 1; }
      [ -n "${!var:-}" ] || { echo "Для запуска от имени $login задайте переменную $var (токен GitHub участника)"; exit 1; }
      export GH_TOKEN="${!var}"
      # git push по https с тем же токеном через gh, а не через ssh-ключ владельца машины.
      export GIT_CONFIG_COUNT=3
      export GIT_CONFIG_KEY_0="credential.https://github.com.helper" GIT_CONFIG_VALUE_0=""
      export GIT_CONFIG_KEY_1="credential.https://github.com.helper" GIT_CONFIG_VALUE_1="!gh auth git-credential"
      export GIT_CONFIG_KEY_2="url.https://github.com/.insteadOf" GIT_CONFIG_VALUE_2="git@github.com:"
      local id
      id=$(gh api "users/$login" --jq .id)
      export GIT_AUTHOR_NAME="$login" GIT_COMMITTER_NAME="$login"
      export GIT_AUTHOR_EMAIL="$id+$login@users.noreply.github.com" GIT_COMMITTER_EMAIL="$id+$login@users.noreply.github.com"
    fi
    export HARNESS_MODE=autonomous HARNESS_ACTOR="$login" HARNESS_ZADANIA_DIR="$ZADANIA"

    local lock="$STATE/lock-$login"
    mkdir "$lock" 2>/dev/null || { echo "Для $login уже идёт запуск ($lock)"; exit 0; }
    trap 'rmdir "$lock"' EXIT

    local log
    log="$STATE/runs/$(date +%Y%m%d-%H%M%S)-$login-$skill.json"
    local prompt="/$skill ${args[*]:-}"
    echo "▶ $login: $prompt → $log"
    claude -p "$prompt" \
      --permission-mode "${HARNESS_PERMISSION_MODE:-dontAsk}" \
      --permission-prompts none \
      --add-dir "$ZADANIA" \
      --append-system-prompt "$(cat .harness/autonomous.md)" \
      --output-format json \
      ${HARNESS_MAX_TURNS:+--max-turns "$HARNESS_MAX_TURNS"} \
      ${HARNESS_MODEL:+--model "$HARNESS_MODEL"} \
      > "$log" || echo "  claude завершился с ошибкой — см. $log"
    python3 - "$log" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:  # журнал неполный — показываем как есть
    print(f"  журнал не разобран: {exc}")
    sys.exit(0)
# --output-format json: объект результата или массив сообщений, где результат — последний элемент типа result.
results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"] if isinstance(data, list) else [data]
result = results[-1] if results else {}
print("  итог:", (result.get("result") or "").strip()[:4000])
print(f"  стоимость: {result.get('total_cost_usd', '?')} USD, ходов: {result.get('num_turns', '?')}")
for denial in result.get("permission_denials") or []:
    print(f"  отклонено: {denial.get('tool_name')} {str(denial.get('tool_input', {}).get('command', ''))[:120]}")
PY
  )
}

if [ "$as" = "all" ]; then
  logins=$(py 'print(" ".join(m["login"] for m in load_config()["members"]))')
  for pass in $(seq 1 "$passes"); do
    echo "=== проход $pass из $passes"
    for login in $logins; do run_as "$login"; done
  done
else
  run_as "${as:-$default_login}"
fi
