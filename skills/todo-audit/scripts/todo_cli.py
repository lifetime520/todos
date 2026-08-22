#!/usr/bin/env python3
"""待辦 CLI —— DB 是真相來源，本檔是唯一官方入口。

為什麼不讓人／agent 直接讀儲存層：條目裡的行號與數字都是稽核當下的快照，
直接讀出來的內容看起來完整、格式漂亮，於是不會被質疑。本檔的每一條讀取
指令都強制在最前面印出新鮮度，並讓每個條目自帶 state 標籤，使過期資料
無法偽裝成現況。
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import todo_flags
import todo_store


def audit_dir():
    return Path(os.environ.get('HOME', Path.home())) / '.claude' / 'todos' / '.audit'


def db_for(project):
    return audit_dir() / f'{project}.sqlite'


def mirror_path(project):
    return (Path(os.environ.get('HOME', Path.home()))
            / '.claude' / 'todos' / f'{project}.view.md')


def header(con):
    f = todo_store.freshness(con)
    if f['last_run'] is None:
        return ('⚠️ 從未稽核 —— 本清單沒有任何新鮮度證據。\n'
                '   動工前請跑：todo_cli.py audit\n')
    age = f['age_hours']
    when = f'{age:.0f} 小時前' if age < 48 else f'{age/24:.0f} 天前'
    mark = '🔴' if f['stale'] else '⚠️'
    return (f"{mark} 上次稽核 {f['last_run'][:16]}（{when}）"
            f"· 標紅 {f['flagged']} 條 · 其中 {f['unreviewed']} 條從未人工複驗\n"
            f"   本輸出為稽核當下的觀察，動工前請重跑：todo_cli.py audit\n")


def rows(con, include_all=False, section=None, only_doing=False, by=None):
    q = ('SELECT short_id, key, raw_title, status, status_by, status_at,'
         ' progress, spec_path, memory_ref'
         ' FROM todo WHERE sort_order IS NOT NULL')
    p = []
    if not include_all:
        q += " AND status IN ('pending','doing')"
    if only_doing:
        q += " AND status='doing'"
    if by:
        q += ' AND status_by=?'
        p.append(by)
    if section:
        q += ' AND section=?'
        p.append(section)
    q += ' ORDER BY sort_order'
    return con.execute(q, p).fetchall()


def claim_tag(status, status_by, status_at):
    """認領資訊的顯示形式。pending 不印任何東西 —— 沒認領的條目
    多一段括號只是噪音；有認領的則必須同時帶「誰」與「多久前」，
    缺後者就分不出這是活著的 session 還是三天前的殘留。"""
    if not status or status == 'pending':
        return ''
    who = status_by or '(未署名)'
    return f' ({status} by {who} · {todo_store.humanize_age(status_at)})'


def body_of(con, key):
    return [t for (t,) in con.execute(
        'SELECT text FROM todo_line WHERE todo_key=? ORDER BY seq', (key,))]


_FLAG_LABELS = {'implemented': 'implemented', 'reviewed': 'reviewed',
               'committed': 'committed', 'compiled': 'compiled',
               'tested': 'tested', 'live_tested': 'live_tested',
               'deployed': 'deployed'}


def progress_bar(progress):
    p = progress or 0
    return ' '.join(
        f"{'✅' if todo_flags.has(p, name) else '⬜'}{_FLAG_LABELS[name]}"
        for name in todo_flags.ORDER)


def item_progress_lines(progress, spec_path, memory_ref, indent='  '):
    """進度視覺化＋非空 spec/memory 參照的顯示行，供 list/show/dump 三處共用
    ——避免三個指令各自重複同一段列印邏輯。"""
    lines = [f'{indent}進度：{progress_bar(progress)}']
    if spec_path:
        lines.append(f'{indent}spec: {spec_path}')
    if memory_ref:
        lines.append(f'{indent}memory: {memory_ref}')
    return lines


def cmd_list(con, args):
    print(header(con))
    for (sid, key, raw, status, sby, sat, progress, spec_path,
        memory_ref) in rows(con, section=args.section, only_doing=args.doing,
                            by=args.by):
        st = todo_store.state_of(con, key)
        print(f'  {sid}  [{st}]{claim_tag(status, sby, sat)} {raw[6:]}')
        for line in item_progress_lines(progress, spec_path, memory_ref,
                                        indent='      '):
            print(line)


def cmd_show(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    print(header(con))
    r = con.execute('SELECT short_id, raw_title, status, status_note,'
                    ' status_by, status_at, progress, spec_path, memory_ref'
                    ' FROM todo WHERE key=?', (key,)).fetchone()
    owner = (f'  by={r[4] or "(未署名)"} @ {r[5] or "時間不明"}'
             f'（{todo_store.humanize_age(r[5])}）'
             if r[2] and r[2] != 'pending' else '')
    print(f'  {r[0]}  [{todo_store.state_of(con, key)}]  status={r[2]}{owner}'
          + (f'  note={r[3]}' if r[3] else ''))
    print(f'  {r[1]}')
    for line in item_progress_lines(r[6], r[7], r[8], indent='  '):
        print(line)
    # 印出 seq —— edit --line / rm --line 要靠它指名，不顯示就沒法用
    for seq, text in con.execute(
            'SELECT seq, text FROM todo_line WHERE todo_key=? ORDER BY seq',
            (key,)):
        print(f'{seq:>3} {text}' if args.seq else text)


def cmd_dump(con, args):
    if args.format == 'json':
        out = []
        for (sid, key, raw, status, sby, sat, progress, spec_path,
            memory_ref) in rows(con, include_all=args.all,
                                section=args.section):
            out.append({'short_id': sid, 'key': key, 'title': raw[6:],
                        'status': status, 'status_by': sby, 'status_at': sat,
                        'state': todo_store.state_of(con, key),
                        'progress': todo_flags.summary(progress),
                        'spec_path': spec_path, 'memory_ref': memory_ref,
                        'body': body_of(con, key)})
        print(json.dumps({'freshness': todo_store.freshness(con), 'items': out},
                         ensure_ascii=False, indent=2))
        return
    print(header(con))
    for (sid, key, raw, status, sby, sat, progress, spec_path,
        memory_ref) in rows(con, include_all=args.all, section=args.section):
        st = todo_store.state_of(con, key)
        print(f'\n{sid} [{st}]{claim_tag(status, sby, sat)} {raw}')
        for line in item_progress_lines(progress, spec_path, memory_ref,
                                        indent='    '):
            print(line)
        for text in body_of(con, key):
            print(text)


def cmd_search(con, args):
    print(header(con))
    hits = todo_store.search(con, args.pattern, include_all=args.all)
    print(f'「{args.pattern}」命中 {len(hits)} 條：')
    for sid, key, raw, status in hits:
        st = todo_store.state_of(con, key)
        tag = '' if status == 'pending' else f' ({status})'
        print(f'  {sid}  [{st}]{tag} {raw[6:]}')


def cmd_note(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    seq = todo_store.append_note(con, key, args.text)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                      (key,)).fetchone()[0]
    print(f'{sid} 追加第 {seq} 行')


def cmd_edit(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    if (args.title is None and args.line is None
            and args.spec is None and args.memory is None):
        print('需指定 --title、--line N <text>、--spec 或 --memory',
              file=sys.stderr)
        return 5
    if args.title is not None:
        key = todo_store.edit_title(con, key, args.title)
    if args.line is not None:
        if args.text is None:
            print('--line 需搭配新內容（位置參數 text）', file=sys.stderr)
            return 5
        todo_store.edit_line(con, key, args.line, args.text)
    if args.spec is not None:
        todo_store.set_spec_path(con, key, args.spec)
    if args.memory is not None:
        todo_store.set_memory_ref(con, key, args.memory)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                      (key,)).fetchone()[0]
    print(f'{sid} 已更新')


def cmd_rm(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    sid, raw = con.execute('SELECT short_id, raw_title FROM todo WHERE key=?',
                           (key,)).fetchone()
    if args.line is not None:
        todo_store.remove_line(con, key, args.line)
        print(f'{sid} 已刪除第 {args.line} 行')
    else:
        # 刪整條是抹除，不是完成 —— 要求明示，避免手滑
        if not args.force:
            print(f'即將**永久刪除** {sid}：{raw[6:]}', file=sys.stderr)
            print('這是抹除不是完成。若只是做完了，用 todo-done.sh 或 '
                  'mark done（會保留記錄）。', file=sys.stderr)
            print('確定要刪請加 --force。', file=sys.stderr)
            return 5
        todo_store.remove_item(con, key)
        print(f'{sid} 已永久刪除')
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))


def cmd_stats(con, args):
    print(header(con))
    total = 0
    for k, v in con.execute(
            'SELECT status, COUNT(*) FROM todo WHERE sort_order IS NOT NULL'
            ' GROUP BY status'):
        print(f'  {k or "(null)":<10} {v}')
        total += v
    print(f'  {"total":<10} {total}')


def cmd_mark(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    todo_store.set_status(con, key, args.status, by=args.by, note=args.note,
                          force=args.force)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    # pending 時 by 已在 store 層被清掉，印出來會誤導成「仍掛在他名下」
    who = ('（已釋放認領）' if args.status == 'pending'
           else f'（by {args.by}）' if args.by else '')
    print(f'{args.ref} → {args.status}{who}')


def cmd_flag(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    new_progress = todo_store.set_progress(con, key, args.op, args.name)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                      (key,)).fetchone()[0]
    print(f'{sid} 進度 {args.op} {args.name} → {progress_bar(new_progress)}')


def cmd_add(con, args):
    # 重複提示（不阻擋）—— 沿用 todo-add.sh 原有行為。
    # 刻意設計成「提示但不阻擋」：高相似不等於重複，實測錨點共現滿分的
    # 兩條（preview 超時／dry-run 超時）其實是同一件事的兩半，都得做。
    # 自動合併或忽略會靜默吃掉待辦。
    if not args.no_similar:
        try:
            import contextlib
            import todo_audit
            # 提示一律走 stderr —— stdout 是 short_id 的專用通道，
            # 呼叫端（todo-add.sh 的使用者、腳本）靠它取回新條目編號。
            # 原 todo-add.sh:80 也是 >&2，這個約定不能在薄殼化時弄丟。
            with contextlib.redirect_stdout(sys.stderr):
                todo_audit.similar_mode(db_for(args.project_resolved),
                                        args.title, topn=5)
        except Exception as e:
            print(f'[add] (相似度檢查跳過：{e} — 不影響寫入)', file=sys.stderr)
    sid = todo_store.append_item(con, args.project_resolved,
                                 args.title, args.tags, args.note)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    print(sid)


def cmd_render(con, args):
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    print(mirror_path(args.project_resolved))


def cmd_similar(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    r = con.execute('SELECT short_id, title FROM todo WHERE key=?',
                    (key,)).fetchone()
    query = f"{r[1]} {' '.join(body_of(con, key))}"
    import todo_audit
    print(header(con))
    print(f'與 {r[0]} 相似的條目：')
    todo_audit.similar_mode(db_for(args.project_resolved), query,
                            topn=args.top, exclude_title=r[1])


def cmd_audit(con, args):
    import subprocess
    con.close()
    script = Path(__file__).resolve().parent / 'todo_audit.py'
    return subprocess.call([sys.executable, str(script),
                            str(db_for(args.project_resolved)), '.'])


def _resolve_ref_path(base, value):
    """spec_path 相對 repo root、memory_ref 相對 HOME 解析；
    絕對路徑原樣使用。"""
    p = Path(value)
    return p if p.is_absolute() else base / value


def cmd_doctor(con, args):
    """自我診斷（REQ-3）。逐行 OK/WARN/FAIL 前綴、純文字無顏色、可 grep，
    exit code 恆為 0 —— 診斷工具的職責是印出發現，不是自己跑不起來。

    `con` 可能是 None：main() 依 G-3（零寫入副作用）在觸碰
    `todo_store.connect()` 之前就把「DB 不存在」的案例分流到這裡 ——
    `connect()` 對不存在的路徑會直接建檔（executescript(BASE_SCHEMA)），
    這本身就是診斷動作不該有的副作用，所以連呼叫都不能發生。

    「repo root」用執行時的 cwd（與 cmd_audit 把 `'.'` 傳給
    todo_audit.py 的既有慣例一致），不是靠 `--path` 或往上找 marker。

    search_dirs 的來源層級直接消費 `todo_config.load_config()` 回傳的
    provenance（'builtin'/'user-global'/'per-repo'，逐字沿用，不翻譯、
    不自行重做三層探測 —— 兩份合併邏輯遲早分歧）。
    """
    import todo_audit
    import todo_config

    repo = Path.cwd()
    db = db_for(args.project_resolved)
    home = Path(os.environ.get('HOME', Path.home()))

    print(f'OK   project={args.project_resolved}  path={repo}')

    if con is None:
        print(f'FAIL DB 不存在：{db}')
        print(f'     請先建庫：python3 {Path(__file__).resolve()} init '
              f'--project {args.project_resolved}')
    else:
        print(f'OK   DB 存在：{db}')
        f = todo_store.freshness(con)
        if f['last_run'] is None:
            print('WARN 從未稽核 —— 動工前請跑：todo_cli.py audit')
        else:
            age = f['age_hours']
            when = f'{age:.0f} 小時前' if age < 48 else f'{age / 24:.0f} 天前'
            print(f'OK   上次稽核 {f["last_run"][:16]}（{when}）')
            row = con.execute(
                'SELECT degraded FROM run ORDER BY id DESC LIMIT 1').fetchone()
            if row and row[0]:
                print('WARN 上次稽核處於 WEAK_AUDIT 降級狀態 —— '
                      'search_dirs 零命中，死碼偵測（GONE 判定）當次失效')
            else:
                print('OK   上次稽核未處於降級狀態')

    if con is not None:
        for sid, spec_path, memory_ref in con.execute(
                "SELECT short_id, spec_path, memory_ref FROM todo"
                " WHERE sort_order IS NOT NULL"
                " AND (spec_path IS NOT NULL OR memory_ref IS NOT NULL)"):
            if spec_path and not _resolve_ref_path(repo, spec_path).exists():
                print(f'WARN {sid} 的 spec_path 指向不存在的檔案：{spec_path}')
            if memory_ref and not _resolve_ref_path(home, memory_ref).exists():
                print(f'WARN {sid} 的 memory_ref 指向不存在的檔案：{memory_ref}')

    defaults = {'search_dirs': list(todo_audit.SEARCH_DIRS),
               'scan_exts': list(todo_audit.SCAN_EXTS)}
    config, provenance, config_warnings = todo_config.load_config(repo, defaults, home=home)
    # Stage 7 第五輪：某一層 config 被拒絕（JSON 語法錯誤／型別錯誤）時，
    # 先前只印到 stderr——若 fallback 到的下一層剛好也命中檔案，下面的
    # search_dirs 分支會印出一行乾淨的 OK，使用者完全看不出 config 其實
    # 被拒絕過。這裡把每個被拒絕層的原因印在 OK/WARN 命中判定之前，
    # 用 doctor 既有的 WARN 前綴格式（可被同一套 PREFIX_RE 抓到）。
    for w in config_warnings:
        print(f'WARN config 層被忽略：{w}')
    files, _by_name = todo_audit.collect_source_files(repo, config)
    hit_count = len(files)
    source = provenance.get('search_dirs', 'builtin')
    # 判定規則（「未設定」vs「設定但零命中」）與 todo_audit.py 的降級警告
    # 共用同一份實作，不各自重做一份〔finding 3〕。
    diag = todo_config.zero_hit_diagnosis(repo, config, provenance, home=home)

    if hit_count == 0:
        print('WARN search_dirs 命中 0 個檔案 —— 死碼偵測（GONE 判定）將失效')
        if diag['configured']:
            print(f'     目前生效的 search_dirs = {diag["search_dirs"]}'
                  f'（來源：{diag["source"]}）')
            print(f'     請檢查設定內容：{diag["per_repo_path"]} 或 {diag["user_global_path"]}')
        else:
            print(f'     本專案未設定 search_dirs'
                  f'（找不到 {diag["per_repo_path"]} 或 {diag["user_global_path"]}）')
            print(f'     建立設定檔以縮小掃描範圍，例如 {diag["per_repo_path"]}：')
            print(f'     {json.dumps(diag["example"])}')
    else:
        # REQ-3 明文要求涵蓋「search_dirs 生效值與其來源層級」——
        # 命中分支先前只印數量與來源，沒印生效值本身，設好 config 之後
        # 使用者恰恰最需要確認「到底哪幾個目錄生效了」〔finding 2〕。
        print(f'OK   search_dirs 命中 {hit_count} 個檔案（來源：{source}，'
              f'生效值：{config["search_dirs"]}）')

    return 0


def cmd_init(con, args):
    """為新專案建立待辦庫。**唯一會建庫的指令。**

    建庫本身已由 main() 的 todo_store.connect(db) 完成 —— connect 是
    idempotent 的（CREATE TABLE IF NOT EXISTS ＋ 容錯 migration），
    故本函式只負責回報，不重複建表。

    為什麼需要這支指令（2026-08-20）：建庫能力原本只綁在
    migrate_md_to_db.py 裡，而那支對沒有舊 md 的專案輸出 "skip: no md"
    直接返回、不建庫 —— 於是全新專案永遠開不了第一條待辦，而錯誤訊息
    還指向那支不會建庫的腳本。

    為什麼不讓任何讀寫指令自動建庫：專案名打錯時會靜默生出一個空庫，
    把「打錯字」偽裝成「這個專案還沒有待辦」。這兩者的正確反應相反，
    而錯的那個不會有任何錯誤訊息。建庫必須是明確意圖。
    """
    db = db_for(args.project_resolved)

    if not args.db_existed:
        print(f'✅ 已建立 {db}')
        print(f'   專案：{args.project_resolved}')
        print('   下一步：bash ~/.claude/hooks/todo-add.sh "標題" '
              '"🏷️  tags" "💡  上次做到哪：X，下一步：Y"')
        return 0

    # ── TODO：DB 已存在時的行為（見下方說明，待決定）──
    # 目前先以「明確告知＋不改動」收尾，回傳 0。
    print(f'ℹ️  {db} 已存在，未做任何改動。')
    return 0


def main():
    # --project 同時掛在頂層與每個子指令上，讓兩種順序都能用：
    #   todo_cli.py --project x list   ／   todo_cli.py list --project x
    # 只掛頂層的話後者會被 argparse 判成 unrecognized arguments，
    # 而那正是最自然的打法。
    # ⚠️ default 一律用 SUPPRESS，不可用 None。
    # parents=[common] 會讓子 parser 也帶這些選項，而子 parser 解析時會用
    # 自己的 default 覆蓋頂層已解析到的值 —— 於是
    # `todo_cli.py --project X list`（選項在子指令前）會被洗成 None，
    # 靜默退回 cwd 名稱。兩個 helper 薄殼用的正是這個順序，
    # 只因為它們在對應 repo root 執行、cwd 名稱剛好相同才沒出事。
    # SUPPRESS 表示「沒給就別建這個屬性」，子 parser 便不會覆蓋。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', default=argparse.SUPPRESS)
    # 由 todo-add.sh / todo-done.sh 從 project-resolve.sh 取得後傳入。
    # 有給就驗專案綁定（md 首行 marker 的 DB 版），沒給就跳過 ——
    # 直接手打 CLI 的情境不強制，但經 helper 的寫入一律驗。
    common.add_argument('--path', default=argparse.SUPPRESS)
    common.add_argument('--remote', default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog='todo_cli.py', parents=[common])
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('list', parents=[common])
    p.add_argument('--section')
    p.add_argument('--doing', action='store_true')
    # 「session 結束前列出仍掛在自己名下的項目」要能真的查得出來，
    # 而不是查出全體 session 的 doing 讓人自己認
    p.add_argument('--by', help='只列這個認領者的條目')
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser('show', parents=[common])
    p.add_argument('ref')
    p.add_argument('--seq', action='store_true',
                   help='顯示 body 行序號（給 edit --line / rm --line 用）')
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser('dump', parents=[common])
    p.add_argument('--format', default='md', choices=['md', 'json'])
    p.add_argument('--all', action='store_true')
    p.add_argument('--section')
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser('search', parents=[common])
    p.add_argument('pattern')
    p.add_argument('--all', action='store_true')
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser('note', parents=[common])
    p.add_argument('ref')
    p.add_argument('text', help='需自帶 marker，如 "🔍  2026-08-09 複驗：…"')
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser('edit', parents=[common])
    p.add_argument('ref')
    p.add_argument('text', nargs='?', help='--line 時的新內容（需自帶 marker）')
    p.add_argument('--title', help='新標題（日期前綴自動保留）')
    p.add_argument('--line', type=int, help='要改的 body 行序號（見 show）')
    p.add_argument('--spec', help='規格文件路徑參照（只存字串，不驗證存在）')
    p.add_argument('--memory', help='auto memory 系統的相關檔案路徑參照')
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser('rm', parents=[common])
    p.add_argument('ref')
    p.add_argument('--line', type=int, help='只刪某一行；不給則刪整條')
    p.add_argument('--force', action='store_true', help='刪整條時必須明示')
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser('stats', parents=[common])
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser('mark', parents=[common])
    p.add_argument('ref')
    p.add_argument('status', choices=['pending', 'doing', 'done', 'unpick'])
    p.add_argument('--note')
    # 預設刻意留空：舊版 default='claude' 讓所有 session 同名，
    # 認領資訊看起來存在、實際上無法辨識任何人。doing 現在強制必填。
    p.add_argument('--by')
    p.add_argument('--force', action='store_true',
                   help='接管他人的 doing —— 先確認對方 session 真的結束了')
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser('flag', parents=[common])
    p.add_argument('ref')
    p.add_argument('op', choices=['set', 'clear', 'toggle'])
    p.add_argument('name', choices=list(todo_flags.FLAGS.keys()))
    p.set_defaults(fn=cmd_flag)

    p = sub.add_parser('add', parents=[common])
    p.add_argument('title')
    p.add_argument('tags')
    p.add_argument('note')
    p.add_argument('--no-similar', action='store_true')
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser('render', parents=[common])
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser('similar', parents=[common])
    p.add_argument('ref')
    p.add_argument('--top', type=int, default=10)
    p.set_defaults(fn=cmd_similar)

    p = sub.add_parser('audit', parents=[common])
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser('init', parents=[common],
                       help='為新專案建立待辦庫（唯一會建庫的指令）')
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser('doctor', parents=[common],
                       help='自我診斷：DB／search_dirs／稽核新鮮度是否正常'
                            '（唯讀，零寫入副作用，exit code 恆為 0）')
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args()
    # SUPPRESS 的選項沒給時屬性不存在，故一律用 getattr
    project = getattr(args, 'project', None) or Path.cwd().name
    args.project_resolved = project
    args.path = getattr(args, 'path', None)
    args.remote = getattr(args, 'remote', '(no-remote)')
    db = db_for(project)
    # 必須在 connect() 之前快照 —— connect 會把 DB 建出來，
    # 等 cmd_init 拿到 con 時「原本存不存在」已被自己的副作用抹掉。
    args.db_existed = db.exists()
    # doctor 是第二個豁免的指令，但豁免理由與 init 相反：init 要把
    # 「不存在」變成「存在」，doctor 只是要**報告**「不存在」，兩者都不能
    # 被下面這道「找不到 db 就攔下」的關卡擋住——差別在於 doctor 連
    # connect() 都不能碰（G-3：零寫入副作用，connect() 對不存在的路徑
    # 會直接建檔），所以必須在碰 connect() 之前就把它送進 cmd_doctor()，
    # 而不是走下面 init 共用的 `con = todo_store.connect(db)` 那條路。
    if args.cmd == 'doctor' and not args.db_existed:
        return cmd_doctor(None, args)
    # init 是唯一「自動建庫」的指令：它的職責就是把「不存在」變成「存在」。
    # 其餘指令一律擋下而非自動建庫 —— 理由見 cmd_init 的 docstring。
    if args.cmd != 'init' and not args.db_existed:
        print(f'找不到 {db}', file=sys.stderr)
        print(f'  新專案 → python3 {Path(__file__).resolve()} init', file=sys.stderr)
        print('  舊 md 遷移 → python3 migrate_md_to_db.py', file=sys.stderr)
        return 2
    if args.cmd == 'init':
        # sqlite3.connect 會建檔，但不會建父目錄 —— 首次安裝時 .audit/
        # 還不存在，直接 connect 會拋 OperationalError。
        # 只在 init 建目錄：放進 todo_store.connect() 會讓任何一次讀取都
        # 默默造出目錄，繞過「建庫必須是明確意圖」這條規則。
        db.parent.mkdir(parents=True, exist_ok=True)
    # doctor 的第三種豁免：DB 檔案存在，但不是合法 sqlite（寫入中斷、磁碟
    # 滿、被 `echo >` 誤覆蓋…）。connect() 對這種檔案會拋
    # sqlite3.DatabaseError（executescript(BASE_SCHEMA) 踩到壞檔），
    # 且這個例外不在下面 except 清單裡 —— 一旦漏接就直接 traceback + exit
    # 1，違反 REQ-3「exit code 恆為 0、不 traceback」。只在 doctor 這裡
    # 攔，是因為只有 doctor 的職責是「診斷並回報」，其他指令 DB 壞了本來
    # 就該讓使用者看到真正的失敗，不該被吞掉。
    if args.cmd == 'doctor':
        try:
            con = todo_store.connect(db)
        except sqlite3.DatabaseError as e:
            print(f'FAIL DB 檔案損毀（{e}）：{db}')
            print(f'     請確認檔案完整性，必要時重新 init：'
                  f'python3 {Path(__file__).resolve()} init '
                  f'--project {args.project_resolved}')
            return 0
    else:
        con = todo_store.connect(db)
    try:
        # doctor 的第四種豁免：bind_project() 檢查跳過。doctor 繼承了
        # common parser 的 --path/--remote，若使用者對一個已綁定到別處的
        # project 名跑 `doctor --path <另一路徑>`，bind_project() 會拋
        # ProjectBindingConflict，診斷還沒開始就被下面的 except 攔截、
        # exit=6、零診斷輸出——這正是 doctor 存在的理由要避免的失效模式：
        # 自我診斷工具不該因為別的安全檢查而自己先跑不起來（Stage 7 第三輪
        # 用戶裁決）。其餘指令仍照常走綁定檢查。
        if args.path and args.cmd != 'doctor':
            todo_store.bind_project(con, project, args.path, args.remote)
        rc = args.fn(con, args)
        return rc or 0
    except todo_store.ProjectBindingConflict as e:
        print(f'❌ {e}', file=sys.stderr)
        return 6
    except todo_store.ClaimConflict as e:
        print(f'🔒 {e}', file=sys.stderr)
        return 7
    except todo_store.AmbiguousRef as e:
        print(f'「{args.ref}」命中 {len(e.candidates)} 筆，請指名：', file=sys.stderr)
        for sid, key, title in e.candidates:
            print(f'  {sid}  {title[:60]}', file=sys.stderr)
        return 3
    except KeyError:
        print(f'查無條目：{args.ref}', file=sys.stderr)
        return 4
    except ValueError as e:
        print(e, file=sys.stderr)
        return 5
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
