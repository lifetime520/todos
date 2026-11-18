#!/bin/bash
# ~/.claude/hooks/window-inbox.sh
# UserPromptSubmit —— 窗口被任何理由喚醒時，注入收件匣未讀並標為已讀（B 案，D-048，2026-11-17）。
#
# 為什麼：#13 被 peer 喚醒的 153 段中，「讀完沒回」占 18% cache read（原估「只回收到」13% 經逐則檢查是裁決轉達，不計）。
# peer 改把不需回應的回報寫收件匣（todo_cli inbox post），窗口醒來時一次看完，不另外喚醒。
# 同一個時機順帶提醒可疑認領（孤兒／停滯，D-086），每 24 小時最多一次。
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
# 可疑認領提醒（D-086）。提醒要跑 git，以 3 秒鬧鐘為上限（外層 timeout 5 秒）：
# 卡住時放棄提醒、照送收件匣，不能讓提醒拖垮收件匣。
remind = None
import signal
class _Late(Exception):  # 不可用 TimeoutError：它是 OSError，會被 git 掃描內的 except OSError 吞掉
    pass
def _give_up(*_):
    raise _Late
try:
    signal.signal(signal.SIGALRM, _give_up)
    signal.alarm(3)
    remind = coord_guards.suspect_claims_reminder(con)
except Exception:
    remind = None
finally:
    signal.alarm(0)
if remind:
    msg = f'{msg}\n\n{remind}' if msg else remind
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
