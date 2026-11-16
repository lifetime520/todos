#!/bin/bash
# ~/.claude/hooks/window-context-guard.sh
# UserPromptSubmit —— 窗口 context 超過門檻時提醒輪替（2026-11-16 Cast 核准，#13 績效診斷）。
#
# 為什麼：peer 訊息也會觸發 UserPromptSubmit，窗口每次被叫醒都重讀整個 context。
# #13 的 peer 喚醒段占 63.5% 呼叫，首呼叫平均 20.8 萬、峰值 40 萬 token。
# 只提示、不阻擋；同一 session 30 分鐘內最多提醒一次。任何錯誤都靜默放行。
INPUT="$(cat)"
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"
_resolve_project 2>/dev/null || exit 0
DB="$HOME/.claude/todos/.audit/${PROJECT}.sqlite"
[ -f "$DB" ] || exit 0

HOOK_INPUT="$INPUT" timeout 5 python3 - "$DB" <<'PYEOF' 2>/dev/null
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home() / '.claude/skills/todo-audit/scripts'))
try:
    import todo_store, todo_coord, coord_guards
    todo_store.BUSY_TIMEOUT_S = 2
    inp = json.loads(os.environ.get('HOOK_INPUT') or '{}')
    con = todo_store.connect(sys.argv[1])
except Exception:
    sys.exit(0)
sid = inp.get('session_id', '')
r = todo_coord.role_get(con, todo_coord.short_name(todo_coord.name_for_session_id(sid) or ''))
role = r['role'] if r else None
if role != 'window':
    sys.exit(0)
msg = coord_guards.context_guard(role, coord_guards.context_tokens(inp.get('transcript_path', '')))
if not msg:
    sys.exit(0)
mark = Path(sys.argv[1]).parent / f'.ctxguard-{sid}'
try:
    if time.time() - mark.stat().st_mtime < 1800:
        sys.exit(0)
except OSError:
    pass
mark.touch()
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                         'additionalContext': msg}}, ensure_ascii=False))
PYEOF
exit 0
