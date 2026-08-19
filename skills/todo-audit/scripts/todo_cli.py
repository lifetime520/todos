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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    q = ('SELECT short_id, key, raw_title, status, status_by, status_at'
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


def cmd_list(con, args):
    print(header(con))
    for sid, key, raw, status, sby, sat in rows(con, section=args.section,
                                                only_doing=args.doing,
                                                by=args.by):
        st = todo_store.state_of(con, key)
        print(f'  {sid}  [{st}]{claim_tag(status, sby, sat)} {raw[6:]}')


def cmd_show(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    print(header(con))
    r = con.execute('SELECT short_id, raw_title, status, status_note,'
                    ' status_by, status_at FROM todo WHERE key=?',
                    (key,)).fetchone()
    owner = (f'  by={r[4] or "(未署名)"} @ {r[5] or "時間不明"}'
             f'（{todo_store.humanize_age(r[5])}）'
             if r[2] and r[2] != 'pending' else '')
    print(f'  {r[0]}  [{todo_store.state_of(con, key)}]  status={r[2]}{owner}'
          + (f'  note={r[3]}' if r[3] else ''))
    print(f'  {r[1]}')
    # 印出 seq —— edit --line / rm --line 要靠它指名，不顯示就沒法用
    for seq, text in con.execute(
            'SELECT seq, text FROM todo_line WHERE todo_key=? ORDER BY seq',
            (key,)):
        print(f'{seq:>3} {text}' if args.seq else text)


def cmd_dump(con, args):
    if args.format == 'json':
        out = []
        for sid, key, raw, status, sby, sat in rows(con, include_all=args.all,
                                                    section=args.section):
            out.append({'short_id': sid, 'key': key, 'title': raw[6:],
                        'status': status, 'status_by': sby, 'status_at': sat,
                        'state': todo_store.state_of(con, key),
                        'body': body_of(con, key)})
        print(json.dumps({'freshness': todo_store.freshness(con), 'items': out},
                         ensure_ascii=False, indent=2))
        return
    print(header(con))
    for sid, key, raw, status, sby, sat in rows(con, include_all=args.all,
                                                section=args.section):
        st = todo_store.state_of(con, key)
        print(f'\n{sid} [{st}]{claim_tag(status, sby, sat)} {raw}')
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
    if args.title is None and args.line is None:
        print('需指定 --title 或 --line N <text>', file=sys.stderr)
        return 5
    if args.title is not None:
        key = todo_store.edit_title(con, key, args.title)
    if args.line is not None:
        if args.text is None:
            print('--line 需搭配新內容（位置參數 text）', file=sys.stderr)
            return 5
        todo_store.edit_line(con, key, args.line, args.text)
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

    args = ap.parse_args()
    # SUPPRESS 的選項沒給時屬性不存在，故一律用 getattr
    project = getattr(args, 'project', None) or Path.cwd().name
    args.project_resolved = project
    args.path = getattr(args, 'path', None)
    args.remote = getattr(args, 'remote', '(no-remote)')
    db = db_for(project)
    if not db.exists():
        print(f'找不到 {db} —— 先跑 migrate_md_to_db.py', file=sys.stderr)
        return 2
    con = todo_store.connect(db)
    try:
        if args.path:
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
