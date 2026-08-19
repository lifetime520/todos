#!/bin/bash
# ~/.claude/hooks/todo-done.sh
# 標記 todo 條目為完成。真相來源已遷至 sqlite（2026-08-08），本檔改為薄殼。
#
# ⚠️ 行為變更（唯一一項）：舊版是**把條目整塊刪掉**，完成記錄隨之消失。
#    現在改標 status='done'，條目留在 DB 裡，用 `todo_cli.py dump --all` 看得到。
#    鏡像 {project}.view.md 只列 pending/doing，所以肉眼看到的效果與舊版相同。
#
# 保留的語義（與舊版一致）：
#   - 必須在 repo root
#   - 同名專案衝突拒動
#   - 關鍵字必須**唯一命中**：0 命中拒動（可能已刪或打錯）；
#     >1 命中拒動並列出候選（避免一次誤標多條）
#
# Usage:
#   bash ~/.claude/hooks/todo-done.sh "關鍵字（標題行的一段）"
#   bash ~/.claude/hooks/todo-done.sh "T-042"          # 短碼也接受
#
# Exit codes（刻意沿用舊契約，內部由 todo_cli.py 的碼映射過來）：
#   0 — 成功
#   1 — 非 git repo root / 缺參數
#   2 — 同名 project 衝突（拒動）
#   3 — 關鍵字 0 或 >1 命中（拒動）

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"

KEYWORD="$1"

if [ -z "$KEYWORD" ]; then
    echo "Usage: $0 \"關鍵字（標題行的一段）\"" >&2
    exit 1
fi

if ! _resolve_project; then
    echo "❌ [todo-done] PWD ($PWD) 不是 repo root — 請 cd 到專案 root 或 worktree root" >&2
    exit 1
fi

python3 "$HOME/.claude/skills/todo-audit/scripts/todo_cli.py" \
    --project "$PROJECT" --path "$PROJECT_PATH" --remote "$GIT_REMOTE" \
    mark "$KEYWORD" done
rc=$?

# 映射回舊 exit code 契約：CLI 的 3(多筆)/4(0筆) 都是「關鍵字不唯一」→ 3；
# 6(專案綁定衝突) → 2。其餘原樣傳遞。
case $rc in
    3|4) exit 3 ;;
    6)   exit 2 ;;
    *)   exit $rc ;;
esac
