"""符號樣本太小時，命中率門檻改為 WEAK_AUDIT 降級，不再 FATAL 中止。

背景（兩起同日、獨立 repo 的實測事故，根因都是掃描範圍而非掃描器故障）：
  cast-power  0/1 = 0%  —— 純文件 repo，符號住在無副檔名的 scripts/castpower
  todos       1/5 = 20% —— search_dirs 排除了 tests/（commit 60a80b0 改 config）
兩起都以 FATAL 收場、完全拿不到稽核結果。

門檻本身（todo_audit.py 的 0.30）在 len(symbols) <= 3 時會退化成「命中數是不是 0」
——1/3 = 33% 已高於門檻，n<=3 唯一能觸發它的就是零命中。本檔釘住的是：

  1. 小樣本零命中 → exit 0、印 WEAK_AUDIT 警告、run.degraded 為真
  2. 降級後每一條的顯示狀態是 WEAK_AUDIT（保護沒被拿掉：假陽性 ALL_GONE
     不可能被當成「可移除」）
  3. 樣本夠大（n>=4）時 FATAL 行為完全不變 —— 這條是防止「修好癱瘓」變成
     「順手把護欄也拆了」
  4. 小樣本但命中 → 不降級，正常稽核（不得為了修 1 而讓所有小 repo 恆降級）

測試會實際跑 todo_audit.py 主流程（含真實 git 子行程），因此每個測試自建一次性
臨時 git repo；HOME 一律指向 tempfile，不得碰真實 ~/.claude/todos。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import todo_store  # noqa: E402


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'test')
    _git(repo, 'config', 'commit.gpgsign', 'false')


def run_audit(md, repo, db, home):
    env = {'HOME': str(home),
           'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'todo_audit.py'), str(md), str(repo),
         '--db', str(db)],
        capture_output=True, text=True, env=env)


class SmallSampleFixture(unittest.TestCase):
    """repo 內有可掃的檔案（所以**不會**走既有的零命中降級），
    差別只在待辦裡有幾個符號、命中幾個 —— 把變因單獨隔離在樣本大小上。

    'scripts' 是內建 SEARCH_DIRS 之一，'.sh' 是內建 scan_exts 之一，
    所以不必寫 config 就能保證 files 非空。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        _init_repo(self.repo)
        (self.repo / 'scripts').mkdir()
        # existingSymbol 真的存在於被掃描的檔案裡，供「小樣本但命中」那條用
        (self.repo / 'scripts' / 'tool.sh').write_text(
            '#!/bin/sh\nexistingSymbol() { echo hi; }\n', encoding='utf-8')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-q', '-m', 'add scripts')

        self.date = '2026-01-01'
        self.md = self.root / 'todo.md'
        self.db = self.root / 'audit.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def write_todo(self, title):
        self.full_title = f'[{self.date}] {title}'
        self.key = todo_store.todo_key(self.date, self.full_title)
        self.md.write_text(f'- [ ] [{self.date}] {title}\n', encoding='utf-8')

    def assert_scanned_something(self, out):
        """守住這批測試自己的前提：必須是「有掃到檔案」的情境，
        否則測到的是既有的零命中降級，不是本次要釘的小樣本降級。"""
        self.assertNotIn('search_dirs 零命中', out,
                         '這批測試的前提是有掃到檔案；掉進零命中降級就測錯東西了')


