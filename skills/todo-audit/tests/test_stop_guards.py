"""stop_guards：回合結束守衛（2026-11-17，#11 空轉 19.5 小時檢討後 Cast 核准的改進 1、3）。

實證（#11 transcript 045b68fe…）：
1. 11-17 03:09 回報結尾寫「下一步是 B……我會先提出具體設計……等你確認後再實作」後結束回合，
   設計沒寫、也沒提問，空轉到 22:37 Cast 來問。記憶裡早有「宣告下一步不是進度」，擋不住。
2. 同 session 底下 12 個 4～11 天前的等待迴圈未結束，其中一個 `do :; done` 忙迴圈燒掉 32 小時 CPU。
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import stop_guards as g

# 11-17 03:09 實際結尾（節錄）
REAL_FAILURE = (
    '**下一步是 B（peer 回報批次化）**。這會改變所有 peer 的回報方式，我會先提出具體設計：'
    '哪些算緊急、收件檔的格式、#13 多久讀一次。等你確認後再實作。\n\n'
    '本 session 新增 0 條待辦、關閉 1 條（T-1014，tester #801 PASS 後自動結案）。')


class TestFutureGuard(unittest.TestCase):
    def test_real_failure_blocks(self):
        r = g.future_guard(REAL_FAILURE, used_ask=False, stop_hook_active=False)
        self.assertIsNotNone(r)
        self.assertIn('AskUserQuestion', r)

    def test_question_mark_in_tail_allows(self):
        self.assertIsNone(g.future_guard('我會照 A 做。要用 A 還是 B？', False, False))

    def test_used_ask_allows(self):
        self.assertIsNone(g.future_guard(REAL_FAILURE, used_ask=True, stop_hook_active=False))

    def test_stop_hook_active_allows(self):
        self.assertIsNone(g.future_guard(REAL_FAILURE, used_ask=False, stop_hook_active=True))

    def test_plain_completion_allows(self):
        self.assertIsNone(g.future_guard('已完成，tester #801 PASS。本 session 新增 0 條。', False, False))

    def test_future_phrase_only_far_above_tail_allows(self):
        text = '我會先查 log。\n' + ('已查證。' * 300) + '\n結論：根因是 X，已修。'
        self.assertIsNone(g.future_guard(text, False, False))

    def test_other_future_forms_block(self):
        for t in ('接下來我會補測試。', '稍後再處理 B。', '之後再做這段。', '等你回覆後再動手。'):
            self.assertIsNotNone(g.future_guard(t, False, False), t)

    def test_external_wait_allows(self):
        for t in ('等 #744 跑完我會量，再補一筆 commit。', 'tester #801 PASS 後我會清工作區。',
                  'Task 3 還在跑，完成後我會收到通知。', '結果回來後，我會整理成總評。',
                  'fast 轉綠後，我會請審查者複審。', '跑完我會把 log 交回實作者。',
                  '目前在跑 build 和單元測試。跑完會重新送審、提交、送 tester。',
                  '全綠之後，我會把這兩處修正送原本那位 code-reviewer 複審。'):
            self.assertIsNone(g.future_guard(t, False, False), t)

    def test_user_wait_blocks_even_with_external_wait(self):
        t = 'fast 跑完後我會整理設計，等你確認後再實作。'
        self.assertIsNotNone(g.future_guard(t, False, False))

    def test_waiting_on_user_without_question_blocks(self):
        for t in ('設計文件還在等你核准，核准後我就寫實作計畫。', '還在等你決定。'):
            self.assertIsNotNone(g.future_guard(t, False, False), t)

    def test_unconditional_promise_blocks(self):
        self.assertIsNotNone(g.future_guard('我會整理收尾清單給 #13，確認能不能結案。', False, False))

    def test_external_wait_elsewhere_does_not_whitewash_promise(self):
        # 審查發現 1：結案句裡的「PASS 後」不得放行另一句的無條件承諾
        for t in ('我會先提出具體設計。\n\n本 session 關閉 1 條（T-1014，tester #801 PASS 後自動結案）。',
                  '我會整理清單給 #13。（T-1000 已部署後驗證完）',
                  '最後我會補文件。', '這段之後再做。', '我會先查；等 tester 跑完後就回報'):
            self.assertIsNotNone(g.future_guard(t, False, False), t)

    def test_benign_phrases_allowed(self):
        # 審查發現 3
        for t in ('我會議記錄已更新。', '下一步：無。全部完成。', '已修好。之後再有類似問題可以直接查 log。',
                  '這次不需要等你確認，我已直接做完。', '不必等你確認，已做完。'):
            self.assertIsNone(g.future_guard(t, False, False), t)

    def test_quoted_phrases_ignored(self):
        # 2026-11-18 實際誤擋：引述句型「還在等你決定」不是自己的承諾
        for t in ('多數是「還在等你決定」卻沒用問句結尾，這是刻意要擋的。',
                  '結尾寫了「我會先提出設計，等你確認後再實作」就結束回合。',
                  '偵測 `我會` 這類字樣。'):
            self.assertIsNone(g.future_guard(t, False, False), t)

    def test_promise_outside_quotes_still_blocks(self):
        self.assertIsNotNone(g.future_guard('「A」選項我會再研究，等你確認後再做。', False, False))

    def test_empty_or_none_allows(self):
        self.assertIsNone(g.future_guard('', False, False))
        self.assertIsNone(g.future_guard(None, False, False))


class TestTurnUsedAsk(unittest.TestCase):
    def _write(self, records):
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    @staticmethod
    def _user(text):
        return {'type': 'user', 'message': {'role': 'user', 'content': text}}

    @staticmethod
    def _tool(name):
        return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': {}}]}}

    @staticmethod
    def _result():
        return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}}

    def test_ask_in_current_turn(self):
        p = self._write([self._user('hi'), self._tool('AskUserQuestion'), self._result(), self._tool('Bash'), self._result()])
        self.assertTrue(g.turn_used_ask(p))

    def test_ask_only_in_previous_turn(self):
        p = self._write([self._user('a'), self._tool('AskUserQuestion'), self._result(), self._user('b'), self._tool('Bash'), self._result()])
        self.assertFalse(g.turn_used_ask(p))

    def test_odd_json_lines_skipped(self):
        p = self._write([self._user('hi'), self._tool('AskUserQuestion'), 123, {'type': 'user', 'message': 'str'}, self._result()])
        self.assertTrue(g.turn_used_ask(p))

    def test_missing_file(self):
        self.assertFalse(g.turn_used_ask('/nonexistent/x.jsonl'))


class TestProcessGuard(unittest.TestCase):
    def test_new_stale_blocks_and_records(self):
        stale = [(111, 4 * 86400, "until ! pgrep -f 'x'; do sleep 5; done")]
        reason, pids = g.process_guard(stale, reported=set(), stop_hook_active=False)
        self.assertIn('111', reason)
        self.assertIn('kill', reason)
        self.assertEqual(pids, {111})

    def test_already_reported_silent(self):
        reason, pids = g.process_guard([(111, 9000, 'x')], reported={111}, stop_hook_active=False)
        self.assertIsNone(reason)

    def test_none_or_hook_active_silent(self):
        self.assertIsNone(g.process_guard([], set(), False)[0])
        self.assertIsNone(g.process_guard([(1, 9000, 'x')], set(), True)[0])


class TestStaleChildren(unittest.TestCase):
    """真程序實測：起一個帶 shell-snapshots 字樣的子 shell，門檻設 0 秒應被列出；不帶字樣的不列。"""

    def _spawn(self, cmd):
        p = subprocess.Popen(['bash', '-c', cmd], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def cleanup():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        self.addCleanup(cleanup)
        return p

    def test_lists_only_bash_tool_shells_over_age(self):
        tagged = self._spawn('true shell-snapshots; sleep 30; true')
        plain = self._spawn('sleep 30; true')
        time.sleep(0.2)
        pids = [p for p, _, _ in g.stale_children(os.getpid(), min_age_s=0)]
        self.assertIn(tagged.pid, pids)
        self.assertNotIn(plain.pid, pids)
        self.assertEqual([], g.stale_children(os.getpid(), min_age_s=3600))

    def test_stat_handles_parens_and_spaces_in_comm(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        exe = Path(d) / 'a) (b c'
        shutil.copy('/bin/sleep', exe)
        p = subprocess.Popen([str(exe), '30'], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.1)
        comm, ppid, _ = g._stat(p.pid)
        self.assertEqual((comm, ppid), ('a) (b c', os.getpid()))

    def test_stat_missing_pid_none(self):
        self.assertIsNone(g._stat(2 ** 22 + 12345))

    def test_find_claude_pid_walks_up(self):
        stats = {10: ('bash', 9, 0), 9: ('python3', 8, 0), 8: ('claude', 1, 0)}
        with mock.patch.object(g, '_stat', side_effect=lambda pid: stats.get(pid)):
            self.assertEqual(g.find_claude_pid(10), 8)
        with mock.patch.object(g, '_stat', side_effect=lambda pid: {10: ('bash', 1, 0)}.get(pid)):
            self.assertIsNone(g.find_claude_pid(10))


class TestRunHook(unittest.TestCase):
    """hook 主流程：注入 claude pid 與狀態目錄，與執行環境無關。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def _run(self, inp, stale):
        with mock.patch.object(g, 'stale_children', return_value=stale):
            return g.run_hook(inp, claude_pid=4242, state_dir=self.dir)

    def test_same_pid_reported_once_via_state_file(self):
        inp = {'session_id': 's1', 'stop_hook_active': False}
        out = self._run(inp, [(77, 9000, 'x')])
        self.assertEqual(out['decision'], 'block')
        self.assertIn('77', out['reason'])
        self.assertIsNone(self._run(inp, [(77, 9000, 'x')]))
        self.assertEqual(self._run(inp, [(77, 9000, 'x'), (78, 9000, 'y')])['decision'], 'block')

    def test_corrupt_state_file_does_not_disable_guard(self):
        (self.dir / 's1.json').write_text('123')
        out = self._run({'session_id': 's1', 'stop_hook_active': False}, [(77, 9000, 'x')])
        self.assertEqual(out['decision'], 'block')

    def test_own_state_file_touched_so_not_pruned(self):
        # 長壽 session：狀態檔 8 天沒寫、這次也沒新 pid；別的 session 觸發清理時不得把它刪掉
        st = self.dir / 's1.json'
        st.write_text('[77]')
        os.utime(st, (time.time() - 8 * 86400,) * 2)
        self.assertIsNone(self._run({'session_id': 's1', 'stop_hook_active': False}, [(77, 9000, 'x')]))
        self._run({'session_id': 's2', 'stop_hook_active': False}, [(90, 9000, 'z')])
        self.assertTrue(st.exists())

    def test_old_state_files_pruned(self):
        old = self.dir / 'old.json'
        old.write_text('[]')
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        self._run({'session_id': 's1', 'stop_hook_active': False}, [(77, 9000, 'x')])
        self.assertFalse(old.exists())

    def test_both_guards_combined(self):
        out = self._run({'session_id': 's1', 'stop_hook_active': False,
                         'last_assistant_message': REAL_FAILURE}, [(77, 9000, 'x')])
        self.assertIn('未來式', out['reason'])
        self.assertIn('77', out['reason'])

    def test_active_allows_everything(self):
        self.assertIsNone(self._run({'session_id': 's1', 'stop_hook_active': True,
                                     'last_assistant_message': REAL_FAILURE}, [(77, 9000, 'x')]))


class TestHookEndToEnd(unittest.TestCase):
    HOOK = Path(__file__).resolve().parents[3] / 'hooks' / 'stop-guards.sh'

    def _run(self, payload, home):
        env = dict(os.environ, HOME=home, STOP_GUARDS_SCRIPTS=str(Path(g.__file__).parent), STOP_GUARDS_NO_PROC='1')
        return subprocess.run(['bash', str(self.HOOK)], input=payload, capture_output=True, text=True, env=env, timeout=20)

    def test_blocks_real_failure_then_allows_when_active(self):
        with tempfile.TemporaryDirectory() as h:
            inp = {'session_id': 's1', 'transcript_path': '/nonexistent', 'stop_hook_active': False,
                'last_assistant_message': REAL_FAILURE}
            out = self._run(json.dumps(inp, ensure_ascii=False), h)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(json.loads(out.stdout)['decision'], 'block')
            inp['stop_hook_active'] = True
            self.assertEqual(self._run(json.dumps(inp, ensure_ascii=False), h).stdout.strip(), '')

    def test_garbage_input_fails_open(self):
        with tempfile.TemporaryDirectory() as h:
            out = self._run('not json', h)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, ''))


if __name__ == '__main__':
    unittest.main()
