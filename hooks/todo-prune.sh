#!/bin/bash
# ~/.claude/hooks/todo-prune.sh
# 每週 cron：對所有專案刪除建立超過 N 天（預設 60）、未開工的 pending。
# 判準細節見 todo_cli.py cmd_prune()；這裡只負責逐專案呼叫。
#
# 跳過 *-pre-migrate-* 快照：那是遷移前的封存備份，不是活的待辦庫。
# 單一專案失敗不中斷其他專案，但整體 exit code 會反映失敗。
#
# Usage:
#   bash ~/.claude/hooks/todo-prune.sh [--dry-run]
#   TODO_PRUNE_DAYS=90 bash ~/.claude/hooks/todo-prune.sh

DAYS="${TODO_PRUNE_DAYS:-60}"
CLI="$HOME/.claude/skills/todo-audit/scripts/todo_cli.py"
rc=0

echo "=== $(date '+%F %T') todo-prune older-than=${DAYS}d $*"
for db in "$HOME"/.claude/todos/.audit/*.sqlite; do
    [ -e "$db" ] || continue
    project="$(basename "$db" .sqlite)"
    case "$project" in *-pre-migrate-*) continue ;; esac
    python3 "$CLI" --project "$project" prune --older-than-days "$DAYS" "$@" || rc=1
done
exit $rc
