#!/bin/bash
# ~/.claude/hooks/todo-add.sh
# 新增 todo 條目。真相來源已遷至 sqlite（2026-08-08），本檔改為薄殼。
#
# 呼叫介面刻意保持不變 —— 它寫死在 ~/.claude/CLAUDE.md 裡：
#   bash ~/.claude/hooks/todo-add.sh "任務描述"
#   bash ~/.claude/hooks/todo-add.sh "任務描述" "🏷️ keyword1, keyword2" "💡 上次做到哪..."
#
# 原檔的三個保護都保留，只是實作搬到 DB 層：
#   1. 必須在 repo root                    → _resolve_project（本檔）
#   2. 同名專案衝突拒寫（Cast 2026-05-24）  → todo_store.bind_project（DB 版 marker）
#   3. 相似度提示，提示但不阻擋              → todo_cli.py add 內部呼叫
#
# Exit codes:
#   0 — 成功
#   1 — 非 git repo root（拒寫）
#   6 — 同名 project 衝突（拒寫）
#   其他 — 見 todo_cli.py

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"

TASK="$1"
TAG_LINE="${2:-}"
RECALL_LINE="${3:-}"

if [ -z "$TASK" ]; then
    echo "Usage: $0 \"任務描述\" [\"🏷️ tag\"] [\"💡 recall\"]" >&2
    exit 1
fi

if ! _resolve_project; then
    echo "❌ [todo-add] PWD ($PWD) 不是 repo root — 不能建立 todo（請 cd 到專案 root 或 worktree root）" >&2
    exit 1
fi

exec python3 "$HOME/.claude/skills/todo-audit/scripts/todo_cli.py" \
    --project "$PROJECT" --path "$PROJECT_PATH" --remote "$GIT_REMOTE" \
    add "$TASK" "$TAG_LINE" "$RECALL_LINE"
