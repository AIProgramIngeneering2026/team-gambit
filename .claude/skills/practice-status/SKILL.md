---
name: practice-status
description: Показать состояние практики без изменений — срок, чек-лист, задачи, PR и review, личный вклад каждого, вопросы к команде, отчёт, что делать дальше каждому участнику. Вызов — /practice-status [pN].
argument-hint: "[pN]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 scripts/harness/*)
  - Bash(gh *)
---

# Статус практики

Только чтение: ничего не создавай и не меняй.

- Активные практики: !`python3 scripts/harness/zadania.py current 2>&1`

1. Практика — `$ARGUMENTS` или первая из активных.
2. `python3 scripts/harness/status.py pN` и `python3 scripts/harness/zadania.py brief pN`.
3. Последний комментарий `<!-- harness:audit -->` в трекинг-issue — аудит чек-листа, если есть.
4. Ответ — коротко:
   - срок и сколько осталось;
   - чек-лист `C1…Cn`: готово / в работе (PR) / не начато / под угрозой — со ссылками;
   - личный вклад: у кого нет влитого PR или содержательного review, у кого нет перекрёстного review (с p3);
   - что ждёт людей: открытые вопросы, `blocked`, `needs-human`, PR без review дольше 24 часов;
   - отчёт в Zadania: не открыт / открыт / метка;
   - следующий шаг для каждого участника одной строкой и команда (`/practice`, `/interview`, `/review-pr <N>`).
