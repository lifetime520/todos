"""REQ-2：SEARCH_DIRS 零命中時降級為全 repo 掃描，並全程標記為 WEAK_AUDIT。

釘住 `.castpower/todo-audit-cfg/requirements.md` 的 REQ-2 五條驗收，含 G-1/G-2/G-6
裁決後的正確版本：
  - G-1：WEAK_AUDIT 是 run 級旗標，probe.state 保留 classify() 的真值；
    todo_store.state_of() 才是顯示層取代的地方。
  - G-2：警告訊息要區分「找不到任何 config 檔」與「config 存在但合併後仍零命中」。
  - G-6：classify()（todo_audit.py:492-511）的判定邏輯與輸出集合（六態）完全不變，
    降級與否不得混進 classify() 本身。

這些測試會實際跑 `todo_audit.py` 主流程（含真實 git 子行程），因此每個測試都自建一個
一次性的臨時 git repo；HOME 一律指向 tempfile，不得碰真實 `~/.claude/todos`。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import todo_audit  # noqa: E402
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


def _commit_all(repo, msg):
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', msg)


def _write_md(path, date, title):
    path.write_text(f'- [ ] [{date}] {title}\n', encoding='utf-8')


def run_audit(md, repo, db, home):
    env = {'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'todo_audit.py'), str(md), str(repo),
         '--db', str(db)],
        capture_output=True, text=True, env=env)


class ZeroHitFixture(unittest.TestCase):
    """『測試用零錨點項目』：標題不含任何檔名/符號/commit/SQL/config-key 的樣式，
    因此 checks 恆為空清單，classify() 恆回 NO_ANCHOR —— 用它來乾淨地區分
    「probe.state 的真值」與「state_of() 的顯示值」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        _init_repo(self.repo)
        # 'hooks' 不在任何內建 SEARCH_DIRS 之下 —— 現行條件下這是零命中情境
        (self.repo / 'hooks').mkdir()
        (self.repo / 'hooks' / 'foo.sh').write_text('#!/bin/sh\necho hi\n', encoding='utf-8')
        _commit_all(self.repo, 'add hooks')

        self.date = '2026-01-01'
        self.title = '測試用零錨點項目'
        self.full_title = f'[{self.date}] {self.title}'
        self.key = todo_store.todo_key(self.date, self.full_title)
        self.md = self.root / 'todo.md'
        _write_md(self.md, self.date, self.title)
        self.db = self.root / 'audit.sqlite'

    def tearDown(self):
        self.tmp.cleanup()


