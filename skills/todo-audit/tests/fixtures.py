"""測試用的真實資料來源。

Phase 4（2026-08-09）刪掉 `~/.claude/todos/*.md` 之後，round-trip 與 parity
測試就找不到輸入了，會全部靜默 skip —— 測試報 OK 但什麼都沒驗，比紅燈更危險。

故改用 `.audit/{project}-pre-migrate-*.md` 這些遷移備份當 fixture：
它們是真實資料的快照、永久保存、且正是遷移當下驗證過 byte-identical 的那份。
"""
from pathlib import Path

AUDIT = Path.home() / '.claude' / 'todos' / '.audit'


def latest_snapshot(project):
    """回傳該專案最新一份 pre-migrate 備份；沒有則回 None。"""
    snaps = sorted(AUDIT.glob(f'{project}-pre-migrate-*.md'))
    return snaps[-1] if snaps else None


def all_projects():
    """從備份檔名反推有哪些專案。"""
    names = set()
    for p in AUDIT.glob('*-pre-migrate-*.md'):
        stem = p.name.rsplit('-pre-migrate-', 1)[0]
        if not stem.endswith('.view'):
            names.add(stem)
    return sorted(names)
