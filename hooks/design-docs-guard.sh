#!/bin/bash
# ~/.claude/hooks/design-docs-guard.sh
# PreToolUse(Agent|SendMessage) —— 歸屬／記帳／資金費題先讀既有教訓；審查 prompt 不得鎖定前提。
# 2026-11-16 Cast 核准（#13 績效診斷）：S9b 歸屬設計被否決 5 輪，第 4 次糾正後才第一次讀
# position-attribution-model.md；共審 prompt 把文件明列的陷阱公式鎖成「不要建議推翻」。
# 判斷邏輯在 coord_guards.design_docs_guard。只在專案內有對應文件時生效；錯誤一律放行。
INPUT="$(cat)"
HOOK_INPUT="$INPUT" timeout 5 python3 - <<'PYEOF' 2>/dev/null
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / '.claude/skills/todo-audit/scripts'))
try:
    import coord_guards
    inp = json.loads(os.environ.get('HOOK_INPUT') or '{}')
except Exception:
    sys.exit(0)
tool = inp.get('tool_name', '')
ti = inp.get('tool_input') or {}
parts = [ti.get('description'), ti.get('prompt'), ti.get('summary')]
m = ti.get('message')
parts.append(m if isinstance(m, str) else json.dumps(m, ensure_ascii=False) if m else '')
label = f"{ti.get('description') or ''} {ti.get('subagent_type') or ''}"
out = coord_guards.design_docs_guard(tool, ' '.join(p for p in parts if p),
                                     inp.get('cwd') or os.getcwd(), review_label=label)
if out:
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', **out}}, ensure_ascii=False))
PYEOF
exit 0
