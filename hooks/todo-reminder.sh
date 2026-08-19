#!/bin/bash
# ~/.claude/hooks/todo-reminder.sh
# Stop hook —— session 結束時提醒待辦狀態。
#
# 2026-08-08 遷移：真相來源改為 sqlite，且輸出改為摘要。
#   舊版把全部條目（190 條 × 標題+🏷️+💡 ≈ 570 行）印出來，每次停止一次。
#   全量傾印本身就是問題的一部分 —— 看得到就會被當現況引用，
#   而條目裡的數字都是稽核當下的快照。
#
# 顯示三件事：總數、掛在自己名下沒收尾的（doing）、稽核新鮮度。

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_SCRIPT_DIR/lib/project-resolve.sh"

_resolve_project 2>/dev/null || exit 0

DB="$HOME/.claude/todos/.audit/${PROJECT}.sqlite"
[ -f "$DB" ] || exit 0

python3 - "$DB" "$PROJECT" << 'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / '.claude/skills/todo-audit/scripts'))
try:
    import todo_store
except Exception:
    sys.exit(0)

db, project = sys.argv[1:3]
try:
    con = todo_store.connect(db)
except Exception:
    sys.exit(0)

counts = dict(con.execute(
    'SELECT status, COUNT(*) FROM todo WHERE sort_order IS NOT NULL'
    ' GROUP BY status').fetchall())
pending, doing = counts.get('pending', 0), counts.get('doing', 0)
if pending + doing == 0:
    sys.exit(0)

p0 = con.execute(
    "SELECT COUNT(*) FROM todo WHERE sort_order IS NOT NULL"
    " AND section='urgent' AND status IN ('pending','doing')").fetchone()[0]
f = todo_store.freshness(con)

print()
print('┌─────────────────────────────────────────────────────┐')
print(f'│ 📋 [{project}] 待辦 {pending + doing} 項'
      f'（P0 {p0}）'.ljust(48) + '│')
print('└─────────────────────────────────────────────────────┘')

if doing:
    print()
    print(f'  ⏳ 進行中 {doing} 項 —— 沒收尾就會留給下個 session：')
    for sid, title, by in con.execute(
            "SELECT short_id, title, status_by FROM todo"
            " WHERE status='doing' ORDER BY sort_order"):
        print(f'     {sid} [{by or "?"}] {title[:56]}')

if f['last_run'] is None:
    print('\n  ⚠️ 從未稽核 —— 動工前務必先跑 audit')
elif f['stale']:
    print(f"\n  🔴 稽核已過期（{f['age_hours'] / 24:.0f} 天前）"
          f"· 標紅 {f['flagged']} 條")
else:
    print(f"\n  上次稽核 {f['age_hours']:.0f} 小時前 · 標紅 {f['flagged']} 條"
          f" · {f['unreviewed']} 條從未人工複驗")

print()
print('  → python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py list')
con.close()
PYEOF
