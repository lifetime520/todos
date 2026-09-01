"""`todo_cli.py doctor` 自我診斷（REQ-3）。

對照 requirements.md 的 4 條驗收（非 brief 的實作步驟）：
  1. search_dirs 零命中 → 至少一行 WARN/FAIL，帶修復指引
  2. 設好 config 後命中 → 對應行為 OK，命中檔案數 > 0
  3. 兩種情況 exit code 皆為 0
  4. DB 不存在 → 不 traceback，FAIL 行提示 init

外加 G-3（零寫入副作用：DB 不存在的判定必須在 connect() 之前完成）與
G-4（search_dirs 來源層級直接消費 load_scan_config() 的 provenance，
不得自行重做三層探測）—— 這兩條在 requirements 是明文的硬約束，
不是「涵蓋清單」裡可有可無的一項，所以各自成獨立測試。

依賴：`todo_config.load_scan_config()`（Task 1，尚未存在）。本檔預期在
Task 1 落地前是 RED —— `doctor` 子指令連 argparse 都還沒註冊，所以
第一層 RED 甚至到不了「import 不到 todo_config」那一步。

REQ 編號的作用域：本檔的 REQ-n 是「新增 todo_cli.py doctor」那次交付（commit 528c175）的需求編號。
本 repo 的 REQ 編號**逐檔案局部有效** —— 不同測試檔的 REQ-1 指涉
完全不同的需求，不要跨檔對照。原始需求文件住在交付當下的 castpower
工作目錄（`.castpower/`，被 gitignore 完全排除、不進版控），所以這裡
指的是 **commit**：`git show 528c175` 永遠查得到，路徑不會。
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import todo_store  # noqa: E402


def run_cli(*args, env_home, cwd):
    """執行 todo_cli.py。

    doctor 的行為與「repo root」強相關（search_dirs 命中與否、config
    從哪一層生效都看它），而 cmd_audit 既有的呼叫慣例是把 cwd 當 repo
    root 傳給 todo_audit.py（`'.'`）。故這裡強制要求呼叫端明示 cwd，
    不像 test_cli.py 的同名 helper那樣隱式繼承 test runner 自己的 cwd
    —— 隱式繼承會讓「repo root 到底是誰」這件事在測試裡說不清楚。
    """
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'todo_cli.py'), *args],
        capture_output=True, text=True, cwd=str(cwd),
        env={'HOME': str(env_home), 'PATH': '/usr/bin:/bin:/usr/local/bin'})


ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
PREFIX_RE = re.compile(r'^\s*(OK|WARN|FAIL)\b')


def _write_json(path, obj):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding='utf-8')


def _init_db(home, project):
    """直接建庫（不透過 CLI 的 init，省一次 subprocess），回傳 db 路徑。"""
    audit = home / '.claude' / 'todos' / '.audit'
    audit.mkdir(parents=True, exist_ok=True)
    db = audit / f'{project}.sqlite'
    con = todo_store.connect(db)
    con.close()
    return db


class TestDoctorZeroHitBoundary(unittest.TestCase):
    """邊界值：search_dirs 零命中（REQ-3 驗收 1，也是本需求的起源案例——
    cast-power repo 上，內建 BTSE 路徑在別的 repo 下必然一個都不存在）。
    """

    def setUp(self):
        self.tmp = self._mk_tmp()
        self.home = self.tmp / 'home'
        self.repo = self.tmp / 'repo'
        self.repo.mkdir(parents=True)
        _init_db(self.home, 'castpower')

    def _mk_tmp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        return Path(self._tmpdir.name)

    # REQ-3 驗收 1：無 config、內建 search_dirs 在這個 repo 下零命中
    def test_zero_hit_search_dirs_produces_warn_or_fail_with_guidance(self):
        r = run_cli('doctor', '--project', 'castpower',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        lines = [l for l in out.splitlines() if PREFIX_RE.match(l)]
        hit_lines = [l for l in lines
                    if 'search_dirs' in l or '命中' in l or 'search dir' in l.lower()]
        self.assertTrue(hit_lines, f'找不到談 search_dirs 命中狀況的行：\n{out}')
        bad = [l for l in hit_lines if PREFIX_RE.match(l).group(1) in ('WARN', 'FAIL')]
        self.assertTrue(bad, f'零命中理應是 WARN 或 FAIL，實際：\n{hit_lines}')
        # 修復指引：至少要指到 config 檔路徑，不能只說「壞了」不說怎麼修
        self.assertIn('todo-audit.json', out,
                       'WARN/FAIL 行必須帶修復指引（config 檔路徑）')

    # REQ-3 驗收 3：零命中不等於 doctor 本身失敗，exit code 仍是 0
    def test_zero_hit_still_exits_zero(self):
        r = run_cli('doctor', '--project', 'castpower',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestDoctorHappyPath(unittest.TestCase):
    """正常流程：per-repo config 命中，doctor 應回報 OK 與實際命中數
    （REQ-3 驗收 2）。"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        scripts_dir = self.repo / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'a.sh').write_text('#!/bin/sh\necho a\n', encoding='utf-8')
        (scripts_dir / 'b.sh').write_text('#!/bin/sh\necho b\n', encoding='utf-8')
        _write_json(self.repo / '.claude' / 'todo-audit.json',
                    {'search_dirs': ['scripts']})
        _init_db(self.home, 'demo')

    def test_configured_hit_reports_ok_with_positive_count_and_source(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        lines = [l for l in out.splitlines() if PREFIX_RE.match(l)]
        hit_lines = [l for l in lines
                    if 'search_dirs' in l or '命中' in l]
        self.assertTrue(hit_lines, f'找不到談 search_dirs 命中狀況的行：\n{out}')
        ok_lines = [l for l in hit_lines if PREFIX_RE.match(l).group(1) == 'OK']
        self.assertTrue(ok_lines, f'設好 config 後命中理應是 OK，實際：\n{hit_lines}')
        # 命中檔案數 > 0：兩個 .sh 檔案至少要有一個非零正整數出現在該行
        self.assertTrue(any(re.search(r'[1-9]\d*', l) for l in ok_lines),
                         f'OK 行沒有帶命中檔案數：\n{ok_lines}')

    def test_configured_hit_reports_effective_search_dirs_value(self):
        # Stage 5 finding 2：REQ-3 明文要求涵蓋「search_dirs 生效值與其
        # 來源層級」。零命中分支本來就印生效值，命中分支先前只印數量與
        # 來源、沒印生效值本身 —— 使用者設好 config 之後，恰恰最需要
        # 確認「到底哪幾個目錄生效了」，doctor 卻不告訴他。
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        ok_hit_lines = [l for l in out.splitlines()
                        if PREFIX_RE.match(l)
                        and PREFIX_RE.match(l).group(1) == 'OK'
                        and ('search_dirs' in l or '命中' in l)]
        self.assertTrue(ok_hit_lines, f'找不到談 search_dirs 命中狀況的 OK 行：\n{out}')
        self.assertTrue(any('scripts' in l for l in ok_hit_lines),
                        f'OK 行必須印出生效的 search_dirs 值本身（此例應含 "scripts"），'
                        f'不能只有數量與來源：\n{ok_hit_lines}')

    def test_configured_hit_reports_per_repo_as_provenance_source(self):
        # G-4：來源層級直接消費 load_scan_config() 的 provenance。
        # 這裡只設了 per-repo 一層，provenance 理應標為 'per-repo'
        # 而不是 doctor 自己另外猜出來的字串。
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertIn('per-repo', r.stdout + r.stderr)

    def test_output_lines_are_grep_friendly_and_colorless(self):
        # brief 明文：逐行 OK/WARN/FAIL 前綴，純文字無顏色，可 grep
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertNotRegex(out, ANSI_RE, 'doctor 輸出不得含 ANSI 顏色碼')
        prefixed = [l for l in out.splitlines() if PREFIX_RE.match(l)]
        # 至少涵蓋：project 名/路徑、DB 存在性、稽核新鮮度、search_dirs、
        # 命中數、降級狀態 —— 6 個診斷面向，逐行前綴的行數不該只有 1 行
        self.assertGreaterEqual(len(prefixed), 3,
                                f'OK/WARN/FAIL 前綴行數過少：\n{out}')


class TestDoctorProvenanceNotReimplemented(unittest.TestCase):
    """G-4：user-global 與 per-repo 同鍵衝突時，doctor 回報的來源層級
    必須跟 load_scan_config() 的合併結果（per-repo 勝）一致。

    如果 doctor 自己另外重做一次三層探測（而不是直接消費 loader 的
    provenance），兩份合併邏輯遲早分歧 —— 這條測試就是釘住「不會分歧」
    這件事，而不是釘住某個字串長什麼樣子。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'

        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'scripts' / 'a.sh').write_text('echo a\n', encoding='utf-8')
        (self.repo / 'userglobaldir').mkdir(parents=True)
        # user-global 目錄故意不建在 repo 裡也不影響——它的鍵會被 per-repo 蓋掉，
        # 根本不會被拿去掃描；重點是 provenance 要標對，不是掃到它。

        _write_json(self.home / '.claude' / 'todo-audit.json',
                    {'search_dirs': ['userglobaldir']})
        _write_json(self.repo / '.claude' / 'todo-audit.json',
                    {'search_dirs': ['scripts']})
        _init_db(self.home, 'demo')

    def test_per_repo_wins_over_user_global_in_reported_provenance(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        lines = [l for l in out.splitlines()
                if 'search_dirs' in l or '命中' in l]
        self.assertTrue(lines, f'找不到 search_dirs 相關行：\n{out}')
        combined = '\n'.join(lines)
        self.assertIn('per-repo', combined)
        self.assertNotIn('user-global', combined,
                          'per-repo 與 user-global 同鍵衝突時 per-repo 應勝出，'
                          '回報的來源層級不該是 user-global')


class TestDoctorMissingDb(unittest.TestCase):
    """異常流程：DB 不存在（REQ-3 驗收 4 + G-3）。

    這是唯一一組直接驗證「零寫入副作用」的測試：診斷工具若在判斷
    DB 是否存在之前就呼叫 connect()，會把 DB 建出來，讓「請跑 init」
    的提示自相矛盾（因為庫已經被診斷本身建好了）。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.home.mkdir(parents=True)
        # 刻意不建 .claude/todos/.audit —— DB 與其父目錄都不存在。
        self.db = self.home / '.claude' / 'todos' / '.audit' / 'ghost.sqlite'

    def test_missing_db_reports_fail_with_init_hint_and_no_traceback(self):
        r = run_cli('doctor', '--project', 'ghost',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertNotIn('Traceback', out, f'doctor 對 DB 缺失不得 traceback：\n{out}')
        fail_lines = [l for l in out.splitlines()
                     if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'FAIL']
        self.assertTrue(fail_lines, f'DB 不存在理應有一行 FAIL：\n{out}')
        self.assertIn('init', out, 'FAIL 行必須提示用 init 建庫')

    def test_missing_db_still_exits_zero(self):
        r = run_cli('doctor', '--project', 'ghost',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # G-3：DB 不存在的判定必須在 todo_store.connect() 之前完成 ——
    # 診斷動作本身絕不能把庫建出來。
    def test_doctor_does_not_create_db_as_side_effect(self):
        self.assertFalse(self.db.exists())
        run_cli('doctor', '--project', 'ghost', env_home=self.home, cwd=self.repo)
        self.assertFalse(self.db.exists(),
                          'doctor 是唯讀診斷，不得因為呼叫 connect() 而把 DB 建出來'
                          '（違反「只有 init 建庫」原則，且讓 FAIL 行的 init 提示'
                          '自相矛盾）')


class TestDoctorCorruptDb(unittest.TestCase):
    """異常流程：DB 檔案存在但已損毀（非合法 sqlite）。

    Stage 6 finding：與 TestDoctorMissingDb（「不存在」）互補的另一種
    真實失效模式（寫入中斷、磁碟滿、`echo >` 誤覆蓋…）。main() 原本只
    對「不存在」做了特判，`todo_store.connect()` 對損毀檔案會拋
    `sqlite3.DatabaseError`（`executescript(BASE_SCHEMA)` 踩到壞檔），
    這個例外不在既有的 except 清單裡，於是直接 traceback + exit 1，
    違反 REQ-3「exit code 恆為 0、不 traceback」的硬約束。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        self.db = audit / 'demo.sqlite'
        # 刻意寫入非法 sqlite 內容（不是空檔、不是不存在，是「壞掉的檔案」）。
        self.db.write_bytes(b'\xff\xff\xff\xff not a real sqlite file \x00\x01')

    def test_corrupt_db_reports_fail_with_no_traceback_and_exits_zero(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn('Traceback', out, f'doctor 對損毀 DB 不得 traceback：\n{out}')
        fail_lines = [l for l in out.splitlines()
                     if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'FAIL']
        self.assertTrue(fail_lines, f'DB 損毀理應有一行 FAIL：\n{out}')


class TestDoctorAuditRecency(unittest.TestCase):
    """至少涵蓋：上次稽核距今多久（requirements.md REQ-3 條列第二點）。"""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')

    def test_never_audited_reported_distinctly(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # 沿用既有 header() 的既定用語（'從未稽核'），doctor 的「上次稽核
        # 距今多久」若查到 run 表是空的，不該印出一個虛構的時間。
        self.assertIn('從未', r.stdout + r.stderr)

    def test_has_run_is_not_reported_as_never_audited(self):
        con = todo_store.connect(self.db)
        con.execute(
            "INSERT INTO run(started_at, todo_file, repo, todo_count,"
            " symbol_count, hit_rate) VALUES(datetime('now'), '', ?, 0, 0, 1.0)",
            (str(self.repo),))
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('從未', r.stdout + r.stderr)


class TestDoctorProjectNameAndPath(unittest.TestCase):
    """至少涵蓋：解析出的 project 名與路徑（requirements.md REQ-3 條列
    第一點）。Stage 7 驗收發現 todo_cli.py:284 已經印出
    `OK   project=<name>  path=<repo>` 這一行，但既有測試只用
    `test_output_lines_are_grep_friendly_and_colorless` 斷言前綴行數，
    從未斷言這一行本身的內容——`grep 'project='` 在既有測試裡零命中。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        _init_db(self.home, 'demo')

    def test_reports_ok_line_with_project_name_and_repo_path(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        project_lines = [l for l in out.splitlines() if 'project=' in l]
        self.assertTrue(project_lines,
                        f'找不到含 project= 的行（REQ-3「解析出的 project 名與'
                        f'路徑」無測試覆蓋）：\n{out}')
        line = project_lines[0]
        self.assertEqual(PREFIX_RE.match(line).group(1), 'OK',
                          f'project 名/路徑行應為 OK 前綴：{line}')
        self.assertIn('project=demo', line, f'未印出實際 project 名：{line}')
        self.assertIn(f'path={self.repo}', line, f'未印出實際 repo 路徑：{line}')


class TestDoctorDegradedState(unittest.TestCase):
    """至少涵蓋：是否處於降級狀態（requirements.md REQ-3 條列第六點）。

    Stage 7 驗收發現 todo_cli.py:299-305 有 `run.degraded` 之後的
    WARN/OK 分支，但既有測試（TestDoctorAuditRecency）插入的 run 其
    degraded 一律是 NULL（未指定該欄位），從未走到 degraded=1 的
    WARN 分支——WARN 分支完全沒被踩過。本 class 分別構造 degraded=1
    與 degraded=0（顯式非降級）兩種 run，證明兩條分支都被踩到、且
    互斥（不會同時出現彼此的文字）。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')

    def _insert_run(self, degraded):
        con = todo_store.connect(self.db)
        con.execute(
            "INSERT INTO run(started_at, todo_file, repo, todo_count,"
            " symbol_count, hit_rate, degraded)"
            " VALUES(datetime('now'), '', ?, 0, 0, 1.0, ?)",
            (str(self.repo), degraded))
        con.commit()
        con.close()

    def test_degraded_run_reports_warn_with_weak_audit_text(self):
        self._insert_run(1)
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn('WEAK_AUDIT 降級狀態', out,
                       f'run.degraded=1 時應印出降級狀態的 WARN 文字：\n{out}')
        warn_lines = [l for l in out.splitlines()
                     if 'WEAK_AUDIT 降級狀態' in l]
        self.assertTrue(warn_lines and
                        PREFIX_RE.match(warn_lines[0]) and
                        PREFIX_RE.match(warn_lines[0]).group(1) == 'WARN',
                        f'降級狀態那行應為 WARN 前綴：\n{warn_lines}')
        self.assertNotIn('上次稽核未處於降級狀態', out,
                          '降級時不該同時出現非降級分支的文字')

    def test_non_degraded_run_reports_ok_without_weak_audit_text(self):
        self._insert_run(0)
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn('上次稽核未處於降級狀態', out,
                       f'run.degraded=0 時應印出非降級分支的 OK 文字：\n{out}')
        self.assertNotIn('WEAK_AUDIT 降級狀態', out,
                          '非降級時不該出現降級分支的 WARN 文字（證明分支互斥、'
                          '兩邊都被踩過而非誤判為同一條路徑）')


class TestDoctorDbExistsHappyPath(unittest.TestCase):
    """REQ-3「DB 是否存在」涵蓋項（Stage 7 R2 缺口 3）。

    既有測試只斷言過負面情況：`TestDoctorMissingDb`（DB 不存在 → FAIL）與
    `TestDoctorCorruptDb`（DB 損毀 → FAIL）。DB 正常存在時的那行
    `OK   DB 存在：{db}`（todo_cli.py:291）從未被任何測試斷言過——
    `TestDoctorHappyPath` 等測試雖然也用了正常的 DB，但只斷言
    search_dirs／provenance 相關的行，沒人讀過這一行本身的前綴與內容。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')

    def test_db_exists_reports_ok_line_with_db_path(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        db_lines = [l for l in out.splitlines() if 'DB 存在' in l]
        self.assertTrue(db_lines,
                        f'DB 正常存在時應有一行「DB 存在」訊息，實際找不到：\n{out}')
        line = db_lines[0]
        self.assertEqual(PREFIX_RE.match(line).group(1), 'OK',
                          f'DB 存在時該行應為 OK 前綴，實際：{line}')
        self.assertIn(str(self.db), line,
                      f'該行應帶出實際 DB 路徑：{line}')
        # 與負面情況（FAIL：不存在／損毀）互斥，證明這是不同的分支
        self.assertNotIn('FAIL', line)


class TestDoctorBypassesBindProjectConflict(unittest.TestCase):
    """Stage 7 第三輪裁決（requirements.md「Stage 7 第三輪裁決記錄」）：
    doctor 繞過 bind_project() 檢查（REQ-3 驗收 5，新增）。

    doctor 繼承 common parser 的 --path/--remote，main() 在把控制權交給
    cmd_doctor 之前，會無條件呼叫 todo_store.bind_project()。若對一個
    已綁定到路徑 A 的 project 名跑 `doctor --path <路徑 B>`，
    bind_project() 會拋 ProjectBindingConflict，main() 既有的
    except todo_store.ProjectBindingConflict 分支會印出綁定衝突錯誤、
    exit=6——cmd_doctor 一行都沒機會執行，doctor 存在的目的（自我診斷）
    在這條路徑上完全失效，違反 REQ-3「exit code 恆為 0」。

    用戶裁決：doctor 是診斷工具，不應該因為別的安全檢查而自己先跑不起來，
    故 doctor 繞過 bind_project() 檢查；其餘指令仍照常走綁定檢查
    （不在本檔測試範圍，因為改動只動 doctor 這一條分支）。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo_a = tmp / 'repo_a'
        self.repo_b = tmp / 'repo_b'
        self.repo_a.mkdir(parents=True)
        self.repo_b.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')
        # 先把 'demo' 綁定到 repo_a，模擬既有專案已綁定到別處
        # （寫法沿用 todo_store.bind_project 本身的簽名，與
        # todo_store.py:509 的 docstring 範例一致）。
        con = todo_store.connect(self.db)
        todo_store.bind_project(con, 'demo', str(self.repo_a), '(no-remote)')
        con.close()

    def test_conflicting_path_still_exits_zero_with_diagnostics(self):
        # 對同一個 project 名跑 doctor，但 --path 指向另一個路徑（repo_b）
        # ——這正是 bind_project() 會拋 ProjectBindingConflict 的條件。
        r = run_cli('doctor', '--project', 'demo', '--path', str(self.repo_b),
                    env_home=self.home, cwd=self.repo_b)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0,
                         f'doctor 遇到綁定衝突仍須 exit 0，不得提前中止：\n{out}')
        self.assertNotIn('❌', out,
                         f'doctor 不該因綁定衝突印出 main() 既有的錯誤訊息：\n{out}')
        self.assertNotIn('不能共用待辦', out,
                         f'doctor 輸出不該含 ProjectBindingConflict 的錯誤文字：\n{out}')
        # 必須真的跑到 cmd_doctor：照常印出 project=/path= 那一行診斷內容
        project_lines = [l for l in out.splitlines() if 'project=' in l]
        self.assertTrue(project_lines,
                        f'doctor 必須照常印出診斷輸出（不得因綁定衝突而零輸出）：\n{out}')
        self.assertEqual(PREFIX_RE.match(project_lines[0]).group(1), 'OK',
                         f'project 名/路徑行應為 OK 前綴：{project_lines[0]}')
        self.assertIn('project=demo', project_lines[0])
        self.assertIn(f'path={self.repo_b}', project_lines[0])

    def test_conflicting_path_reports_multiple_diagnostic_lines(self):
        # 不只驗證單一行存在，還要確認整段診斷邏輯（DB 存在性、稽核新鮮度、
        # search_dirs 等）都有跑完，而不是提前在某個中間點被截斷。
        r = run_cli('doctor', '--project', 'demo', '--path', str(self.repo_b),
                    env_home=self.home, cwd=self.repo_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        prefixed = [l for l in out.splitlines() if PREFIX_RE.match(l)]
        self.assertGreaterEqual(len(prefixed), 3,
                                f'綁定衝突下 doctor 的診斷行數不該比正常路徑少：\n{out}')


class TestDoctorAuditRecencyFormat(unittest.TestCase):
    """Stage 7 第三輪裁決（requirements.md「Stage 7 第三輪裁決記錄」）
    REQ-3『距今多久』涵蓋項的驗收判準補充：既有的
    `TestDoctorAuditRecency` 只斷言了『從未』字樣的有/無
    （test_never_audited_reported_distinctly /
    test_has_run_is_not_reported_as_never_audited），從未真正構造一個
    『已知時間差』的 run 紀錄去斷言 todo_cli.py:296-298 那段
    `X 小時前`/`X 天前` 格式化邏輯本身的輸出內容是否正確反映了實際
    經過時間，也沒驗過 48 小時的單位切換門檻（`age < 48` 用小時，
    否則用天）真的生效。本 class 直接寫入一個帶已知過去 `started_at`
    的 run，驗證：
      1. 3 小時前的紀錄 → 印出「3 小時前」（小時分支，且不誤入天分支）
      2. 72 小時（3 天）前的紀錄 → 印出「3 天前」，不是「72 小時前」
      3. 49 小時前的紀錄（剛超過 48 小時門檻）→ 印出「2 天前」而非
         小時，證明門檻本身（不只是格式字串）確實在 48 這個值上切換
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')

    def _insert_run_started_hours_ago(self, hours_ago):
        # started_at 直接寫成 Python 算好的過去時間點（與 todo_store.py
        # freshness() 的 `datetime.now() - datetime.fromisoformat(started)`
        # 用同一套 naive local clock，不透過 SQLite 的 datetime('now')
        # ——後者是 UTC，混用會讓時間差失去確定性，測不出精確的『距今多久』）。
        import datetime as dt
        started = (dt.datetime.now() - dt.timedelta(hours=hours_ago)).isoformat(sep=' ')
        con = todo_store.connect(self.db)
        con.execute(
            "INSERT INTO run(started_at, todo_file, repo, todo_count,"
            " symbol_count, hit_rate) VALUES(?, '', ?, 0, 0, 1.0)",
            (started, str(self.repo)))
        con.commit()
        con.close()

    def _recency_line(self, out):
        lines = [l for l in out.splitlines() if '上次稽核' in l and 'OK' in l]
        self.assertTrue(lines, f'找不到「上次稽核」那行 OK 訊息：\n{out}')
        return lines[0]

    def test_three_hours_ago_reports_hour_unit_with_correct_count(self):
        self._insert_run_started_hours_ago(3)
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = self._recency_line(r.stdout + r.stderr)
        self.assertIn('3 小時前', line,
                       f'3 小時前的 run 應格式化為「3 小時前」，實際：{line}')
        self.assertNotIn('天前', line,
                          f'3 小時前不該被誤判進天數分支：{line}')

    def test_seventy_two_hours_ago_reports_day_unit_not_hour_unit(self):
        self._insert_run_started_hours_ago(72)
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = self._recency_line(r.stdout + r.stderr)
        self.assertIn('3 天前', line,
                       f'72 小時（3 天）前的 run 應格式化為「3 天前」，實際：{line}')
        self.assertNotIn('72 小時前', line,
                          f'超過 48 小時門檻不該仍用小時單位：{line}')

    def test_just_over_48_hour_threshold_switches_to_day_unit(self):
        # 49 小時：剛超過 age<48 的門檻，驗證切換點本身（不是任意大數字
        # 才切換），round(49/24)=2 → 應印「2 天前」。
        self._insert_run_started_hours_ago(49)
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = self._recency_line(r.stdout + r.stderr)
        self.assertIn('2 天前', line,
                       f'49 小時前應剛好切換到天單位並顯示「2 天前」，實際：{line}')
        self.assertNotIn('小時前', line,
                          f'超過 48 小時門檻不該仍落在小時分支：{line}')


class TestDoctorConfigTypeError(unittest.TestCase):
    """Stage 7 第四輪裁決 / REQ-3 驗收 3（追加情境）：config 值型別錯誤
    （例如 search_dirs 是數字而非 list[str]）時，doctor 仍須 exit 0、
    不 traceback，且**不得印出 OK**（避免靜默回報健康——這是本次裁決
    要堵的失效模式：字串型的 search_dirs 若不驗型別會被逐字元展開成
    5 個單字元目錄，doctor 卻誤報 OK）。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        # 型別錯誤：search_dirs 應為 list[str]，這裡故意寫成數字——
        # 對照組（人工手誤最常見的另一種）是裸字串，見下面的 subTest。
        _write_json(self.repo / '.claude' / 'todo-audit.json',
                    {'search_dirs': 5})
        _init_db(self.home, 'demo')

    def test_type_error_config_exits_zero_no_traceback_no_ok_search_dirs(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn('Traceback', out, f'doctor 不得 crash 印 traceback：\n{out}')
        ok_search_dirs_lines = [
            l for l in out.splitlines()
            if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'OK'
            and ('search_dirs' in l or '命中' in l)]
        self.assertFalse(ok_search_dirs_lines,
                          f'search_dirs 型別錯誤時不得印出 OK（靜默回報健康）：\n{ok_search_dirs_lines}')
        # Stage 7 第五輪補強：型別錯誤本身也要被回報成一行 WARN，
        # 不能只留在 stderr 的舊訊息格式裡，讓 doctor 消費者無從得知。
        warn_lines = [
            l for l in out.splitlines()
            if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'WARN'
            and '型別' in l]
        self.assertTrue(warn_lines,
                         f'config 型別錯誤時 doctor 應印出一行 WARN 前綴的診斷：\n{out}')

    def test_string_type_search_dirs_exits_zero_no_traceback_no_ok(self):
        # 常見手誤：忘了寫成陣列，寫成裸字串 'hooks'。這是不 crash 但
        # 最危險的情境——字串被當可迭代物逐字元展開，若不驗型別，doctor
        # 會誤報 OK，使用者完全看不出設定沒生效。
        _write_json(self.repo / '.claude' / 'todo-audit.json',
                    {'search_dirs': 'hooks'})
        # mutation test 補強：這條測試原本恆真（fixture 是空 repo，
        # collect_source_files() 不論型別檢查存不存在都命中 0 個檔案，
        # 「不印 OK」永遠成立，跟型別檢查有沒有生效無關）。
        # 'hooks' 逐字元展開後是 {'h','o','k','s'}（set，重複的 'o' 不影響
        # collect_source_files() 用 str.startswith(tuple) 做前綴比對的結果）。
        # 在這裡實際建出其中一個會被展開命中的目錄 'h'，放一個副檔名屬於
        # 內建 SCAN_EXTS 的檔案 —— 若型別檢查被拿掉，'hooks' 會被展開成
        # 這幾個單字元目錄，其中 'h' 命中這個檔案，doctor 會印出 OK；
        # 型別檢查存在時，整層 config 因型別不合法被忽略、退回 builtin
        # 的 SEARCH_DIRS（這個 tmp repo 底下不存在），維持零命中的 WARN。
        # 兩種情況因此產生可觀察的差異，測試才真正鎖住型別檢查這個行為。
        hooks_dir = self.repo / 'h'
        hooks_dir.mkdir(parents=True)
        (hooks_dir / 'a.sh').write_text('#!/bin/sh\necho a\n', encoding='utf-8')
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn('Traceback', out, f'doctor 不得 crash 印 traceback：\n{out}')
        ok_search_dirs_lines = [
            l for l in out.splitlines()
            if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'OK'
            and ('search_dirs' in l or '命中' in l)]
        self.assertFalse(ok_search_dirs_lines,
                          f'search_dirs 為裸字串時不得印出 OK（靜默回報健康，'
                          f'字串會被逐字元展開）：\n{ok_search_dirs_lines}')
        # Stage 7 第五輪補強：同上，裸字串手誤也要有一行 WARN 可見。
        warn_lines = [
            l for l in out.splitlines()
            if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'WARN'
            and '型別' in l]
        self.assertTrue(warn_lines,
                         f'config 型別錯誤時 doctor 應印出一行 WARN 前綴的診斷：\n{out}')


class TestDoctorConfigTypeErrorFallbackAlsoHits(unittest.TestCase):
    """Stage 7 第五輪：先前兩條 TestDoctorConfigTypeError 測試的 fixture 都是
    空 repo，fallback 到 builtin 之後零命中，所以「不印 OK」永遠成立，測不出
    「config 被拒絕」這件事本身有沒有被回報。

    這裡刻意讓 fallback 也命中：repo 底下有 `scripts/foo.sh`（`scripts` 剛好
    是內建 SEARCH_DIRS 之一，`.sh` 是內建 SCAN_EXTS 之一），per-repo config
    寫了型別錯誤的 `{"search_dirs": "hooks"}`（字串手誤）。型別檢查正確地
    拒絕這一層、退回 builtin，而 builtin 的 `scripts/` 命中這個檔案 —— 於是
    doctor 會印出一行乾淨的 `OK search_dirs 命中 1 個檔案`，如果沒有把
    「這一層曾被拒絕」往上傳遞出來，使用者完全看不出自己的 config 其實沒生效。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        _write_json(self.repo / '.claude' / 'todo-audit.json',
                    {'search_dirs': 'hooks'})
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'scripts' / 'foo.sh').write_text('#!/bin/sh\necho x\n', encoding='utf-8')
        _init_db(self.home, 'demo')

    def test_warn_for_rejected_layer_present_even_though_fallback_hits(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn('Traceback', out, f'doctor 不得 crash 印 traceback：\n{out}')

        lines = [l for l in out.splitlines() if PREFIX_RE.match(l)]
        warn_lines = [l for l in lines if PREFIX_RE.match(l).group(1) == 'WARN']
        rejected_layer_warns = [
            l for l in warn_lines
            if str(self.repo / '.claude' / 'todo-audit.json') in l and '型別' in l]
        self.assertTrue(
            rejected_layer_warns,
            f'config 層被型別錯誤拒絕時，即使 fallback 命中檔案，doctor 仍須印出'
            f'一行 WARN 指出是哪個 config 檔案、為何被拒絕：\n{out}')

        ok_lines = [l for l in lines if PREFIX_RE.match(l).group(1) == 'OK'
                    and ('search_dirs' in l or '命中' in l)]
        self.assertTrue(
            ok_lines,
            f'fallback 到 builtin 的 scripts/ 應該真的命中 foo.py，這行 OK 應該存在：\n{out}')


class TestDoctorDanglingRefs(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        db = _init_db(self.home, 'demo')
        con = todo_store.connect(db)
        sid = todo_store.append_item(con, 'demo', '測試條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        todo_store.set_spec_path(con, key, 'docs/specs/not-exist.md')
        todo_store.set_memory_ref(con, key, 'memory/not-exist.md')
        con.close()
        self.sid = sid

    def test_dangling_spec_path_warns_on_stdout(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertIn('WARN', r.stdout)
        self.assertIn('not-exist.md', r.stdout)
        self.assertIn(self.sid, r.stdout)

    def test_dangling_ref_does_not_fail_doctor(self):
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_existing_ref_does_not_warn(self):
        (self.repo / 'docs' / 'specs').mkdir(parents=True)
        (self.repo / 'docs' / 'specs' / 'ok.md').write_text('x', encoding='utf-8')
        con = todo_store.connect(_init_db(self.home, 'demo2'))
        sid = todo_store.append_item(con, 'demo2', '正常條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        todo_store.set_spec_path(con, key, 'docs/specs/ok.md')
        con.close()
        r = run_cli('doctor', '--project', 'demo2',
                    env_home=self.home, cwd=self.repo)
        self.assertNotIn('ok.md', r.stdout.replace('OK', ''))


class TestDoctorDependencyIntegrity(unittest.TestCase):
    """REQ-8：doctor 檢查 todo_dep 的懸空邊與環狀依賴，只 WARN 不修復。

    懸空邊／環都是直接寫原生 SQL 造出來的「已經壞掉」資料 —— 正常路徑
    （`todo_store.add_dep()`）本身會擋掉不存在的 key 與環，所以要模擬
    「已經壞掉的資料」（人工改 DB、舊版程式漏檢查留下的殘局）只能繞過
    它直接 INSERT，這與 brief 的作法一致。

    每個測試都聚焦一件事：懸空邊分 from_key／to_key 兩種角色分別驗證
    （避免只檢查其中一側的實作被誤判為完整）；環另外驗證 WARN 訊息用
    的是可讀的 short_id 而非不可讀的原始 hash key（沿用上一個 task 在
    收件檢查中發現的同類缺陷：印出 hash 而非人類可讀識別碼）；REQ-8
    「只 WARN 不修復」的核心——DB 在 doctor 跑完後原封不動——每種問題
    都各自有一條測試直接查 DB 驗證，不是只驗證 stdout 有沒有字樣。
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')

    def _insert_raw_dep(self, con, from_key, to_key, kind='blocks'):
        con.execute(
            "INSERT INTO todo_dep(from_key,to_key,kind,created_at,created_by)"
            " VALUES(?,?,?,?,?)", (from_key, to_key, kind, 'now', None))

    def _warn_lines(self, out):
        return [l for l in out.splitlines()
                if PREFIX_RE.match(l) and PREFIX_RE.match(l).group(1) == 'WARN']

    # REQ-8：to_key 指向不存在的條目要被 WARN，且訊息帶出懸空的 key。
    def test_dangling_to_key_warns_with_key(self):
        con = todo_store.connect(self.db)
        sid = todo_store.append_item(con, 'demo', '測試條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        self._insert_raw_dep(con, key, 'ghost-to-key')
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        hit = [l for l in self._warn_lines(out) if 'ghost-to-key' in l]
        self.assertTrue(hit, f'找不到懸空 to_key 的 WARN 行：\n{out}')

    # REQ-8：from_key 指向不存在的條目同樣要被 WARN（不能只檢查 to_key
    # 那一側 —— 兩個角色都要各自查存在性）。
    def test_dangling_from_key_warns_with_key(self):
        con = todo_store.connect(self.db)
        sid = todo_store.append_item(con, 'demo', '測試條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        self._insert_raw_dep(con, 'ghost-from-key', key)
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        hit = [l for l in self._warn_lines(out) if 'ghost-from-key' in l]
        self.assertTrue(hit, f'找不到懸空 from_key 的 WARN 行：\n{out}')

    # REQ-8 核心：doctor 對懸空邊只 WARN，不得刪除 —— 直接查 DB 而非只看
    # stdout，證明「跑完 doctor 後該筆懸空邊仍存在於 DB」。
    def test_dangling_edge_survives_doctor_unmodified(self):
        con = todo_store.connect(self.db)
        sid = todo_store.append_item(con, 'demo', '測試條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        self._insert_raw_dep(con, key, 'ghost-survives')
        con.commit()
        con.close()
        run_cli('doctor', '--project', 'demo', env_home=self.home, cwd=self.repo)
        con = todo_store.connect(self.db)
        row = con.execute(
            "SELECT COUNT(*) FROM todo_dep WHERE from_key=? AND to_key=?"
            " AND kind='blocks'", (key, 'ghost-survives')).fetchone()
        con.close()
        self.assertEqual(row[0], 1,
                         'doctor 是診斷工具，不得刪除懸空邊——REQ-8 只 WARN 不修復')

    # REQ-8：即使發現懸空邊，doctor 的 exit code 仍恆為 0。
    def test_dangling_edge_doctor_exit_code_zero(self):
        con = todo_store.connect(self.db)
        sid = todo_store.append_item(con, 'demo', '測試條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        self._insert_raw_dep(con, key, 'ghost-exit-zero')
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # REQ-8：blocks 邊構成的環要被 WARN，且訊息用可讀的 short_id
    # （T-NNN），不是不可讀的原始 sha1 hash key —— 上一個 task 的收件
    # 檢查發現過同類缺陷（依賴事件印成 hash），這裡直接釘住不重蹈覆轍。
    def test_cyclic_blocks_warns_with_readable_short_ids_not_raw_keys(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (b,)).fetchone()[0]
        for f, t in ((ak, bk), (bk, ak)):
            self._insert_raw_dep(con, f, t, 'blocks')
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        cyc_lines = [l for l in self._warn_lines(out) if '環' in l]
        self.assertTrue(cyc_lines, f'找不到環狀依賴的 WARN 行：\n{out}')
        combined = '\n'.join(cyc_lines)
        self.assertIn(a, combined, 'WARN 訊息必須用可讀的 short_id 呈現環路徑')
        self.assertIn(b, combined, 'WARN 訊息必須用可讀的 short_id 呈現環路徑')
        self.assertNotIn(ak, combined, 'WARN 訊息不該印出不可讀的原始 hash key')
        self.assertNotIn(bk, combined, 'WARN 訊息不該印出不可讀的原始 hash key')

    # REQ-8 核心：doctor 對環狀依賴只 WARN，不得斷開（刪除任何一條邊）
    # —— 直接查 DB，兩條邊都要還在。
    def test_cyclic_blocks_edges_survive_doctor_unmodified(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (b,)).fetchone()[0]
        for f, t in ((ak, bk), (bk, ak)):
            self._insert_raw_dep(con, f, t, 'blocks')
        con.commit()
        con.close()
        run_cli('doctor', '--project', 'demo', env_home=self.home, cwd=self.repo)
        con = todo_store.connect(self.db)
        count = con.execute(
            "SELECT COUNT(*) FROM todo_dep WHERE kind='blocks'").fetchone()[0]
        con.close()
        self.assertEqual(count, 2,
                         'doctor 不得為了「解環」而刪除任何一條 blocks 邊')

    # REQ-8 + G-3：parent-child 邊構成的環也要被獨立偵測到（不是只查
    # blocks 這一種 kind）。
    def test_cyclic_parent_child_also_warns(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'P', '', '')
        b = todo_store.append_item(con, 'demo', 'C', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (b,)).fetchone()[0]
        for f, t in ((ak, bk), (bk, ak)):
            self._insert_raw_dep(con, f, t, 'parent-child')
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        cyc_lines = [l for l in self._warn_lines(out) if '環' in l]
        self.assertTrue(cyc_lines,
                        f'parent-child 邊構成的環也應該被 WARN：\n{out}')

    # REQ-8 + G-3：related 是無序語意，即使兩個方向都寫了邊，也不該被
    # 誤判成環（環檢測只適用 blocks/parent-child）。
    def test_related_kind_reciprocal_edges_not_flagged_as_cycle(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (b,)).fetchone()[0]
        for f, t in ((ak, bk), (bk, ak)):
            self._insert_raw_dep(con, f, t, 'related')
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        out = r.stdout + r.stderr
        self.assertNotIn('環', out,
                         'related 邊是無序語意，doctor 不該對它做環狀依賴檢查')

    # 反例：透過正常路徑（add_dep）建立的乾淨依賴，不該產生任何懸空／
    # 環狀依賴的 WARN。
    def test_clean_dep_via_add_dep_produces_no_dependency_warn(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (b,)).fetchone()[0]
        todo_store.add_dep(con, ak, bk, 'blocks')
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertNotIn('懸空', out)
        self.assertNotIn('環', out)

    # REQ-8：懸空邊與環狀依賴同時存在時，doctor 仍要跑完全部檢查、
    # exit code 仍恆為 0（不因發現多個問題而提前中止或非零退出）。
    def test_doctor_exit_code_zero_with_both_dangling_and_cycle(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?',
                         (b,)).fetchone()[0]
        for f, t in ((ak, bk), (bk, ak)):
            self._insert_raw_dep(con, f, t, 'blocks')
        self._insert_raw_dep(con, ak, 'ghost-combo', 'blocks')
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn('ghost-combo', out)
        self.assertTrue(self._warn_lines(out), f'應同時印出多行 WARN：\n{out}')


if __name__ == '__main__':
    unittest.main()