class TestZeroHitDegradesInsteadOfFatal(ZeroHitFixture):

    def test_zero_hit_exits_zero_not_fatal(self):
        # REQ-2 驗收 1：不再 sys.exit FATAL
        r = run_audit(self.md, self.repo, self.db, self.home)
        self.assertEqual(r.returncode, 0,
                         f'零命中不得 FATAL 中止 —— stderr:{r.stderr}\nstdout:{r.stdout}')

    def test_zero_hit_without_config_warns_not_configured(self):
        # REQ-2 驗收 2（G-2 前因 a：找不到任何 config 檔）
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assertIn('未設定', out)
        self.assertIn('todo-audit.json', out, '修復指引須含 config 路徑')

    def test_zero_hit_with_config_present_does_not_say_not_configured(self):
        # REQ-2 驗收 2（G-2 前因 b：config 存在但合併後仍零命中）
        cfg_dir = self.repo / '.claude'
        cfg_dir.mkdir()
        (cfg_dir / 'todo-audit.json').write_text(
            json.dumps({'search_dirs': ['still-empty-dir']}), encoding='utf-8')
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assertNotIn('未設定', out,
                         '有 config 但零命中時不得再說「未設定」—— 兩種前因的修復動作不同')
        self.assertIn('still-empty-dir', out, '須印出實際生效的 search_dirs 值')
        self.assertIn('per-repo', out, '須印出該值的來源層級')

    def test_illegal_config_json_during_zero_hit_still_exits_zero(self):
        # 異常流程：per-repo config 本身是非法 JSON，且降級後仍是零命中 —— 兩個失效模式疊加時也不得 crash
        cfg_dir = self.repo / '.claude'
        cfg_dir.mkdir()
        (cfg_dir / 'todo-audit.json').write_text('{{{ 壞掉的 json', encoding='utf-8')
        r = run_audit(self.md, self.repo, self.db, self.home)
        self.assertEqual(r.returncode, 0,
                         f'非法 config 疊加零命中，仍不得讓稽核 FATAL 中止 —— stderr:{r.stderr}')

    def test_weak_audit_overrides_display_but_not_stored_probe_state(self):
        # REQ-2 驗收 3 + G-1：probe.state 存真值，state_of() 顯示層才取代成 WEAK_AUDIT
        r = run_audit(self.md, self.repo, self.db, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        con = todo_store.connect(self.db)
        try:
            row = con.execute(
                'SELECT state FROM probe WHERE todo_key=? ORDER BY run_id DESC LIMIT 1',
                (self.key,)).fetchone()
            self.assertIsNotNone(row, '這條待辦應該被稽核過並留下 probe 記錄')
            self.assertEqual(row[0], 'NO_ANCHOR',
                             'probe.state 必須是 classify() 的真值，不得被降級旗標覆蓋（G-1）—— '
                             '否則 freshness() 的 "標紅 N 條" 統計會靜默失真')
            self.assertEqual(todo_store.state_of(con, self.key), 'WEAK_AUDIT',
                             '顯示層在降級 run 應把顯示值取代為 WEAK_AUDIT')
        finally:
            con.close()


class TestHitRestoresNormalStates(unittest.TestCase):
    """邊界：同一個 repo，只差在有沒有把 search_dirs 設到真正命中的目錄 ——
    驗證『零命中 → 命中』這個邊界翻過去之後，降級標記要跟著消失。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        _init_repo(self.repo)
        (self.repo / 'hooks').mkdir()
        (self.repo / 'hooks' / 'foo.sh').write_text('#!/bin/sh\necho hi\n', encoding='utf-8')
        cfg_dir = self.repo / '.claude'
        cfg_dir.mkdir()
        (cfg_dir / 'todo-audit.json').write_text(
            json.dumps({'search_dirs': ['hooks']}), encoding='utf-8')
        _commit_all(self.repo, 'add hooks + config')

        self.date = '2026-01-01'
        self.title = '測試用零錨點項目'
        self.full_title = f'[{self.date}] {self.title}'
        self.key = todo_store.todo_key(self.date, self.full_title)
        self.md = self.root / 'todo.md'
        _write_md(self.md, self.date, self.title)
        self.db = self.root / 'audit.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def test_hit_search_dirs_produces_no_weak_audit_tag(self):
        # REQ-2 驗收 4
        r = run_audit(self.md, self.repo, self.db, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        con = todo_store.connect(self.db)
        try:
            state = todo_store.state_of(con, self.key)
            self.assertNotEqual(state, 'WEAK_AUDIT', '有命中時不該出現降級標籤')
            self.assertEqual(state, 'NO_ANCHOR', '應回到既有六態的正常判定，不受降級機制干擾')
        finally:
            con.close()


class TestCliDisplaysWeakAuditTag(unittest.TestCase):
    """端到端：`todo_cli.py list` 的每一行都要帶 [WEAK_AUDIT] 前綴（驗收 3 原文指名的入口）。

    `todo_audit.py <md> <repo>` 這條路徑只寫 `persist()` 的稽核欄位（key/date/title/
    first_seen_run/last_seen_run/content_hash），不會寫 `sort_order`/`status` ——
    這兩欄只有 `todo_store.append_item()`（`todo_cli.py add` 走的路徑）才會寫。
    `todo_cli.py list` 的查詢固定要求 `sort_order IS NOT NULL`（見 todo_cli.py:47），
    所以純跑 audit 而不先建條目，這一行測試永遠看不到任何輸出、與降級與否無關。
    修法：先用 append_item() 建出同一個 key 的條目（讓它有 sort_order/status），
    audit 再對同一個 key 補寫 probe/state。`append_item()` 的日期固定用「今天」
    （刻意設計、不接受偽造歷史，見 todo_store.py:456），因此這裡的 self.date 也必須
    跟著用今天，兩條路徑算出來的 `todo_key()` 才會落在同一列。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.repo = self.root / 'demoproj'
        _init_repo(self.repo)
        (self.repo / 'hooks').mkdir()
        (self.repo / 'hooks' / 'foo.sh').write_text('#!/bin/sh\n', encoding='utf-8')
        _commit_all(self.repo, 'add hooks')

        self.date = datetime.now().strftime('%Y-%m-%d')
        self.title = '測試用零錨點項目'
        self.md = self.root / 'todo.md'
        _write_md(self.md, self.date, self.title)

        self.db = self.home / '.claude' / 'todos' / '.audit' / 'demoproj.sqlite'
        self.db.parent.mkdir(parents=True)
        con = todo_store.connect(self.db)
        try:
            todo_store.append_item(con, 'demoproj', self.title, '', '')
        finally:
            con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_todo_cli_list_prefixes_weak_audit(self):
        # REQ-2 驗收 3
        env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / 'todo_audit.py'), str(self.md), str(self.repo)],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, f'零命中不得 FATAL —— stderr:{r.stderr}')

        r2 = subprocess.run(
            [sys.executable, str(SCRIPTS / 'todo_cli.py'), 'list', '--project', 'demoproj'],
            capture_output=True, text=True, env=env)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn('[WEAK_AUDIT]', r2.stdout)


