#!/usr/bin/env python3
"""一次性遷移：{project}.md → sqlite，並以 byte-diff 驗收。

驗收不過即 exit 1，且不刪除任何 md。
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import todo_store

TODOS = Path.home() / '.claude' / 'todos'
AUDIT = TODOS / '.audit'


def migrate(project):
    md = TODOS / f'{project}.md'
    if not md.exists():
        print(f'skip {project}: no md')
        return True
    original = md.read_text(encoding='utf-8')

    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = AUDIT / f'{project}-pre-migrate-{ts}.md'
    AUDIT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(md, backup)

    con = todo_store.connect(AUDIT / f'{project}.sqlite')
    parsed = todo_store.parse_md_lossless(original)
    todo_store.save_parsed(con, project, parsed)
    todo_store.assign_short_ids(con)
    rendered = todo_store.render(todo_store.load_parsed(con, project))
    con.close()

    if rendered != original:
        a, b = original.split('\n'), rendered.split('\n')
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else '<EOF>'
            y = b[i] if i < len(b) else '<EOF>'
            if x != y:
                print(f'❌ {project} 第 {i+1} 行不符\n  原始: {x!r}\n  渲染: {y!r}')
                break
        print(f'❌ {project} byte-diff 未通過 —— 遷移中止，md 未動。備份: {backup}')
        return False

    print(f'✅ {project}: {len(parsed["items"])} 條，byte-identical。備份: {backup.name}')
    return True


def purge(project):
    """Phase 4：刪除已遷移的 md 來源檔。

    **只在當下重新驗證 byte-identical 之後才刪。** 待辦檔是多 session 共寫的
    活檔（實測一小時內被改三次），若 md 有 DB 尚未收到的新條目就直接刪，
    那些條目會無聲消失。故 purge 一律先跑一次完整 migrate + 驗證。
    """
    md = TODOS / f'{project}.md'
    if not md.exists():
        print(f'skip {project}: md 已不存在')
        return True
    if not migrate(project):
        print(f'❌ {project}: 驗證未過 —— 拒絕刪除')
        return False
    # migrate 已在 .audit/ 留下 {project}-pre-migrate-{ts}.md 備份
    md.unlink()
    print(f'🗑️  {project}.md 已刪除（備份留在 .audit/）')
    return True


def discover_targets(todos_dir=None):
    """列出可遷移的專案名。

    排除 .view.md —— 那是 DB 產出的唯讀鏡像，不是來源。不排除的話
    glob 會把它當成名為 "{project}.view" 的專案，建出一個垃圾 DB，
    而且內容是自己的衍生物（實測踩過）。
    """
    d = Path(todos_dir) if todos_dir else TODOS
    return sorted(p.stem for p in d.glob('*.md')
                  if not p.name.endswith('.view.md'))


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    do_purge = '--purge' in sys.argv
    targets = args or discover_targets()
    fn = purge if do_purge else migrate
    ok = all(fn(t) for t in sorted(targets))
    sys.exit(0 if ok else 1)
