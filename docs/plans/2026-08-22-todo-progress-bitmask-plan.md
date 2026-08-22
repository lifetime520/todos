# Todo 交付進度位元旗標 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓每條 todo 能疊加記錄「實作/review/commit/compile/test/live_tested/deploy」七個獨立交付進度位元，並能參照外部 spec 文件與 auto memory 檔案，全部透過位元運算查詢當前進度。

**Architecture:** 新增一個不碰 DB 的純函式模組 `todo_flags.py` 定義位元常數與運算；`todo_store.py` 新增 `progress`/`spec_path`/`memory_ref` 三個 nullable 欄位（沿用既有 idempotent migration 模式）與 `set_progress()`/`set_spec_path()`/`set_memory_ref()`；`todo_cli.py` 新增 `flag` 子指令、`edit --spec/--memory`，並在 `list`/`show`/`dump` 顯示進度視覺化；`doctor` 增加參照完整性檢查。

**Tech Stack:** Python 3、sqlite3、argparse、unittest（沿用既有 stack，不引入新依賴）

**Spec:** `docs/specs/2026-08-22-todo-progress-bitmask-design.md`

## Global Constraints

- 既有 `status` 欄位（`pending`/`doing`/`done`/`unpick`）語意與互斥性完全不變
- pipeline 七個位元之間**不強制順序**，各自獨立可點
- 進度全滿時**單向**自動轉 `status='done'`：`unpick`/`done` 不觸發；觸發時**不**經過 `set_status()` 的 `ClaimConflict` 擁有者比對、且**保留原有 `status_by`**
- 手動 `mark <ref> done` 時**不**強制把 `progress` 一併點滿
- `spec_path`/`memory_ref` 只存路徑字串，寫入當下**不驗證**檔案存在
- `doctor` 的 WARN 一律印在 **stdout**（現有既定原則，不能只丟 stderr）
- 所有 migration 必須 idempotent（`connect()` 可重複呼叫不炸、不重複造成資料損壞）

---

## 檔案結構總覽

| 檔案 | 動作 | 職責 |
|---|---|---|
| `skills/todo-audit/scripts/todo_flags.py` | 新增 | 位元旗標常數與純函式運算（`has`/`set_`/`clear`/`toggle`/`is_complete`/`summary`） |
| `skills/todo-audit/tests/test_flags.py` | 新增 | `todo_flags.py` 單元測試 |
| `skills/todo-audit/scripts/todo_store.py` | 修改 | schema migration、backfill、`set_progress`/`set_spec_path`/`set_memory_ref` |
| `skills/todo-audit/tests/test_store.py` | 修改 | 上述新函式與 migration 的測試 |
| `skills/todo-audit/scripts/todo_cli.py` | 修改 | `flag` 子指令、`edit --spec/--memory`、`list`/`show`/`dump` 顯示進度、doctor 參照檢查 |
| `skills/todo-audit/tests/test_cli.py` | 修改 | CLI 層整合測試 |
| `skills/todo-audit/tests/test_doctor.py` | 修改 | doctor 參照完整性 WARN 測試 |
| `docs/TODO-SYSTEM.md` | 修改 | 補文件：`flag` 指令、`--spec`/`--memory`、進度視覺化格式 |

---

### Task 1: `todo_flags.py` —— 位元旗標純函式模組

**Files:**
- Create: `skills/todo-audit/scripts/todo_flags.py`
- Test: `skills/todo-audit/tests/test_flags.py`

**Interfaces:**
- Produces：`FLAGS: dict[str,int]`（7 個旗標名 → 位元值）、`ORDER: tuple[str,...]`（固定顯示順序）、`ALL_FLAGS: int`（=127）、`has(progress,name)->bool`、`set_(progress,name)->int`、`clear(progress,name)->int`、`toggle(progress,name)->int`、`is_complete(progress)->bool`、`summary(progress)->list[str]`。未知 `name` 一律 `raise ValueError`。`progress` 可為 `None`，等同 0。

- [ ] **Step 1: 寫失敗測試 `test_flags.py`**

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_flags


