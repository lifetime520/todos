"""T-001：`never_existed()` 的 symbol_history 快取必須尊重 `--db` 參數。

todo_audit.py:1028 呼叫 `never_existed(repo, gone_syms, default_db(repo))`，
硬編 `default_db(repo)`，而使用者在命令列傳入的 `--db PATH` 要到第 1052 行
才被解析、只用在 `persist()`。結果是：`never_existed()` 的 symbol_history
快取永遠寫進 `default_db(repo)`（$HOME/.claude/todos/.audit/{repo}.sqlite），
即使使用者明確要求把所有 sqlite 輸出導向另一個路徑。

這條測試釘住「傳 --db 時，never_existed 的快取也該落在同一個檔案，
default_db(repo) 完全不該被建立」。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import todo_audit  # noqa: E402


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'test')
    _git(repo, 'config', 'commit.gpgsign', 'false')


def _commit_all(repo, msg):
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', msg)


class TestNeverExistedRespectsDbFlag(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        _init_repo(self.repo)
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'foo.py').write_text('def foo(): pass\n', encoding='utf-8')
        _commit_all(self.repo, 'init')

        # `PhantomSymbolNeverExisted123` 從未出現在任何檔案或 git 歷史裡，
        # 因此一定落入 gone_syms → 觸發 never_existed() 的快取寫入路徑。
        self.md = self.root / 'todo.md'
        self.md.write_text(
            '- [ ] [2026-01-01] 測試用 `PhantomSymbolNeverExisted123`\n',
            encoding='utf-8')

        self.custom_db = self.root / 'custom.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / 'todo_audit.py'), str(self.md), str(self.repo),
             '--db', str(self.custom_db)],
            capture_output=True, text=True, env=env)

    def test_symbol_history_written_to_custom_db_not_default(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, f'stdout={r.stdout}\nstderr={r.stderr}')

        default_db_path = self.home / '.claude' / 'todos' / '.audit' / f'{self.repo.name}.sqlite'
        self.assertFalse(
            default_db_path.exists(),
            f'never_existed() 不該碰 default_db(repo)（{default_db_path}）——'
            f'使用者已明確指定 --db {self.custom_db}')

        self.assertTrue(self.custom_db.exists(), '--db 指定的檔案應該被建立')
        con = sqlite3.connect(self.custom_db)
        rows = con.execute(
            'SELECT never_existed FROM symbol_history WHERE symbol = ?',
            ('PhantomSymbolNeverExisted123',)).fetchall()
        con.close()
        self.assertEqual(rows, [(1,)],
                          '快取應該落在 --db 指定的檔案，且標記為從未存在')


if __name__ == '__main__':
    unittest.main()
