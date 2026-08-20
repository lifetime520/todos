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


if __name__ == '__main__':
    unittest.main()