class TestClassifyUnaffectedByDegradation(unittest.TestCase):
    """G-6：classify() 的判定邏輯與輸出集合完全不變 —— 純邏輯測試，不需要 repo/git。

    這條測試對照的是『修正後』的原文：輸出是六態
    （ALL_GONE/PARTIAL_GONE/TOUCHED/DRIFT/ALIVE/NO_ANCHOR），不是誤植的『五態』；
    WEAK_AUDIT 屬於顯示層，不得混進這個集合。
    """

    def test_classify_output_set_is_exactly_six_states(self):
        # REQ-2 驗收 5（G-6）
        cases = [
            ([], [], 'NO_ANCHOR'),
            ([{'state': 'GONE'}], [], 'ALL_GONE'),
            ([{'state': 'GONE'}, {'state': 'OK'}], [], 'PARTIAL_GONE'),
            ([{'state': 'OK'}], {'sym': [('h', '2026-01-02', 'msg')]}, 'TOUCHED'),
            ([{'state': 'DRIFT'}], [], 'DRIFT'),
            ([{'state': 'OK'}], [], 'ALIVE'),
        ]
        seen = set()
        for checks, touches, expected in cases:
            got = todo_audit.classify(checks, touches)
            self.assertEqual(got, expected)
            seen.add(got)
        self.assertEqual(seen, {'NO_ANCHOR', 'ALL_GONE', 'PARTIAL_GONE',
                                'TOUCHED', 'DRIFT', 'ALIVE'})
        self.assertNotIn('WEAK_AUDIT', seen,
                         'WEAK_AUDIT 是顯示層概念，不得混進 classify() 本身的判定邏輯')


class TestDegradedWithRealSymbolAnchorDoesNotFatal(unittest.TestCase):
    """Stage 5 finding 1 回歸測試。

    既有 `ZeroHitFixture` 都用『測試用零錨點項目』：`symbols` 恆為空
    → `hit_rate = len(sym_index) / len(symbols) if symbols else 1.0`
    走 `else 1.0` 短路，結構性永遠到不了 `todo_audit.py:997` 的
    `if symbols and hit_rate < 0.30: sys.exit(...)` 分支 —— REQ-2 驗收 1
    因此被一個測不到該分支的測試背書。

    這裡逐字沿用審查者的實跑重現：repo 含 `hooks/foo.sh`（合格副檔名但
    `hooks/` 不在內建 SEARCH_DIRS）與 `mymod.py`（`.py` 不在內建
    SCAN_EXTS），待辦標題含反引號錨點 `` `load_config` ``（`RE_BACKTICK`
    抽出的真實符號，非零 `symbols`）。降級後 `search_dirs` 放寬為全 repo，
    但 `scan_exts` 依 finding 1 的修法方向刻意不放寬，repo 裡也沒有任何
    掃描得到的檔案含這個符號字串 —— `sym_index` 因此仍是空的，
    `hit_rate` 為 0% 且低於 0.30 門檻，真正踩中 :997 那個分支。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        _init_repo(self.repo)
        # 'hooks' 不在任何內建 SEARCH_DIRS 之下 —— 零命中情境（同 ZeroHitFixture）
        (self.repo / 'hooks').mkdir()
        (self.repo / 'hooks' / 'foo.sh').write_text('#!/bin/sh\necho hi\n', encoding='utf-8')
        # '.py' 不在內建 SCAN_EXTS 之下 —— 降級後放寬 search_dirs 也掃不到它
        (self.repo / 'mymod.py').write_text('def load_config():\n    pass\n', encoding='utf-8')
        _commit_all(self.repo, 'add hooks + mymod.py')

        self.date = '2026-01-01'
        # 反引號錨點 → RE_BACKTICK 抽出 symbol='load_config'，symbols 非空
        self.title = '修好 `load_config` 的合併順序'
        self.md = self.root / 'todo.md'
        _write_md(self.md, self.date, self.title)
        self.db = self.root / 'audit.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def test_degraded_low_symbol_hit_rate_does_not_fatal(self):
        r = run_audit(self.md, self.repo, self.db, self.home)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0,
                         f'降級後、真實符號命中率低於門檻仍不得 FATAL —— stderr:{r.stderr}\nstdout:{r.stdout}')
        self.assertNotIn('FATAL', out,
                         '降級狀態下低命中率是已知且已被警告過的預期結果，'
                         '不該再被 fail-loud 門檻攔截為故障')
        self.assertIn('WEAK_AUDIT', out, '應仍照常印出降級警告')


if __name__ == '__main__':
    unittest.main()
