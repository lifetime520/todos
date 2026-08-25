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


class TestInit(unittest.TestCase):
    """init 是唯一會建庫的指令（2026-08-20）。

    在此之前建庫能力只綁在 migrate_md_to_db.py 裡，而那支對沒有舊 md 的
    專案直接 skip 不建庫 —— 全新專案永遠開不了第一條待辦。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # 刻意不建 .claude/todos/.audit —— 首次安裝時它並不存在，
        # 而 sqlite3.connect 只建檔不建父目錄。開發者的機器上這個目錄
        # 早就存在，所以這個缺陷只有在乾淨 HOME 才會顯形。
        self.audit = self.home / '.claude' / 'todos' / '.audit'

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_db_when_audit_dir_does_not_exist(self):
        self.assertFalse(self.audit.exists())
        r = run_cli('init', '--project', 'fresh', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.audit / 'fresh.sqlite').exists())

    def test_init_is_idempotent(self):
        run_cli('init', '--project', 'fresh', env_home=self.home)
        before = (self.audit / 'fresh.sqlite').read_bytes()
        r = run_cli('init', '--project', 'fresh', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.audit / 'fresh.sqlite').read_bytes(), before,
                         'init 對既有 DB 不得有任何位元改動')

    def test_missing_db_points_at_init_not_the_migration_script(self):
        # 舊訊息叫人去跑 migrate_md_to_db.py，但那支對新專案不建庫 ——
        # 錯誤訊息把人導向一條走不通的路，比沒有訊息更糟。
        r = run_cli('list', '--project', 'fresh', env_home=self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn('init', r.stderr)

    def test_non_init_commands_never_create_the_db(self):
        # 打錯專案名時靜默建出空庫，會讓「打錯字」偽裝成「沒有待辦」。
        for cmd in ('list', 'stats', 'dump'):
            with self.subTest(cmd=cmd):
                run_cli(cmd, '--project', 'typo-project', env_home=self.home)
                self.assertFalse((self.audit / 'typo-project.sqlite').exists())

    def test_add_works_after_init(self):
        run_cli('init', '--project', 'fresh', env_home=self.home)
        r = run_cli('add', '新條目', '🏷️  t', '💡  n',
                    '--project', 'fresh', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = run_cli('list', '--project', 'fresh', env_home=self.home).stdout
        self.assertIn('新條目', out)

    def test_init_only_touches_its_own_project(self):
        run_cli('init', '--project', 'alpha', env_home=self.home)
        run_cli('init', '--project', 'beta', env_home=self.home)
        self.assertTrue((self.audit / 'alpha.sqlite').exists())
        self.assertTrue((self.audit / 'beta.sqlite').exists())
        self.assertEqual(len(list(self.audit.glob('*.sqlite'))), 2)


class TestSimilarWithMixedLineOrigins(unittest.TestCase):
    """similar 的排序 tuple 帶著 md_line，而該欄位有兩種來源：

    md 遷移來的條目是整數行號，append_item() 建的條目是 NULL（它的 INSERT
    根本沒列 md_line）。兩條同分時 tuple 比較就會拿 int 去比 None 而炸。
    兩條都 NULL 反而不炸 —— tuple 先用 == 比，None == None 為真就跳過去了，
    所以必須新舊混合才重現得出來。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')

        # 三條共用同一個錨點，彼此都能配對上。A / B 標題逐字相同，
        # 因此 score / anchor_j / text_j 三個數值欄位必然相等，
        # 排序只能往下比第四欄 md_line —— 正是本測試要釘住的那一格。
        rows = [
            ('k-query', '查詢對象 BtseOkHttpClient.java 排查', 7),
            ('k-old', '撤單失敗 BtseOkHttpClient.java 修正', 42),
            ('k-new', '撤單失敗 BtseOkHttpClient.java 修正', None),
        ]
        for i, (key, title, md_line) in enumerate(rows):
            full = f'[2026-08-16] {title}'
            con.execute(
                'INSERT INTO todo(key,date,title,raw_title,section,'
                'sort_order,status,md_line) VALUES(?,?,?,?,?,?,?,?)',
                (key, '2026-08-16', full, f'- [ ] {full}', 'normal',
                 i + 1, 'pending', md_line))
        con.commit()
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_similar_survives_null_and_int_line_numbers_at_equal_score(self):
        r = run_cli('similar', 'k-query', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('Traceback', r.stderr)
        self.assertIn('撤單失敗', r.stdout)

    def test_similar_does_not_print_LNone_for_db_native_items(self):
        # 印 LNone 等於把人導向一個不存在的行號。DB 原生條目沒有 md 行號
        # 是常態，不是異常。
        r = run_cli('similar', 'k-query', '--project', 'demo',
                    env_home=self.home)
        self.assertNotIn('LNone', r.stdout)



class TestProgressFlagCommand(unittest.TestCase):
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

    def test_flag_set_then_show_reflects_progress(self):
        r = run_cli('flag', 'T-001', 'set', 'implemented',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('✅implemented', r.stdout)
        self.assertIn('進度', r.stdout)

    def test_flag_unknown_name_rejected(self):
        r = run_cli('flag', 'T-001', 'set', 'not_a_flag',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)

    def test_flag_clear_and_toggle(self):
        run_cli('flag', 'T-001', 'set', 'reviewed',
                '--project', 'demo', env_home=self.home)
        r = run_cli('flag', 'T-001', 'clear', 'reviewed',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('flag', 'T-001', 'toggle', 'committed',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_completing_all_flags_shows_done_status_in_list(self):
        for name in ('implemented', 'reviewed', 'committed', 'compiled',
                    'tested', 'live_tested', 'deployed'):
            run_cli('flag', 'T-001', 'set', name,
                    '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('status=done', r.stdout)

    def test_completing_all_flags_announces_auto_done_in_flag_stdout(self):
        # I-4：set_progress() 補滿七旗標時會靜默把 status 轉成 done——
        # cmd_flag 必須在觸發那次呼叫的 stdout 上公告，而不是要人再跑一次
        # show 才看得到，否則條目會無聲從 list 消失。
        names = ('implemented', 'reviewed', 'committed', 'compiled',
                'tested', 'live_tested', 'deployed')
        for name in names[:-1]:
            r = run_cli('flag', 'T-001', 'set', name,
                        '--project', 'demo', env_home=self.home)
            self.assertNotIn('自動轉為 done', r.stdout)
        r = run_cli('flag', 'T-001', 'set', names[-1],
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('自動轉為 done', r.stdout)

    def test_edit_spec_and_memory_ref_show_in_show_output(self):
        r = run_cli('edit', 'T-001', '--spec', 'docs/specs/x.md',
                    '--memory', 'memory/y.md',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('docs/specs/x.md', r.stdout)
        self.assertIn('memory/y.md', r.stdout)

    def test_list_never_shows_progress_or_refs(self):
        # list 是掃視用的清單，進度/spec/memory 屬於「要細節時才看」的
        # 資訊（show/dump 已提供）——即使條目確實設了旗標與參照，list
        # 也不該印出來，不然每條都多一行洗版。
        run_cli('flag', 'T-001', 'set', 'implemented',
                '--project', 'demo', env_home=self.home)
        run_cli('edit', 'T-001', '--spec', 'docs/specs/x.md',
                '--memory', 'memory/y.md',
                '--project', 'demo', env_home=self.home)
        r = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('進度', r.stdout)
        self.assertNotIn('spec:', r.stdout)
        self.assertNotIn('memory:', r.stdout)

    def test_dump_json_includes_progress_and_refs(self):
        run_cli('flag', 'T-001', 'set', 'implemented',
                '--project', 'demo', env_home=self.home)
        run_cli('edit', 'T-001', '--spec', 'docs/specs/x.md',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dump', '--format', 'json', '--all',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        item = next(i for i in data['items'] if i['short_id'] == 'T-001')
        self.assertEqual(item['progress'], ['implemented'])
        self.assertEqual(item['spec_path'], 'docs/specs/x.md')


class TestDepCommand(unittest.TestCase):
    """`dep add/rm/list` 與 `list --ready` 的 CLI 層行為。

    這一層要驗的是子指令的 exit code 與 stdout/stderr 內容，不是重測
    todo_store.add_dep/remove_dep/list_deps/ready_keys 本身的邏輯
    （那些已在 test_store.py 覆蓋）。
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

    # ---- REQ-1: dep add/rm/list CLI 介面 ----

    # REQ-1
    def test_dep_add_then_list_shows_relation(self):
        r = run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)
        self.assertIn('blocks', r.stdout)
        self.assertIn('T-002', r.stdout)

    # REQ-1: dep list 必須雙向可查 —— 被依賴那一端（to_ref）也要能看到關係，
    # 不是只有發起依賴那一端（from_ref）才看得到。cmd_dep 的 in/out 分支
    # 是 CLI 層才有的格式化邏輯，值得獨立驗證。
    def test_dep_list_shows_incoming_direction_on_target(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'list', 'T-002', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)
        self.assertIn('blocks', r.stdout)
        self.assertIn('T-002', r.stdout)

    # REQ-1：非 0 exit code 必須是「因為 bogus 這個值不合法」，不能只是
    # 剛好因為別的理由而非 0——單獨斷言 != 0 對還沒有 dep 子指令的當下
    # 也會巧合通過（argparse 對 'dep' 本身就會報 invalid choice），
    # 所以額外釘住錯誤內容要指名是 bogus。
    def test_dep_add_unknown_kind_rejected(self):
        r = run_cli('dep', 'add', 'T-001', 'bogus', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('bogus', r.stdout + r.stderr)

    # REQ-1：未知 kind 不只要回非 0，還不能悄悄把邊寫進去。
    def test_dep_add_unknown_kind_does_not_write(self):
        run_cli('dep', 'add', 'T-001', 'bogus', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('沒有任何依賴關係', r.stdout)

    # REQ-1
    def test_dep_rm_removes_relation(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'rm', 'T-001', 'blocks', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('沒有任何依賴關係', r.stdout)

    # REQ-1：刪除不存在的邊是錯誤，不是靜默成功（比照 rm --line 的既有慣例）。
    # 光斷言 != 0 在 dep 子指令還不存在時也會巧合通過，所以另外造一條
    # 真實存在的 related 邊當對照組——失敗的刪除不該連帶影響它。
    def test_dep_rm_nonexistent_edge_rejected(self):
        run_cli('dep', 'add', 'T-001', 'related', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'rm', 'T-001', 'blocks', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        show = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('related', show.stdout)

    # REQ-1（G-5 裁決）：重複新增同一條邊（from/to/kind 皆相同）視為錯誤，
    # 不得靜默吞掉——否則打錯 kind 名稱後補一次正確呼叫，會被誤判成
    # 「這次也失敗了」。
    # 光斷言 != 0 在 dep 子指令還不存在時也會巧合通過，所以額外驗證
    # dep list 只列出一條邊，不是被重複插入兩條。
    def test_dep_add_duplicate_edge_rejected(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        show = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(show.stdout.count('blocks'), 1)

    # REQ-1：from_ref 查無條目時，CLI 層要報錯而不是把不存在的 ref 當成
    # 有效 key 寫進 todo_dep（那會留下指向不存在條目的孤兒邊）。
    # 光斷言 != 0 在 dep 子指令還不存在時也會巧合通過，所以額外驗證
    # T-001（存在的那一端）沒有被留下任何孤兒邊。
    def test_dep_add_missing_from_ref_errors(self):
        r = run_cli('dep', 'add', 'T-999', 'blocks', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        show = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('沒有任何依賴關係', show.stdout)

    # REQ-1：to_ref 查無條目時同上。
    def test_dep_add_missing_to_ref_errors(self):
        r = run_cli('dep', 'add', 'T-001', 'blocks', 'T-999',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        show = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('沒有任何依賴關係', show.stdout)

    # ---- REQ-2: CLI 層環狀依賴拒絕 ----

    # REQ-2
    def test_dep_add_cycle_rejected_with_message(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'add', 'T-002', 'blocks', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('環', r.stderr)

    # REQ-2：驗收判準明講「至少 A、B 兩個 short_id」，不是空泛的「有環」。
    def test_dep_add_cycle_message_contains_both_short_ids(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'add', 'T-002', 'blocks', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertIn('T-001', r.stderr)
        self.assertIn('T-002', r.stderr)

    # REQ-2：自我依賴（A blocks A）是環的最小情況（長度 1 的環路徑），
    # 即使沒有任何既有邊也該被擋下——這是前兩個 task 的收件複審點名
    # 「自我依賴邊界」容易被 brief 建議測試漏掉的地方。
    def test_dep_add_self_blocks_dependency_rejected(self):
        r = run_cli('dep', 'add', 'T-001', 'blocks', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('環', r.stderr)

    # REQ-2：related 明文排除在環檢測之外，A related B 後 B related A
    # 必須成功，不能被誤判成環。
    def test_dep_add_related_reverse_not_treated_as_cycle(self):
        r = run_cli('dep', 'add', 'T-001', 'related', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'add', 'T-002', 'related', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    # REQ-2：discovered-from 同樣排除在環檢測之外，requirements.md 原文
    # 明講 related／discovered-from 兩者都不做此檢查，只測 related 會漏掉
    # discovered-from 若被誤接進環檢測的情況。
    def test_dep_add_discovered_from_reverse_not_treated_as_cycle(self):
        r = run_cli('dep', 'add', 'T-001', 'discovered-from', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'add', 'T-002', 'discovered-from', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    # REQ-2：parent-child 與 blocks 同樣要做環檢測，不是只有 blocks。
    def test_dep_add_parent_child_cycle_also_rejected(self):
        run_cli('dep', 'add', 'T-001', 'parent-child', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'add', 'T-002', 'parent-child', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('環', r.stderr)

    # REQ-2（G-3 裁決）：blocks 與 parent-child 分開跑環檢測，不合併成
    # 同一張圖。T-001 是 T-002 的 parent，T-002 又 blocks T-001（子任務
    # 卡住父任務完成）——這是合法且常見的模式，合併成同一張圖會把它
    # 誤判成死鎖而拒絕寫入。這正是需求文件點名要求測試作者提高警覺的
    # 「圖獨立性」缺口。
    def test_dep_add_parent_child_and_blocks_graphs_independent(self):
        r = run_cli('dep', 'add', 'T-001', 'parent-child', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'add', 'T-002', 'blocks', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    # ---- REQ-3: list --ready CLI 介面 ----

    # REQ-3
    def test_list_ready_excludes_blocked_item(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('list', '--ready', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)
        self.assertNotIn('T-002', r.stdout)

    # REQ-3：驗收判準的另一半——A 完成後 B 不再被卡住，必須出現在
    # list --ready 裡。只測「還沒完成時排除」會漏掉「完成後回到清單」
    # 這個方向。
    def test_list_ready_item_becomes_ready_after_blocker_done(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        run_cli('mark', 'T-001', 'done', '--project', 'demo', env_home=self.home)
        r = run_cli('list', '--ready', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-002', r.stdout)

    # REQ-3：只有 blocks 邊會卡住 ready 判定。related 邊不該讓另一端從
    # list --ready 消失。
    def test_list_ready_related_kind_does_not_block(self):
        run_cli('dep', 'add', 'T-001', 'related', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('list', '--ready', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-001', r.stdout)
        self.assertIn('T-002', r.stdout)


class TestMarkUnblockHint(unittest.TestCase):
    """`mark <ref> done/unpick` 之後的解阻塞提示（REQ-4）。

    這一層要驗的是 cmd_mark 的 stdout 呈現與「純資訊、不觸發自動轉態」
    這個核心不變式；`newly_unblocked_after` 本身的圖論邏輯已在
    test_store.py 覆蓋，這裡不重測。
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

    def _db(self):
        return todo_store.connect(
            self.home / '.claude' / 'todos' / '.audit' / 'demo.sqlite')

    # REQ-4：核心驗收判準——印出提示的同一時刻，下游條目的 status 必須
    # 原封不動地留在 pending。只斷言 stdout 含 short_id 會漏掉「提示印了，
    # 但背地裡把下游偷偷轉態」這種假紅測試抓不到的 bug，所以額外查 DB。
    def test_mark_done_prints_hint_and_leaves_downstream_status_pending(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('mark', 'T-001', 'done', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-002', r.stdout)
        self.assertIn('可動手', r.stdout)
        con = self._db()
        row = con.execute("SELECT status, status_by FROM todo"
                          " WHERE short_id='T-002'").fetchone()
        con.close()
        self.assertEqual(row[0], 'pending')
        self.assertIsNone(row[1])

    # REQ-4：驗收判準明講 done 與 unpick 兩者都要觸發提示，只測 done
    # 會漏掉 unpick 這一半（例如實作只在 status == 'done' 判斷，漏了
    # 'unpick' 分支）。
    def test_mark_unpick_prints_hint_and_leaves_downstream_status_pending(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('mark', 'T-001', 'unpick', '--note', '不做了',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-002', r.stdout)
        self.assertIn('可動手', r.stdout)
        con = self._db()
        row = con.execute("SELECT status FROM todo"
                          " WHERE short_id='T-002'").fetchone()
        con.close()
        self.assertEqual(row[0], 'pending')

    # REQ-4（G-2 裁決）：pending 不觸發解阻塞提示查詢——可觀察行為是
    # stdout 不印「可動手」。
    def test_mark_pending_never_prints_unblock_hint(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        run_cli('mark', 'T-001', 'doing', '--by', 'sess-a',
                '--project', 'demo', env_home=self.home)
        # 釋放自己的認領需要 --by 指名（比照既有 CLAUDE.md 操作手冊：
        # `mark <ref> pending --by <誰>`），否則會被 ClaimConflict 擋下，
        # 那是另一回事，不是這個測試要驗的行為。
        r = run_cli('mark', 'T-001', 'pending', '--by', 'sess-a',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('可動手', r.stdout)

    # REQ-4：沒有任何下游因此變 ready 時，不該印出提示行——否則每次
    # mark done 都洗一行空提示，會讓「真的有事要看」跟「例行公事」
    # 混在一起分不出來。
    def test_mark_done_with_no_downstream_omits_unblock_line(self):
        r = run_cli('mark', 'T-001', 'done', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('可動手', r.stdout)

    # REQ-4：直接下游還有其他未完成的 blocker 時，不該被列入「因此變
    # 可動手」——這是最容易被實作者漏掉的一格：naive 實作可能只看
    # 「done_key 直接指向的下游」而不檢查該下游是否還被別的邊卡著。
    def test_mark_done_does_not_list_downstream_with_remaining_blocker(self):
        add = run_cli('add', '第三條', '🏷️  x', '💡  y',
                      '--project', 'demo', env_home=self.home)
        t3 = add.stdout.strip()
        run_cli('dep', 'add', 'T-001', 'blocks', t3,
                '--project', 'demo', env_home=self.home)
        run_cli('dep', 'add', 'T-002', 'blocks', t3,
                '--project', 'demo', env_home=self.home)
        r = run_cli('mark', 'T-001', 'done', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(t3, r.stdout)
        con = self._db()
        row = con.execute('SELECT status FROM todo WHERE short_id=?',
                          (t3,)).fetchone()
        con.close()
        self.assertEqual(row[0], 'pending')

    # REQ-4：下游本身已經不是 pending（例如已被人認領 doing）時，即使
    # 唯一的 blocker 完成了，也不該被當成「因此變可動手」——ready 的
    # 定義是「pending 且未被卡住」，doing 中的條目不該混進提示裡。
    def test_mark_done_does_not_list_non_pending_downstream(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        run_cli('mark', 'T-002', 'doing', '--by', 'sess-b',
                '--project', 'demo', env_home=self.home)
        r = run_cli('mark', 'T-001', 'done', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('可動手', r.stdout)


class TestShowChangeHistory(unittest.TestCase):
    """`show <ref>` 的變更軌跡段落（REQ-6）。"""

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

    # REQ-6：轉態事件要含轉態前後值與操作者，缺任何一項都讓「覆盤撞車
    # 事故」這個目的落空。
    def test_show_lists_status_transition_with_old_new_and_owner(self):
        run_cli('mark', 'T-001', 'doing', '--by', 'sess-a',
                '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('變更軌跡', r.stdout)
        self.assertIn('pending → doing', r.stdout)
        self.assertIn('sess-a', r.stdout)

    # REQ-6：從沒發生過任何轉態／依賴變更的條目，不該印出空的「變更
    # 軌跡：」標頭——那會讓人誤以為有記錄可看，點進去卻是空的。
    def test_show_omits_change_history_section_when_no_events(self):
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('變更軌跡', r.stdout)

    # REQ-6：驗收判準明講「至少涵蓋 status 轉換...與依賴增刪兩類事件」，
    # 只測 status 轉換會漏掉依賴事件完全沒進軌跡的情況。
    def test_show_change_history_includes_dep_add_event(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('變更軌跡', r.stdout)
        history = r.stdout.split('變更軌跡', 1)[1]
        self.assertIn('T-002', history)

    # REQ-6：dep_rm 同樣屬於驗收判準點名的「依賴增刪兩類事件」之一，
    # 只測 dep_add 會漏掉刪除這一半（例如實作只在 cmd_dep 的 add 分支
    # 呼叫 _record_event，rm 分支漏寫）。
    def test_show_change_history_includes_dep_rm_event(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        run_cli('dep', 'rm', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('變更軌跡', r.stdout)
        history = r.stdout.split('變更軌跡', 1)[1]
        # dep_add 與 dep_rm 兩筆都要留下獨立記錄（append-only），
        # 不是刪除後只剩空白。
        self.assertEqual(history.count('T-002'), 2)

    # REQ-6：驗收判準明講「由新到舊排序」——依序做 doing → done →
    # dep add 三個動作，軌跡裡最後發生的（dep add）必須排在最前面，
    # 最早發生的（doing）排在最後面。
    def test_show_change_history_ordered_newest_first(self):
        run_cli('mark', 'T-001', 'doing', '--by', 'sess-a',
                '--project', 'demo', env_home=self.home)
        # 轉 done 要帶同一個 --by，否則會被既有認領守衛以 ClaimConflict
        # 擋下（set_status() 只認「當前 doing 的擁有者」，見 test_store.py
        # 的 TestClaimGuard；Task 2 的 QA 曾在 test_list_events_orders_
        # newest_first 踩過同一個坑，這裡是同一類錯誤）。
        run_cli('mark', 'T-001', 'done', '--by', 'sess-a',
                '--project', 'demo', env_home=self.home)
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('變更軌跡', r.stdout)
        history = r.stdout.split('變更軌跡', 1)[1]
        idx_dep = history.index('T-002')
        idx_done = history.index('doing → done')
        idx_doing = history.index('pending → doing')
        self.assertLess(idx_dep, idx_done,
                        '最新的 dep_add 事件必須排在較早的 status 事件之前')
        self.assertLess(idx_done, idx_doing,
                        'done 轉態必須排在更早的 doing 轉態之前')

    # REQ-6 + REQ-7：被 ClaimConflict 擋下的轉態嘗試不寫入變更軌跡——
    # sess-b 的嘗試被拒絕，show 的變更軌跡裡不該出現 sess-b 這個操作者，
    # 只有 sess-a 成功那筆。這是 CLI 層的整合驗證：即使 store 層的
    # _record_event 沒被呼叫，也要確認 cmd_show 沒有另外用什麼方式
    # 把被拒絕的嘗試呈現出來。
    def test_show_change_history_excludes_rejected_claim_attempt(self):
        run_cli('mark', 'T-001', 'doing', '--by', 'sess-a',
                '--project', 'demo', env_home=self.home)
        conflict = run_cli('mark', 'T-001', 'doing', '--by', 'sess-b',
                           '--project', 'demo', env_home=self.home)
        self.assertEqual(conflict.returncode, 7, conflict.stdout + conflict.stderr)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('變更軌跡', r.stdout)
        history = r.stdout.split('變更軌跡', 1)[1]
        self.assertNotIn('sess-b', history)
        self.assertEqual(history.count('pending → doing'), 1)


class TestNonDoctorCommandRejectsBindingConflict(unittest.TestCase):
    """T-002：`test_doctor.py` 只測過 doctor 對 `ProjectBindingConflict` 的
    豁免行為，其他指令（list/show/dump/...）走 `bind_project()` 判斷式
    （todo_cli.py:773 附近）從未被鎖住——這裡補一條，確認非 doctor 指令
    在綁定衝突時仍照常被擋下：exit=6、輸出含衝突訊息。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        self.db = audit / 'demo.sqlite'
        con = todo_store.connect(self.db)
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        # 先把 'demo' 綁定到 repo_a，模擬既有專案已綁定到別處
        todo_store.bind_project(con, 'demo', '/repo_a', '(no-remote)')
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_with_conflicting_path_exits_6_with_message(self):
        r = run_cli('list', '--project', 'demo', '--path', '/repo_b',
                    env_home=self.home)
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn('不能共用待辦', r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
