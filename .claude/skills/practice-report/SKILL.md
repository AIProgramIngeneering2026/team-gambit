---
name: practice-report
description: Подготовить и сдать отчёт команды teams/gambit/pN.md в Zadania по шаблону — ссылки на артефакты в main, «Вклад участников» из данных GitHub, проверка структуры как у бота, PR из своего fork с названием «pN — gambit». Вызов — /practice-report pN.
argument-hint: "pN"
---

# Отчёт $ARGUMENTS в Zadania

## 1. Кто сдаёт
Сдаёт участник `members[(N - 1) % 3]` из `.harness/config.toml`. Если это не ты (`gh api user --jq .login`), сдавай,
только когда до срока меньше `harness.report_fallback_hours` часов и PR отчёта ещё нет. Иначе остановись: отчёт сдаст другой.
PR уже есть (`python3 scripts/harness/status.py pN --json` → `report_prs`) — обновляй его ветку, новый не открывай.
Метка `rework` — прочитай комментарий преподавателя и исправь именно это.

## 2. Данные
```bash
python3 scripts/harness/zadania.py sync
python3 scripts/harness/zadania.py brief pN --json   # шаблон, разделы, чек-лист, путь отчёта
python3 scripts/harness/status.py pN --json          # PR, review, перекрёстное review, вопросы
python3 scripts/harness/status.py pN --contrib       # готовый раздел «Вклад участников»
```
Аудит чек-листа — из последнего комментария агента verifier в трекинг-issue практики или новый запуск verifier.

## 3. Текст
- Копируй шаблон `templates/pN.md` и заполни **каждый** раздел «## …» тем же заголовком. Комментарии `<!-- -->` из шаблона удали: бот их содержимым не считает.
- Ссылки на артефакты — полные ссылки на файлы в `main`: `https://github.com/<team.repo>/blob/main/<путь>`. Каждую проверь:
  `gh api repos/<team.repo>/contents/<путь>?ref=main --jq .path`. Нет файла в main — это не артефакт, а «Препятствие».
- «Вклад участников» — вывод `status.py --contrib` без правок по смыслу. У кого нет вклада — честно в «Препятствиях»: что произошло и что команда сделала.
- «Препятствия» — невыполненные пункты чек-листа, открытые вопросы и блокеры, перекрёстное review по запасной команде. Всё сделано — «нет».
- Разделы с таблицами (злодейства, карты колоды) — по фактическим тестам и документам в main, с путями до тестов.

## 4. Проверка как у бота
```bash
python3 scripts/harness/report_check.py <файл отчёта>
```
`FAIL` — исправь до PR.

## 5. PR из fork
Рабочую копию человека в Zadania не трогай — отдельное дерево:
```bash
Z=$(python3 -c "import sys; sys.path.insert(0,'scripts/harness'); from common import zadania_dir; print(zadania_dir())")
git -C "$Z" fetch origin
git -C "$Z" remote get-url fork || scripts/harness/bootstrap.sh
git -C "$Z" worktree add "$(pwd)/.harness/state/zadania-pN" -B pN-gambit origin/main
# скопируй отчёт в .harness/state/zadania-pN/teams/gambit/pN.md
git -C .harness/state/zadania-pN add teams/gambit/pN.md
git -C .harness/state/zadania-pN commit -m "pN — gambit"
git -C .harness/state/zadania-pN push -u fork pN-gambit
gh pr create -R AIProgramIngeneering2026/Zadania --base main --head <ACTOR>:pN-gambit --title "pN — gambit" --body-file <тело>
```
Тело — по `.github/PULL_REQUEST_TEMPLATE.md` Zadania: «Что сдаю: pN», оба пункта отмечены `[x]`, первая строка `<!-- harness -->`.
Через минуту: `gh pr checks <номер> -R AIProgramIngeneering2026/Zadania` и комментарий бота. Красное — исправь в той же ветке.

## 6. Итог
Ссылка на PR, итог проверки бота, чего не хватает по чек-листу и у кого нет личного вклада.
