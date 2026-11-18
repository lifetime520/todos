#!/bin/bash
# ~/.claude/hooks/stop-guards.sh
# Stop —— 回合結束守衛（2026-11-17 Cast 核准，D-047）。
#   本 session 有存活超過 2 小時的 Bash 子程序 → 擋一次並列出，同一 pid 只提醒一次。
#   （原「未來式承諾沒提問就擋」已於 2026-11-18 依 Cast 裁示全域撤掉：誤傷窗口，逼它一直向 Cast 提問。）
# 判斷邏輯在 skills/todo-audit/scripts/stop_guards.py（run_hook）。任何錯誤都靜默放行。
INPUT="$(cat)"
SCRIPTS="${STOP_GUARDS_SCRIPTS:-$HOME/.claude/skills/todo-audit/scripts}"
HOOK_INPUT="$INPUT" timeout 5 python3 - "$SCRIPTS" <<'PYEOF' 2>/dev/null
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    import stop_guards as g
    inp = json.loads(os.environ.get('HOOK_INPUT') or '{}')
    cpid = None if os.environ.get('STOP_GUARDS_NO_PROC') else g.find_claude_pid(os.getppid())
    out = g.run_hook(inp, cpid, Path.home() / '.claude' / 'stop-guards')
except Exception:
    sys.exit(0)
if out:
    print(json.dumps(out, ensure_ascii=False))
PYEOF
exit 0
