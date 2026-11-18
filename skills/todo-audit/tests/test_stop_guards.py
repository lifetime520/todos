"""stop_guards：回合結束守衛（2026-11-17 D-047；2026-11-18 D-067 撤掉未來式檢查，只留子程序盤點）。

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

    def test_future_promise_no_longer_blocks(self):
        # 2026-11-18 Cast 裁示全域撤掉未來式檢查（誤傷窗口：#13 回合結尾被逼成向 Cast 提問，73%）
        self.assertIsNone(self._run({'session_id': 's1', 'stop_hook_active': False,
                                     'last_assistant_message': REAL_FAILURE}, []))
        out = self._run({'session_id': 's1', 'stop_hook_active': False,
                         'last_assistant_message': REAL_FAILURE}, [(77, 9000, 'x')])
        self.assertNotIn('未來式', out['reason'])
        self.assertIn('77', out['reason'])

    def test_active_allows_everything(self):
        self.assertIsNone(self._run({'session_id': 's1', 'stop_hook_active': True,
                                     'last_assistant_message': REAL_FAILURE}, [(77, 9000, 'x')]))


class TestHookEndToEnd(unittest.TestCase):
    HOOK = Path(__file__).resolve().parents[3] / 'hooks' / 'stop-guards.sh'

    def _run(self, payload, home):
        env = dict(os.environ, HOME=home, STOP_GUARDS_SCRIPTS=str(Path(g.__file__).parent), STOP_GUARDS_NO_PROC='1')
        return subprocess.run(['bash', str(self.HOOK)], input=payload, capture_output=True, text=True, env=env, timeout=20)

    def test_future_promise_passes_through_hook(self):
        with tempfile.TemporaryDirectory() as h:
            inp = {'session_id': 's1', 'transcript_path': '/nonexistent', 'stop_hook_active': False,
                'last_assistant_message': REAL_FAILURE}
            out = self._run(json.dumps(inp, ensure_ascii=False), h)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, ''))

    def test_garbage_input_fails_open(self):
        with tempfile.TemporaryDirectory() as h:
            out = self._run('not json', h)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, ''))


if __name__ == '__main__':
    unittest.main()