class TestFlags(unittest.TestCase):
    def test_has_set_clear_toggle_roundtrip(self):
        p = 0
        self.assertFalse(todo_flags.has(p, 'implemented'))
        p = todo_flags.set_(p, 'implemented')
        self.assertTrue(todo_flags.has(p, 'implemented'))
        p = todo_flags.toggle(p, 'implemented')
        self.assertFalse(todo_flags.has(p, 'implemented'))
        p = todo_flags.set_(p, 'implemented')
        p = todo_flags.clear(p, 'implemented')
        self.assertFalse(todo_flags.has(p, 'implemented'))

    def test_unknown_flag_name_raises(self):
        with self.assertRaises(ValueError):
            todo_flags.has(0, 'not_a_flag')
        with self.assertRaises(ValueError):
            todo_flags.set_(0, 'not_a_flag')
        with self.assertRaises(ValueError):
            todo_flags.clear(0, 'not_a_flag')
        with self.assertRaises(ValueError):
            todo_flags.toggle(0, 'not_a_flag')

    def test_is_complete_requires_all_seven_bits(self):
        p = 0
        for name in todo_flags.ORDER[:-1]:
            p = todo_flags.set_(p, name)
        self.assertFalse(todo_flags.is_complete(p))
        p = todo_flags.set_(p, todo_flags.ORDER[-1])
        self.assertTrue(todo_flags.is_complete(p))
        self.assertEqual(p, todo_flags.ALL_FLAGS)

    def test_all_flags_equals_127(self):
        self.assertEqual(todo_flags.ALL_FLAGS, 127)
        self.assertEqual(len(todo_flags.FLAGS), 7)
        self.assertEqual(set(todo_flags.FLAGS), set(todo_flags.ORDER))

    def test_flags_are_distinct_powers_of_two(self):
        values = sorted(todo_flags.FLAGS.values())
        self.assertEqual(values, [1, 2, 4, 8, 16, 32, 64])

    def test_summary_lists_only_set_flags_in_fixed_order(self):
        p = todo_flags.set_(todo_flags.set_(0, 'deployed'), 'implemented')
        self.assertEqual(todo_flags.summary(p), ['implemented', 'deployed'])

    def test_none_progress_treated_as_zero(self):
        self.assertFalse(todo_flags.has(None, 'implemented'))
        self.assertEqual(todo_flags.summary(None), [])
        self.assertFalse(todo_flags.is_complete(None))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 確認測試失敗（模組還不存在）**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_flags.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'todo_flags'`

- [ ] **Step 3: 實作 `todo_flags.py`**

```python
"""待辦條目的交付進度位元旗標。

純函式，不匯入 sqlite3、不碰 DB —— 方便獨立單元測試，也讓
todo_store.py 之外的呼叫端（未來若有）不需要拉整條 DB 依賴鏈。
"""

FLAGS = {
    'implemented':  1 << 0,   # 實作完成
    'reviewed':     1 << 1,   # code review 完成
    'committed':    1 << 2,   # 已 commit
    'compiled':     1 << 3,   # build/編譯通過
    'tested':       1 << 4,   # 自動化測試綠燈
    'live_tested':  1 << 5,   # 實際跑起來驗證過（curl/手動操作）
    'deployed':     1 << 6,   # 已部署
}

# 固定顯示順序 —— summary() 依此排序，不受點擊順序影響，
# 因為旗標本身只記有無、不記時間先後。
ORDER = ('implemented', 'reviewed', 'committed', 'compiled',
         'tested', 'live_tested', 'deployed')

ALL_FLAGS = sum(FLAGS.values())  # 127


def _bit(name):
    try:
        return FLAGS[name]
    except KeyError:
        raise ValueError(f'unknown progress flag: {name}')


def has(progress, name):
    return bool((progress or 0) & _bit(name))


def set_(progress, name):
    return (progress or 0) | _bit(name)


def clear(progress, name):
    return (progress or 0) & ~_bit(name)


def toggle(progress, name):
    return (progress or 0) ^ _bit(name)


def is_complete(progress):
    p = progress or 0
    return (p & ALL_FLAGS) == ALL_FLAGS


