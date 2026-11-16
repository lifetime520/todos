#!/bin/bash
# ~/.claude/hooks/ask-window-guard.sh
# PreToolUse(AskUserQuestion) —— 協定 v2（2026-11-14，裁決 D-002）的提問提示。
#
# 為什麼：近 3 週 18 個 session 直接問 Cast 約 200 題，名義上的窗口（#13）只佔 5%。
# 各自提問導致 Cast 在不知情下給出互相矛盾的指示（§5.5-3 30 小時翻三次；
# D-006 vs D-005）。這個 hook 在提問前把「該送窗口」與「已有的裁決」放到模型眼前。
#
# 非窗口只提示、不阻擋：輸出 hookSpecificOutput.additionalContext，exit 恆為 0。
# 窗口本身一律擋下（2026-11-16 Cast 核准，#13 績效診斷）：AskUserQuestion 會讓窗口停擺，
# #13 實測 17 次合計 3302 分鐘、期間 26 則 peer 訊息排隊。判斷邏輯在 coord_guards.ask_guard。
# 任何錯誤（DB 鎖、找不到 session）都靜默放行。
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
except Exception:
    sys.exit(0)

me = todo_coord.name_for_session_id(inp.get('session_id', '')) or ''
sn = todo_coord.short_name(me)
r = todo_coord.role_get(con, sn) if sn else None
win = todo_coord.window_name(con)
qs = (inp.get('tool_input') or {}).get('questions') or []
text = ' '.join(f"{q.get('question','')} {q.get('header','')}" for q in qs if isinstance(q, dict))
try:
    hits = todo_coord.decision_match(con, text, limit=3)
except Exception:
    hits = []  # 裁決查詢失敗不影響窗口的 deny

deny = coord_guards.ask_guard(r['role'] if r else None)
if deny:
    for d in hits:
        q = d['cast_quote'] if len(d['cast_quote']) <= 80 else d['cast_quote'][:79] + '…'
        deny['permissionDecisionReason'] += f'\n相關現行裁決 {d["id"]}（{d["topic"]}）：{q}'
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', **deny}},
                     ensure_ascii=False))
    sys.exit(0)

lines = []
if win and sn != win:
    lines.append(f'【協定 v2】設計／決策問題（影響 spec、規則或其他題目）請 SendMessage 給 {win}，'
                 '不要直接問 Cast；只有 Cast 在本 session 親自下指令時，才能直接問自己負責、'
                 '不影響別人的執行細節。若屬後者可繼續。')
for d in hits:
    q = d['cast_quote'] if len(d['cast_quote']) <= 80 else d['cast_quote'][:79] + '…'
    lines.append(f'相關現行裁決 {d["id"]}（{d["topic"]}）：{q}')
if not lines:
    sys.exit(0)
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                         'additionalContext': '\n'.join(lines)[:800]}},
                 ensure_ascii=False))
PYEOF
exit 0
