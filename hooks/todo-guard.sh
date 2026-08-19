#!/bin/bash
# PreToolUse hook —— 擋掉對 ~/.claude/todos/ 的一切直接存取。
#
# 判定的是「路徑出現在 Claude 的工具參數裡」，不是「行程讀了那個檔」。
# todo_cli.py 子行程內部的 sqlite3.connect() 不經本 hook，故官方入口天然放行。
#
# 為什麼要這道閘門（2026-08-08 事故）：
#   待辦條目內的數字與行號都是稽核當下的快照。直接 grep/Read 拿到的內容
#   看起來完整、格式漂亮，於是不會被質疑 —— 實測 188 條中 55 條已標紅、
#   52 條從未複驗，而照抄出去的清單裡就混著一條 PARTIAL_GONE。
#   官方入口一律附上新鮮度與逐條 state，讓過期資料自己帶著標籤出現。
#
# exit 0 = 放行；exit 2 = deny（stderr 內容回饋給 Claude）
set -uo pipefail

payload=$(cat)

# 快路徑：完全沒提到受保護目錄 → 放行。
#
# 比對必須錨定完整路徑，不可用 "todos" 做子字串比對（否則
# `grep -r todos ~/.claude/hooks/` 這種正當搜尋會被誤擋）。
# 但也不能只認帶尾斜線的形式 —— Grep 工具傳的 path 是
# `~/.claude/todos`（無斜線），只認 `todos/` 會讓整個目錄被 grep 繞過。
# 故收尾接受：斜線、引號、空白、或字串結束。
guarded_re="${HOME}/\.claude/todos(/|\"|'|[[:space:]]|$)"
if ! printf '%s' "$payload" | grep -qE "$guarded_re"; then
    exit 0
fi

# 提到了受保護目錄 —— 只有「官方入口作為命令主體」才放行。
#
# 注意這裡不能只做子字串比對：`cat ~/.claude/todos/x.md && todo_cli.py list`
# 同時含 guarded 路徑與官方 script 名，寬鬆比對會讓它整條溜過去。
# 故要求官方 script 出現在命令開頭（允許 python3/bash 前綴與路徑前綴），
# 且命令中不得出現 shell 串接。
official='todo_cli\.py|todo-add\.sh|todo-done\.sh|todo_audit\.py|migrate_md_to_db\.py'

cmd=$(printf '%s' "$payload" \
    | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\(.*\)".*/\1/p')

if [ -n "$cmd" ]; then
    # 有串接就不放行 —— 串接後面可以藏任何直接讀取
    if printf '%s' "$cmd" | grep -qE '&&|\|\||;|\||`|\$\('; then
        :
    elif printf '%s' "$cmd" \
        | grep -qE "^[[:space:]]*(python3|python|bash|sh)?[[:space:]]*[^[:space:]]*($official)"; then
        exit 0
    fi
fi

cat >&2 << EOF
🚫 ~/.claude/todos/ 已改由 todo_cli.py 管理，禁止直接存取。

原因：直接讀取繞過稽核新鮮度標註，會把過期快照當現況使用
（2026-08-08 實證：188 條中 55 條已標紅，其中 52 條從未複驗）。

改用：
  todo_cli.py list                 列出待辦（自帶新鮮度與每條 state）
  todo_cli.py show <T-NNN>         看單條全文
  todo_cli.py similar <T-NNN>      找相似條目
  todo_cli.py audit                重跑稽核取得新鮮證據
EOF
exit 2
