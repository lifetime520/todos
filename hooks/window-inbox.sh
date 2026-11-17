#!/bin/bash
# ~/.claude/hooks/window-inbox.sh
# UserPromptSubmit —— 窗口被任何理由喚醒時，注入收件匣未讀並標為已讀（B 案，D-048，2026-11-17）。
#
# 為什麼：#13 被 peer 喚醒的 153 段中，「讀完沒回」＋「只回收到」占 31% cache read。
# peer 改把不需回應的回報寫收件匣（todo_cli inbox post），窗口醒來時一次看完，不另外喚醒。
# 非窗口不注入、也不標已讀。任何錯誤都靜默放行。
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
    con = todo_store.connect(sys.argv[1])
    me = todo_coord.short_name(todo_coord.name_for_session_id(inp.get('session_id', '')) or '')
    win = todo_coord.window_name(con)
    if not me or me != win:
        sys.exit(0)
    items = todo_coord.inbox_take_unread(con, win, mark=False)
    msg = coord_guards.inbox_inject_text(items)
except Exception:
    sys.exit(0)
if msg:
    # 先送出再標已讀：中途被 timeout 打斷時，下次重複注入，不會遺失
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                             'additionalContext': msg}}, ensure_ascii=False), flush=True)
    try:
        todo_coord.inbox_mark_read(con, [i['id'] for i in items])
    except Exception:
        pass
PYEOF
exit 0
