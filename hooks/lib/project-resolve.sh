#!/bin/bash
# ~/.claude/hooks/lib/project-resolve.sh
# Resolves canonical {project} for todo system, with conflict detection.
#
# ⚠️ 2026-08-09 遷移後的現況：
#   仍在用   —— _resolve_project()：提供 PROJECT / PROJECT_PATH / GIT_REMOTE，
#                todo-add.sh / todo-done.sh / todo-startup.sh / todo-reminder.sh 都靠它。
#   已失效   —— TODO_FILE 指向的 {project}.md 已刪除（真相來源改為
#                .audit/{project}.sqlite）。因此 _verify_markers / _read_markers /
#                _make_marker_line / _print_conflict 這組 marker 函式**已無呼叫端**：
#                同名專案衝突偵測改由 todo_store.bind_project() 以 doc_meta 實作。
#                保留這些函式是為了不破壞可能的外部呼叫端；檔案不存在時
#                _read_markers 回 1、_verify_markers 回 0（視為無衝突），行為安全。
#
# Rules (Cast 2026-05-24 拍板):
#   1. PWD 必須是 repo root：有 .git 目錄（一般 checkout）或 .git 檔案（linked worktree）
#      → 否則 exit 1
#   2. PROJECT = basename(主 worktree root)。在 linked worktree 裡會反解回主 worktree，
#      不用 worktree 目錄自己的名字（否則同一專案會分裂出多個 todo 檔）
#   3. TODO file = $HOME/.claude/todos/{PROJECT}.md
#      首行格式：<!-- project_path: /abs/path | git_remote: <url> -->
#   4. 載入時若首行 marker 與當前 PWD/remote 不 match → 阻止 + 警告
#
# Usage:
#   source project-resolve.sh   # 然後 call functions
#   或 bash project-resolve.sh resolve   # 印 PROJECT + TODO_FILE
#   或 bash project-resolve.sh verify    # 驗證 marker，回 exit 0/2/3
#
# Functions:
#   _resolve_project        — sets PROJECT, TODO_FILE, PROJECT_PATH, GIT_REMOTE
#   _verify_markers         — 比對 todo file 首行 marker，回 0 (ok) / 2 (conflict) / 3 (missing marker)
#   _make_marker_line       — 印出標準 marker 首行
#   _print_conflict         — 印出衝突訊息（包含 saved vs current 對照）

# Resolve project context. Returns 0 on success (sets globals), 1 if not at a repo root.
#
# 兩種 root 都接受，但 PROJECT 一律解析成「主 worktree 的 basename」：
#   一般 checkout   → $PWD/.git 是目錄
#   linked worktree → $PWD/.git 是檔案（gitdir: 指標）
#
# 為什麼 worktree 不能直接用 basename $PWD：worktree 目錄叫 castpower-skill，
# 但專案是 tradingbot。直接用會寫出 ~/.claude/todos/castpower-skill.md ——
# 一個跟專案脫節的孤兒 todo 檔，比直接失敗更糟。所以反解回主 worktree 再取名。
#
# 仍然要求「在 root」（子目錄一律拒絕），否則 basename 會取到錯的名字。
_resolve_project() {
    local root git_common
    if [ -d "$PWD/.git" ]; then
        root="$PWD"
    elif [ -f "$PWD/.git" ]; then
        git_common=$(git rev-parse --git-common-dir 2>/dev/null) || return 1
        # --git-common-dir 可能回相對路徑，先絕對化再取父層
        case "$git_common" in /*) ;; *) git_common="$PWD/$git_common" ;; esac
        root=$(cd "$git_common/.." 2>/dev/null && pwd) || return 1
        [ -d "$root/.git" ] || return 1
    else
        return 1
    fi
    PROJECT=$(basename "$root")
    TODO_FILE="$HOME/.claude/todos/${PROJECT}.md"
    PROJECT_PATH="$root"
    GIT_REMOTE=$(git -C "$root" remote get-url origin 2>/dev/null || echo "(no-remote)")
    return 0
}

# Generate the canonical first-line marker for a fresh todo file.
_make_marker_line() {
    echo "<!-- project_path: ${PROJECT_PATH} | git_remote: ${GIT_REMOTE} -->"
}

# Read saved markers from existing todo file.
# Sets: SAVED_PATH, SAVED_REMOTE (empty if not found).
_read_markers() {
    SAVED_PATH=""
    SAVED_REMOTE=""
    if [ ! -f "$TODO_FILE" ]; then
        return 1
    fi
    local first_line
    first_line=$(head -1 "$TODO_FILE")
    # 用 # 當 sed delimiter（避免跟 marker 內的 | 衝突）
    SAVED_PATH=$(echo "$first_line" | sed -n 's#.*project_path: \([^|]*\) | git_remote:.*#\1#p' | sed 's/ *$//')
    SAVED_REMOTE=$(echo "$first_line" | sed -n 's#.*git_remote: \(.*\) -->.*#\1#p' | sed 's/ *$//')
    return 0
}

# Verify markers match current project.
# Returns: 0 = match, 2 = conflict (different project), 3 = file exists but no marker (legacy)
_verify_markers() {
    _read_markers || return 0   # no file = no conflict (caller decides next step)
    if [ -z "$SAVED_PATH" ] && [ -z "$SAVED_REMOTE" ]; then
        return 3   # legacy file without marker
    fi
    if [ -n "$SAVED_PATH" ] && [ "$SAVED_PATH" != "$PROJECT_PATH" ]; then
        return 2
    fi
    # Remote 比對只在兩邊都非空且當前有 remote 時才檢查
    if [ -n "$SAVED_REMOTE" ] && [ "$SAVED_REMOTE" != "(no-remote)" ] \
       && [ "$GIT_REMOTE" != "(no-remote)" ] && [ "$SAVED_REMOTE" != "$GIT_REMOTE" ]; then
        return 2
    fi
    return 0
}

# Print conflict diagnostic to stderr.
_print_conflict() {
    cat >&2 << EOF
⚠️ TODO 專案衝突偵測

檔案: $TODO_FILE
  標注 project_path: $SAVED_PATH
  標注 git_remote:   $SAVED_REMOTE
當前:
  PWD:              $PROJECT_PATH
  git remote:       $GIT_REMOTE

兩個專案重名但實際路徑/remote 不同 — 不能共用此 todo 檔。
請改名其中一個專案，或手動為當前 project 另設 todo 路徑。
EOF
}

# Print legacy-file warning (no marker).
_print_legacy_warning() {
    cat >&2 << EOF
⚠️ TODO 缺首行 marker（legacy 格式）

檔案: $TODO_FILE
建議補上首行：$(_make_marker_line)
（未補 marker 時暫時放行，但無法偵測未來同名專案衝突）
EOF
}

# Standalone CLI mode for debugging.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-resolve}" in
        resolve)
            if _resolve_project; then
                echo "PROJECT=$PROJECT"
                echo "TODO_FILE=$TODO_FILE"
                echo "PROJECT_PATH=$PROJECT_PATH"
                echo "GIT_REMOTE=$GIT_REMOTE"
            else
                echo "Not at a repo root (need .git dir or worktree .git file in PWD)" >&2
                exit 1
            fi
            ;;
        verify)
            _resolve_project || { echo "Not at git root" >&2; exit 1; }
            _verify_markers
            rc=$?
            case $rc in
                0) echo "OK: markers match" ;;
                2) _print_conflict ;;
                3) _print_legacy_warning ;;
            esac
            exit $rc
            ;;
        *)
            echo "Usage: $0 {resolve|verify}" >&2
            exit 1
            ;;
    esac
fi
