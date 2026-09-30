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
#   bash ~/.claude/hooks/todo-done.sh "T-042" --by <認領者識別碼>
#
# --by：條目若已被 `mark <T> doing --by <誰>` 認領，必須帶**同一個**識別碼，
#   否則認領守衛會把本次標記當成「他人的 doing」擋下（exit 7）。2026-10-01 前
#   本檔根本不傳 --by，照規則先認領再收尾的條目一律標不掉。
#   未認領的條目不需要 --by，舊用法不變。
#
# Exit codes（刻意沿用舊契約，內部由 todo_cli.py 的碼映射過來）：
#   0 — 成功
#   1 — 非 git repo root / 缺參數
#   2 — 同名 project 衝突（拒動）
#   3 — 關鍵字 0 或 >1 命中（拒動）
#   7 — 條目被他人認領（或自己認領但沒帶 --by），由 todo_cli.py 原樣傳遞

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"

usage() {
    echo "Usage: $0 \"關鍵字（標題行的一段）\" [--by <認領者識別碼>]" >&2
    exit 1
}

KEYWORD="$1"
[ -n "$KEYWORD" ] || usage
shift

# 多餘或打錯的參數一律拒絕，不靜默忽略：`-by` 打錯若被吞掉，
# 會退化成「沒帶 --by」而被認領守衛擋下，錯誤訊息卻看不出是參數的問題。
BY_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --by)
            [ -n "${2:-}" ] || usage
            BY_ARGS=(--by "$2")
            shift 2 ;;
        *) usage ;;
    esac
done

if ! _resolve_project; then
    echo "❌ [todo-done] PWD ($PWD) 不是 repo root — 請 cd 到專案 root 或 worktree root" >&2
    exit 1
fi

python3 "$HOME/.claude/skills/todo-audit/scripts/todo_cli.py" \
    --project "$PROJECT" --path "$PROJECT_PATH" --remote "$GIT_REMOTE" \
    mark "$KEYWORD" done "${BY_ARGS[@]}"
rc=$?

# 映射回舊 exit code 契約：CLI 的 3(多筆)/4(0筆) 都是「關鍵字不唯一」→ 3；
# 6(專案綁定衝突) → 2。其餘原樣傳遞。
case $rc in
    3|4) exit 3 ;;
    6)   exit 2 ;;
    *)   exit $rc ;;
esac
