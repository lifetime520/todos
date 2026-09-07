"""T-014：檔案錨點補上「從未存在於 git 歷史 → 不算 GONE 訊號」的過濾。

符號錨點自 never_existed() 起就有這層過濾（理由：待辦作者常自創描述性稱呼，
查無此符號不代表它曾被移除）。檔案錨點沒有 —— verify() 對 file 一律是
`'OK' if hits else 'GONE'`。這個不對稱在 anchor_exts 開放 md 之前是沉睡的，
開了之後直接變成假 GONE 的來源，實測兩類：

  glob 樣板      cast-power 的 collab-stage*-round-*-notes.md 被 RE_FILE
                 截成 notes.md，本地當然查無此檔 → 假 GONE
  repo 外產物    tradingbot 若全域開 md 會多出 21 個查無此檔的錨點，
                 都是 `~/.claude/…` 底下的檔、以及 analysis.md／bindings.md
                 這類跑完就刪的 workspace 產物

兩類都從未進過版控，判 GONE 等於把仍成立的待辦標成可移除 —— 本工具最怕的失敗。
tests/test_anchor_exts.py 的檔頭已記載這個缺口，當時選擇用 opt-in 繞過而非修補。

本檔釘住四件事：
  1. 從未存在於 git 歷史的檔名，不得構成 GONE 訊號
  2. **曾存在後被刪**的檔案仍須判 GONE —— 這是本修改唯一可能失去的訊號，
     若這條鬆掉，等於把過濾做成了「檔案錨點一律不檢查」
  3. file:line 形式與裸檔名形式受同一層保護（兩者比對鍵都是 basename）
  4. 快取落在 file_history、尊重 --db，且不與 symbol_history 共用鍵空間
"""
import json
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


