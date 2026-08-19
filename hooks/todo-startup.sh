#!/bin/bash
# ~/.claude/hooks/todo-startup.sh
# SessionStart hook —— 只注入摘要與新鮮度，不注入清單全文。
#
# 為什麼改（2026-08-08 事故）：
#   舊版把整份 todo（115KB）塞進 additionalContext。一開機就有現成清單
#   可抄，於是取證路徑反而比捷徑貴 —— 那天照抄出去的清單裡混著一條
#   PARTIAL_GONE，而全庫 55 條標紅、52 條從未複驗。
#   清單全文從 context 拿掉之後，要回答待辦就只能去跑指令，
#   而指令的輸出一律自帶新鮮度與逐條 state。
#
# 始終 exit 0（不阻斷 session）。

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"

_resolve_project 2>/dev/null || exit 0

DB="$HOME/.claude/todos/.audit/${PROJECT}.sqlite"
[ -f "$DB" ] || exit 0

python3 - "$DB" "$PROJECT" "$PROJECT_PATH" "$GIT_REMOTE" << 'PYEOF'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / '.claude/skills/todo-audit/scripts'))
try:
    import todo_store
except Exception:
    sys.exit(0)

db, project, cur_path, cur_remote = sys.argv[1:5]

try:
    con = todo_store.connect(db)
except Exception:
    sys.exit(0)

# 專案綁定衝突（md 首行 marker 的 DB 版）：同名專案在不同路徑時不載入
saved = dict(con.execute(
    "SELECT k, v FROM doc_meta WHERE project=? AND k IN"
    " ('project_path','git_remote')", (project,)).fetchall())
if saved.get('project_path') and saved['project_path'] != cur_path:
    print(json.dumps({"systemMessage":
        f"⚠️ TODO 衝突：{project} 已綁定 {saved['project_path']}，"
        f"當前 {cur_path}。兩個專案重名但路徑不同 —— 不載入待辦。"}))
    sys.exit(0)

counts = dict(con.execute(
    'SELECT status, COUNT(*) FROM todo WHERE sort_order IS NOT NULL'
    ' GROUP BY status').fetchall())
pending = counts.get('pending', 0)
doing = counts.get('doing', 0)
if pending + doing == 0:
    print(json.dumps({"systemMessage": f"[{project}] 無待辦事項"}))
    sys.exit(0)

p0 = con.execute(
    "SELECT COUNT(*) FROM todo WHERE sort_order IS NOT NULL"
    " AND section='urgent' AND status IN ('pending','doing')").fetchone()[0]
f = todo_store.freshness(con)

lines = [f"[{project}] 待辦 {pending + doing} 項（P0 {p0} 項）"]
if doing:
    owners = con.execute(
        "SELECT short_id, status_by FROM todo WHERE status='doing'"
        ' ORDER BY sort_order').fetchall()
    who = ', '.join(f'{s}@{b or "?"}' for s, b in owners)
    lines.append(f"進行中 {doing} 項：{who}"
                 "（可能掛在別的 session，動工前先確認）")

if f['last_run'] is None:
    lines.append("⚠️ 從未稽核 —— 沒有任何新鮮度證據")
else:
    age = f['age_hours']
    when = f'{age:.0f} 小時前' if age < 48 else f'{age / 24:.0f} 天前'
    mark = '🔴 稽核已過期' if f['stale'] else '上次稽核'
    lines.append(f"{mark} {f['last_run'][:16]}（{when}）· "
                 f"標紅 {f['flagged']} 條 · 其中 {f['unreviewed']} 條從未人工複驗")

summary = '\n'.join(lines)
ctx = (
    f"{summary}\n\n"
    "⚠️ 待辦清單全文不再注入 context，且 ~/.claude/todos/ 已由 hook 擋住直接讀取。\n"
    "要查待辦必須跑指令，輸出會自帶新鮮度與每條的 state：\n"
    "  python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py list [--section urgent] [--doing]\n"
    "  python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py show <T-NNN>\n"
    "  python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py audit   # 重跑稽核\n"
    "禁止憑記憶回答待辦內容 —— 條目裡的行號與數字都是稽核當下的快照。"
)

print(json.dumps({
    "systemMessage": f"[{project}] {pending + doing} 項待辦（P0 {p0}）",
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx
    }
}))
PYEOF