class TestSmallSampleDegradesInsteadOfFatal(SmallSampleFixture):

    def test_single_missing_symbol_does_not_fatal(self):
        # 驗收 1：n=1、命中 0 —— 正是 cast-power 那起
        self.write_todo('修掉 `nonexistentSymbolOne` 的問題')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assert_scanned_something(out)
        self.assertEqual(r.returncode, 0,
                         f'小樣本零命中不得 FATAL 中止 —— stderr:{r.stderr}\nstdout:{r.stdout}')
        self.assertNotIn('FATAL', out)

    def test_single_missing_symbol_warns_weak_audit(self):
        # 驗收 1：要出聲，不能靜默放行 —— 靜默放行等於把護欄拆掉
        self.write_todo('修掉 `nonexistentSymbolOne` 的問題')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assertIn('WEAK_AUDIT', out, '降級必須明確告知，否則使用者會照常採信 GONE 判定')
        self.assertIn('todo-audit.json', out, '修復指引須含 config 路徑（兩起事故根因都是掃描範圍）')

    def test_degraded_flag_persisted_and_state_masked(self):
        # 驗收 2：保護沒被拿掉 —— 顯示層仍蓋成 WEAK_AUDIT，假 ALL_GONE 不可能被當成可移除
        self.write_todo('修掉 `nonexistentSymbolOne` 的問題')
        r = run_audit(self.md, self.repo, self.db, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        con = todo_store.connect(self.db)
        try:
            row = con.execute(
                'SELECT degraded FROM run ORDER BY id DESC LIMIT 1').fetchone()
            self.assertIsNotNone(row, '應該要有一筆 run 記錄')
            self.assertEqual(row[0], 1, 'run.degraded 必須落地，否則後續讀取端看不出這次不可盡信')
            self.assertEqual(todo_store.state_of(con, self.key), 'WEAK_AUDIT',
                             '降級 run 的顯示狀態必須是 WEAK_AUDIT')
        finally:
            con.close()

    def test_three_missing_symbols_still_degrades(self):
        # 邊界內側：n=3 時 0.30 門檻仍退化成「命中數是不是 0」，一樣降級
        self.write_todo('處理 `symbolAlpha`、`symbolBeta`、`symbolGamma`')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assert_scanned_something(out)
        self.assertEqual(r.returncode, 0, f'n=3 仍屬小樣本 —— stderr:{r.stderr}')
        self.assertIn('WEAK_AUDIT', out)


class TestLargeSampleStillFatal(SmallSampleFixture):
    """護欄回歸：修好「小樣本癱瘓」不得順手把大樣本的保護也拆掉。
    已知真實事故是 136 個符號全零命中 → 75 條假陽性 ALL_GONE。"""

    def test_five_missing_symbols_still_fatal(self):
        # 驗收 3：n=5、命中 0 —— 樣本夠大，命中率有鑑別力，維持 FATAL
        self.write_todo('處理 `symbolAlpha`、`symbolBeta`、`symbolGamma`、'
                        '`symbolDelta`、`symbolEpsilon`')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assert_scanned_something(out)
        self.assertNotEqual(r.returncode, 0, '樣本夠大時必須維持 FATAL 中止')
        self.assertIn('FATAL', out)

    def test_fatal_message_points_at_scan_scope_first(self):
        # 兩起實測事故的根因都是掃描範圍，訊息要先指向它，別讓人從讀原始碼開始查
        self.write_todo('處理 `symbolAlpha`、`symbolBeta`、`symbolGamma`、'
                        '`symbolDelta`、`symbolEpsilon`')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assertIn('search_dirs', out, 'FATAL 訊息須指出目前的掃描範圍')
        self.assertIn('todo-audit.json', out, 'FATAL 訊息須指出改哪個檔')


class TestSmallSampleWithHitIsNotDegraded(SmallSampleFixture):
    """反向邊界：不得為了修「小樣本零命中」而讓所有小 repo 恆為降級 ——
    那會讓 WEAK_AUDIT 變成永遠亮著的燈，等於沒有燈。"""

    def test_single_hitting_symbol_stays_normal(self):
        # 驗收 4：n=1 但命中 1（hit_rate=100%）→ 根本不該進入門檻分支
        self.write_todo('確認 `existingSymbol` 還在')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assert_scanned_something(out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('WEAK_AUDIT', out, '命中率 100% 不得被標成降級')
        con = todo_store.connect(self.db)
        try:
            row = con.execute(
                'SELECT degraded FROM run ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(row[0], 0, '命中的小樣本 run.degraded 必須是 0')
        finally:
            con.close()


if __name__ == '__main__':
    unittest.main()
