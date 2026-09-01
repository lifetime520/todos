import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_store


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 't.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_adds_columns_to_fresh_db(self):
        con = todo_store.connect(self.db)
        cols = {r[1] for r in con.execute('PRAGMA table_info(todo)')}
        for c in ('raw_title', 'section', 'group_marker', 'sort_order',
                  'status', 'status_by', 'status_at', 'status_note', 'short_id'):
            self.assertIn(c, cols, f'missing column {c}')
        con.close()

    def test_migration_preserves_existing_rows(self):
        # 先造一個「舊版」DB：只有原始六表與一列資料
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE todo(key TEXT PRIMARY KEY, date TEXT, title TEXT,
              first_seen_run INT, last_seen_run INT, content_hash TEXT);
        """)
        con.execute("INSERT INTO todo VALUES('abc','2026-08-08','old title',1,1,'h')")
        con.commit()
        con.close()

        con = todo_store.connect(self.db)
        row = con.execute("SELECT date, title FROM todo WHERE key='abc'").fetchone()
        self.assertEqual(row, ('2026-08-08', 'old title'))
        con.close()

    def test_migration_is_idempotent(self):
        todo_store.connect(self.db).close()
        todo_store.connect(self.db).close()   # 第二次不得拋錯
        con = todo_store.connect(self.db)
        cols = {r[1] for r in con.execute('PRAGMA table_info(todo)')}
        self.assertIn('short_id', cols)
        con.close()

    def test_todo_key_matches_legacy_algorithm(self):
        # 必須與 todo_audit.py:110 完全一致，否則歷史 probe 斷鏈
        import hashlib
        expected = hashlib.sha1('2026-08-08|some title'.encode()).hexdigest()[:16]
        self.assertEqual(todo_store.todo_key('2026-08-08', 'some title'), expected)

    def test_todo_line_table_exists(self):
        con = todo_store.connect(self.db)
        con.execute("INSERT INTO todo_line VALUES('k',0,'💡','text')")
        row = con.execute('SELECT marker, text FROM todo_line').fetchone()
        self.assertEqual(row, ('💡', 'text'))
        con.close()


SAMPLE = """<!-- project_path: /x | git_remote: g -->

# demo Pending

> 前言說明

## 🔴 立即處理（P0 / 資金安全）（1）

- [ ] [2026-08-08] [P0] 第一條
  > 🔗  ⚓ OrderService(15)
  > 🏷️  tagA, tagB
  > 💡  說明文字

<!-- ⚓ OrderService -->

- [ ] [2026-08-07] 第二條
  > 🏷️  tagC
  > 💡  多行說明第一段
  > 💡  多行說明第二段
  > ⚠️  警告
