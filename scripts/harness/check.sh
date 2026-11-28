#!/usr/bin/env bash
# Обязательные проверки перед PR и перед сдачей практики. Запуск: scripts/harness/check.sh
# Каждая проверка включается, только если в репозитории есть то, что она проверяет.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

failed=0
run() {
  local name="$1"; shift
  echo "== $name"
  if "$@"; then echo "   ok"; else echo "   FAIL: $name"; failed=1; fi
}

run "тесты харнеса" python3 -m unittest discover -s scripts/harness/tests -q
run "ссылки в документах" python3 scripts/harness/fitness.py doc-links

if [ -f docs/api/openapi.yaml ]; then
  run "OpenAPI валиден" uvx --quiet --from openapi-spec-validator openapi-spec-validator docs/api/openapi.yaml
fi

if [ -f pyproject.toml ]; then
  run "ядро не зависит от фреймворка и БД" python3 scripts/harness/fitness.py core-imports
  if grep -q '\[tool.ruff' pyproject.toml; then run "ruff" uv run ruff check .; fi
  run "pytest с измерением покрытия" uv run coverage run -m pytest -q
  run "порог покрытия" uv run coverage report
fi

exit "$failed"
