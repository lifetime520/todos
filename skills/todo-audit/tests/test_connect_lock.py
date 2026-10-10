"""connect() 不得在已遷移的 DB 上寫入，且等鎖時間要足夠長。

2026-10-06 tradingbot 實證：別的程序寫 todo DB 時，唯讀的 `todo_cli show` 因
connect() 每次都跑 migration／backfill UPDATE 而搶寫鎖，5 秒（sqlite3 預設）
等不到就拋 `database is locked`，被呼叫端誤報成「查無此條」（tradingbot T-722）。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_audit
import todo_flags
import todo_store


class TestConnectLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 't.sqlite'
        todo_store.connect(self.db).close()  # 建庫並完成遷移

    def tearDown(self):
        self.tmp.cleanup()

    def _raw(self):
        return sqlite3.connect(str(self.db), timeout=0.05)

    def test_connect_on_migrated_db_succeeds_while_another_writer_holds_lock(self):
        writer = self._raw()
        writer.execute('BEGIN IMMEDIATE')  # 另一個程序正在寫（持 RESERVED 鎖）
        try:
            with mock.patch.object(todo_store, 'BUSY_TIMEOUT_S', 0.05):
                con = todo_store.connect(self.db)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM todo').fetchone()[0], 0)
            con.close()
        finally:
            writer.rollback()
            writer.close()

    def test_connect_waits_long_enough_for_lock(self):
        con = todo_store.connect(self.db)
        self.assertGreaterEqual(con.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
        con.close()

    def test_null_progress_still_backfilled_to_zero_on_migrated_db(self):
        # todo_audit.persist 以 6 欄 INSERT 新增的列 progress 為 NULL
        raw = self._raw()
        raw.execute("INSERT INTO todo(key,date,title,content_hash) VALUES('k1','2026-10-10','t','h')")
        raw.commit(); raw.close()
        con = todo_store.connect(self.db)
        self.assertEqual(con.execute("SELECT progress FROM todo WHERE key='k1'").fetchone()[0], 0)
        con.close()

    def test_migrated_db_done_row_with_null_progress_is_not_flooded_to_all_flags(self):
        # 「舊 done 灌滿旗標」是上線前的一次性回填；遷移後才出現的 NULL done 列
        # （audit 插入後被標 done）若被灌成 ALL_FLAGS，等於憑空宣告已部署、已驗收
        raw = self._raw()
        raw.execute("INSERT INTO todo(key,date,title,content_hash,status)"
                    " VALUES('k2','2026-10-10','t','h','done')")
        raw.commit(); raw.close()
        con = todo_store.connect(self.db)
        self.assertEqual(con.execute("SELECT progress FROM todo WHERE key='k2'").fetchone()[0], 0)
        con.close()

    def test_legacy_db_done_rows_still_backfilled_to_all_flags(self):
        # 未蓋指紋的舊 DB：維持既有一次性回填語意
        raw = self._raw()
        raw.execute("INSERT INTO todo(key,date,title,content_hash,status)"
                    " VALUES('k3','2026-10-10','t','h','done')")
        raw.execute('PRAGMA user_version = 0')
        raw.commit(); raw.close()
        con = todo_store.connect(self.db)
        self.assertEqual(con.execute("SELECT progress FROM todo WHERE key='k3'").fetchone()[0],
                         todo_flags.ALL_FLAGS)
        con.close()

    def test_schema_fingerprint_mismatch_reruns_migrations(self):
        raw = self._raw()
        raw.execute('DROP INDEX ux_todo_short_id')
        raw.execute('PRAGMA user_version = 12345')
        raw.commit(); raw.close()
        con = todo_store.connect(self.db)
        idx = con.execute("SELECT name FROM sqlite_master WHERE name='ux_todo_short_id'").fetchone()
        self.assertIsNotNone(idx)
        con.close()

    def test_null_progress_backfill_skipped_when_locked_read_still_succeeds(self):
        # NULL 列補 0 只是整理，不得讓唯讀查詢等滿 BUSY_TIMEOUT 後拋 locked
        raw = self._raw()
        raw.execute("INSERT INTO todo(key,date,title,content_hash) VALUES('k4','2026-10-10','t','h')")
        raw.commit(); raw.close()
        writer = self._raw()
        writer.execute('BEGIN IMMEDIATE')
        try:
            with mock.patch.object(todo_store, 'BUSY_TIMEOUT_S', 0.05):
                con = todo_store.connect(self.db)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM todo').fetchone()[0], 1)
            con.close()
        finally:
            writer.rollback()
            writer.close()

    def test_audit_persist_new_key_on_migrated_db(self):
        # 舊版 `INSERT INTO todo VALUES(?,?,?,?,?,?)` 在已遷移（20 欄）的 DB 上拋
        # 「table todo has N columns but 6 values were supplied」
        todos = [{'date': '2026-10-10', 'title': '新條目', 'body': ['- [ ] 新條目']}]
        results = [{'checks': [], 'state': 'ALIVE', 'anchor_count': 0, 'gone': [],
                    'commits_since': []}]
        todo_audit.persist(self.db, 'x.md', '/repo', todos, results, set(), 0.0,
                           '2026-10-10T00:00:00')
        con = todo_store.connect(self.db)
        row = con.execute('SELECT title, progress FROM todo WHERE key=?',
                          (todo_audit.todo_key(todos[0]),)).fetchone()
        self.assertEqual(row, ('新條目', 0))
        con.close()


if __name__ == '__main__':
    unittest.main()
