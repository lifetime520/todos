import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import todo_audit
import todo_store
from fixtures import latest_snapshot

# 來源是 .audit/ 的遷移備份，不是 ~/.claude/todos/tradingbot.md ——
# 後者已於 Phase 4（2026-08-09）刪除。指著不存在的檔會讓測試靜默 skip，
# 報 OK 卻什麼都沒驗。
MD = latest_snapshot('tradingbot')


class TestParity(unittest.TestCase):
    """換輸入層而輸出不變 —— 這是遷移沒改變語義的證據。

    不比對歷史 baseline 的絕對數字（那是快照，多 session 共寫會變），
    而是同一時刻用兩條輸入路徑跑，比對彼此。
    """

    def setUp(self):
        # 刻意不 skipTest —— 沒有 fixture 就是保護失效，必須紅燈
        self.assertIsNotNone(MD, '找不到 tradingbot 的 pre-migrate 備份')
        self.assertTrue(MD.exists(), f'{MD} 不存在')
        # ⚠️ 不能直接拿線上 DB 跟 md 比 —— 待辦檔是多 session 共寫的活檔
        # （實測 2026-08-08 一小時內被別的 session 改了三次），兩邊取樣時刻
        # 不同就會比出差異，而那測到的是「資料變沒變」不是「程式對不對」。
        # 故每次都用當下的 md 現建一個臨時 DB，隔離掉資料變動這個變因。
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'tradingbot.sqlite'
        con = todo_store.connect(self.db)
        todo_store.save_parsed(
            con, 'tradingbot',
            todo_store.parse_md_lossless(MD.read_text(encoding='utf-8')))
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_db_source_yields_same_todos_as_md(self):
        from_md = todo_audit.load_todos(MD)
        from_db = todo_audit.load_todos(self.db)
        self.assertEqual(len(from_md), len(from_db),
                         'md 與 DB 的條目數不同')
        for a, b in zip(from_md, from_db):
            self.assertEqual(a['title'], b['title'])
            self.assertEqual(a['date'], b['date'])
            self.assertEqual(a['body'], b['body'])
            self.assertEqual(a['line'], b['line'], '行號必須一致')

    def test_anchor_extraction_identical(self):
        for a, b in zip(todo_audit.load_todos(MD),
                        todo_audit.load_todos(self.db)):
            self.assertEqual(todo_audit.extract_anchors(a),
                             todo_audit.extract_anchors(b))

    def test_md_path_still_uses_legacy_parser(self):
        # 回歸保護：.md 走 parse_todos，行為與遷移前完全相同
        self.assertEqual(todo_audit.load_todos(MD), todo_audit.parse_todos(MD))

    def test_done_items_excluded_from_audit(self):
        # done/unpick 不該進稽核 —— 稽核的對象是待辦，不是完成記錄
        before = len(todo_audit.load_todos(self.db))
        con = todo_store.connect(self.db)
        key = con.execute('SELECT key FROM todo WHERE sort_order=0').fetchone()[0]
        todo_store.set_status(con, key, 'done')
        other = con.execute('SELECT key FROM todo WHERE sort_order=1').fetchone()[0]
        todo_store.set_status(con, other, 'unpick', note='暫不處理')
        con.close()
        after = len(todo_audit.load_todos(self.db))
        self.assertEqual(after, before - 2,
                         'done 與 unpick 的條目仍被算進稽核')


if __name__ == '__main__':
    unittest.main()
