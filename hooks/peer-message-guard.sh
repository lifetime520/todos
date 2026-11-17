#!/bin/bash
# ~/.claude/hooks/peer-message-guard.sh
# PreToolUse(SendMessage) —— 收件匣分流（B 案，D-048，2026-11-17）。
#   peer → 窗口：開頭沒有【問題／阻塞／交付／轉達／彙整／緊急】就擋下，提示改寫收件匣。
#   窗口 → peer：只回「收到／了解」這類純確認就擋下（會再喚醒對方一次）。
# 判斷邏輯在 coord_guards.peer_message_guard。任何錯誤都靜默放行。
INPUT="$(cat)"
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"
_resolve_project 2>/dev/null || exit 0
DB="$HOME/.claude/todos/.audit/${PROJECT}.sqlite"
[ -f "$DB" ] || exit 0

HOOK_INPUT="$INPUT" timeout 5 python3 - "$DB" <<'PYEOF' 2>/dev/null
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / '.claude/skills/todo-audit/scripts'))
try:
    import todo_store, todo_coord, coord_guards
    todo_store.BUSY_TIMEOUT_S = 2
    inp = json.loads(os.environ.get('HOOK_INPUT') or '{}')
    if inp.get('tool_name') != 'SendMessage':
        sys.exit(0)
    ti = inp.get('tool_input') or {}
    msg = ti.get('message')
    if not isinstance(msg, str):
        sys.exit(0)
    con = todo_store.connect(sys.argv[1])
    me = todo_coord.short_name(todo_coord.name_for_session_id(inp.get('session_id', '')) or '') or None
    out = coord_guards.peer_message_guard(me, todo_coord.resolve_target(ti.get('to')),
                                          todo_coord.window_name(con), msg)
except Exception:
    sys.exit(0)
if out:
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', **out}}, ensure_ascii=False))
PYEOF
exit 0
