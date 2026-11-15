"""歸屬紀錄生命週期（2026-11-15）：以待辦編號去重、待辦結案即釋放、無編號到期、孤兒認領。"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_store  # noqa: E402
import todo_coord  # noqa: E402


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _add(self, title):
        todo_store.append_item(self.con, 'p', title, '', '')
        key, sid = self.con.execute(
            'SELECT key, short_id FROM todo ORDER BY sort_order DESC LIMIT 1').fetchone()
        if not sid:
            todo_store.assign_short_ids(self.con)
            sid = self.con.execute('SELECT short_id FROM todo WHERE key=?', (key,)).fetchone()[0]
        return key, sid

    def _topics(self, include_expired=True):
        return sorted(o['topic'] for o in todo_coord.owner_who(self.con, '', include_expired=include_expired))


class TestDedupeByTodoId(_DB):
    def test_same_todo_different_topic_text_keeps_one_row(self):
        todo_coord.owner_set(self.con, 'T-990 手動現貨下單收尾（PD-1b）', 'tradingbot #3', todo_ids='T-990')
        todo_coord.owner_set(self.con, 'T-990 手動現貨下單收尾', 'tradingbot #3', todo_ids='T-990')
        self.assertEqual(self._topics(), ['T-990 手動現貨下單收尾'])

    def test_todo_id_derived_from_topic_prefix(self):
        todo_coord.owner_set(self.con, 'T-1008 COIN-M T1 符號', 'tradingbot #1')
        self.assertEqual(todo_coord.owner_list(self.con)[0]['todo_ids'], 'T-1008')

    def test_different_todo_sets_not_merged(self):
        todo_coord.owner_set(self.con, '傘', 'tradingbot #13', todo_ids='T-957')
        todo_coord.owner_set(self.con, '子題', 'tradingbot #1', todo_ids='T-957, T-995')
        self.assertEqual(self._topics(), ['傘', '子題'])


class TestReleaseOnDone(_DB):
    def test_mark_done_removes_owner_row(self):
        key, sid = self._add('做某事')
        todo_coord.owner_set(self.con, f'{sid} 做某事', 'tradingbot #3')
        todo_store.set_status(self.con, key, 'done', by='x')
        self.assertEqual(self._topics(), [])

    def test_unpick_removes_owner_row(self):
        key, sid = self._add('擱置的事')
        todo_coord.owner_set(self.con, '擱置', 'tradingbot #3', todo_ids=sid)
        todo_store.set_status(self.con, key, 'unpick', by='x', note='不做了')
        self.assertEqual(self._topics(), [])

    def test_pending_keeps_owner_row(self):
        key, sid = self._add('還在做')
        todo_coord.owner_set(self.con, '還在做', 'tradingbot #3', todo_ids=sid)
        todo_store.set_status(self.con, key, 'pending', by='x')
        self.assertEqual(self._topics(), ['還在做'])

    def test_multi_id_row_shrinks_until_all_done(self):
        k1, s1 = self._add('一')
        k2, s2 = self._add('二')
        todo_coord.owner_set(self.con, '兩題', 'tradingbot #1', todo_ids=f'{s1}, {s2}')
        todo_store.set_status(self.con, k1, 'done', by='x')
        rows = todo_coord.owner_list(self.con)
        self.assertEqual([(r['topic'], r['todo_ids']) for r in rows], [('兩題', s2)])
        todo_store.set_status(self.con, k2, 'done', by='x')
        self.assertEqual(self._topics(), [])

    def test_prefix_match_is_exact_id(self):
        # T-99 結案不得動到 T-990 的紀錄
        k, sid = self._add('短號')
        self.con.execute('UPDATE todo SET short_id=? WHERE key=?', ('T-99', k))
        self.con.commit()
        todo_coord.owner_set(self.con, '長號', 'tradingbot #3', todo_ids='T-990')
        todo_store.set_status(self.con, k, 'done', by='x')
        self.assertEqual(self._topics(), ['長號'])

    def test_auto_done_by_flags_releases(self):
        import todo_flags
        key, sid = self._add('旗標')
        todo_coord.owner_set(self.con, '旗標', 'tradingbot #3', todo_ids=sid)
        for name in todo_flags.FLAGS:
            todo_store.set_progress(self.con, key, 'set', name)
        self.assertEqual(self._topics(), [])


class TestReleaseEdges(_DB):
    def test_release_failure_rolls_back_status(self):
        from unittest import mock
        key, sid = self._add('回滾')
        todo_coord.owner_set(self.con, '回滾', 'tradingbot #3', todo_ids=sid)
        with mock.patch.object(todo_store, 'release_owner_rows', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                todo_store.set_status(self.con, key, 'done', by='x')
        self.con.rollback()
        self.assertEqual(self.con.execute('SELECT status FROM todo WHERE key=?', (key,)).fetchone()[0], 'pending')
        self.assertEqual(self._topics(), ['回滾'])

    def test_auto_done_with_null_short_id_does_not_crash(self):
        import todo_flags
        key, sid = self._add('無短號')
        todo_coord.owner_set(self.con, '別人', 'tradingbot #3', todo_ids='T-1')
        self.con.execute('UPDATE todo SET short_id=NULL WHERE key=?', (key,))
        self.con.commit()
        for name in todo_flags.FLAGS:
            todo_store.set_progress(self.con, key, 'set', name)
        self.assertEqual(self.con.execute('SELECT status FROM todo WHERE key=?', (key,)).fetchone()[0], 'done')
        self.assertEqual(self._topics(), ['別人'])


class TestNoIdExpiry(_DB):
    def _age(self, topic, hours):
        t = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')
        self.con.execute('UPDATE coord_owner SET updated_at=? WHERE topic=?', (t, topic))
        self.con.commit()

    def test_no_id_row_older_than_ttl_hidden_by_default(self):
        todo_coord.owner_set(self.con, '無編號調查', 'tradingbot #3')
        self._age('無編號調查', todo_coord.OWNER_NOID_TTL_H + 1)
        self.assertEqual(self._topics(include_expired=False), [])
        rows = todo_coord.owner_list(self.con)  # list 預設全列（castpower 閘門依賴）
        self.assertTrue(rows[0]['expired'])

    def test_no_id_row_within_ttl_listed(self):
        todo_coord.owner_set(self.con, '無編號調查', 'tradingbot #3')
        self._age('無編號調查', todo_coord.OWNER_NOID_TTL_H - 1)
        self.assertEqual(self._topics(include_expired=False), ['無編號調查'])

    def test_id_row_never_expires(self):
        todo_coord.owner_set(self.con, '有編號', 'tradingbot #3', todo_ids='T-1')
        self._age('有編號', 24 * 30)
        self.assertEqual(self._topics(include_expired=False), ['有編號'])

    def test_board_skips_expired(self):
        sess = Path(self.tmp.name) / 'sessions'
        sess.mkdir()
        import os
        (sess / f'{os.getpid()}.json').write_text(json.dumps(
            {'pid': os.getpid(), 'sessionId': 's', 'name': 'tradingbot #3', 'status': 'idle'}))
        todo_coord.owner_set(self.con, '舊調查', 'tradingbot #3')
        self._age('舊調查', todo_coord.OWNER_NOID_TTL_H + 1)
        rows = todo_coord.board_rows(self.con, sessions_dir=sess)
        self.assertEqual(rows[0]['topics'], [])


class TestOrphanClaims(_DB):
    def test_ws_claim_without_workspace_is_orphan(self):
        ws = Path(self.tmp.name) / 'ws'
        (ws / 'tradingbot-alive').mkdir(parents=True)
        k1, s1 = self._add('孤兒')
        k2, s2 = self._add('有家')
        k3, s3 = self._add('非工作區認領')
        todo_store.set_status(self.con, k1, 'doing', by='ws:gone')
        todo_store.set_status(self.con, k2, 'doing', by='ws:alive')
        todo_store.set_status(self.con, k3, 'doing', by='tradingbot #3')
        self.con.execute("UPDATE todo SET status_at='2026-01-01T00:00:00' WHERE key=?", (k1,))
        self.con.execute("UPDATE todo_event SET at='2026-01-01T00:00:00' WHERE todo_key=?", (k1,))
        self.con.commit()
        todo_store.append_note(self.con, k1, '🔍  等驗收')
        rows = todo_coord.orphan_claims(self.con, ws_root=ws)
        self.assertEqual([r['short_id'] for r in rows], [s1])
        self.assertEqual(rows[0]['by'], 'ws:gone')
        self.assertEqual(rows[0]['claimed_at'], '2026-01-01T00:00:00')
        self.assertGreater(rows[0]['last_activity'], '2026-11-01')  # 來自 note 事件

    def test_suffix_of_other_workspace_is_not_a_match(self):
        ws = Path(self.tmp.name) / 'ws'
        (ws / 'tradingbot-coinm-t2').mkdir(parents=True)
        self.con.execute("INSERT OR REPLACE INTO doc_meta(k, v) VALUES('project_path', '/var/repos/tradingbot')")
        k, s = self._add('短名')
        todo_store.set_status(self.con, k, 'doing', by='ws:t2')
        self.assertEqual([r['short_id'] for r in todo_coord.orphan_claims(self.con, ws_root=ws)], [s])

    def test_missing_ws_root_reports_nothing(self):
        k1, _ = self._add('孤兒')
        todo_store.set_status(self.con, k1, 'doing', by='ws:gone')
        self.assertEqual(todo_coord.orphan_claims(self.con, ws_root=Path(self.tmp.name) / 'nope'), [])



class TestOwnerPrune(_DB):
    def _raw(self, topic, ids, at):
        self.con.execute('INSERT INTO coord_owner(topic, owner, todo_ids, updated_at)'
                         ' VALUES(?,?,?,?)', (topic, 'tradingbot #3', ids, at))
        self.con.commit()

    def test_prune_legacy_rows(self):
        kd, sd = self._add('已完成')
        ko, so = self._add('進行中')
        todo_store.set_status(self.con, kd, 'done', by='x')
        self._raw(f'{sd} 舊題', None, '2026-11-14T10:00:00')        # 無 todo_ids、待辦已 done
        self._raw(f'{so} 新寫法', so, '2026-11-15T10:00:00')
        self._raw(f'{so} 舊寫法', so, '2026-11-14T10:00:00')        # 重複
        self._raw('混合', f'{sd}, {so}', '2026-11-13T10:00:00')     # 拿掉已結案的那個後也和上面重複
        msgs = todo_coord.owner_prune(self.con)
        rows = todo_coord.owner_list(self.con, include_expired=True)
        self.assertEqual([(r['topic'], r['todo_ids']) for r in rows], [(f'{so} 新寫法', so)])
        self.assertTrue(any('刪除（待辦已結案）' in m for m in msgs))
        self.assertEqual(todo_coord.owner_prune(self.con), ['沒有需要清理的紀錄。'])

    def test_dry_run_writes_nothing(self):
        kd, sd = self._add('已完成')
        todo_store.set_status(self.con, kd, 'done', by='x')
        self._raw(f'{sd} 舊題', None, '2026-11-14T10:00:00')
        msgs = todo_coord.owner_prune(self.con, dry_run=True)
        self.assertTrue(msgs and msgs != ['沒有需要清理的紀錄。'])
        rows = todo_coord.owner_list(self.con, include_expired=True)
        self.assertEqual([(r['topic'], r['todo_ids']) for r in rows], [(f'{sd} 舊題', None)])


if __name__ == '__main__':
    unittest.main()
