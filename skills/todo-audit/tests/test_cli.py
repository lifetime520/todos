import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import todo_store
from test_store import SAMPLE


def run_cli(*args, env_home):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'todo_cli.py'), *args],
        capture_output=True, text=True,
        env={'HOME': str(env_home), 'PATH': '/usr/bin:/bin:/usr/local/bin'})


class TestReadCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_shows_short_ids_not_line_numbers(self):
        r = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)
        self.assertIn('第一條', r.stdout)

    def test_project_option_works_before_subcommand(self):
        # argparse parents 陷阱：子 parser 的 default 會覆蓋頂層解析到的值，
        # 讓這個順序靜默退回 cwd 名稱。兩個 helper 薄殼用的正是這個順序。
        r = run_cli('--project', 'demo', 'list', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)

    def test_wrong_project_before_subcommand_is_not_silently_ignored(self):
        # 給不存在的專案必須報錯，而不是靜默退回 cwd 的專案
        r = run_cli('--project', 'nosuchproject', 'list', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('nosuchproject', r.stdout + r.stderr)

    def test_every_output_carries_freshness_header(self):
        r = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertIn('稽核', r.stdout)

    def test_never_audited_is_reported_as_such(self):
        r = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertIn('從未稽核', r.stdout)

    def test_dump_tags_every_item_with_state(self):
        r = run_cli('dump', '--project', 'demo', env_home=self.home)
        # 從未稽核 ⇒ 每條都應標 NO_AUDIT，不得裸奔
        self.assertGreaterEqual(r.stdout.count('[NO_AUDIT]'), 2)

    def test_show_accepts_short_id(self):
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('第一條', r.stdout)
        self.assertIn('🔗', r.stdout)

    def test_show_ambiguous_substring_lists_candidates_not_first_match(self):
        r = run_cli('show', '第', '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        both = r.stdout + r.stderr
        self.assertIn('T-001', both)
        self.assertIn('T-002', both)

    def test_dump_json_includes_freshness_and_state(self):
        import json
        r = run_cli('dump', '--format', 'json', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIn('freshness', data)
        self.assertTrue(all('state' in i for i in data['items']))

    def test_unknown_ref_exits_nonzero(self):
        r = run_cli('show', 'T-999', '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)


class TestSearchAndNote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_matches_title(self):
        r = run_cli('search', '第一條', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)
        self.assertNotIn('T-002', r.stdout)

    def test_search_matches_body(self):
        # tagC 只出現在第二條的 body
        r = run_cli('search', 'tagC', '--project', 'demo', env_home=self.home)
        self.assertIn('T-002', r.stdout)
        self.assertNotIn('T-001', r.stdout)

    def test_search_is_case_insensitive(self):
        r = run_cli('search', 'TAGC', '--project', 'demo', env_home=self.home)
        self.assertIn('T-002', r.stdout)

    def test_search_no_hit_reports_zero(self):
        r = run_cli('search', 'zzz不存在zzz', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0)
        self.assertIn('0 條', r.stdout)

    def test_note_appends_to_body_end(self):
        r = run_cli('note', 'T-001', '🔍  2026-08-09 複驗：仍成立',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        show = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('🔍  2026-08-09 複驗：仍成立', show.stdout)
        # 必須在末尾，不可插隊
        lines = [l for l in show.stdout.split('\n') if l.startswith('  > ')]
        self.assertTrue(lines[-1].endswith('仍成立'))

    def test_note_marker_recognised(self):
        run_cli('note', 'T-001', '🔍  x', '--project', 'demo',
                env_home=self.home)
        con = todo_store.connect(
            self.home / '.claude/todos/.audit/demo.sqlite')
        markers = [m for (m,) in con.execute(
            "SELECT marker FROM todo_line WHERE todo_key="
            "(SELECT key FROM todo WHERE short_id='T-001') ORDER BY seq")]
        con.close()
        self.assertEqual(markers[-1], '🔍')


class TestEditRemoveCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_show_seq_displays_line_numbers(self):
        r = run_cli('show', 'T-001', '--seq', '--project', 'demo',
                    env_home=self.home)
        self.assertRegex(r.stdout, r'\n\s*0 \s*> ')

    def test_edit_title(self):
        r = run_cli('edit', 'T-001', '--title', '換了新標題',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        lst = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertIn('換了新標題', lst.stdout)
        self.assertNotIn('第一條', lst.stdout)

    def test_edit_line(self):
        r = run_cli('edit', 'T-001', '🏷️  換了 tag', '--line', '1',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        show = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('🏷️  換了 tag', show.stdout)

    def test_edit_without_target_errors(self):
        r = run_cli('edit', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)

    def test_rm_line(self):
        r = run_cli('rm', 'T-001', '--line', '0', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        show = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertNotIn('OrderService', show.stdout)

    def test_rm_item_requires_force(self):
        r = run_cli('rm', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--force', r.stdout + r.stderr)
        # 未加 --force 時不得真的刪掉
        lst = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertIn('T-001', lst.stdout)

    def test_rm_item_with_force(self):
        r = run_cli('rm', 'T-001', '--force', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        lst = run_cli('dump', '--all', '--project', 'demo', env_home=self.home)
        self.assertNotIn('T-001', lst.stdout)


class TestWriteCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        return todo_store.connect(
            self.home / '.claude' / 'todos' / '.audit' / 'demo.sqlite')

    def test_mark_doing_records_owner_and_time(self):
        r = run_cli('mark', 'T-001', 'doing', '--project', 'demo',
                    '--by', 'sess-a', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        con = self._db()
        row = con.execute("SELECT status, status_by, status_at FROM todo"
                          " WHERE short_id='T-001'").fetchone()
        con.close()
        self.assertEqual(row[0], 'doing')
        self.assertEqual(row[1], 'sess-a')
        self.assertIsNotNone(row[2])

    def test_unpick_requires_note(self):
        r = run_cli('mark', 'T-001', 'unpick', '--project', 'demo',
                    env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('note', (r.stdout + r.stderr).lower())

    def test_unpick_with_note_succeeds(self):
        r = run_cli('mark', 'T-001', 'unpick', '--note', '等上游決定',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_done_item_hidden_from_list_but_present_with_all(self):
        run_cli('mark', 'T-001', 'done', '--project', 'demo', env_home=self.home)
        plain = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertNotIn('T-001', plain.stdout)
        allout = run_cli('dump', '--all', '--project', 'demo', env_home=self.home)
        self.assertIn('T-001', allout.stdout)

    def test_mirror_regenerated_after_write(self):
        run_cli('mark', 'T-001', 'done', '--project', 'demo', env_home=self.home)
        mirror = self.home / '.claude' / 'todos' / 'demo.view.md'
        self.assertTrue(mirror.exists())
        text = mirror.read_text(encoding='utf-8')
        self.assertIn('GENERATED', text.split('\n')[0])
        self.assertNotIn('第一條', text)   # done 的不出現在鏡像

    def test_add_creates_item_and_returns_short_id(self):
        r = run_cli('add', '新任務標題', '🏷️  a, b', '💡  說明',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r'^T-\d{3}$')
        lst = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertIn('新任務標題', lst.stdout)

    def test_add_then_mark_done_keeps_history(self):
        # 舊 todo-done.sh 是刪行，完成記錄全丟；改標 done 後歷史留存
        add = run_cli('add', '短命任務', '🏷️  x', '💡  y',
                      '--project', 'demo', env_home=self.home)
        sid = add.stdout.strip()
        run_cli('mark', sid, 'done', '--project', 'demo', env_home=self.home)
        con = self._db()
        row = con.execute('SELECT status FROM todo WHERE short_id=?',
                          (sid,)).fetchone()
        con.close()
        self.assertEqual(row[0], 'done')


class TestClaimVisibility(unittest.TestCase):
    """認領資訊必須出現在讀取路徑上。

    status_by / status_at 一直有寫進 DB，但 list/show/dump 全部丟棄它 ——
    於是三天前掛著的死 session 認領，跟五分鐘前的認領長得一模一樣。
    存了卻不顯示，等價於沒存。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _claim(self, by='sess-a'):
        r = run_cli('mark', 'T-001', 'doing', '--by', by,
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_list_shows_owner_and_age(self):
        self._claim()
        out = run_cli('list', '--project', 'demo', env_home=self.home).stdout
        self.assertIn('sess-a', out)
        # 剛認領時是「剛剛」（<60 秒），不是「N 分鐘前」
        self.assertRegex(out, r'sess-a[^\n]*(剛剛|分鐘前|小時前|天前)')

    def test_show_shows_owner(self):
        self._claim()
        out = run_cli('show', 'T-001', '--project', 'demo',
                      env_home=self.home).stdout
        self.assertIn('sess-a', out)

    def test_dump_json_carries_owner_fields(self):
        self._claim()
        out = run_cli('dump', '--format', 'json', '--project', 'demo',
                      env_home=self.home).stdout
        item = next(i for i in json.loads(out)['items']
                    if i['short_id'] == 'T-001')
        self.assertEqual(item['status_by'], 'sess-a')
        self.assertIsNotNone(item['status_at'])

    def test_pending_item_shows_no_owner_noise(self):
        out = run_cli('list', '--project', 'demo', env_home=self.home).stdout
        self.assertNotIn('by ', out)

    def test_doing_without_by_is_rejected(self):
        r = run_cli('mark', 'T-001', 'doing', '--project', 'demo',
                    env_home=self.home)
        # 斷言具體 exit code：argparse 參數錯也是非零，只驗 !=0 會讓
        # 「守衛根本沒跑到」的情況矇混過關（本次實測踩過）
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn('--by', r.stdout + r.stderr)

    def test_conflicting_claim_is_rejected_with_owner_in_message(self):
        self._claim('sess-a')
        r = run_cli('mark', 'T-001', 'doing', '--by', 'sess-b',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn('sess-a', out)
        # 訊息要報使用者認得的編號，不是內部 key 的前 8 碼
        self.assertIn('T-001', out)

    def test_force_takes_over(self):
        self._claim('sess-a')
        r = run_cli('mark', 'T-001', 'doing', '--by', 'sess-b', '--force',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = run_cli('list', '--project', 'demo', env_home=self.home).stdout
        self.assertIn('sess-b', out)

    def test_done_by_other_session_is_rejected(self):
        self._claim('sess-a')
        r = run_cli('mark', 'T-001', 'done', '--by', 'sess-b',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)

    def test_list_doing_filters_by_owner(self):
        # 「Session 結束前列出仍掛在自己名下的項目」要能真的查得出來
        self._claim('sess-a')
        run_cli('mark', 'T-002', 'doing', '--by', 'sess-b',
                '--project', 'demo', env_home=self.home)
        out = run_cli('list', '--doing', '--by', 'sess-a',
                      '--project', 'demo', env_home=self.home).stdout
        self.assertIn('T-001', out)
        self.assertNotIn('T-002', out)


if __name__ == '__main__':
    unittest.main()