class _RepoCase(unittest.TestCase):
    """共用 fixture：一個開了 anchor_exts=md 的 repo，內含一個已被刪除的 legacy.md。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        _init_repo(self.repo)

        # scripts/ 讓預設 search_dirs 有命中 —— 否則稽核降級為全 repo 掃描
        # 並印 WEAK_AUDIT，GONE 判定本身失效，這幾條測試就測不到東西了。
        (self.repo / 'scripts').mkdir()
        (self.repo / 'scripts' / 'tool.sh').write_text('#!/bin/sh\n', encoding='utf-8')
        (self.repo / 'HANDBOOK.md').write_text('# handbook\n', encoding='utf-8')
        (self.repo / 'legacy.md').write_text('# legacy\n', encoding='utf-8')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-q', '-m', 'init')

        # legacy.md 曾存在於歷史、現已刪除 —— 真 GONE 的對照組
        (self.repo / 'legacy.md').unlink()
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'drop legacy')

        (self.repo / '.claude').mkdir()
        (self.repo / '.claude' / 'todo-audit.json').write_text(
            json.dumps({'anchor_exts': list(todo_audit.ANCHOR_EXTS) + ['md']}),
            encoding='utf-8')

        self.md = self.root / 'todo.md'
        self.db = self.root / 'audit.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def _todo(self, line):
        self.md.write_text(line if line.endswith('\n') else line + '\n',
                           encoding='utf-8')

    def _run(self):
        env = {'HOME': str(self.home),
               'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / 'todo_audit.py'), str(self.md),
             str(self.repo), '--db', str(self.db)],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, f'stdout={r.stdout}\nstderr={r.stderr}')
        return r


class TestPhantomFilesAreNotGone(_RepoCase):

    def test_never_existed_file_is_not_a_gone_signal(self):
        """`phantom.md` 從未進過版控 —— 零訊號，不得判 GONE。"""
        self._todo('- [ ] [2026-01-01] 產出的 `phantom.md` 要留存')
        out = self._run().stdout
        self.assertNotRegex(
            out, r'ALL_GONE\s+1',
            f'從未存在於 git 歷史的檔名不構成 GONE 訊號\n{out}')

    def test_glob_template_basename_is_not_a_gone_signal(self):
        """cast-power T-009 的實際成因：glob 樣板被 RE_FILE 截成裸檔名。"""
        self._todo('- [ ] [2026-01-01] 保存 collab-stage1-round-1-notes.md 逐輪記錄')
        out = self._run().stdout
        self.assertNotRegex(out, r'ALL_GONE\s+1', out)

    def test_file_line_form_gets_the_same_filter(self):
        """兩種形式的比對鍵都是 basename，保護必須一致。"""
        self._todo('- [ ] [2026-01-01] 對照 docs/phantom.md:42 的說明')
        out = self._run().stdout
        self.assertNotRegex(out, r'ALL_GONE\s+1', out)


class TestRealDeletionsStillGone(_RepoCase):
    """本修改唯一可能失去的訊號 —— 必須保住，否則過濾就成了全面停檢。"""

    def test_deleted_file_is_still_gone(self):
        self._todo('- [ ] [2026-01-01] 清掉 `legacy.md` 的殘留引用')
        out = self._run().stdout
        self.assertRegex(
            out, r'ALL_GONE\s+1',
            f'legacy.md 曾存在於 git 歷史後被刪，仍是真 GONE 訊號\n{out}')

    def test_live_file_stays_alive(self):
        self._todo('- [ ] [2026-01-01] 更新 `HANDBOOK.md` 的章節')
        out = self._run().stdout
        self.assertRegex(out, r'ALIVE\s+1', out)


class TestFileHistoryCache(_RepoCase):

    def test_cache_lands_in_file_history_respecting_db_flag(self):
        self._todo('- [ ] [2026-01-01] 產出的 `phantom.md` 要留存')
        self._run()

        default_db = self.home / '.claude' / 'todos' / '.audit' / f'{self.repo.name}.sqlite'
        self.assertFalse(default_db.exists(),
                         f'指定 --db 時不該碰 default_db（{default_db}）')

        con = sqlite3.connect(self.db)
        rows = con.execute(
            'SELECT never_existed FROM file_history WHERE path = ?',
            ('phantom.md',)).fetchall()
        # 鍵空間必須分開：檔案查路徑（git log -- pathspec）、符號查內容（-S），
        # 語意不同，同名時共用一張表會互相污染判定。
        sym = con.execute(
            'SELECT COUNT(*) FROM symbol_history WHERE symbol = ?',
            ('phantom.md',)).fetchone()[0]
        con.close()

        self.assertEqual(rows, [(1,)], '檔案版快取應落在 file_history 並標為從未存在')
        self.assertEqual(sym, 0, 'file_history 不得與 symbol_history 共用鍵空間')


class TestFilesNeverExistedUnit(unittest.TestCase):
    """單元層：不經主流程直接驗判定本身。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'repo'
        _init_repo(self.repo)
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs' / 'kept.md').write_text('x\n', encoding='utf-8')
        (self.repo / 'gone.md').write_text('y\n', encoding='utf-8')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-q', '-m', 'init')
        (self.repo / 'gone.md').unlink()
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'drop')

    def tearDown(self):
        self.tmp.cleanup()

    def test_classifies_by_git_history_not_worktree(self):
        got = todo_audit.files_never_existed(
            self.repo, {'kept.md', 'gone.md', 'phantom.md'})
        self.assertEqual(got, {'phantom.md'},
                         'kept/gone 都在歷史裡；只有 phantom 從未存在')

    def test_subdirectory_files_are_found_by_basename(self):
        """錨點記的是裸檔名，pathspec 必須同時涵蓋根目錄與子目錄。"""
        self.assertEqual(
            todo_audit.files_never_existed(self.repo, {'kept.md'}), set(),
            'docs/kept.md 在子目錄，以 basename 查仍須命中歷史')


if __name__ == '__main__':
    unittest.main()
