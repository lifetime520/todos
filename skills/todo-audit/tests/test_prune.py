"""prune：依年齡自動清除「未開工」的 pending 條目（每週 cron 呼叫）。

判準刻意收窄 —— 誤刪是靜默資料遺失，所以只刪最沒爭議的一類：
status='pending' 且 progress 為 0（沒點過任何交付旗標）且日期早於門檻。
done／unpick 是歷史紀錄、doing 有人認領、點過旗標的 pending 已經開工，
一律不動。日期解析失敗的條目 date 會落成 1970-01-01，那是「未知」
不是「很舊」，也不動。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import todo_store

SAMPLE = """<!-- project_path: /x | git_remote: g -->

# demo Pending

## 一般

- [ ] [2026-07-01] 舊的未開工
  > 💡  a

- [ ] [2026-07-02] 舊的但點過旗標
  > 💡  b

- [ ] [2026-07-03] 舊的已完成
  > 💡  c

- [ ] [2026-07-04] 舊的已放棄
  > 💡  d

- [ ] [2026-07-05] 舊的認領中
  > 💡  e

- [ ] [2026-08-01] 剛好六十天
  > 💡  f

- [ ] [2026-09-20] 新的未開工
  > 💡  g

- [ ] 沒有日期前綴
  > 💡  h