"""


class TestLosslessParse(unittest.TestCase):
    def test_item_count_and_order(self):
        p = todo_store.parse_md_lossless(SAMPLE)
        self.assertEqual(len(p['items']), 2)
        self.assertEqual(p['items'][0]['sort_order'], 0)
        self.assertEqual(p['items'][1]['sort_order'], 1)

    def test_raw_title_preserved_verbatim(self):
        p = todo_store.parse_md_lossless(SAMPLE)
        self.assertEqual(p['items'][0]['raw_title'],
                         '- [ ] [2026-08-08] [P0] 第一條')

    def test_body_raw_keeps_indentation(self):
        # 這是與 todo_audit.py:178 的關鍵差異：不得 strip
        p = todo_store.parse_md_lossless(SAMPLE)
        self.assertEqual(p['items'][0]['body_raw'][0], '  > 🔗  ⚓ OrderService(15)')

    def test_group_marker_captured(self):
        p = todo_store.parse_md_lossless(SAMPLE)
        self.assertIsNone(p['items'][0]['group_marker'])
        self.assertEqual(p['items'][1]['group_marker'], '<!-- ⚓ OrderService -->')

    def test_repeated_marker_kept_as_separate_lines(self):
        # 實測 tradingbot.md：💡 出現 195 次但只有 188 條，7 條帶多個 💡
        p = todo_store.parse_md_lossless(SAMPLE)
        markers = [todo_store.line_marker(l) for l in p['items'][1]['body_raw']]
        self.assertEqual(markers, ['🏷️', '💡', '💡', '⚠️'])

    def test_key_matches_legacy_title_stripping(self):
        p = todo_store.parse_md_lossless(SAMPLE)
        # legacy: title = raw[6:].strip()
        self.assertEqual(p['items'][0]['title'], '[2026-08-08] [P0] 第一條')
        self.assertEqual(p['items'][0]['key'],
                         todo_store.todo_key('2026-08-08', '[2026-08-08] [P0] 第一條'))

    def test_audit_shape_matches_legacy_parser(self):
        p = todo_store.parse_md_lossless(SAMPLE)
        shaped = todo_store.to_audit_shape(p)
        self.assertEqual(shaped[0]['title'], '[2026-08-08] [P0] 第一條')
        self.assertEqual(shaped[0]['date'], '2026-08-08')
        # legacy body 是 strip 過的
        self.assertEqual(shaped[0]['body'][0], '> 🔗  ⚓ OrderService(15)')


class TestKeyCollision(unittest.TestCase):
    def test_duplicate_title_same_date_both_survive_roundtrip(self):
        dup = ("- [ ] [2026-08-08] 同名\n  > 💡  A\n\n"
               "- [ ] [2026-08-08] 同名\n  > 💡  B\n")
        p = todo_store.parse_md_lossless(dup)
        self.assertEqual(len(p['items']), 2)
        self.assertEqual(todo_store.render(p), dup)

    def test_trailing_newline_preserved(self):
        # 檔尾換行是 byte-identical 的常見破口
        t = "- [ ] [2026-08-08] x\n  > 💡  y\n"
        self.assertEqual(todo_store.render(todo_store.parse_md_lossless(t)), t)

    def test_empty_document_roundtrips(self):
        self.assertEqual(todo_store.render(todo_store.parse_md_lossless('')), '')


class TestSaveParsedRejectsKeyCollision(unittest.TestCase):
    """save_parsed() 遇到同一批重複 key 必須報錯，且一列都不寫。

    上面的 TestKeyCollision 驗的是 parse/render 這一層 —— 它用 sort_order
    索引 layout，兩條都活著。缺口在 save_parsed()：key = sha1(date|title)，
    todo.key 又是 PRIMARY KEY，所以第二條會 UPDATE 掉第一條，DB 只剩一列。
    md 檔看起來完好無損，DB 卻少了一條，沒有任何錯誤訊息 —— 靜默資料遺失。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    _DUP = ("- [ ] [2026-08-08] 同名\n  > 💡  A\n\n"
            "- [ ] [2026-08-08] 同名\n  > 💡  B\n")

    def test_raises_on_duplicate_key_in_same_batch(self):
        parsed = todo_store.parse_md_lossless(self._DUP)
        # 前提：解析層本來就給出兩條（否則這個測試驗的是別的東西）
        self.assertEqual(len(parsed['items']), 2)
        with self.assertRaises(ValueError) as ctx:
            todo_store.save_parsed(self.con, 'demo', parsed)
        # 訊息要讓人當場知道是哪兩條撞了，不能只說「出錯了」
        self.assertIn('同名', str(ctx.exception))

    def test_writes_nothing_when_collision_detected(self):
        """報錯前不得留下半套資料 —— 碰撞偵測必須在任何 INSERT 之前。

        這條是本類別真正的重點：只 raise 但已經寫進去一列的話，DB 會處在
        一個「md 有兩條、DB 有一條」的狀態，而使用者看到的是例外訊息、
        以為什麼都沒發生。
        """
        parsed = todo_store.parse_md_lossless(self._DUP)
        with self.assertRaises(ValueError):
            todo_store.save_parsed(self.con, 'demo', parsed)
        n = self.con.execute('SELECT COUNT(*) FROM todo').fetchone()[0]
        self.assertEqual(n, 0, f'碰撞被擋下後 todo 表應該是空的，實際有 {n} 列')

    def test_normal_batch_still_writes(self):
        """守衛不得誤傷正常批次（否則這個保護是恆假的）。"""
        ok = ("- [ ] [2026-08-08] 甲\n  > 💡  A\n\n"
              "- [ ] [2026-08-08] 乙\n  > 💡  B\n")
        todo_store.save_parsed(self.con, 'demo', todo_store.parse_md_lossless(ok))
        n = self.con.execute('SELECT COUNT(*) FROM todo').fetchone()[0]
        self.assertEqual(n, 2)

    def test_same_title_different_date_is_not_a_collision(self):
        """key 是 sha1(date|title) —— 日期不同就不該被擋。"""
        ok = ("- [ ] [2026-08-08] 同名\n  > 💡  A\n\n"
              "- [ ] [2026-08-09] 同名\n  > 💡  B\n")
        todo_store.save_parsed(self.con, 'demo', todo_store.parse_md_lossless(ok))
        n = self.con.execute('SELECT COUNT(*) FROM todo').fetchone()[0]
        self.assertEqual(n, 2)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 't.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_then_load_roundtrips_through_db(self):
        parsed = todo_store.parse_md_lossless(SAMPLE)
        con = todo_store.connect(self.db)
        todo_store.save_parsed(con, 'demo', parsed)
        reloaded = todo_store.load_parsed(con, 'demo')
        self.assertEqual(todo_store.render(reloaded), SAMPLE)
        con.close()

    def test_short_ids_assigned_in_sort_order(self):
        parsed = todo_store.parse_md_lossless(SAMPLE)
        con = todo_store.connect(self.db)
        todo_store.save_parsed(con, 'demo', parsed)
        todo_store.assign_short_ids(con)
        ids = [r[0] for r in con.execute(
            'SELECT short_id FROM todo ORDER BY sort_order')]
        self.assertEqual(ids, ['T-001', 'T-002'])
        con.close()

    def test_short_ids_are_not_reused_after_new_items(self):
        con = todo_store.connect(self.db)
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        extra = SAMPLE + "\n- [ ] [2026-08-06] 第三條\n  > 💡  x\n"
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(extra))
        todo_store.assign_short_ids(con)
        ids = {r[0] for r in con.execute(
            'SELECT short_id FROM todo WHERE short_id IS NOT NULL')}
        self.assertIn('T-003', ids)
        self.assertEqual(len(ids), 3)
        con.close()

    def test_default_status_is_pending(self):
        con = todo_store.connect(self.db)
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        s = {r[0] for r in con.execute(
            'SELECT status FROM todo WHERE sort_order IS NOT NULL')}
        self.assertEqual(s, {'pending'})
        con.close()

    def test_save_is_idempotent_and_preserves_status(self):
        # 遷移可重跑 —— 第二次不得把已標的 status 洗回 pending
        con = todo_store.connect(self.db)
        parsed = todo_store.parse_md_lossless(SAMPLE)
        todo_store.save_parsed(con, 'demo', parsed)
        key = parsed['items'][0]['key']
        con.execute("UPDATE todo SET status='doing' WHERE key=?", (key,))
        con.commit()
        todo_store.save_parsed(con, 'demo', parsed)
        st = con.execute('SELECT status FROM todo WHERE key=?', (key,)).fetchone()[0]
        self.assertEqual(st, 'doing')
        con.close()


