#!/bin/bash
# ~/.claude/hooks/coord-board.sh
# SessionStart —— 注入「誰在做什麼」看板摘要（協定 v2，2026-11-14）。
# 為什麼：資金費案例中，轉達者與執行者都查不到對方在做同一題；看板讓每個
# session 一開工就看到存活 session 的角色、上下游與負責題目。
# 只注入、不阻擋；exit 恆為 0；上限 600 字。
cat >/dev/null
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"
_resolve_project 2>/dev/null || exit 0
DB="$HOME/.claude/todos/.audit/${PROJECT}.sqlite"
[ -f "$DB" ] || exit 0

timeout 5 python3 - "$DB" <<'PYEOF' 2>/dev/null
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / '.claude/skills/todo-audit/scripts'))
try:
    import todo_store, todo_coord
    todo_store.BUSY_TIMEOUT_S = 2
    con = todo_store.connect(sys.argv[1])
    if not con.execute('SELECT 1 FROM coord_role LIMIT 1').fetchone():
        sys.exit(0)
    text = todo_coord.board_text(con, max_chars=600)
except Exception:
    sys.exit(0)
text += '\n（查題目擁有者：todo_cli.py owner who <關鍵字>；查裁決：todo_cli.py decision list <關鍵字>）'
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                         'additionalContext': text}}, ensure_ascii=False))
PYEOF
exit 0