def summary(progress):
    p = progress or 0
    return [name for name in ORDER if p & FLAGS[name]]
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_flags.py -v`
Expected: PASS，7 個測試全綠

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_flags.py skills/todo-audit/tests/test_flags.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): 新增交付進度位元旗標純函式模組 todo_flags

實作/review/commit/compile/test/live_tested/deploy 七個獨立位元，
提供 has/set_/clear/toggle/is_complete/summary 運算，供 todo_store
的交付進度追蹤使用。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 2: `todo_store.py` —— schema、backfill、`set_progress`/`set_spec_path`/`set_memory_ref`

**Files:**
- Modify: `skills/todo-audit/scripts/todo_store.py`（`MIGRATIONS` 清單、`connect()`、新增三個函式）
- Test: `skills/todo-audit/tests/test_store.py`

**Interfaces:**
- Consumes：Task 1 的 `todo_flags.FLAGS`/`ORDER`/`ALL_FLAGS`/`set_`/`clear`/`toggle`/`is_complete`
- Produces：`set_progress(con, key, op, name) -> int`（`op` ∈ `{'set','clear','toggle'}`，未知 `op`/`name` 皆 `raise ValueError`，`key` 不存在 `raise KeyError`）、`set_spec_path(con, key, path) -> None`、`set_memory_ref(con, key, path) -> None`。DB 新增欄位 `todo.progress INT`、`todo.spec_path TEXT`、`todo.memory_ref TEXT`。

- [ ] **Step 1: 寫失敗測試（附加到 `test_store.py` 檔尾，`if __name__` 區塊之前）**

```python
class TestProgressFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / 't.sqlite'
        self.con = todo_store.connect(self.dbpath)
        todo_store.save_parsed(self.con, 'demo',
                               todo_store.parse_md_lossless(SAMPLE))
        self.key = self.con.execute(
            'SELECT key FROM todo ORDER BY sort_order').fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_migration_adds_progress_columns(self):
        cols = {r[1] for r in self.con.execute('PRAGMA table_info(todo)')}
        for c in ('progress', 'spec_path', 'memory_ref'):
            self.assertIn(c, cols, f'missing column {c}')

    def test_fresh_rows_default_progress_to_zero(self):
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 0)

    def test_existing_done_rows_backfilled_to_full_progress_on_reconnect(self):
        self.con.execute("UPDATE todo SET status='done', progress=NULL"
                         " WHERE key=?", (self.key,))
        self.con.commit()
        self.con.close()
        self.con = todo_store.connect(self.dbpath)
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 127)

    def test_set_progress_sets_single_bit_and_returns_new_value(self):
        new_p = todo_store.set_progress(self.con, self.key, 'set', 'implemented')
        self.assertEqual(new_p, 1)
        stored = self.con.execute('SELECT progress FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(stored, 1)

    def test_set_progress_clear_and_toggle(self):
        todo_store.set_progress(self.con, self.key, 'set', 'reviewed')
        todo_store.set_progress(self.con, self.key, 'clear', 'reviewed')
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 0)
        todo_store.set_progress(self.con, self.key, 'toggle', 'committed')
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 4)

    def test_unknown_progress_op_raises(self):
        with self.assertRaises(ValueError):
            todo_store.set_progress(self.con, self.key, 'bogus', 'implemented')

    def test_unknown_flag_name_raises(self):
        with self.assertRaises(ValueError):
            todo_store.set_progress(self.con, self.key, 'set', 'not_a_flag')

    def test_unknown_key_raises_keyerror(self):
        with self.assertRaises(KeyError):
            todo_store.set_progress(self.con, 'nosuchkey', 'set', 'implemented')

    def test_completing_all_flags_auto_transitions_to_done(self):
        import todo_flags
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        status = self.con.execute('SELECT status FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(status, 'done')

    def test_unpick_item_does_not_auto_transition(self):
        import todo_flags
        todo_store.set_status(self.con, self.key, 'unpick', note='暫不處理')
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        status = self.con.execute('SELECT status FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(status, 'unpick')

    def test_auto_transition_preserves_status_by_of_current_owner(self):
        import todo_flags
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        status, by = self.con.execute(
            'SELECT status, status_by FROM todo WHERE key=?',
            (self.key,)).fetchone()
        self.assertEqual(status, 'done')
        self.assertEqual(by, 'sess-a',
                         'progress 補滿觸發的自動完成不該改寫原本的認領者')

    def test_auto_transition_does_not_raise_claim_conflict_for_other_caller(self):
        # 這是本設計要修的關鍵情境：A 認領中，補滿最後一格的呼叫端不是 A，
        # 不該被 ClaimConflict 擋下 —— 這是工作自然做完，不是搶認領。
        import todo_flags
        todo_store.set_status(self.con, self.key, 'doing', by='sess-a')
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)  # 無 by 參數
        status = self.con.execute('SELECT status FROM todo WHERE key=?',
                                  (self.key,)).fetchone()[0]
        self.assertEqual(status, 'done')

    def test_already_done_progress_completion_does_not_overwrite_status_at(self):
        todo_store.set_status(self.con, self.key, 'done')
        original_at = self.con.execute(
            'SELECT status_at FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        import todo_flags
        for name in todo_flags.ORDER:
            todo_store.set_progress(self.con, self.key, 'set', name)
        after_at = self.con.execute(
            'SELECT status_at FROM todo WHERE key=?', (self.key,)).fetchone()[0]
        self.assertEqual(after_at, original_at)

    def test_manual_mark_done_does_not_force_fill_progress(self):
        todo_store.set_status(self.con, self.key, 'done')
        p = self.con.execute('SELECT progress FROM todo WHERE key=?',
                             (self.key,)).fetchone()[0]
        self.assertEqual(p, 0)

    def test_set_spec_path_and_memory_ref(self):
        todo_store.set_spec_path(self.con, self.key, 'docs/specs/x.md')
        todo_store.set_memory_ref(self.con, self.key, 'memory/y.md')
        row = self.con.execute(
            'SELECT spec_path, memory_ref FROM todo WHERE key=?',
            (self.key,)).fetchone()
        self.assertEqual(row, ('docs/specs/x.md', 'memory/y.md'))
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_store.py -k TestProgressFlags -v`
Expected: FAIL——`progress`/`spec_path`/`memory_ref` 欄位不存在、`set_progress`/`set_spec_path`/`set_memory_ref` 屬性不存在

- [ ] **Step 3: 修改 `todo_store.py`**

在檔案開頭 import 區塊加入：

```python
import todo_flags
```

修改 `MIGRATIONS` 清單（在既有清單最後一項 `"ALTER TABLE run ADD COLUMN degraded INT"` 之後加入）：

```python
MIGRATIONS = [
    # ...既有項目不動...
    "ALTER TABLE run ADD COLUMN degraded INT",
    # 交付進度位元旗標與外部參照（見
    # docs/specs/2026-08-22-todo-progress-bitmask-design.md）。
    "ALTER TABLE todo ADD COLUMN progress INT",
    "ALTER TABLE todo ADD COLUMN spec_path TEXT",
    "ALTER TABLE todo ADD COLUMN memory_ref TEXT",
]
```

修改 `connect()`，在既有的 `CREATE UNIQUE INDEX ...` 之後、`con.commit()` 之前加入 backfill：

```python
def connect(db_path):
    con = sqlite3.connect(str(db_path))
    con.executescript(BASE_SCHEMA)
    for stmt in MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError as e:
            if 'duplicate column name' not in str(e):
                raise
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_todo_short_id"
                " ON todo(short_id) WHERE short_id IS NOT NULL")
    # progress 的一次性 backfill：既有 done 條目視為已跑完整個 pipeline，
    # 其餘一律未知＝0。之後每次呼叫都是 no-op —— 補滿後 `progress IS NULL`
    # 不會再命中任何列，migration 保持 idempotent。
    con.execute("UPDATE todo SET progress=? WHERE status='done'"
                " AND progress IS NULL", (todo_flags.ALL_FLAGS,))
    con.execute("UPDATE todo SET progress=0 WHERE progress IS NULL")
    con.commit()
    return con
```

在檔尾（`remove_item` 之後）新增三個函式：

```python
def set_progress(con, key, op, name):
    """交付進度位元運算。op ∈ {'set','clear','toggle'}，name 見 todo_flags.FLAGS。

    七個旗標全數點滿、且目前不是 unpick/done 時，單向自動轉 status='done'——
    刻意不經過 set_status() 的 ClaimConflict 擁有者比對、也不改動
    status_by：這是工作自然做完的結果，不是新的認領動作。若目前已是
    done，觸發是 no-op，避免洗掉原本的完成時間。
    """
    row = con.execute('SELECT progress, status FROM todo WHERE key=?',
                      (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    cur_progress, status = row
    cur_progress = cur_progress or 0
    ops = {'set': todo_flags.set_, 'clear': todo_flags.clear,
          'toggle': todo_flags.toggle}
    if op not in ops:
        raise ValueError(f'unknown progress op: {op}')
    new_progress = ops[op](cur_progress, name)
    con.execute('UPDATE todo SET progress=? WHERE key=?', (new_progress, key))
    if todo_flags.is_complete(new_progress) and status not in ('unpick', 'done'):
        con.execute('UPDATE todo SET status=?, status_at=? WHERE key=?',
                    ('done', datetime.now().isoformat(timespec='seconds'), key))
    con.commit()
    return new_progress


def set_spec_path(con, key, path):
    """設定規格文件參照。只存路徑字串，不驗證存在——寫入當下文件可能還沒
    建好；存在性檢查交給 doctor。"""
    con.execute('UPDATE todo SET spec_path=? WHERE key=?', (path, key))
    con.commit()


def set_memory_ref(con, key, path):
    """設定 auto memory 系統的相關檔案參照，語意同 set_spec_path。"""
    con.execute('UPDATE todo SET memory_ref=? WHERE key=?', (path, key))
    con.commit()
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_store.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_store.py skills/todo-audit/tests/test_store.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): todo_store 新增 progress/spec_path/memory_ref 欄位

set_progress() 疊加式位元運算，全滿時單向自動轉 done 且繞開
ClaimConflict 擁有者比對；set_spec_path()/set_memory_ref() 存純路徑
參照。舊 done 條目於下次 connect() 冪等 backfill 為滿旗標。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 3: `todo_cli.py` —— `flag` 子指令、`edit --spec/--memory`、進度顯示

**Files:**
- Modify: `skills/todo-audit/scripts/todo_cli.py`
- Test: `skills/todo-audit/tests/test_cli.py`

**Interfaces:**
- Consumes：Task 1 的 `todo_flags.FLAGS`/`ORDER`/`has`；Task 2 的 `todo_store.set_progress`/`set_spec_path`/`set_memory_ref`
- Produces：CLI 子指令 `flag <ref> <set|clear|toggle> <name>`；`edit <ref> --spec <path> --memory <path>`；`list`/`show`/`dump` 輸出新增進度行與（非空時的）`spec`/`memory` 行；新增函式 `progress_bar(progress) -> str`、`item_progress_lines(progress, spec_path, memory_ref, indent='  ') -> list[str]`（三個顯示指令共用，避免重複列印邏輯）

- [ ] **Step 1: 寫失敗測試（附加到 `test_cli.py`）**

```python
class TestProgressFlagCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_set_then_show_reflects_progress(self):
        r = run_cli('flag', 'T-001', 'set', 'implemented',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('✅implemented', r.stdout)
        self.assertIn('進度', r.stdout)

    def test_flag_unknown_name_rejected(self):
        r = run_cli('flag', 'T-001', 'set', 'not_a_flag',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)

    def test_flag_clear_and_toggle(self):
        run_cli('flag', 'T-001', 'set', 'reviewed',
                '--project', 'demo', env_home=self.home)
        r = run_cli('flag', 'T-001', 'clear', 'reviewed',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('flag', 'T-001', 'toggle', 'committed',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_completing_all_flags_shows_done_status_in_list(self):
        for name in ('implemented', 'reviewed', 'committed', 'compiled',
                    'tested', 'live_tested', 'deployed'):
            run_cli('flag', 'T-001', 'set', name,
                    '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('status=done', r.stdout)

    def test_edit_spec_and_memory_ref_show_in_show_output(self):
        r = run_cli('edit', 'T-001', '--spec', 'docs/specs/x.md',
                    '--memory', 'memory/y.md',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('docs/specs/x.md', r.stdout)
        self.assertIn('memory/y.md', r.stdout)

    def test_list_omits_spec_memory_lines_when_unset(self):
        r = run_cli('list', '--project', 'demo', env_home=self.home)
        self.assertNotIn('spec:', r.stdout)
        self.assertNotIn('memory:', r.stdout)

    def test_dump_json_includes_progress_and_refs(self):
        run_cli('flag', 'T-001', 'set', 'implemented',
                '--project', 'demo', env_home=self.home)
        run_cli('edit', 'T-001', '--spec', 'docs/specs/x.md',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dump', '--format', 'json', '--all',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        item = next(i for i in data['items'] if i['short_id'] == 'T-001')
        self.assertEqual(item['progress'], ['implemented'])
        self.assertEqual(item['spec_path'], 'docs/specs/x.md')
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_cli.py -k TestProgressFlagCommand -v`
Expected: FAIL——`flag` 子指令不存在（argparse 報 invalid choice）、`edit` 無 `--spec`/`--memory`

- [ ] **Step 3: 修改 `todo_cli.py`**

在 import 區塊加入：

```python
import todo_flags
```

新增顯示 helper（放在 `body_of` 之後）：

```python
_FLAG_LABELS = {'implemented': 'implemented', 'reviewed': 'reviewed',
               'committed': 'committed', 'compiled': 'compiled',
               'tested': 'tested', 'live_tested': 'live_tested',
               'deployed': 'deployed'}


def progress_bar(progress):
    p = progress or 0
    return ' '.join(
        f"{'✅' if todo_flags.has(p, name) else '⬜'}{_FLAG_LABELS[name]}"
        for name in todo_flags.ORDER)


def item_progress_lines(progress, spec_path, memory_ref, indent='  '):
    """進度視覺化＋非空 spec/memory 參照的顯示行，供 list/show/dump 三處共用
    ——避免三個指令各自重複同一段列印邏輯。"""
    lines = [f'{indent}進度：{progress_bar(progress)}']
    if spec_path:
        lines.append(f'{indent}spec: {spec_path}')
    if memory_ref:
        lines.append(f'{indent}memory: {memory_ref}')
    return lines
```

修改 `rows()`，加入三個欄位：

```python
def rows(con, include_all=False, section=None, only_doing=False, by=None):
    q = ('SELECT short_id, key, raw_title, status, status_by, status_at,'
         ' progress, spec_path, memory_ref'
         ' FROM todo WHERE sort_order IS NOT NULL')
    p = []
    if not include_all:
        q += " AND status IN ('pending','doing')"
    if only_doing:
        q += " AND status='doing'"
    if by:
        q += ' AND status_by=?'
        p.append(by)
    if section:
        q += ' AND section=?'
        p.append(section)
    q += ' ORDER BY sort_order'
    return con.execute(q, p).fetchall()
```

修改 `cmd_list`：

```python
def cmd_list(con, args):
    print(header(con))
    for (sid, key, raw, status, sby, sat, progress, spec_path,
        memory_ref) in rows(con, section=args.section, only_doing=args.doing,
                            by=args.by):
        st = todo_store.state_of(con, key)
        print(f'  {sid}  [{st}]{claim_tag(status, sby, sat)} {raw[6:]}')
        for line in item_progress_lines(progress, spec_path, memory_ref,
                                        indent='      '):
            print(line)
```

修改 `cmd_show`：

```python
def cmd_show(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    print(header(con))
    r = con.execute('SELECT short_id, raw_title, status, status_note,'
                    ' status_by, status_at, progress, spec_path, memory_ref'
                    ' FROM todo WHERE key=?', (key,)).fetchone()
    owner = (f'  by={r[4] or "(未署名)"} @ {r[5] or "時間不明"}'
             f'（{todo_store.humanize_age(r[5])}）'
             if r[2] and r[2] != 'pending' else '')
    print(f'  {r[0]}  [{todo_store.state_of(con, key)}]  status={r[2]}{owner}'
          + (f'  note={r[3]}' if r[3] else ''))
    print(f'  {r[1]}')
    for line in item_progress_lines(r[6], r[7], r[8], indent='  '):
        print(line)
    for seq, text in con.execute(
            'SELECT seq, text FROM todo_line WHERE todo_key=? ORDER BY seq',
            (key,)):
        print(f'{seq:>3} {text}' if args.seq else text)
```

修改 `cmd_dump`：

```python
def cmd_dump(con, args):
    if args.format == 'json':
        out = []
        for (sid, key, raw, status, sby, sat, progress, spec_path,
            memory_ref) in rows(con, include_all=args.all,
                                section=args.section):
            out.append({'short_id': sid, 'key': key, 'title': raw[6:],
                        'status': status, 'status_by': sby, 'status_at': sat,
                        'state': todo_store.state_of(con, key),
                        'progress': todo_flags.summary(progress),
                        'spec_path': spec_path, 'memory_ref': memory_ref,
                        'body': body_of(con, key)})
        print(json.dumps({'freshness': todo_store.freshness(con), 'items': out},
                         ensure_ascii=False, indent=2))
        return
    print(header(con))
    for (sid, key, raw, status, sby, sat, progress, spec_path,
        memory_ref) in rows(con, include_all=args.all, section=args.section):
        st = todo_store.state_of(con, key)
        print(f'\n{sid} [{st}]{claim_tag(status, sby, sat)} {raw}')
        for line in item_progress_lines(progress, spec_path, memory_ref,
                                        indent='    '):
            print(line)
        for text in body_of(con, key):
            print(text)
```

修改 `cmd_edit`：

```python
def cmd_edit(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    if (args.title is None and args.line is None
            and args.spec is None and args.memory is None):
        print('需指定 --title、--line N <text>、--spec 或 --memory',
              file=sys.stderr)
        return 5
    if args.title is not None:
        key = todo_store.edit_title(con, key, args.title)
    if args.line is not None:
        if args.text is None:
            print('--line 需搭配新內容（位置參數 text）', file=sys.stderr)
            return 5
        todo_store.edit_line(con, key, args.line, args.text)
    if args.spec is not None:
        todo_store.set_spec_path(con, key, args.spec)
    if args.memory is not None:
        todo_store.set_memory_ref(con, key, args.memory)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                      (key,)).fetchone()[0]
    print(f'{sid} 已更新')
```

新增 `cmd_flag`（放在 `cmd_mark` 之後）：

```python
def cmd_flag(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    new_progress = todo_store.set_progress(con, key, args.op, args.name)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                      (key,)).fetchone()[0]
    print(f'{sid} 進度 {args.op} {args.name} → {progress_bar(new_progress)}')
```

在 `main()` 裡，`edit` 子 parser 加兩個選項：

```python
    p = sub.add_parser('edit', parents=[common])
    p.add_argument('ref')
    p.add_argument('text', nargs='?', help='--line 時的新內容（需自帶 marker）')
    p.add_argument('--title', help='新標題（日期前綴自動保留）')
    p.add_argument('--line', type=int, help='要改的 body 行序號（見 show）')
    p.add_argument('--spec', help='規格文件路徑參照（只存字串，不驗證存在）')
    p.add_argument('--memory', help='auto memory 系統的相關檔案路徑參照')
    p.set_defaults(fn=cmd_edit)
```

在 `mark` 子 parser 之後加入 `flag` 子 parser：

```python
    p = sub.add_parser('flag', parents=[common])
    p.add_argument('ref')
    p.add_argument('op', choices=['set', 'clear', 'toggle'])
    p.add_argument('name', choices=list(todo_flags.FLAGS.keys()))
    p.set_defaults(fn=cmd_flag)
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_cli.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_cli.py skills/todo-audit/tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): CLI 新增 flag 子指令與 edit --spec/--memory

list/show/dump 加入進度視覺化與 spec/memory 參照顯示；flag
<ref> set|clear|toggle <name> 疊加式操作交付進度位元。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 4: `doctor` —— `spec_path`/`memory_ref` 參照完整性檢查

**Files:**
- Modify: `skills/todo-audit/scripts/todo_cli.py`（`cmd_doctor` 函式）
- Test: `skills/todo-audit/tests/test_doctor.py`

**Interfaces:**
- Consumes：Task 2/3 已存在的 `spec_path`/`memory_ref` 欄位與 `set_spec_path`/`set_memory_ref`
- Produces：新增模組級函式 `_resolve_ref_path(base, value) -> Path`；`doctor` stdout 新增 `WARN <short_id> 的 spec_path/memory_ref 指向不存在的檔案：<path>` 這類行（沿用既有 `PREFIX_RE` 可解析格式）

- [ ] **Step 1: 寫失敗測試（附加到 `test_doctor.py`）**

```python
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
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_doctor.py -k TestDoctorDanglingRefs -v`
Expected: FAIL——doctor 尚未印出任何 `spec_path`/`memory_ref` 相關 WARN

- [ ] **Step 3: 修改 `todo_cli.py` 的 `cmd_doctor`**

在 `cmd_doctor` 函式所在模組加入 helper（放在 `cmd_doctor` 定義之前）：

```python
def _resolve_ref_path(base, value):
    """spec_path 相對 repo root、memory_ref 相對 HOME 解析；
    絕對路徑原樣使用。"""
    p = Path(value)
    return p if p.is_absolute() else base / value
```

在 `cmd_doctor` 內，於既有 `if con is None: ... else: ...`（DB 存在性與新鮮度）區塊**之後**、`defaults = {...}` 這行**之前**插入：

```python
    if con is not None:
        for sid, spec_path, memory_ref in con.execute(
                "SELECT short_id, spec_path, memory_ref FROM todo"
                " WHERE sort_order IS NOT NULL"
                " AND (spec_path IS NOT NULL OR memory_ref IS NOT NULL)"):
            if spec_path and not _resolve_ref_path(repo, spec_path).exists():
                print(f'WARN {sid} 的 spec_path 指向不存在的檔案：{spec_path}')
            if memory_ref and not _resolve_ref_path(home, memory_ref).exists():
                print(f'WARN {sid} 的 memory_ref 指向不存在的檔案：{memory_ref}')
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_doctor.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_cli.py skills/todo-audit/tests/test_doctor.py
git commit -m "$(cat <<'EOF'
fix(todo-audit): doctor 檢查 spec_path/memory_ref 參照完整性

壞掉的規格文件/memory 參照現在會在 doctor stdout 印 WARN，
不再靜默過期。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 5: 文件更新

**Files:**
- Modify: `docs/TODO-SYSTEM.md`

**Interfaces:**
- Consumes：Task 1-4 全部落地的功能（純文件，無程式碼介面）

- [ ] **Step 1: 在「管理指令速查」區塊的「修改條目內容」之後加入新段落**

```markdown
# 交付進度位元旗標（實作/review/commit/compile/test/live_tested/deploy）
python3 $T flag <T-NNN> set <name>      # 例：flag T-042 set reviewed
python3 $T flag <T-NNN> clear <name>
python3 $T flag <T-NNN> toggle <name>
# name ∈ implemented / reviewed / committed / compiled / tested / live_tested / deployed
# 七個旗標全數點滿時，status 會自動轉為 done（unpick 條目不受影響）

# 規格文件 / session memory 參照（只存路徑字串，不驗證存在；doctor 會檢查）
python3 $T edit <T-NNN> --spec "docs/specs/2026-08-22-xxx-design.md"
python3 $T edit <T-NNN> --memory "memory/xxx.md"
```

在文件的「三態語意」表格前後，補充一段說明兩組欄位的關係（可放在「完成判準」小節之後）：

```markdown
## 交付進度與 status 的關係

`status`（pending/doing/done/unpick）與交付進度 `progress` 位元旗標是
兩個正交的維度：前者答「誰在做、要不要做」，後者答「做到哪個階段」。
七個位元互不強制順序，各自獨立可點。**進度全滿時單向自動轉
`status=done`**（`unpick` 的條目不受此影響）；反過來手動 `mark done`
不會強制把進度一併點滿——不是每條 todo 都走得完整個 pipeline。

`doctor` 會檢查每條 `--spec`/`--memory` 參照的檔案是否存在，壞掉的
參照印 WARN，避免靜默過期。詳細設計見
`docs/specs/2026-08-22-todo-progress-bitmask-design.md`。
```

- [ ] **Step 2: 確認文件無語法問題（人工檢視即可，無自動化測試）**

Run: `cd /var/repos/todos && git diff docs/TODO-SYSTEM.md`
Expected: diff 內容與上方一致，Markdown 格式正確

- [ ] **Step 3: Commit**

```bash
git add docs/TODO-SYSTEM.md
git commit -m "$(cat <<'EOF'
docs(todo-audit): 補齊交付進度位元旗標的操作手冊說明

新增 flag 子指令與 edit --spec/--memory 的用法，以及 progress
與 status 兩個正交維度的關係說明。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```