class TestReviewFindings(unittest.TestCase):
    """釘住 2026-08-08 code review 抓到的四個問題，防回歸。"""

    def test_line_marker_rejects_free_text(self):
        # (\S+) 會把自由文字第一個詞當 marker，污染 todo_line.marker
        self.assertIsNone(todo_store.line_marker('  > 說明文字沒有 marker'))
        self.assertIsNone(todo_store.line_marker('  > TODO: 改用 X'))
        self.assertIsNone(todo_store.line_marker('  > 2026-08-08 記錄'))
        self.assertEqual(todo_store.line_marker('  > 💡  x'), '💡')
        self.assertEqual(todo_store.line_marker('  > 🔍  y'), '🔍')

    def test_section_follows_heading_over_title_marker(self):
        # 人把條目搬進「不急」，標題殘留 [P0] 不該把它拉回 urgent
        t = ("## ⚪ 觀察 / 技術債（不急）（1）\n\n"
             "- [ ] [2026-08-08] [P0] 已被降級的事\n  > 💡  x\n")
        p = todo_store.parse_md_lossless(t)
        self.assertEqual(p['items'][0]['section'], 'later')
        self.assertEqual(p['items'][0]['heading'], '## ⚪ 觀察 / 技術債（不急）（1）')

    def test_section_falls_back_to_title_when_no_heading(self):
        t = "- [ ] [2026-08-08] [P0] 沒有章節\n  > 💡  x\n"
        self.assertEqual(
            todo_store.parse_md_lossless(t)['items'][0]['section'], 'urgent')

    def test_audit_shape_line_is_real_md_lineno(self):
        # todo_audit.py 有五處直接印 L{line}，給序位會指向錯位置
        p = todo_store.parse_md_lossless(SAMPLE)
        shaped = todo_store.to_audit_shape(p)
        lines = SAMPLE.split('\n')
        for it in shaped:
            self.assertTrue(lines[it['line'] - 1].startswith('- [ ] '),
                            f"line {it['line']} 不是條目行")

    def test_render_refuses_missing_item_instead_of_silent_drift(self):
        t = ("# H\n\n- [ ] [2026-08-08] A\n  > 💡  a\n\n"
             "<!-- ⚓ Foo -->\n\n- [ ] [2026-08-08] B\n  > 💡  b\n")
        p = todo_store.parse_md_lossless(t)
        p['items'] = [it for it in p['items'] if it['sort_order'] != 1]
        with self.assertRaises(KeyError):
            todo_store.render(p)


class TestAppendItem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _body(self, key):
        return [t for (t,) in self.con.execute(
            'SELECT text FROM todo_line WHERE todo_key=? ORDER BY seq', (key,))]

    def test_marker_not_duplicated(self):
        # todo-add.sh 的介面是 "🏷️  a, b"（自帶 marker），不可再補一個
        todo_store.append_item(self.con, 'p', '標題', '🏷️  a, b', '💡  說明')
        key = self.con.execute('SELECT key FROM todo').fetchone()[0]
        body = self._body(key)
        self.assertEqual(body[0], '  > 🏷️  a, b')
        self.assertEqual(body[1], '  > 💡  說明')
        self.assertNotIn('🏷️  🏷️', '\n'.join(body))

    def test_empty_tag_or_note_creates_no_line(self):
        todo_store.append_item(self.con, 'p', '只有標題', '', '')
        key = self.con.execute('SELECT key FROM todo').fetchone()[0]
        self.assertEqual(self._body(key), [])

    def test_appended_item_roundtrips(self):
        todo_store.append_item(self.con, 'p', '標題', '🏷️  x', '💡  y')
        key = self.con.execute('SELECT key FROM todo').fetchone()[0]
        raw = self.con.execute('SELECT raw_title FROM todo WHERE key=?',
                               (key,)).fetchone()[0]
        self.assertTrue(raw.startswith('- [ ] ['))
        self.assertTrue(raw.endswith('標題'))

    def test_appended_item_then_done_survives_reconnect_without_backfill(self):
        # I-2 回歸測試：append_item 若漏寫 progress=0，新條目的 progress
        # 留 NULL；同連線內若之後被標成 done，下一次 connect() 的 backfill
        # （UPDATE ... WHERE status='done' AND progress IS NULL）會誤判成
        #「上線前的舊 done」而灌成 127（ALL_FLAGS）——謊稱 reviewed/
        # compiled/tested/live_tested/deployed 全部發生過。
        db_path = Path(self.tmp.name) / 't.sqlite'
        todo_store.append_item(self.con, 'p', '標題', '', '')
        key = self.con.execute('SELECT key FROM todo').fetchone()[0]
        todo_store.set_status(self.con, key, 'done', by='tester')
        self.con.close()

        con2 = todo_store.connect(db_path)
        progress = con2.execute('SELECT progress FROM todo WHERE key=?',
                                (key,)).fetchone()[0]
        con2.close()
        self.assertEqual(progress, 0)


