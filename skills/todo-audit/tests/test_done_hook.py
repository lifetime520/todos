"""hooks/todo-done.sh 的端到端測試（直接跑 shell，不經 todo_cli 的 Python 介面）。

起因（2026-10-01，cast-power T-017）：照規則先 `mark <T> doing --by <自己>` 認領，
做完後照規則用 `todo-done.sh` 標完成，卻被 set_status() 的認領守衛以
ClaimConflict 擋下——todo-done.sh 從不傳 `--by`，守衛把「本次標記者」讀成
空字串，於是任何已認領的條目都被當成「他人的 doing」。而錯誤訊息只建議
`--force`，把合法認領者導向「強制接管」這條本來給搶單用的路。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / 'scripts'
REPO = SKILL.parent.parent
HOOK = REPO / 'hooks' / 'todo-done.sh'
sys.path.insert(0, str(SCRIPTS))
import todo_store


class TestTodoDoneHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / 'home'
        # todo-done.sh 寫死呼叫 $HOME/.claude/skills/todo-audit/scripts/todo_cli.py，
        # 連回本 checkout 的 skill，測的才是這份原始碼而不是機器上裝的那份
        skills = self.home / '.claude' / 'skills'
        skills.mkdir(parents=True)
        (skills / 'todo-audit').symlink_to(SKILL)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        todo_store.connect(audit / 'demo.sqlite').close()
        # PROJECT = repo root 的 basename，所以專案目錄就叫 demo
        self.proj = base / 'demo'
        self.proj.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.proj)], check=True)
        self.env = {'HOME': str(self.home),
                    'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        add = self._cli('add', '要收尾的任務', '🏷️  x', '💡  y')
        self.assertEqual(add.returncode, 0, add.stderr)
        self.sid = add.stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def _cli(self, *args):
        # 與 todo-done.sh 相同的專案綁定參數，否則會撞 bind_project 的衝突偵測
        return subprocess.run(
            [sys.executable, str(SCRIPTS / 'todo_cli.py'),
             '--project', 'demo', '--path', str(self.proj),
             '--remote', '(no-remote)', *args],
            capture_output=True, text=True, env=self.env)

    def _hook(self, *args):
        return subprocess.run(['bash', str(HOOK), *args], cwd=self.proj,
                              capture_output=True, text=True, env=self.env)

    def _status(self):
        con = todo_store.connect(
            self.home / '.claude' / 'todos' / '.audit' / 'demo.sqlite')
        try:
            return con.execute('SELECT status, status_by FROM todo'
                               ' WHERE short_id=?', (self.sid,)).fetchone()
        finally:
            con.close()

    def test_claimant_can_mark_own_item_done_with_by(self):
        self._cli('mark', self.sid, 'doing', '--by', 'sess-a')
        r = self._hook(self.sid, '--by', 'sess-a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._status(), ('done', 'sess-a'))

    def test_without_by_claimed_item_stays_blocked_and_hints_by(self):
        # 不帶 --by 時守衛仍要擋——它防的是「B 把 A 正在做的條目標掉」。
        # 但訊息必須告訴合法認領者正確的路是 --by，而不是只給 --force。
        self._cli('mark', self.sid, 'doing', '--by', 'sess-a')
        r = self._hook(self.sid)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._status(), ('doing', 'sess-a'))
        self.assertIn('--by', r.stderr)

    def test_other_session_by_is_still_blocked(self):
        self._cli('mark', self.sid, 'doing', '--by', 'sess-a')
        r = self._hook(self.sid, '--by', 'sess-b')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._status(), ('doing', 'sess-a'))

    def test_unclaimed_item_old_usage_still_works(self):
        # 舊介面（只給關鍵字）對未認領條目的行為不變
        r = self._hook(self.sid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._status()[0], 'done')

    def test_unknown_argument_is_usage_error_not_ignored(self):
        # 靜默吞掉多餘參數的話，打錯成 `-by` 會退化成「不帶 --by」而且沒人發現
        r = self._hook(self.sid, '-by', 'sess-a')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._status()[0], 'pending')

    def test_by_without_value_is_usage_error(self):
        r = self._hook(self.sid, '--by')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._status()[0], 'pending')


if __name__ == '__main__':
    unittest.main()
