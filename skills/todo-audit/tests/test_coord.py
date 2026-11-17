"""多 session 協作：角色、題目擁有者、裁決紀錄、看板（P1，2026-11-14）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_store  # noqa: E402
import todo_coord  # noqa: E402


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = todo_store.connect(Path(self.tmp.name) / 't.sqlite')
        self.sess = Path(self.tmp.name) / 'sessions'
        self.sess.mkdir()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _session(self, pid, name, sid, status='idle'):
        (self.sess / f'{pid}.json').write_text(json.dumps(
            {'pid': pid, 'sessionId': sid, 'name': name, 'status': status,
             'updatedAt': 1794621650004}))


class TestShortName(unittest.TestCase):
    def test_strips_role_suffix(self):
        self.assertEqual(todo_coord.short_name('tradingbot #3｜實作｜←#13'), 'tradingbot #3')

    def test_plain_name_unchanged(self):
        self.assertEqual(todo_coord.short_name(' tradingbot #13 '), 'tradingbot #13')


class TestRole(_DB):
    def test_set_then_get_uses_short_name(self):
        todo_coord.role_set(self.con, 'tradingbot #3｜實作｜←#13', 'impl', reports_to='tradingbot #13')
        r = todo_coord.role_get(self.con, 'tradingbot #3')
        self.assertEqual((r['role'], r['reports_to']), ('impl', 'tradingbot #13'))

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            todo_coord.role_set(self.con, 'tradingbot #3', 'boss')

    def test_window_lookup(self):
        todo_coord.role_set(self.con, 'tradingbot #13', 'window')
        self.assertEqual(todo_coord.window_name(self.con), 'tradingbot #13')

    def test_no_window_returns_none(self):
        self.assertIsNone(todo_coord.window_name(self.con))


class TestOwner(_DB):
    def test_set_overwrites_previous_owner(self):
        todo_coord.owner_set(self.con, 'S9b-F 資金費', 'tradingbot #11', role='research', by='x')
        todo_coord.owner_set(self.con, 'S9b-F 資金費', 'tradingbot #13', role='research', by='y')
        rows = todo_coord.owner_who(self.con, '資金費')
        self.assertEqual([(r['topic'], r['owner']) for r in rows], [('S9b-F 資金費', 'tradingbot #13')])

    def test_who_matches_todo_ids(self):
        todo_coord.owner_set(self.con, '手動帳本略過現貨', 'tradingbot #13', todo_ids='T-991', by='x')
        self.assertEqual(len(todo_coord.owner_who(self.con, 't-991')), 1)

    def test_who_no_hit_is_empty(self):
        self.assertEqual(todo_coord.owner_who(self.con, '不存在'), [])

    def test_rm(self):
        todo_coord.owner_set(self.con, 'a', 'tradingbot #1', by='x')
        self.assertTrue(todo_coord.owner_rm(self.con, 'a'))
        self.assertFalse(todo_coord.owner_rm(self.con, 'a'))


class TestDecision(_DB):
    def test_ids_increment(self):
        a = todo_coord.decision_add(self.con, topic='t', quote='q1')
        b = todo_coord.decision_add(self.con, topic='t', quote='q2')
        self.assertEqual((a, b), ('D-001', 'D-002'))

    def test_supersede_marks_old_and_hides_from_active(self):
        todo_coord.decision_add(self.con, topic='資金費', quote='自算')
        todo_coord.decision_add(self.con, topic='資金費', quote='只歸 key', supersedes='D-001')
        active = todo_coord.decision_list(self.con, topic='資金費')
        self.assertEqual([d['id'] for d in active], ['D-002'])
        allrows = todo_coord.decision_list(self.con, topic='資金費', include_all=True)
        self.assertEqual({d['id']: d['status'] for d in allrows},
                         {'D-001': 'superseded', 'D-002': 'active'})

    def test_supersede_unknown_id_rejected_and_nothing_written(self):
        with self.assertRaises(ValueError):
            todo_coord.decision_add(self.con, topic='t', quote='q', supersedes='D-099')
        self.assertEqual(todo_coord.decision_list(self.con, include_all=True), [])

    def test_cannot_supersede_already_superseded(self):
        todo_coord.decision_add(self.con, topic='t', quote='a')
        todo_coord.decision_add(self.con, topic='t', quote='b', supersedes='D-001')
        with self.assertRaises(ValueError):
            todo_coord.decision_add(self.con, topic='t', quote='c', supersedes='D-001')
        active = [d['id'] for d in todo_coord.decision_list(self.con, topic='t')]
        self.assertEqual(active, ['D-002'])

    def test_superseded_not_matched(self):
        todo_coord.decision_add(self.con, topic='資金費歸屬', quote='資金費自算')
        todo_coord.decision_add(self.con, topic='資金費歸屬', quote='資金費只歸 key', supersedes='D-001')
        self.assertEqual([d['id'] for d in todo_coord.decision_match(self.con, '資金費歸屬怎麼算')], ['D-002'])

    def test_explicit_id_kept(self):
        self.assertEqual(todo_coord.decision_add(self.con, topic='t', quote='q', id='D-005'), 'D-005')
        self.assertEqual(todo_coord.decision_add(self.con, topic='t', quote='q'), 'D-006')

    def test_match_by_text_returns_active_hits(self):
        todo_coord.decision_add(self.con, topic='S9b-F 資金費歸屬', quote='資金費僅歸在 api key 底下')
        todo_coord.decision_add(self.con, topic='協作協定', quote='指派 #1 #3 #4')
        hits = todo_coord.decision_match(self.con, '資金費要不要算進網格損益？')
        self.assertEqual([d['id'] for d in hits], ['D-001'])


class TestSessions(_DB):
    def test_live_sessions_filters_dead_pids(self):
        self._session(os.getpid(), 'tradingbot #11｜重工改進計劃｜直屬 Cast', 'sid-a')
        self._session(999999999, 'tradingbot #9', 'sid-dead')
        names = [s['name'] for s in todo_coord.live_sessions(self.sess)]
        self.assertEqual(names, ['tradingbot #11｜重工改進計劃｜直屬 Cast'])

    def test_name_for_session_id(self):
        self._session(os.getpid(), 'tradingbot #13', 'sid-13')
        self.assertEqual(todo_coord.name_for_session_id('sid-13', self.sess), 'tradingbot #13')
        self.assertIsNone(todo_coord.name_for_session_id('nope', self.sess))

    def test_board_lists_role_and_topics_within_limit(self):
        self._session(os.getpid(), 'tradingbot #13｜窗口＋研究', 'sid-13')
        todo_coord.role_set(self.con, 'tradingbot #13', 'window')
        todo_coord.owner_set(self.con, 'S9b-F 資金費', 'tradingbot #13', role='research', by='x')
        text = todo_coord.board_text(self.con, self.sess, max_chars=600)
        self.assertIn('tradingbot #13', text)
        self.assertIn('窗口', text)
        self.assertIn('S9b-F 資金費', text)
        self.assertLessEqual(len(text), 600)

    def test_board_flags_session_without_role_or_title_role(self):
        self._session(os.getpid(), 'tradingbot #2', 'sid-2')
        text = todo_coord.board_text(self.con, self.sess, max_chars=600)
        self.assertIn('tradingbot #2 ｜ ? ⚠ 未指派角色', text)

    def test_board_flags_title_without_role_suffix(self):
        self._session(os.getpid(), 'tradingbot #2', 'sid-2')
        todo_coord.role_set(self.con, 'tradingbot #2', 'general')
        text = todo_coord.board_text(self.con, self.sess, max_chars=600)
        self.assertIn('⚠ 標題缺角色，應改為「tradingbot #2｜一般」', text)

    def test_board_without_warning_when_role_and_title_ok(self):
        self._session(os.getpid(), 'tradingbot #13｜窗口＋研究', 'sid-13')
        todo_coord.role_set(self.con, 'tradingbot #13', 'window')
        rows = todo_coord.board_rows(self.con, self.sess)
        self.assertEqual([r['warn'] for r in rows], [None])

    def test_in_project_filter(self):
        f = todo_coord._in_project
        self.assertTrue(f('/var/repos/tradingbot', '/var/repos/tradingbot'))
        self.assertTrue(f('/home/u/WorkSpace/ws/tradingbot-binance-s6', '/var/repos/tradingbot'))
        self.assertFalse(f('/home/u', '/var/repos/tradingbot'))
        self.assertFalse(f('/var/repos/tradingbot-other', '/var/repos/tradingbot'))



class TestInbox(_DB):
    """B 案（D-048）：peer 不需回應的回報寫收件匣，窗口被喚醒時才一次讀。"""

    def setUp(self):
        super().setUp()
        todo_coord.role_set(self.con, 'tradingbot #13', 'window')

    def test_post_defaults_to_window_and_take_marks_read(self):
        r = todo_coord.inbox_post(self.con, '已推送待 tester #900', 'tradingbot #1｜實作', todo_ids='T-1')
        self.assertEqual(r['to'], 'tradingbot #13')
        items = todo_coord.inbox_take_unread(self.con, 'tradingbot #13｜窗口')
        self.assertEqual([(i['from_name'], i['body'], i['todo_ids']) for i in items],
                         [('tradingbot #1', '已推送待 tester #900', 'T-1')])
        self.assertEqual(todo_coord.inbox_take_unread(self.con, 'tradingbot #13'), [])

    def test_take_without_mark_keeps_unread_until_marked(self):
        todo_coord.inbox_post(self.con, 'a', 'tradingbot #1')
        items = todo_coord.inbox_take_unread(self.con, 'tradingbot #13', mark=False)
        self.assertEqual(len(todo_coord.inbox_take_unread(self.con, 'tradingbot #13', mark=False)), 1)
        todo_coord.inbox_mark_read(self.con, [i['id'] for i in items])
        self.assertEqual(todo_coord.inbox_take_unread(self.con, 'tradingbot #13'), [])

    def test_take_respects_char_budget_and_keeps_rest_unread(self):
        for i in range(5):
            todo_coord.inbox_post(self.con, f'{i}' + 'x' * 400, 'tradingbot #1')
        first = todo_coord.inbox_take_unread(self.con, 'tradingbot #13', max_chars=1000)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(todo_coord.inbox_take_unread(self.con, 'tradingbot #13')), 3)

    def test_take_always_returns_at_least_one(self):
        todo_coord.inbox_post(self.con, 'y' * 1500, 'tradingbot #1')
        self.assertEqual(len(todo_coord.inbox_take_unread(self.con, 'tradingbot #13', max_chars=100)), 1)

    def test_flush_only_when_backlog_old_and_not_recently_flushed(self):
        t0 = '2026-11-17T10:00:00'
        r = todo_coord.inbox_post(self.con, 'a', 'tradingbot #1', now=t0)
        self.assertFalse(r['flush'])
        r = todo_coord.inbox_post(self.con, 'b', 'tradingbot #3', now='2026-11-17T10:59:00')
        self.assertFalse(r['flush'])
        r = todo_coord.inbox_post(self.con, 'c', 'tradingbot #3', now='2026-11-17T11:01:00')
        self.assertTrue(r['flush'])
        self.assertEqual((r['unread'], r['oldest_min']), (3, 61))
        r = todo_coord.inbox_post(self.con, 'd', 'tradingbot #4', now='2026-11-17T11:30:00')
        self.assertFalse(r['flush'])  # 60 分鐘內已彙整過
        r = todo_coord.inbox_post(self.con, 'e', 'tradingbot #4', now='2026-11-17T12:02:00')
        self.assertTrue(r['flush'])

    def test_no_window_raises(self):
        self.con.execute('DELETE FROM coord_role')
        with self.assertRaises(ValueError):
            todo_coord.inbox_post(self.con, 'a', 'tradingbot #1')

    def test_overlong_body_raises(self):
        with self.assertRaises(ValueError):
            todo_coord.inbox_post(self.con, 'x' * (todo_coord.INBOX_MAX_CHARS + 1), 'tradingbot #1')

    def test_empty_body_raises(self):
        with self.assertRaises(ValueError):
            todo_coord.inbox_post(self.con, '  ', 'tradingbot #1')


class TestResolveTarget(_DB):
    def test_name_forms(self):
        for to in ('tradingbot #13', 'tradingbot #13｜窗口', 'tradingbot #13 [3fa9c1]',
                   'tradingbot #13｜窗口 [3fa9c1]'):
            self.assertEqual(todo_coord.resolve_target(to, self.sess), 'tradingbot #13', to)

    def test_uds_address_via_sessions(self):
        self._session(os.getpid(), 'tradingbot #13｜窗口', 'sid-w')
        to = f'uds:/run/user/1000/cc-socks/{os.getpid()}.sock'
        self.assertEqual(todo_coord.resolve_target(to, self.sess), 'tradingbot #13')

    def test_unknown_uds_none(self):
        self.assertIsNone(todo_coord.resolve_target('uds:/x/999999.sock', self.sess))
        self.assertIsNone(todo_coord.resolve_target('', self.sess))


if __name__ == '__main__':
    unittest.main()