class TestEditAndRemove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')
        todo_store.save_parsed(self.con, 'demo',
                               todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(self.con)
        self.key = self.con.execute(
            'SELECT key FROM todo WHERE sort_order=0').fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _body(self, key):
        return [t for (t,) in self.con.execute(
            'SELECT text FROM todo_line WHERE todo_key=? ORDER BY seq', (key,))]

    def test_edit_title_migrates_key_and_keeps_history(self):
        # key = sha1(date|title)，改標題必然換 key。關聯資料若不跟著搬，
        # 稽核歷史（anchor/probe/verdict）與 body 全部斷鏈。
        self.con.execute('INSERT INTO anchor VALUES(?,?,?,?)',
                         (self.key, 'symbol', 'FooService', None))
        self.con.execute('INSERT INTO probe VALUES(?,?,?,?,?,?,?)',
                         (1, self.key, 'ALIVE', 1, '[]', '[]', '[]'))
        self.con.commit()
        body_before = self._body(self.key)

        new_key = todo_store.edit_title(self.con, self.key, '改過的標題')
        self.assertNotEqual(new_key, self.key)

        self.assertEqual(self._body(new_key), body_before, 'body 沒跟著搬')
        self.assertEqual(self.con.execute(
            'SELECT COUNT(*) FROM anchor WHERE todo_key=?',
            (new_key,)).fetchone()[0], 1, 'anchor 沒跟著搬')
        self.assertEqual(self.con.execute(
            'SELECT COUNT(*) FROM probe WHERE todo_key=?',
            (new_key,)).fetchone()[0], 1, 'probe 沒跟著搬')
        # 舊 key 不得殘留
        self.assertEqual(self.con.execute(
            'SELECT COUNT(*) FROM todo WHERE key=?',
            (self.key,)).fetchone()[0], 0)

    def test_edit_title_keeps_date_prefix(self):
        new_key = todo_store.edit_title(self.con, self.key, '改過的標題')
        title, raw = self.con.execute(
            'SELECT title, raw_title FROM todo WHERE key=?', (new_key,)).fetchone()
        self.assertTrue(title.startswith('[2026-08-08]'), f'日期前綴掉了: {title}')
        self.assertTrue(raw.startswith('- [ ] [2026-08-08]'))
        self.assertTrue(raw.endswith('改過的標題'))

    def test_edit_title_keeps_heading_priority(self):
        # 條目在 `## 🔴 立即處理` 底下，改標題加 [P2] 不該把它拉走 ——
        # heading 優先於標題標記是刻意設計（見 _section_of 的 docstring）
        new_key = todo_store.edit_title(self.con, self.key, '[P2] 降級了')
        sec = self.con.execute('SELECT section FROM todo WHERE key=?',
                               (new_key,)).fetchone()[0]
        self.assertEqual(sec, 'urgent')

    def test_edit_title_recomputes_section_without_heading(self):
        # 無 heading 時才由標題標記決定
        con2 = todo_store.connect(Path(self.tmp.name) / 'u.sqlite')
        todo_store.save_parsed(con2, 'd', todo_store.parse_md_lossless(
            '- [ ] [2026-08-08] [P0] 無章節\n  > 💡  x\n'))
        k = con2.execute('SELECT key FROM todo').fetchone()[0]
        self.assertEqual(con2.execute(
            'SELECT section FROM todo WHERE key=?', (k,)).fetchone()[0], 'urgent')
        nk = todo_store.edit_title(con2, k, '[P2] 降級了')
        self.assertEqual(con2.execute(
            'SELECT section FROM todo WHERE key=?', (nk,)).fetchone()[0], 'later')
        con2.close()

    def test_edit_line_replaces_content(self):
        todo_store.edit_line(self.con, self.key, 1, '🏷️  改過的 tag')
        self.assertEqual(self._body(self.key)[1], '  > 🏷️  改過的 tag')

    def test_edit_line_rejects_out_of_range(self):
        with self.assertRaises(IndexError):
            todo_store.edit_line(self.con, self.key, 99, '🏷️  x')

    def test_remove_line(self):
        before = self._body(self.key)
        todo_store.remove_line(self.con, self.key, 0)
        after = self._body(self.key)
        self.assertEqual(len(after), len(before) - 1)
        self.assertNotIn(before[0], after)

    def test_remove_item_deletes_all_related_rows(self):
        self.con.execute('INSERT INTO anchor VALUES(?,?,?,?)',
                         (self.key, 'symbol', 'FooService', None))
        self.con.execute('INSERT INTO probe VALUES(?,?,?,?,?,?,?)',
                         (1, self.key, 'ALIVE', 1, '[]', '[]', '[]'))
        self.con.commit()
        todo_store.remove_item(self.con, self.key)
        for tbl in ('todo', 'todo_line', 'anchor', 'probe', 'verdict'):
            n = self.con.execute(
                f'SELECT COUNT(*) FROM {tbl} WHERE '
                + ('key=?' if tbl == 'todo' else 'todo_key=?'),
                (self.key,)).fetchone()[0]
            self.assertEqual(n, 0, f'{tbl} 有殘留')


class TestClaimGuard(unittest.TestCase):
    """認領守衛：擋掉「B session 覆蓋 A 正在做的條目」。

    守衛條件是「當前是他人的 doing」，不是「本次要寫 doing」——
    B 把 A 的 doing 直接標 done 比重複認領更糟：A 還在做，條目已消失。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')
        todo_store.save_parsed(self.con, 'demo',
                               todo_store.parse_md_lossless(SAMPLE))
        self.key = self.con.execute(
            'SELECT key FROM todo ORDER BY sort_order').fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_doing_requires_explicit_owner(self):
        # 沒有身分的認領無法防撞車 —— 全部 session 都叫同一個名字等於沒有名字
        with self.assertRaises(ValueError) as cm:
            todo_store.set_status(self.con, self.key, 'doing', by=None)
        self.assertIn('by', str(cm.exception).lower())

    def test_other_session_cannot_overwrite_doing(self):
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        with self.assertRaises(todo_store.ClaimConflict):
            todo_store.set_status(self.con, self.key, 'doing', by='sess-b')

    def test_other_session_cannot_mark_someone_elses_doing_as_done(self):
        # 最危險的一種：A 還在做，B 把條目標完成，A 的工作從清單上消失
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        with self.assertRaises(todo_store.ClaimConflict):
            todo_store.set_status(self.con, self.key, 'done', by='sess-b')

    def test_same_owner_may_re_mark(self):
        # 續作、或同一 session 重跑，不該被自己的鎖擋住
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        todo_store.set_status(self.con, self.key, 'done', by='sess-a')
        self.assertEqual(self._status(), 'done')

    def test_force_overrides_and_is_recorded(self):
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        todo_store.set_status(self.con, self.key, 'doing', by='sess-b',
                              force=True)
        row = self.con.execute('SELECT status_by FROM todo WHERE key=?',
                               (self.key,)).fetchone()
        self.assertEqual(row[0], 'sess-b')

    def test_unowned_doing_is_still_guarded(self):
        # status_by 為 NULL 的 doing（舊資料）：「有人在做但不知是誰」
        # 比「沒人做」更危險，一樣要擋，由人用 --force 裁決
        self.con.execute("UPDATE todo SET status='doing', status_by=NULL"
                         " WHERE key=?", (self.key,))
        self.con.commit()
        with self.assertRaises(todo_store.ClaimConflict):
            todo_store.set_status(self.con, self.key, 'doing', by='sess-b')

    def test_conflict_message_names_owner_and_time(self):
        # 訊息要能讓人當場判斷「該不該搶」—— 缺任一項都判斷不了
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        with self.assertRaises(todo_store.ClaimConflict) as cm:
            todo_store.set_status(self.con, self.key, 'done', by='sess-b')
        msg = str(cm.exception)
        self.assertIn('sess-a', msg)
        self.assertIn('force', msg)

    def test_release_clears_owner(self):
        # 標回 pending = 釋放。留著舊 status_by 會讓消費端分不出
        # 「現任擁有者」與「上一個做過的人」
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        todo_store.set_status(self.con, self.key, 'pending', by='sess-a')
        row = self.con.execute('SELECT status_by, status_at FROM todo'
                               ' WHERE key=?', (self.key,)).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNotNone(row[1], 'status_at 要留著 —— 它是「何時釋放」')

    def test_released_item_is_claimable_again(self):
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        todo_store.set_status(self.con, self.key, 'pending', by='sess-a')
        todo_store.set_status(self.con, self.key, 'doing', by='sess-b')
        self.assertEqual(self._status(), 'doing')

    def test_pending_item_is_free_to_claim(self):
        todo_store.set_status(self.con, self.key, 'doing', by='sess-b')
        self.assertEqual(self._status(), 'doing')

    def _status(self):
        return self.con.execute('SELECT status FROM todo WHERE key=?',
                                (self.key,)).fetchone()[0]


class TestMigrateTargets(unittest.TestCase):
    def test_view_mirror_is_not_treated_as_a_project(self):
        # 鏡像是 DB 的衍生物；當成來源會建出內容是自己衍生物的垃圾 DB
        import migrate_md_to_db
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'proj.md').write_text('x', encoding='utf-8')
            (p / 'proj.view.md').write_text('y', encoding='utf-8')
            self.assertEqual(migrate_md_to_db.discover_targets(p), ['proj'])


class TestProgressFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / 't.sqlite'
        self.con = todo_store.connect(self.dbpath)
        todo_store.save_parsed(self.con, 'demo',
                               todo_store.parse_md_lossless(SAMPLE))
        self.key = self.con.execute(
            'SELECT key FROM todo ORDER BY sort_order').fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_migration_adds_progress_columns(self):
        cols = {r[1] for r in self.con.execute('PRAGMA table_info(todo)')}
        for c in ('progress', 'spec_path', 'memory_ref'):
            self.assertIn(c, cols, f'missing column {c}')

    def test_fresh_rows_default_progress_to_zero(self):
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 0)

    def test_existing_done_rows_backfilled_to_full_progress_on_reconnect(self):
        self.con.execute("UPDATE todo SET status='done', progress=NULL"
                         " WHERE key=?", (self.key,))
        self.con.commit()
        self.con.close()
        self.con = todo_store.connect(self.dbpath)
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 127)

    def test_set_progress_sets_single_bit_and_returns_new_value(self):
        new_p = todo_store.set_progress(self.con, self.key, 'set', 'implemented')
        self.assertEqual(new_p, 1)
        stored = self.con.execute('SELECT progress FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(stored, 1)

    def test_set_progress_clear_and_toggle(self):
        todo_store.set_progress(self.con, self.key, 'set', 'reviewed')
        todo_store.set_progress(self.con, self.key, 'clear', 'reviewed')
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 0)
        todo_store.set_progress(self.con, self.key, 'toggle', 'committed')
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 4)

    def test_unknown_progress_op_raises(self):
        with self.assertRaises(ValueError):
            todo_store.set_progress(self.con, self.key, 'bogus', 'implemented')

    def test_unknown_flag_name_raises(self):
        with self.assertRaises(ValueError):
            todo_store.set_progress(self.con, self.key, 'set', 'not_a_flag')

    def test_unknown_key_raises_keyerror(self):
        with self.assertRaises(KeyError):
            todo_store.set_progress(self.con, 'nosuchkey', 'set', 'implemented')

    def test_completing_all_flags_auto_transitions_to_done(self):
        import todo_flags
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        status = self.con.execute('SELECT status FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(status, 'done')

    def test_unpick_item_does_not_auto_transition(self):
        import todo_flags
        todo_store.set_status(self.con, self.key, 'unpick', note='暫不處理')
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        status = self.con.execute('SELECT status FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(status, 'unpick')

    def test_auto_transition_preserves_status_by_of_current_owner(self):
        import todo_flags
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        status, by = self.con.execute(
            'SELECT status, status_by FROM todo WHERE key=?',
            (self.key,)).fetchone()
        self.assertEqual(status, 'done')
        self.assertEqual(by, 'sess-a',
                         'progress 補滿觸發的自動完成不該改寫原本的認領者')

    def test_auto_transition_does_not_raise_claim_conflict_for_other_caller(self):
        # 這是本設計要修的關鍵情境：A 認領中，補滿最後一格的呼叫端不是 A，
        # 不該被 ClaimConflict 擋下 —— 這是工作自然做完，不是搶認領。
        import todo_flags
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)  # 無 by 參數
        status = self.con.execute('SELECT status FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(status, 'done')

    def test_already_done_progress_completion_does_not_overwrite_status_at(self):
        todo_store.set_status(self.con, self.key, 'done')
        original_at = self.con.execute(
            'SELECT status_at FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        import todo_flags
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        after_at = self.con.execute(
            'SELECT status_at FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        self.assertEqual(after_at, original_at)

    def test_manual_mark_done_does_not_force_fill_progress(self):
        todo_store.set_status(self.con, self.key, 'done')
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 0)

    def test_set_spec_path_and_memory_ref(self):
        todo_store.set_spec_path(self.con, self.key, 'docs/specs/x.md')
        todo_store.set_memory_ref(self.con, self.key, 'memory/y.md')
        row = self.con.execute(
            'SELECT spec_path, memory_ref FROM todo WHERE key=?',
            (self.key,)).fetchone()
        self.assertEqual(row, ('docs/specs/x.md', 'memory/y.md'))


class TestDependencyGraph(unittest.TestCase):
    """todo_dep / todo_event：依賴圖 CRUD 與 append-only 變更軌跡。

    Covers: REQ-1, REQ-2, REQ-3, REQ-5, REQ-6, REQ-7, REQ-9, G-3, G-5, G-6
    （REQ-4 的 CLI 提示文字不在本 task 範圍，這裡只測它依賴的 store 層
    介面 newly_unblocked_after）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / 't.sqlite'
        self.con = todo_store.connect(self.dbpath)
        self.keys = {}
        for label in ('A', 'B', 'C', 'D'):
            sid = todo_store.append_item(self.con, 'demo', f'條目{label}', '', '')
            self.keys[label] = self.con.execute(
                'SELECT key FROM todo WHERE short_id=?', (sid,)).fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _short_id(self, key):
        return self.con.execute(
            'SELECT short_id FROM todo WHERE key=?', (key,)).fetchone()[0]

    def _dep_count(self):
        return self.con.execute('SELECT COUNT(*) FROM todo_dep').fetchone()[0]

    # REQ-9
    def test_migration_creates_dep_and_event_tables(self):
        tables = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('todo_dep', tables)
        self.assertIn('todo_event', tables)

    # REQ-9（驗收：對已升級過的 DB 重複 connect() 不拋例外，資料不變）
    def test_connect_twice_on_upgraded_db_is_idempotent(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        self.con.close()
        self.con = todo_store.connect(self.dbpath)   # 第二次 connect
        self.con.close()
        self.con = todo_store.connect(self.dbpath)   # 第三次，仍不得拋例外
        row = self.con.execute(
            'SELECT from_key, to_key, kind FROM todo_dep').fetchone()
        self.assertEqual(row, (self.keys['A'], self.keys['B'], 'blocks'))

    # REQ-1
    def test_add_dep_creates_row(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks',
                           by='sess-a')
        row = self.con.execute(
            'SELECT from_key, to_key, kind, created_by FROM todo_dep').fetchone()
        self.assertEqual(row, (self.keys['A'], self.keys['B'], 'blocks', 'sess-a'))

    # REQ-6
    def test_add_dep_records_event(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks',
                           by='sess-a')
        action, old, new, by, at = todo_store.list_events(
            self.con, self.keys['A'])[0]
        self.assertEqual(action, 'dep_add')
        self.assertIsNone(old)
        self.assertIn('blocks', new)
        self.assertEqual(by, 'sess-a')

    # REQ-1
    def test_add_dep_unknown_kind_raises_value_error_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'bogus')
        self.assertEqual(self._dep_count(), 0)

    # REQ-1
    def test_add_dep_unknown_key_raises_key_error(self):
        with self.assertRaises(KeyError):
            todo_store.add_dep(self.con, 'nosuchkey', self.keys['B'], 'blocks')
        self.assertEqual(self._dep_count(), 0)

    # G-5：重複邊視為錯誤，不是靜默 no-op —— 訊息要可讀，不是裸
    # sqlite3.IntegrityError 的 traceback；也不得多寫一筆事件。
    def test_add_dep_duplicate_edge_raises_readable_error(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        with self.assertRaises(ValueError) as ctx:
            todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        self.assertNotIsInstance(ctx.exception, sqlite3.IntegrityError)
        self.assertIn('已存在', str(ctx.exception))
        events = [e for e in todo_store.list_events(self.con, self.keys['A'])
                 if e[0] == 'dep_add']
        self.assertEqual(len(events), 1)
        self.assertEqual(self._dep_count(), 1)

    # REQ-2：訊息要附「具體環路徑」（至少含 A、B 的 short_id），
    # 不是「有環」三個字就算數。
    def test_add_dep_cyclic_blocks_rejected_with_readable_message(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        a_sid = self._short_id(self.keys['A'])
        b_sid = self._short_id(self.keys['B'])
        with self.assertRaises(ValueError) as ctx:
            todo_store.add_dep(self.con, self.keys['B'], self.keys['A'], 'blocks')
        msg = str(ctx.exception)
        self.assertIn('環', msg)
        self.assertIn(a_sid, msg)
        self.assertIn(b_sid, msg)
        # 被拒絕的邊不該真的寫進去
        self.assertEqual(self._dep_count(), 1)

    # 全域約束：被環狀依賴拒絕的 dep add 不寫事件
    def test_add_dep_cyclic_reject_does_not_write_event(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        with self.assertRaises(ValueError):
            todo_store.add_dep(self.con, self.keys['B'], self.keys['A'], 'blocks')
        events = [e for e in todo_store.list_events(self.con, self.keys['B'])
                 if e[0] == 'dep_add']
        self.assertEqual(len(events), 0)

    # REQ-2（邊界，比照 test_deps.py 的 test_self_dependency_is_a_cycle）：
    # 條目依賴自己必須被當成環拒絕，不能因為圖裡還沒有其他邊就放行。
    def test_add_dep_self_loop_rejected_as_cycle(self):
        with self.assertRaises(ValueError) as ctx:
            todo_store.add_dep(self.con, self.keys['A'], self.keys['A'], 'blocks')
        self.assertIn('環', str(ctx.exception))
        self.assertEqual(self._dep_count(), 0)

    # REQ-2：related 無序性，反向也該能加，不該被誤判成環
    # （related/discovered-from 兩種邊完全不做環狀依賴檢查）。
    def test_add_dep_related_kind_allows_reciprocal_without_cycle_check(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'related')
        todo_store.add_dep(self.con, self.keys['B'], self.keys['A'], 'related')
        self.assertEqual(self._dep_count(), 2)

    # G-3：blocks 與 parent-child 分開跑環檢查，不合併成同一張圖——
    # 父任務被自己的子任務（透過 blocks 邊）卡住是合法且常見的模式，
    # 若誤合併成一張圖，這種合法情境會被錯誤地當成死鎖擋下。
    def test_add_dep_blocks_and_parent_child_graphs_are_independent(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        # B parent-child-> A：跟上面那條 blocks 邊方向相反，若兩張圖被
        # 誤合併成一張，這裡會被誤判成環而拒絕；分開跑則應該成功。
        todo_store.add_dep(self.con, self.keys['B'], self.keys['A'], 'parent-child')
        kinds = {r[0] for r in self.con.execute('SELECT kind FROM todo_dep')}
        self.assertEqual(kinds, {'blocks', 'parent-child'})

    # REQ-1 / REQ-6
    def test_remove_dep_deletes_row_and_records_event(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.remove_dep(self.con, self.keys['A'], self.keys['B'], 'blocks',
                              by='sess-a')
        self.assertEqual(self._dep_count(), 0)
        action = todo_store.list_events(self.con, self.keys['A'])[0][0]
        self.assertEqual(action, 'dep_rm')

    # REQ-1
    def test_remove_dep_missing_raises_key_error(self):
        with self.assertRaises(KeyError):
            todo_store.remove_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')

    # REQ-1（remove_dep 跟 add_dep 一樣要驗證 kind，不能只驗證存在性）
    def test_remove_dep_unknown_kind_raises_value_error(self):
        with self.assertRaises(ValueError):
            todo_store.remove_dep(self.con, self.keys['A'], self.keys['B'], 'bogus')

    # REQ-1：show 要能同時呈現「我阻塞誰」（out）與「誰阻塞我」（in）
    def test_list_deps_returns_both_directions(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        a_sid = self._short_id(self.keys['A'])
        b_sid = self._short_id(self.keys['B'])
        out_from_a = todo_store.list_deps(self.con, self.keys['A'])
        out_from_b = todo_store.list_deps(self.con, self.keys['B'])
        self.assertEqual(out_from_a, [('out', 'blocks', self.keys['B'], b_sid)])
        self.assertEqual(out_from_b, [('in', 'blocks', self.keys['A'], a_sid)])

    # REQ-3
    def test_is_ready_true_when_no_blockers(self):
        self.assertTrue(todo_store.is_ready(self.con, self.keys['A']))

    # REQ-3
    def test_is_ready_false_when_blocked_by_pending(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        self.assertFalse(todo_store.is_ready(self.con, self.keys['B']))

    # REQ-3
    def test_is_ready_true_once_blocker_done(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.set_status(self.con, self.keys['A'], 'done')
        self.assertTrue(todo_store.is_ready(self.con, self.keys['B']))

    # REQ-3（邊界：查不存在的條目不該悄悄回 False，要讓呼叫端知道 key 錯了）
    def test_is_ready_unknown_key_raises_key_error(self):
        with self.assertRaises(KeyError):
            todo_store.is_ready(self.con, 'nosuchkey')

    # REQ-3
    def test_ready_keys_excludes_blocked_and_non_pending(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.set_status(self.con, self.keys['C'], 'doing', by='x')
        ready = todo_store.ready_keys(self.con)
        self.assertNotIn(self.keys['B'], ready)   # 被 A 卡住
        self.assertNotIn(self.keys['C'], ready)   # 不是 pending
        self.assertIn(self.keys['D'], ready)      # 沒被任何邊卡住

    # REQ-4（store 層介面；CLI 印出「可動手」提示的行為屬於其他 task）
    def test_newly_unblocked_after_done_transition(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.set_status(self.con, self.keys['A'], 'done')
        unblocked = todo_store.newly_unblocked_after(self.con, self.keys['A'])
        b_sid = self._short_id(self.keys['B'])
        self.assertEqual(unblocked, [b_sid])

    # REQ-5：blocked 不得成為 status 的第五個值，即使呼叫端硬塞這個字串
    def test_set_status_rejects_blocked_as_status_value(self):
        with self.assertRaises(ValueError):
            todo_store.set_status(self.con, self.keys['A'], 'blocked')

    # REQ-6
    def test_set_status_records_event(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        action, old, new, by, at = todo_store.list_events(
            self.con, self.keys['A'])[0]
        self.assertEqual(action, 'status')
        self.assertEqual(old, 'pending')
        self.assertEqual(new, 'doing')
        self.assertEqual(by, 'sess-a')

    # REQ-7：軌跡只記真正發生的變更，被 ClaimConflict 擋下的轉態不算數
    def test_set_status_does_not_record_event_on_claim_conflict(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        with self.assertRaises(todo_store.ClaimConflict):
            todo_store.set_status(self.con, self.keys['A'], 'done', by='sess-b')
        events = todo_store.list_events(self.con, self.keys['A'])
        self.assertEqual(len(events), 1)  # 只有原本那次成功的 doing 轉態

    # REQ-6：doing -> done -> dep add 三類事件都要出現在同一份軌跡裡，
    # 且是三個獨立列，不是同一列被 UPDATE 覆寫成最後一次的值。
    def test_event_trail_accumulates_across_status_and_dep_changes(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        # 同一個 owner 把自己認領的條目轉 done，不該被 ClaimConflict 擋下——
        # by 要跟認領時一致，否則守衛會把 None 當成不同人
        todo_store.set_status(self.con, self.keys['A'], 'done', by='sess-a')
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        raw_count = self.con.execute(
            'SELECT COUNT(*) FROM todo_event WHERE todo_key=?',
            (self.keys['A'],)).fetchone()[0]
        self.assertEqual(raw_count, 3, '三次變更該是三個獨立列')
        events = todo_store.list_events(self.con, self.keys['A'])
        self.assertEqual([e[0] for e in events], ['dep_add', 'status', 'status'])
        self.assertEqual(events[1][2], 'done')    # 較新的 status 事件
        self.assertEqual(events[2][2], 'doing')   # 較舊的 status 事件

    # REQ-6
    def test_list_events_orders_newest_first(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        todo_store.set_status(self.con, self.keys['A'], 'done', by='sess-a')
        events = todo_store.list_events(self.con, self.keys['A'])
        self.assertEqual(events[0][2], 'done')
        self.assertEqual(events[1][2], 'doing')

    # G-6：改標題必換 key（既有行為）。todo_dep 有 from_key/to_key 兩個
    # 獨立欄位，各自都要遷移——只改其中一欄會在另一欄留下孤兒邊，
    # 且 doctor 的懸空邊 WARN 會把「條目被改名」誤報成「條目被刪除」。
    def test_edit_title_migrates_both_from_and_to_key_dep_edges(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')  # A->B
        todo_store.add_dep(self.con, self.keys['C'], self.keys['A'], 'blocks')  # C->A
        new_key = todo_store.edit_title(self.con, self.keys['A'], '新標題A')
        self.assertNotEqual(new_key, self.keys['A'])

        from_side = self.con.execute(
            'SELECT from_key FROM todo_dep WHERE to_key=?',
            (self.keys['B'],)).fetchone()[0]
        to_side = self.con.execute(
            'SELECT to_key FROM todo_dep WHERE from_key=?',
            (self.keys['C'],)).fetchone()[0]
        self.assertEqual(from_side, new_key, 'from_key 欄沒跟著搬')
        self.assertEqual(to_side, new_key, 'to_key 欄沒跟著搬')

        # 舊 key 不該再留下任何孤兒邊（不論它原本是 from_key 還是 to_key）
        orphan = self.con.execute(
            'SELECT COUNT(*) FROM todo_dep WHERE from_key=? OR to_key=?',
            (self.keys['A'], self.keys['A'])).fetchone()[0]
        self.assertEqual(orphan, 0)

    # G-6：todo_event 併入 _KEYED_TABLES 的遷移迴圈——舊 key 查詢必須
    # 完全查不到孤兒事件，不是只驗證新 key 查得到就算數。
    def test_edit_title_migrates_event_history_to_new_key(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        new_key = todo_store.edit_title(self.con, self.keys['A'], '新標題A')

        orphan = self.con.execute(
            'SELECT COUNT(*) FROM todo_event WHERE todo_key=?',
            (self.keys['A'],)).fetchone()[0]
        self.assertEqual(orphan, 0, '舊 key 底下不該留下孤兒事件列')
        self.assertEqual(todo_store.list_events(self.con, self.keys['A']), [])

        events_new = todo_store.list_events(self.con, new_key)
        self.assertEqual(len(events_new), 1)
        self.assertEqual(events_new[0][2], 'doing')

    # G-6 最刁鑽的情境：事件記在 A 名下（因為 add_dep 只在 from_key 側寫事件），
    # 但改名的是 B（邊的另一側）——A 名下事件裡嵌的 B key 也要跟著換。
    # 這是全表掃描（而非只篩被改名條目自己的 todo_key）存在的理由。
    def test_edit_title_migrates_embedded_key_in_other_entrys_event(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        new_key_b = todo_store.edit_title(self.con, self.keys['B'], '新標題B')
        ev = todo_store.list_events(self.con, self.keys['A'])
        self.assertEqual(ev[0][2], f'blocks:{new_key_b}')

    def test_remove_item_deletes_dep_edges_where_it_is_from_key(self):
        # T-004：remove_item() 清 todo_dep 目前沒有專屬回歸測試，只在
        # Stage 5 修 C-1（list --ready crash）時手動驗證過——這裡補上。
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.remove_item(self.con, self.keys['A'])
        n = self.con.execute(
            'SELECT COUNT(*) FROM todo_dep WHERE from_key=? OR to_key=?',
            (self.keys['A'], self.keys['A'])).fetchone()[0]
        self.assertEqual(n, 0, 'todo_dep 有殘留 from_key 側的孤兒邊')

    def test_remove_item_deletes_dep_edges_where_it_is_to_key(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.remove_item(self.con, self.keys['B'])
        n = self.con.execute(
            'SELECT COUNT(*) FROM todo_dep WHERE from_key=? OR to_key=?',
            (self.keys['B'], self.keys['B'])).fetchone()[0]
        self.assertEqual(n, 0, 'todo_dep 有殘留 to_key 側的孤兒邊')

    def test_remove_item_then_list_ready_does_not_crash(self):
        # C-1 的重現路徑本身：dep add -> rm --force -> list --ready
        # 不能再 TypeError（is_ready 對已刪除 blocker 的 None 防護）。
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.remove_item(self.con, self.keys['A'])
        ready = todo_store.ready_keys(self.con)
        self.assertIn(self.keys['B'], ready)  # blocker 沒了，B 應變 ready


if __name__ == '__main__':
    unittest.main()