"""

TODAY = '2026-09-30'   # 門檻 = 2026-08-01；早於它才刪


def run_cli(*args, env_home):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'todo_cli.py'), *args],
        capture_output=True, text=True,
        env={'HOME': str(env_home), 'PATH': '/usr/bin:/bin:/usr/local/bin'})


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        self.db = audit / 'demo.sqlite'
        con = todo_store.connect(self.db)
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        k = lambda ref: todo_store.resolve_ref(con, ref)
        todo_store.set_progress(con, k('舊的但點過旗標'), 'set', 'implemented')
        todo_store.set_status(con, k('舊的已完成'), 'done')
        todo_store.set_status(con, k('舊的已放棄'), 'unpick', note='測試')
        todo_store.set_status(con, k('舊的認領中'), 'doing', by='s1')
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def titles(self):
        con = todo_store.connect(self.db)
        try:
            return {t for (t,) in con.execute('SELECT title FROM todo')}
        finally:
            con.close()

    def prune(self, *extra):
        return run_cli('prune', '--project', 'demo', '--older-than-days', '60',
                       '--today', TODAY, *extra, env_home=self.home)

    def test_only_old_untouched_pending_is_removed(self):
        r = self.prune()
        self.assertEqual(r.returncode, 0, r.stderr)
        left = self.titles()
        self.assertNotIn('[2026-07-01] 舊的未開工', left)
        for kept in ('[2026-07-02] 舊的但點過旗標', '[2026-07-03] 舊的已完成',
                     '[2026-07-04] 舊的已放棄', '[2026-07-05] 舊的認領中',
                     '[2026-09-20] 新的未開工'):
            self.assertIn(kept, left)
        self.assertIn('舊的未開工', r.stdout)
        self.assertIn('1', r.stdout)

    def test_boundary_day_is_kept(self):
        # 「保留近兩個月」含門檻當日：date == today-60 不刪
        self.prune()
        self.assertIn('[2026-08-01] 剛好六十天', self.titles())

    def test_undated_item_is_never_pruned(self):
        # 解析失敗的 date=1970-01-01 是「未知」，不是「最舊」
        self.prune()
        self.assertIn('沒有日期前綴', self.titles())

    def test_dry_run_deletes_nothing(self):
        before = self.titles()
        r = self.prune('--dry-run')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('舊的未開工', r.stdout)
        self.assertEqual(self.titles(), before)

    def test_nothing_to_prune_is_success(self):
        self.prune()
        r = self.prune()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('0', r.stdout)

    def test_removes_dependency_edges(self):
        con = todo_store.connect(self.db)
        old = todo_store.resolve_ref(con, '舊的未開工')
        new = todo_store.resolve_ref(con, '新的未開工')
        con.execute("INSERT INTO todo_dep(from_key,to_key,kind) VALUES(?,?,'blocks')",
                    (old, new))
        con.commit()
        con.close()
        self.prune()
        con = todo_store.connect(self.db)
        n = con.execute('SELECT COUNT(*) FROM todo_dep WHERE from_key=? OR to_key=?',
                        (old, old)).fetchone()[0]
        con.close()
        self.assertEqual(n, 0)

    def test_reports_removed_dependency_edges(self):
        # 被刪的條目若擋著別人，對方會默默變成 ready —— 至少要在 log 裡留痕
        con = todo_store.connect(self.db)
        old = todo_store.resolve_ref(con, '舊的未開工')
        new = todo_store.resolve_ref(con, '新的未開工')
        con.execute("INSERT INTO todo_dep(from_key,to_key,kind) VALUES(?,?,'blocks')",
                    (old, new))
        con.commit()
        new_sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                              (new,)).fetchone()[0]
        con.close()
        r = self.prune()
        self.assertIn('解除依賴', r.stdout)
        self.assertIn(new_sid, r.stdout)

    def test_rejects_too_small_threshold(self):
        # TODO_PRUNE_DAYS 打錯（0、負數、6）會一次清光所有 pending 且無備份
        for bad in ('-1', '0', '29'):
            r = run_cli('prune', '--project', 'demo', '--older-than-days', bad,
                        '--today', TODAY, env_home=self.home)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertIn('[2026-07-01] 舊的未開工', self.titles())

    def test_no_toctou_between_select_and_delete(self):
        # 競態窗口：prune 選出候選之後、刪除之前，別的 session 把條目標成
        # doing。修正前 remove_item 只看 key 就刪 → 認領中的條目被抹除，
        # 對方毫無線索。修正後選取與刪除在同一個 BEGIN IMMEDIATE 內，
        # 對方的寫入會被鎖擋下（稍後重試會得到「查無條目」，序列化正確）。
        import argparse
        import todo_cli
        con = todo_store.connect(self.db)
        victim = todo_store.resolve_ref(con, '舊的未開工')
        outcome = {}

        class Proxy:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *a):
                cur = self._real.execute(sql, *a)
                if sql.lstrip().startswith('SELECT key, short_id') and not outcome:
                    # 先把結果 fetch 完再插入對方寫入：未讀完的 cursor 會持有
                    # SHARED 鎖擋住對方 commit，讓舊版也「剛好」通過（假綠）
                    rows = cur.fetchall()
                    cur = type('Rows', (), {'fetchall': lambda _s: rows})()
                    other = todo_store.sqlite3.connect(str(self._db), timeout=0.1)
                    try:
                        other.execute("UPDATE todo SET status='doing' WHERE key=?",
                                      (victim,))
                        other.commit()
                        outcome['claimed'] = True
                    except todo_store.sqlite3.OperationalError:
                        outcome['claimed'] = False
                    finally:
                        other.close()
                return cur

        proxy = Proxy(con)
        proxy._db = self.db
        args = argparse.Namespace(today=TODAY, older_than_days=60, dry_run=False,
                                  project_resolved='demo')
        env_home = todo_cli.os.environ.get('HOME')
        todo_cli.os.environ['HOME'] = str(self.home)
        try:
            todo_cli.cmd_prune(proxy, args)
        finally:
            todo_cli.os.environ['HOME'] = env_home
            con.close()
        con = todo_store.connect(self.db)
        still = con.execute('SELECT status FROM todo WHERE key=?', (victim,)).fetchone()
        con.close()
        self.assertIn('claimed', outcome)
        if outcome['claimed']:
            self.assertEqual(still, ('doing',), '認領成功的條目被 prune 抹除')
        else:
            self.assertIsNone(still)

if __name__ == '__main__':
    unittest.main()
