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


if __name__ == '__main__':
    unittest.main()
