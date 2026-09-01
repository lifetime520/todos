"""set_section() 與 CLI `edit --section` 的測試（REQ-3）。

只測 REQ-3；REQ-1/2/4/5 由其他 task 覆蓋。

RED 期望：本檔在 `todo_store.set_section()` 尚未實作、`edit` subparser
尚未加 `--section` 的樹上執行，應全部失敗——
- store 層測試：`AttributeError: module 'todo_store' has no attribute
  'set_section'`
- CLI 層測試：argparse 對 `--section` 報 unrecognized argument（非零
  exit，但不是「因為需求邏輯拒絕」，是「這個旗標根本不存在」）

沿用 test_store.py 的 DB fixture 寫法（TestEditAndRemove）與 test_cli.py
的 run_cli 端到端寫法（TestEditRemoveCommands）。
"""
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
from test_cli import run_cli


class TestSetSectionStoreLevel(unittest.TestCase):
    """單元層：直接呼叫 todo_store.set_section()。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')
        todo_store.save_parsed(self.con, 'demo',
                               todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(self.con)
        # sort_order=0 條目在 SAMPLE 裡位於 `## 🔴 立即處理` 標頭下，
        # 起始 section 為 urgent —— 搬去 'later' 才能明顯看出變化。
        self.key = self.con.execute(
            'SELECT key FROM todo WHERE sort_order=0').fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    # REQ-3 測試點 1：正常搬移後 heading 與 section 兩欄皆更新。
    #
    # 只斷言其中一欄是不夠的——todo_cli.py 的 rows() 直接對 section 欄位
    # 做 WHERE section=?，不會重新用 heading 推導；只寫 heading 不寫
    # section 的實作，這裡就會在「section 欄」這一斷言上被抓到，
    # 不需要依賴測試點 5 的 CLI 端到端才能發現。
    def test_set_section_updates_both_heading_and_section_columns(self):
        before_section = self.con.execute(
            'SELECT section FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        self.assertEqual(before_section, 'urgent',
                         '前置條件錯誤：這條在 SAMPLE 裡本該屬於 urgent')

        todo_store.set_section(self.con, self.key, 'later')

        heading, section = self.con.execute(
            'SELECT heading, section FROM todo WHERE key=?',
            (self.key,)).fetchone()
        self.assertEqual(section, 'later', 'section 欄沒有被更新')
        self.assertIn('⚪', heading or '', 'heading 欄沒有換成 later 對應符號')

    # REQ-3 測試點 2：不合法的 section 值 -> ValueError。
    def test_set_section_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            todo_store.set_section(self.con, self.key, 'bogus-section')
        # 不合法值不該把資料寫壞——section 欄仍是搬移前的值。
        section = self.con.execute(
            'SELECT section FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        self.assertEqual(section, 'urgent')

    # REQ-3 測試點 3：不存在的 key -> KeyError。
    def test_set_section_unknown_key_raises_key_error(self):
        with self.assertRaises(KeyError):
            todo_store.set_section(self.con, 'nosuchkey0000', 'later')


class TestEditSectionCliLevel(unittest.TestCase):
    """CLI 端到端：`edit --section`。"""

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

    # REQ-3 測試點 4：CLI 層 edit --section 端到端可用。
    def test_edit_section_end_to_end_succeeds_and_persists(self):
        r = run_cli('edit', 'T-001', '--section', 'later',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        con = todo_store.connect(
            self.home / '.claude' / 'todos' / '.audit' / 'demo.sqlite')
        section = con.execute(
            "SELECT section FROM todo WHERE short_id='T-001'").fetchone()[0]
        con.close()
        self.assertEqual(section, 'later')

    # REQ-3 測試點 5：搬移後 `list --section <新值>` 確實反映。
    #
    # 這是這個功能存在的理由：_section_of() 中 heading 優先於標題關鍵字，
    # 過去靠 --title 塞 [P2] 之類標記其實不生效，必須直接改 heading/section。
    def test_edit_section_then_list_section_reflects_move(self):
        run_cli('edit', 'T-001', '--section', 'later',
                '--project', 'demo', env_home=self.home)

        later_out = run_cli('list', '--section', 'later',
                            '--project', 'demo', env_home=self.home).stdout
        self.assertIn('T-001', later_out, 'list --section later 沒有反映搬移結果')

        urgent_out = run_cli('list', '--section', 'urgent',
                             '--project', 'demo', env_home=self.home).stdout
        self.assertNotIn('T-001', urgent_out, '搬走後不該還留在原本的 urgent 章節')

    # REQ-3 測試點 6：單獨下 --section（不帶 --title/--line/--spec/--memory）
    # 不被 cmd_edit() 的「需指定參數」檢查誤拒。
    #
    # 若實作只把 --section 加進 subparser，卻忘記把 args.section 算進
    # cmd_edit() 的必要參數判斷式，這裡會拿到 exit=5 與「需指定」訊息，
    # 而不是真的執行搬移。
    def test_edit_section_alone_is_not_rejected_as_missing_args(self):
        r = run_cli('edit', 'T-001', '--section', 'decision',
                    '--project', 'demo', env_home=self.home)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 5,
                            f'單獨下 --section 被誤判成「未指定任何參數」: {out}')
        self.assertNotIn('需指定', out)
        self.assertEqual(r.returncode, 0, out)


class TestSetSectionWritesFullHeadingLine(unittest.TestCase):
    """REQ-4：set_section() 寫入完整標頭行，而非裸符號。

    `todo.heading` 欄的不變式只到「完整標頭行」這一層，不到「必為某份 md
    檔的原文」（見 `todo_store.py` schema 註解 :26-39）：`parse_md_lossless()`
    → `save_parsed()` 這條路徑寫入的是解析既有 md 檔得到的標頭原文整行
    （例如 `## 🔴 立即處理（P0 / 資金安全）（1）`）；`set_section()` 這條
    路徑寫入的則是 `_SECTION_HEADING` 表合成出來的 canonical 整行（如
    `## 🔴 緊急`），不保證對應任何 md 檔裡真實存在的行。本類別鎖定的是
    `set_section()` 這條路徑，用 `assertEqual` 斷言字面值，因為它過去曾
    直接寫入裸符號（如 `'🔴'`），與整行格式不一致；子字串比對
    （`assertIn`）分不出兩者差異——`'## 🔴 緊急'` 與 `'🔴'` 都能讓
    `assertIn('🔴', ...)` 成立。

    對照：`test_set_section_updates_both_heading_and_section_columns`
    （`:65`）用的就是 `assertIn('⚪', ...)`。
    """

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

    def _heading_after_move(self, section):
        todo_store.set_section(self.con, self.key, section)
        return self.con.execute(
            'SELECT heading FROM todo WHERE key=?', (self.key,)).fetchone()[0]

    def test_set_section_writes_full_heading_line_for_urgent(self):
        self.assertEqual(self._heading_after_move('urgent'), '## 🔴 緊急')

    def test_set_section_writes_full_heading_line_for_decision(self):
        self.assertEqual(self._heading_after_move('decision'), '## 🟠 待拍板決策')

    def test_set_section_writes_full_heading_line_for_normal(self):
        self.assertEqual(self._heading_after_move('normal'), '## 🟡 一般')

    def test_set_section_writes_full_heading_line_for_later(self):
        self.assertEqual(self._heading_after_move('later'), '## ⚪ 之後再看')

    def test_set_section_writes_heading_starting_with_hash_marker(self):
        """四種 section 皆須以 `'## '` 開頭——這是與裸符號的關鍵區別。

        單獨列一條斷言 `startswith('## ')`，即使上面四條字面值比對已經隱含
        這件事，也要讓「是不是整行標頭」這個判準有一個不依賴確切措辭、
        只依賴格式的獨立錨點。
        """
        for section in ('urgent', 'decision', 'normal', 'later'):
            with self.subTest(section=section):
                heading = self._heading_after_move(section)
                self.assertTrue(
                    heading.startswith('## '),
                    f'{section} 的 heading 不是完整標頭行：{heading!r}')


class TestSectionOfResolvesNewHeadingLiteral(unittest.TestCase):
    """REQ-4 行為不回歸：set_section() 寫入新標頭行後，
    `_section_of(title, heading)` 仍能正確判定回同一個 section。

    `_section_of()` 對 heading 是子字串比對（`sym in heading`），理論上符號
    還在完整標頭行裡就不受影響；但這件事必須實測，不能只靠推論——萬一
    canonical 標頭行的符號位置或組字方式讓 `in` 判斷失效，這裡會抓到。
    """

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

    def _resolved_section_after_move(self, target_section):
        todo_store.set_section(self.con, self.key, target_section)
        heading = self.con.execute(
            'SELECT heading FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        # 標題刻意不帶任何 [P0]/[Cast 拍板]/[P2] 標記，逼 _section_of()
        # 只能靠 heading 判定——若判定失準，fallback 會把它誤判成 normal。
        return todo_store._section_of('與 heading 判定無關的標題', heading)

    def test_section_of_resolves_urgent_from_new_heading(self):
        self.assertEqual(self._resolved_section_after_move('urgent'), 'urgent')

    def test_section_of_resolves_decision_from_new_heading(self):
        self.assertEqual(
            self._resolved_section_after_move('decision'), 'decision')

    def test_section_of_resolves_normal_from_new_heading(self):
        self.assertEqual(self._resolved_section_after_move('normal'), 'normal')

    def test_section_of_resolves_later_from_new_heading(self):
        self.assertEqual(self._resolved_section_after_move('later'), 'later')


class TestSetSectionKeepsSectionColumnQueryable(unittest.TestCase):
    """REQ-4 行為不回歸：搬移後 `WHERE section=?`（`todo_cli.rows()` 的
    查詢方式）仍撈得到該條目——heading 欄格式改變不該影響 section 欄的
    獨立查詢，因為 `rows()` 從不現算 `_section_of()`，只讀 section 欄。
    """

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

    def _keys_in_section(self, section):
        return [r[0] for r in self.con.execute(
            'SELECT key FROM todo WHERE section=?', (section,)).fetchall()]

    def test_moved_key_is_queryable_by_section_column_for_decision(self):
        todo_store.set_section(self.con, self.key, 'decision')
        self.assertIn(self.key, self._keys_in_section('decision'))

    def test_moved_key_is_queryable_by_section_column_for_normal(self):
        todo_store.set_section(self.con, self.key, 'normal')
        self.assertIn(self.key, self._keys_in_section('normal'))

    def test_moved_key_is_queryable_by_section_column_for_later(self):
        todo_store.set_section(self.con, self.key, 'later')
        self.assertIn(self.key, self._keys_in_section('later'))

    def test_moved_key_no_longer_queryable_under_original_urgent_section(self):
        todo_store.set_section(self.con, self.key, 'later')
        self.assertNotIn(self.key, self._keys_in_section('urgent'))


class TestSectionTablesShareOneSource(unittest.TestCase):
    """兩個方向的查表必須衍生自同一份 _SECTIONS 定義。

    過去 `_HEADING_SECTION`（符號→section）與 `_SECTION_HEADING`
    （section→標頭整行）是兩張獨立維護的表，新增一種 section 要記得改兩處。
    漏改一處**不會有任何測試變紅** —— 該分類會靜默地只在單一方向生效
    （例如 set_section() 搬得進去，但 _section_of() 認不出來）。

    這裡斷言的是「兩張表確實由同一個來源導出」這個結構性質，而不是
    比對兩份寫死的清單 —— 後者只會變成第三個要同步維護的地方。
    """

    def test_every_section_has_canonical_heading(self):
        """_SECTIONS 的每一列都要能導出 canonical 標頭。"""
        for section, heading, _syms in todo_store._SECTIONS:
            self.assertEqual(todo_store._SECTION_HEADING[section], heading)

    def test_section_heading_has_no_extra_entries(self):
        """反向：_SECTION_HEADING 不得有 _SECTIONS 以外的鍵。

        這條擋的是「有人繞過 _SECTIONS 直接往字典塞一筆」。
        """
        self.assertEqual(
            set(todo_store._SECTION_HEADING),
            {section for section, _h, _s in todo_store._SECTIONS})

    def test_every_symbol_maps_back_to_its_section(self):
        """每個可辨識符號都要能經 _HEADING_SECTION 回到原本的 section。"""
        for section, _heading, syms in todo_store._SECTIONS:
            for sym in syms:
                self.assertIn((sym, section), todo_store._HEADING_SECTION)

    def test_heading_section_has_no_extra_entries(self):
        expected = {(sym, section)
                    for section, _h, syms in todo_store._SECTIONS
                    for sym in syms}
        self.assertEqual(set(todo_store._HEADING_SECTION), expected)

    def test_canonical_heading_resolves_back_to_its_own_section(self):
        """端到端閉環：canonical 標頭餵回 _section_of() 要回到原 section。

        這是本類別最重要的一條 —— 它同時看住兩張表，任一邊漏改都會紅。
        標題刻意用會觸發**別的**分支的標記（[P0]→urgent），確保回傳值
        來自 heading 判定而不是標題 fallback。
        """
        for section, heading, _syms in todo_store._SECTIONS:
            with self.subTest(section=section):
                self.assertEqual(
                    todo_store._section_of('[P0] 標題殘留別的標記', heading),
                    section)

    def test_later_accepts_both_historical_symbols(self):
        """⚪ 與 🟢 都要塌成 later —— 這是多對一，不能用反轉字典實作。

        沒有這條的話，「把 _SECTIONS 改成 section→單一符號」這種看似
        更簡潔的重構會靜默地讓 🟢 失去分類能力。
        """
        self.assertEqual(todo_store._section_of('x', '## ⚪ 之後再看'), 'later')
        self.assertEqual(todo_store._section_of('x', '## 🟢 未來規劃'), 'later')


if __name__ == '__main__':
    unittest.main()
