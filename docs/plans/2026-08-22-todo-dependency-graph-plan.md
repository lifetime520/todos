# Todo 依賴圖與變更軌跡 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 todo 條目之間能記錄有向依賴關係（blocks/related/parent-child/discovered-from），查出「無阻塞、可動手」的條目，並用一張 append-only 表記錄 status 轉換與依賴增刪的完整變更軌跡。

**Architecture:** 新增一個不碰 DB 的純函式模組 `todo_deps.py`（kind 驗證、環狀依賴偵測、ready 判斷、下游解阻塞計算），比照 `todo_flags.py` 的隔離原則；`todo_store.py` 新增 `todo_dep`／`todo_event` 兩張表與對應的 CRUD／查詢函式，並在既有 `set_status()` 掛上事件記錄；`todo_cli.py` 新增 `dep` 子指令、`list --ready`，並在 `mark done`／`show`／`doctor` 各自掛上對應的顯示與檢查邏輯。

**Tech Stack:** Python 3、sqlite3、argparse、pytest（沿用既有 stack，不引入新依賴）

**Spec:** `docs/specs/2026-08-22-todo-dependency-graph-design.md`

## Global Constraints

- 既有 `status` 欄位（`pending`/`doing`/`done`/`unpick`）語意與互斥性完全不變；`blocked` **不**新增為 `status` 的第五個值，是依賴圖算出的動態視圖
- `todo_dep.kind` 合法值僅 `blocks`/`related`/`parent-child`/`discovered-from`，未知值一律 `raise ValueError`（比照 `todo_flags.py` 對未知 `name` 的既有處理方式）
- 只有 `blocks`/`parent-child` 兩種邊在寫入時做環狀依賴檢查；`related`/`discovered-from` 是純資訊性關聯，不構成執行順序，不檢查
- 偵測到環狀依賴時，錯誤訊息必須附上具體的環路徑（用 short_id 呈現），不是「有環」三個字
- `todo_event` 只記**真正發生**的變更——被 `ClaimConflict` 擋下的轉態、被環狀依賴拒絕的 `dep add` 都不寫事件，避免軌跡表混進雜訊
- `mark done`/`mark unpick` 之後印出的「因此變可動手」清單是**純資訊提示**，不觸發任何自動狀態變更——動不動手仍由人決定
- `doctor` 新增的懸空依賴／環狀依賴檢查**只 WARN，不 `--fix`**——稽核只給證據，不自動修
- `set_progress()` 的自動轉 `done` 觸發（既有邏輯）本次**不**寫 `todo_event`——維持 spec 定義的 MVP 範圍（只涵蓋透過 `set_status()` 的轉態），如需涵蓋屬於未來的獨立任務
- 兩張新表用 `CREATE TABLE IF NOT EXISTS` 加進 `BASE_SCHEMA`，不是 `MIGRATIONS` 的 `ALTER TABLE`——這是新表不是新欄位，且沒有舊資料需要 backfill
- 所有 schema 變更必須 idempotent（`connect()` 可重複呼叫不炸）

---

## 檔案結構總覽

| 檔案 | 動作 | 職責 |
|---|---|---|
| `skills/todo-audit/scripts/todo_deps.py` | 新增 | 依賴圖純函式：`validate_kind`／`find_cycle`／`any_cycle`／`is_ready`／`newly_unblocked` |
| `skills/todo-audit/tests/test_deps.py` | 新增 | `todo_deps.py` 單元測試 |
| `skills/todo-audit/scripts/todo_store.py` | 修改 | schema（`todo_dep`/`todo_event`）、`add_dep`/`remove_dep`/`list_deps`/`is_ready`/`ready_keys`/`newly_unblocked_after`/`list_events`、`set_status` 掛事件記錄 |
| `skills/todo-audit/tests/test_store.py` | 修改 | 上述新函式的測試 |
| `skills/todo-audit/scripts/todo_cli.py` | 修改 | `dep` 子指令、`list --ready`、`mark` 印新解阻塞提示、`show` 顯示變更軌跡、`doctor` 兩項新檢查 |
| `skills/todo-audit/tests/test_cli.py` | 修改 | `dep`/`list --ready`/`mark` 提示 的整合測試 |
| `skills/todo-audit/tests/test_doctor.py` | 修改 | doctor 懸空依賴／環狀依賴 WARN 測試 |
| `docs/TODO-SYSTEM.md` | 修改 | 補文件：`dep` 指令、`list --ready`、變更軌跡格式 |

---

### Task 1: `todo_deps.py` —— 依賴圖純函式模組

**Files:**
- Create: `skills/todo-audit/scripts/todo_deps.py`
- Test: `skills/todo-audit/tests/test_deps.py`

**Interfaces:**
- Produces：`KINDS: set[str]`（四個合法 kind）、`validate_kind(kind) -> None`（未知值 `raise ValueError`）、`find_cycle(edges, new_from, new_to) -> list[str] | None`（插入前檢查，`edges` 是既有 `(from_key, to_key)` tuple 的 list，回傳環路徑或 `None`）、`any_cycle(edges) -> list[str] | None`（對整個 `edges` 集合做全圖環檢測，doctor 用）、`is_ready(todo_status, blocker_statuses) -> bool`、`newly_unblocked(done_key, all_edges, all_statuses) -> list[str]`（`all_edges` 是全部 `blocks` 邊、`all_statuses` 是 `{key: status}`，只回傳**直接**下游、不做遞移）

- [ ] **Step 1: 寫失敗測試 `test_deps.py`**

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_deps


class TestValidateKind(unittest.TestCase):
    def test_known_kinds_pass(self):
        for k in ('blocks', 'related', 'parent-child', 'discovered-from'):
            todo_deps.validate_kind(k)  # 不 raise 即通過

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            todo_deps.validate_kind('bogus')


class TestFindCycle(unittest.TestCase):
    def test_direct_cycle_detected(self):
        # A blocks B，現在要加 B blocks A —— 直接互相阻塞
        edges = [('A', 'B')]
        cyc = todo_deps.find_cycle(edges, 'B', 'A')
        self.assertIsNotNone(cyc)
        self.assertEqual(cyc[0], 'B')
        self.assertEqual(cyc[-1], 'B')

    def test_indirect_cycle_detected(self):
        # A blocks B blocks C，現在要加 C blocks A —— 三節點環
        edges = [('A', 'B'), ('B', 'C')]
        cyc = todo_deps.find_cycle(edges, 'C', 'A')
        self.assertIsNotNone(cyc)
        self.assertIn('A', cyc)
        self.assertIn('B', cyc)
        self.assertIn('C', cyc)

    def test_diamond_shape_is_not_a_false_positive(self):
        # A blocks B、A blocks C、B blocks D、C blocks D 是合法的鑽石形，
        # 加 D blocks E 不該被誤判成環
        edges = [('A', 'B'), ('A', 'C'), ('B', 'D'), ('C', 'D')]
        self.assertIsNone(todo_deps.find_cycle(edges, 'D', 'E'))

    def test_unrelated_edge_addition_has_no_cycle(self):
        edges = [('A', 'B')]
        self.assertIsNone(todo_deps.find_cycle(edges, 'C', 'D'))


class TestAnyCycle(unittest.TestCase):
    def test_detects_cycle_in_existing_edge_set(self):
        edges = [('A', 'B'), ('B', 'C'), ('C', 'A')]
        cyc = todo_deps.any_cycle(edges)
        self.assertIsNotNone(cyc)

    def test_dag_has_no_cycle(self):
        edges = [('A', 'B'), ('A', 'C'), ('B', 'D'), ('C', 'D')]
        self.assertIsNone(todo_deps.any_cycle(edges))

    def test_empty_edges_has_no_cycle(self):
        self.assertIsNone(todo_deps.any_cycle([]))


class TestIsReady(unittest.TestCase):
    def test_pending_with_no_blockers_is_ready(self):
        self.assertTrue(todo_deps.is_ready('pending', []))

    def test_pending_with_all_blockers_done_is_ready(self):
        self.assertTrue(todo_deps.is_ready('pending', ['done', 'unpick']))

    def test_pending_with_one_blocker_still_pending_is_not_ready(self):
        self.assertFalse(todo_deps.is_ready('pending', ['done', 'doing']))

    def test_non_pending_status_is_never_ready(self):
        self.assertFalse(todo_deps.is_ready('doing', []))
        self.assertFalse(todo_deps.is_ready('done', []))


class TestNewlyUnblocked(unittest.TestCase):
    def test_direct_downstream_becomes_ready(self):
        # A blocks B；A 剛轉 done，B 沒有其他 blocker，應該變 ready
        edges = [('A', 'B')]
        statuses = {'A': 'done', 'B': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), ['B'])

    def test_still_blocked_by_other_blocker_is_excluded(self):
        # A blocks C、X blocks C；A 剛轉 done，但 X 還沒完成，C 不該出現
        edges = [('A', 'C'), ('X', 'C')]
        statuses = {'A': 'done', 'X': 'pending', 'C': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), [])

    def test_indirect_downstream_not_included(self):
        # A blocks B blocks C —— A 完成只解 B，不遞移去看 C
        edges = [('A', 'B'), ('B', 'C')]
        statuses = {'A': 'done', 'B': 'pending', 'C': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), ['B'])

    def test_non_pending_downstream_excluded(self):
        edges = [('A', 'B')]
        statuses = {'A': 'done', 'B': 'doing'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), [])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 確認測試失敗（模組還不存在）**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_deps.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'todo_deps'`

- [ ] **Step 3: 實作 `todo_deps.py`**

```python
"""待辦條目間的依賴關係運算。

純函式，不匯入 sqlite3、不碰 DB —— 方便獨立單元測試，也讓
todo_store.py 之外的呼叫端不需要拉整條 DB 依賴鏈（比照 todo_flags.py
的隔離原則）。
"""

KINDS = {'blocks', 'related', 'parent-child', 'discovered-from'}


def validate_kind(kind):
    if kind not in KINDS:
        raise ValueError(f'unknown dep kind: {kind}')


def find_cycle(edges, new_from, new_to):
    """插入 (new_from, new_to) 這條邊之前的環狀依賴檢查。

    edges 是既有的 (from_key, to_key) tuple list（只含 blocks/parent-child
    這兩種有序性的邊）。從 new_to 出發沿既有邊 DFS，若能走回 new_from，
    代表插入後會成環，回傳完整環路徑（含 new_from 開頭與結尾）；
    否則回傳 None。
    """
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    stack = [(new_to, [new_from, new_to])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node == new_from:
            return path
        if node in seen:
            continue
        seen.add(node)
        for nxt in adj.get(node, ()):
            stack.append((nxt, path + [nxt]))
    return None


def any_cycle(edges):
    """對整個 edges 集合做一次全圖環檢測（doctor 用，非插入時的檢查）。

    標準三色 DFS：找到第一個環就回傳節點路徑，沒有環回 None。
    用於複查「理論上 find_cycle 已擋，但允許人工直接改 DB 或未來程式碼
    有漏洞」的情況（見 spec 第 8 節）。
    """
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    def dfs(u, path):
        color[u] = GRAY
        for v in adj.get(u, ()):
            state = color.get(v, WHITE)
            if state == WHITE:
                result = dfs(v, path + [v])
                if result:
                    return result
            elif state == GRAY:
                idx = path.index(v)
                return path[idx:] + [v]
        color[u] = BLACK
        return None

    for node in list(adj):
        if color.get(node, WHITE) == WHITE:
            result = dfs(node, [node])
            if result:
                return result
    return None


def is_ready(todo_status, blocker_statuses):
    """pending 且所有 blocker 都 done/unpick 才算 ready。"""
    if todo_status != 'pending':
        return False
    return all(s in ('done', 'unpick') for s in blocker_statuses)


def newly_unblocked(done_key, all_edges, all_statuses):
    """done_key 剛轉 done/unpick 後，回傳因此變 ready 的下游 key 清單。

    all_edges：全部 (from_key, to_key) 的 blocks 邊。all_statuses：轉態後
    的 {key: status}（done_key 本身的新狀態已經反映在裡面）。只檢查
    **直接**受 done_key 阻塞的下游，不做遞移——多層依賴鏈要等它自己的
    直接上游都解除，才會出現在下一次 mark done 的提示裡；一次提示只講
    「這一步做完後，馬上能動手」的條目，不是整條鏈的預告。
    """
    downstream = [to_ for (from_, to_) in all_edges if from_ == done_key]
    blockers_of = {}
    for from_, to_ in all_edges:
        blockers_of.setdefault(to_, []).append(from_)
    result = []
    for key in downstream:
        status = all_statuses.get(key)
        blocker_statuses = [all_statuses.get(b) for b in blockers_of.get(key, [])]
        if is_ready(status, blocker_statuses):
            result.append(key)
    return result
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_deps.py -v`
Expected: PASS，17 個測試全綠

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_deps.py skills/todo-audit/tests/test_deps.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): 新增依賴圖純函式模組 todo_deps

kind 驗證、插入時的環狀依賴檢查（find_cycle）、全圖環複查
（any_cycle，供 doctor 用）、ready 判斷、下游解阻塞計算，供
todo_store 的依賴圖功能使用。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 2: `todo_store.py` —— schema、依賴 CRUD、變更軌跡

**Files:**
- Modify: `skills/todo-audit/scripts/todo_store.py`（`BASE_SCHEMA`、import、新增函式、`set_status` 掛事件）
- Test: `skills/todo-audit/tests/test_store.py`

**Interfaces:**
- Consumes：Task 1 的 `todo_deps.validate_kind`/`find_cycle`/`any_cycle`/`is_ready`/`newly_unblocked`
- Produces：`add_dep(con, from_key, to_key, kind, by=None) -> None`（未知 kind/key `raise ValueError`/`KeyError`，環狀依賴 `raise ValueError` 附路徑）、`remove_dep(con, from_key, to_key, kind, by=None) -> None`（不存在 `raise KeyError`）、`list_deps(con, key) -> list[tuple]`（`(direction, kind, other_key, other_short_id)`，`direction` ∈ `'in'`/`'out'`）、`is_ready(con, key) -> bool`、`ready_keys(con) -> list[str]`、`newly_unblocked_after(con, key) -> list[str]`（回傳 short_id 清單）、`list_events(con, key) -> list[tuple]`（`(action, old_value, new_value, by, at)`，新到舊）。DB 新增 `todo_dep`、`todo_event` 兩張表。`set_status()` 成功轉態後多寫一筆 `todo_event`。

- [ ] **Step 1: 寫失敗測試（附加到 `test_store.py` 檔尾，`if __name__` 區塊之前）**

```python
class TestDependencyGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / 't.sqlite'
        self.con = todo_store.connect(self.dbpath)
        self.keys = {}
        for label in ('A', 'B', 'C', 'D'):
            sid = todo_store.append_item(self.con, 'demo', f'條目{label}', '', '')
            self.keys[label] = self.con.execute(
                'SELECT key FROM todo WHERE short_id=?', (sid,)).fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_migration_creates_dep_and_event_tables(self):
        tables = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('todo_dep', tables)
        self.assertIn('todo_event', tables)

    def test_add_dep_creates_row(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks',
                           by='sess-a')
        row = self.con.execute(
            'SELECT from_key, to_key, kind, created_by FROM todo_dep').fetchone()
        self.assertEqual(row, (self.keys['A'], self.keys['B'], 'blocks', 'sess-a'))

    def test_add_dep_records_event(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks',
                           by='sess-a')
        action, old, new, by, at = todo_store.list_events(
            self.con, self.keys['A'])[0]
        self.assertEqual(action, 'dep_add')
        self.assertIsNone(old)
        self.assertIn('blocks', new)
        self.assertEqual(by, 'sess-a')

    def test_add_dep_unknown_kind_raises_value_error(self):
        with self.assertRaises(ValueError):
            todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'bogus')

    def test_add_dep_unknown_key_raises_key_error(self):
        with self.assertRaises(KeyError):
            todo_store.add_dep(self.con, 'nosuchkey', self.keys['B'], 'blocks')

    def test_add_dep_cyclic_blocks_rejected_with_readable_message(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        with self.assertRaises(ValueError) as ctx:
            todo_store.add_dep(self.con, self.keys['B'], self.keys['A'], 'blocks')
        self.assertIn('環', str(ctx.exception))

    def test_add_dep_related_kind_allows_reciprocal_without_cycle_check(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'related')
        # related 無序性，反向也該能加，不該被當成環拒絕
        todo_store.add_dep(self.con, self.keys['B'], self.keys['A'], 'related')

    def test_remove_dep_deletes_row_and_records_event(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.remove_dep(self.con, self.keys['A'], self.keys['B'], 'blocks',
                              by='sess-a')
        self.assertEqual(
            self.con.execute('SELECT COUNT(*) FROM todo_dep').fetchone()[0], 0)
        action = todo_store.list_events(self.con, self.keys['A'])[0][0]
        self.assertEqual(action, 'dep_rm')

    def test_remove_dep_missing_raises_key_error(self):
        with self.assertRaises(KeyError):
            todo_store.remove_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')

    def test_list_deps_returns_both_directions(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        out_from_a = todo_store.list_deps(self.con, self.keys['A'])
        out_from_b = todo_store.list_deps(self.con, self.keys['B'])
        self.assertEqual(out_from_a, [('out', 'blocks', self.keys['B'],
                                       self.con.execute(
                                           'SELECT short_id FROM todo WHERE key=?',
                                           (self.keys['B'],)).fetchone()[0])])
        self.assertEqual(out_from_b[0][0], 'in')

    def test_is_ready_true_when_no_blockers(self):
        self.assertTrue(todo_store.is_ready(self.con, self.keys['A']))

    def test_is_ready_false_when_blocked_by_pending(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        self.assertFalse(todo_store.is_ready(self.con, self.keys['B']))

    def test_is_ready_true_once_blocker_done(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.set_status(self.con, self.keys['A'], 'done')
        self.assertTrue(todo_store.is_ready(self.con, self.keys['B']))

    def test_ready_keys_excludes_blocked_and_non_pending(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.set_status(self.con, self.keys['C'], 'doing', by='x')
        ready = todo_store.ready_keys(self.con)
        self.assertNotIn(self.keys['B'], ready)   # 被 A 卡住
        self.assertNotIn(self.keys['C'], ready)   # 不是 pending
        self.assertIn(self.keys['D'], ready)      # 沒被任何邊卡住

    def test_newly_unblocked_after_done_transition(self):
        todo_store.add_dep(self.con, self.keys['A'], self.keys['B'], 'blocks')
        todo_store.set_status(self.con, self.keys['A'], 'done')
        unblocked = todo_store.newly_unblocked_after(self.con, self.keys['A'])
        b_sid = self.con.execute('SELECT short_id FROM todo WHERE key=?',
                                 (self.keys['B'],)).fetchone()[0]
        self.assertEqual(unblocked, [b_sid])

    def test_set_status_records_event(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        action, old, new, by, at = todo_store.list_events(
            self.con, self.keys['A'])[0]
        self.assertEqual(action, 'status')
        self.assertEqual(old, 'pending')
        self.assertEqual(new, 'doing')
        self.assertEqual(by, 'sess-a')

    def test_set_status_does_not_record_event_on_claim_conflict(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        with self.assertRaises(todo_store.ClaimConflict):
            todo_store.set_status(self.con, self.keys['A'], 'done', by='sess-b')
        events = todo_store.list_events(self.con, self.keys['A'])
        self.assertEqual(len(events), 1)  # 只有原本那次成功的 doing 轉態

    def test_list_events_orders_newest_first(self):
        todo_store.set_status(self.con, self.keys['A'], 'doing', by='sess-a')
        todo_store.set_status(self.con, self.keys['A'], 'done')
        events = todo_store.list_events(self.con, self.keys['A'])
        self.assertEqual(events[0][2], 'done')
        self.assertEqual(events[1][2], 'doing')
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_store.py -k TestDependencyGraph -v`
Expected: FAIL——`todo_dep`/`todo_event` 表不存在、`add_dep` 等屬性不存在

- [ ] **Step 3: 修改 `todo_store.py`**

在 import 區塊加入（`import todo_flags` 之後）：

```python
import todo_deps
```

修改 `BASE_SCHEMA`，在結尾（`doc_meta` 表定義之後）加入兩張新表：

```python
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL, todo_file TEXT, repo TEXT,
  todo_count INT, symbol_count INT, hit_rate REAL, degraded INT);
CREATE TABLE IF NOT EXISTS todo(
  key TEXT PRIMARY KEY, date TEXT, title TEXT,
  first_seen_run INT, last_seen_run INT, content_hash TEXT);
CREATE TABLE IF NOT EXISTS anchor(
  todo_key TEXT, kind TEXT, ref TEXT, recorded_line INT,
  PRIMARY KEY(todo_key, kind, ref));
CREATE TABLE IF NOT EXISTS probe(
  run_id INT, todo_key TEXT, state TEXT, anchor_count INT,
  gone TEXT, touched TEXT, commits_since TEXT,
  PRIMARY KEY(run_id, todo_key));
CREATE TABLE IF NOT EXISTS verdict(
  run_id INT, todo_key TEXT, tier TEXT, call TEXT, reason TEXT,
  decided_at TEXT, PRIMARY KEY(run_id, todo_key, tier));
CREATE TABLE IF NOT EXISTS symbol_history(
  symbol TEXT PRIMARY KEY, never_existed INT, checked_at TEXT);
CREATE INDEX IF NOT EXISTS ix_probe_state ON probe(state);
CREATE TABLE IF NOT EXISTS todo_line(
  todo_key TEXT, seq INT, marker TEXT, text TEXT,
  PRIMARY KEY(todo_key, seq));
CREATE TABLE IF NOT EXISTS doc_meta(
  project TEXT, k TEXT, v TEXT, PRIMARY KEY(project, k));
CREATE TABLE IF NOT EXISTS todo_dep(
  from_key TEXT, to_key TEXT, kind TEXT,
  created_at TEXT, created_by TEXT,
  PRIMARY KEY(from_key, to_key, kind));
CREATE TABLE IF NOT EXISTS todo_event(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  todo_key TEXT, action TEXT,
  old_value TEXT, new_value TEXT,
  by TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS ix_event_todo ON todo_event(todo_key);
"""
```

修改 `set_status()`，在既有 `UPDATE todo SET status=...` 之後、`con.commit()` 之前加入事件記錄（`old_status` 取自函式前段已查出的 `cur[0]`）：

```python
def set_status(con, key, status, by=None, note=None, force=False):
    if status not in ('pending', 'doing', 'done', 'unpick'):
        raise ValueError(f'unknown status: {status}')
    if status == 'unpick' and not note:
        raise ValueError('unpick 必須附 --note 理由 —— '
                         '無理由的擱置與遺忘沒有區別')
    if status == 'doing' and not by:
        raise ValueError('認領必須指名：mark <ref> doing --by <誰>。'
                         '所有 session 共用同一個預設名字，等於沒有名字，'
                         '擋不住任何撞車')
    cur = con.execute('SELECT status, status_by, status_at, short_id FROM todo'
                      ' WHERE key=?', (key,)).fetchone()
    if cur is None:
        raise KeyError(key)
    if cur[0] == 'doing' and not force and (cur[1] or '') != (by or ''):
        raise ClaimConflict(
            f'{cur[3] or key[:8]} 已被 {cur[1] or "(未署名)"} 認領於 '
            f'{cur[2] or "時間不明"}（{humanize_age(cur[2])}）。'
            f'確定要接管就加 --force —— 先確認對方 session 真的已經結束')
    old_status = cur[0]
    owner = None if status == 'pending' else by
    con.execute('UPDATE todo SET status=?, status_by=?, status_at=?, status_note=?'
                ' WHERE key=?',
                (status, owner, datetime.now().isoformat(timespec='seconds'),
                 note, key))
    _record_event(con, key, 'status', old_status, status, by)
    con.commit()
```

在檔尾（`set_memory_ref` 之後）新增依賴圖與事件相關函式：

```python
# ---------------------------------------------------------------- 依賴圖／變更軌跡

def _record_event(con, key, action, old_value, new_value, by):
    con.execute(
        'INSERT INTO todo_event(todo_key,action,old_value,new_value,by,at)'
        ' VALUES(?,?,?,?,?,?)',
        (key, action, old_value, new_value, by,
         datetime.now().isoformat(timespec='seconds')))


def list_events(con, key):
    """該條目的完整變更軌跡，新到舊。"""
    return con.execute(
        'SELECT action, old_value, new_value, by, at FROM todo_event'
        ' WHERE todo_key=? ORDER BY at DESC, id DESC', (key,)).fetchall()


def _key_exists(con, key):
    return con.execute('SELECT 1 FROM todo WHERE key=?', (key,)).fetchone() is not None


def add_dep(con, from_key, to_key, kind, by=None):
    """新增一條依賴邊。blocks/parent-child 寫入前做環狀依賴檢查，
    偵測到就 raise ValueError 並附上具體的環路徑（用 short_id 呈現，
    不是「有環」三個字）。重複新增同一條邊是 no-op（INSERT OR IGNORE），
    不視為錯誤——避免重跑腳本時因為邊已存在而中斷。
    """
    todo_deps.validate_kind(kind)
    for k in (from_key, to_key):
        if not _key_exists(con, k):
            raise KeyError(k)
    if kind in ('blocks', 'parent-child'):
        existing = con.execute(
            'SELECT from_key, to_key FROM todo_dep WHERE kind=?',
            (kind,)).fetchall()
        cycle = todo_deps.find_cycle(existing, from_key, to_key)
        if cycle:
            names = ' → '.join(
                con.execute('SELECT short_id FROM todo WHERE key=?',
                            (k,)).fetchone()[0] for k in cycle)
            raise ValueError(f'會造成環狀依賴：{names}')
    con.execute(
        'INSERT OR IGNORE INTO todo_dep(from_key,to_key,kind,created_at,created_by)'
        ' VALUES(?,?,?,?,?)',
        (from_key, to_key, kind,
         datetime.now().isoformat(timespec='seconds'), by))
    _record_event(con, from_key, 'dep_add', None, f'{kind}:{to_key}', by)
    con.commit()


def remove_dep(con, from_key, to_key, kind, by=None):
    """刪除一條依賴邊。邊不存在 raise KeyError（比照 remove_line 的既有慣例：
    刪除不存在的東西是錯誤，不是靜默成功）。"""
    todo_deps.validate_kind(kind)
    cur = con.execute(
        'DELETE FROM todo_dep WHERE from_key=? AND to_key=? AND kind=?',
        (from_key, to_key, kind))
    if cur.rowcount == 0:
        raise KeyError(f'{from_key} -{kind}-> {to_key} 不存在')
    _record_event(con, from_key, 'dep_rm', f'{kind}:{to_key}', None, by)
    con.commit()


def list_deps(con, key):
    """回傳 (direction, kind, other_key, other_short_id) 的 list。
    from_key=key（本條目是起點，direction='out'）與 to_key=key（本條目是
    終點，direction='in'）都要列，show 才能同時呈現「我阻塞誰」與
    「誰阻塞我」。"""
    out = []
    for kind, other in con.execute(
            'SELECT kind, to_key FROM todo_dep WHERE from_key=?', (key,)):
        sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                          (other,)).fetchone()
        out.append(('out', kind, other, sid[0] if sid else other[:8]))
    for kind, other in con.execute(
            'SELECT kind, from_key FROM todo_dep WHERE to_key=?', (key,)):
        sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                          (other,)).fetchone()
        out.append(('in', kind, other, sid[0] if sid else other[:8]))
    return out


def is_ready(con, key):
    """該條目是否 pending 且未被任何 blocks 邊卡住。"""
    row = con.execute('SELECT status FROM todo WHERE key=?', (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    blockers = [r[0] for r in con.execute(
        "SELECT from_key FROM todo_dep WHERE to_key=? AND kind='blocks'", (key,))]
    blocker_statuses = [con.execute('SELECT status FROM todo WHERE key=?',
                                    (b,)).fetchone()[0] for b in blockers]
    return todo_deps.is_ready(row[0], blocker_statuses)


def ready_keys(con):
    """全部 pending 且未被阻塞的條目 key 清單，供 `list --ready` 使用。"""
    pending = [r[0] for r in con.execute(
        "SELECT key FROM todo WHERE status='pending' AND sort_order IS NOT NULL")]
    return [k for k in pending if is_ready(con, k)]


def newly_unblocked_after(con, key):
    """key 剛轉 done/unpick 後，回傳因此變 ready 的下游 short_id 清單。"""
    edges = con.execute(
        "SELECT from_key, to_key FROM todo_dep WHERE kind='blocks'").fetchall()
    all_keys = [r[0] for r in con.execute('SELECT key FROM todo')]
    statuses = {k: con.execute('SELECT status FROM todo WHERE key=?',
                               (k,)).fetchone()[0] for k in all_keys}
    downstream = todo_deps.newly_unblocked(key, edges, statuses)
    return [con.execute('SELECT short_id FROM todo WHERE key=?',
                        (k,)).fetchone()[0] for k in downstream]
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_store.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_store.py skills/todo-audit/tests/test_store.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): todo_store 新增依賴圖與變更軌跡

新表 todo_dep（blocks/related/parent-child/discovered-from）與
todo_event（append-only）。add_dep()/remove_dep() 做環狀依賴檢查，
拒絕時附具體環路徑；set_status() 每次成功轉態多寫一筆事件；
ready_keys()/newly_unblocked_after() 供 CLI 的 --ready 與 mark done
提示使用。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 3: `todo_cli.py` —— `dep` 子指令與 `list --ready`

**Files:**
- Modify: `skills/todo-audit/scripts/todo_cli.py`
- Test: `skills/todo-audit/tests/test_cli.py`

**Interfaces:**
- Consumes：Task 1 的 `todo_deps.KINDS`；Task 2 的 `todo_store.add_dep`/`remove_dep`/`list_deps`/`ready_keys`
- Produces：CLI 子指令 `dep add <from_ref> <kind> <to_ref> [--by X]`／`dep rm <from_ref> <kind> <to_ref>`／`dep list <ref>`；`list --ready`；新增函式 `cmd_dep(con, args)`

- [ ] **Step 1: 寫失敗測試（附加到 `test_cli.py`）**

```python
class TestDepCommand(unittest.TestCase):
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

    def test_dep_add_then_list_shows_relation(self):
        r = run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('T-001', r.stdout)
        self.assertIn('blocks', r.stdout)
        self.assertIn('T-002', r.stdout)

    def test_dep_add_unknown_kind_rejected(self):
        r = run_cli('dep', 'add', 'T-001', 'bogus', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)

    def test_dep_add_cycle_rejected_with_message(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'add', 'T-002', 'blocks', 'T-001',
                    '--project', 'demo', env_home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('環', r.stderr)

    def test_dep_rm_removes_relation(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('dep', 'rm', 'T-001', 'blocks', 'T-002',
                    '--project', 'demo', env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli('dep', 'list', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('沒有任何依賴關係', r.stdout)

    def test_list_ready_excludes_blocked_item(self):
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)
        r = run_cli('list', '--ready', '--project', 'demo', env_home=self.home)
        self.assertIn('T-001', r.stdout)
        self.assertNotIn('T-002', r.stdout)
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_cli.py -k TestDepCommand -v`
Expected: FAIL——`dep` 子指令不存在（argparse 報 invalid choice）、`list` 無 `--ready`

- [ ] **Step 3: 修改 `todo_cli.py`**

在 import 區塊加入（`import todo_flags` 之後）：

```python
import todo_deps
```

新增 `cmd_dep`（放在 `cmd_flag` 之後）：

```python
def cmd_dep(con, args):
    if args.action == 'list':
        key = todo_store.resolve_ref(con, args.ref)
        sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                          (key,)).fetchone()[0]
        deps = todo_store.list_deps(con, key)
        if not deps:
            print(f'{sid} 沒有任何依賴關係')
            return
        for direction, kind, _other_key, other_sid in deps:
            arrow = (f'{sid} -{kind}-> {other_sid}' if direction == 'out'
                     else f'{other_sid} -{kind}-> {sid}')
            print(f'  {arrow}')
        return
    if args.kind is None or args.to_ref is None:
        print('dep add/rm 需要三個參數：<from_ref> <kind> <to_ref>',
              file=sys.stderr)
        return 5
    from_key = todo_store.resolve_ref(con, args.ref)
    to_key = todo_store.resolve_ref(con, args.to_ref)
    if args.action == 'add':
        todo_store.add_dep(con, from_key, to_key, args.kind, by=args.by)
        print(f'{args.ref} -{args.kind}-> {args.to_ref} 已新增')
    else:
        todo_store.remove_dep(con, from_key, to_key, args.kind, by=args.by)
        print(f'{args.ref} -{args.kind}-> {args.to_ref} 已刪除')
```

修改 `cmd_list`，加入 `--ready` 過濾：

```python
def cmd_list(con, args):
    print(header(con))
    ready = set(todo_store.ready_keys(con)) if args.ready else None
    for (sid, key, raw, status, sby, sat, _progress, _spec_path,
        _memory_ref) in rows(con, section=args.section, only_doing=args.doing,
                             by=args.by):
        if ready is not None and key not in ready:
            continue
        st = todo_store.state_of(con, key)
        print(f'  {sid}  [{st}]{claim_tag(status, sby, sat)} {raw[6:]}')
```

在 `main()` 裡，`list` 子 parser 加一個選項：

```python
    p = sub.add_parser('list', parents=[common])
    p.add_argument('--section')
    p.add_argument('--doing', action='store_true')
    p.add_argument('--by', help='只列這個認領者的條目')
    p.add_argument('--ready', action='store_true',
                   help='只列 pending 且未被 blocks 邊卡住的條目')
    p.set_defaults(fn=cmd_list)
```

在 `flag` 子 parser 之後加入 `dep` 子 parser：

```python
    p = sub.add_parser('dep', parents=[common])
    p.add_argument('action', choices=['add', 'rm', 'list'])
    p.add_argument('ref', help='list 時是查詢對象；add/rm 時是 from_ref')
    p.add_argument('kind', nargs='?', choices=sorted(todo_deps.KINDS))
    p.add_argument('to_ref', nargs='?')
    p.add_argument('--by')
    p.set_defaults(fn=cmd_dep)
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_cli.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_cli.py skills/todo-audit/tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): CLI 新增 dep 子指令與 list --ready

dep add/rm/list 操作依賴關係，環狀依賴會被拒絕並附具體路徑；
list --ready 只列未被 blocks 邊卡住的 pending 條目。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 4: `mark` 解阻塞提示與 `show` 變更軌跡

**Files:**
- Modify: `skills/todo-audit/scripts/todo_cli.py`（`cmd_mark`、`cmd_show`）
- Test: `skills/todo-audit/tests/test_cli.py`

**Interfaces:**
- Consumes：Task 2 的 `todo_store.newly_unblocked_after`/`list_events`
- Produces：`mark <ref> done`/`unpick` 成功後，若有下游因此變 ready，stdout 多印一行；`show <ref>` 新增「變更軌跡」段落

- [ ] **Step 1: 寫失敗測試（附加到 `test_cli.py`）**

```python
class TestMarkDoneUnblocksDownstream(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_store.save_parsed(con, 'demo', todo_store.parse_md_lossless(SAMPLE))
        todo_store.assign_short_ids(con)
        con.close()
        run_cli('dep', 'add', 'T-001', 'blocks', 'T-002',
                '--project', 'demo', env_home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_done_prints_newly_unblocked_downstream(self):
        r = run_cli('mark', 'T-001', 'done', '--project', 'demo',
                    env_home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('T-002', r.stdout)
        self.assertIn('可動手', r.stdout)

    def test_mark_pending_does_not_print_unblock_line(self):
        run_cli('mark', 'T-001', 'doing', '--by', 'x',
                '--project', 'demo', env_home=self.home)
        r = run_cli('mark', 'T-001', 'pending', '--project', 'demo',
                    env_home=self.home)
        self.assertNotIn('可動手', r.stdout)

    def test_show_lists_change_history(self):
        run_cli('mark', 'T-001', 'doing', '--by', 'sess-a',
                '--project', 'demo', env_home=self.home)
        r = run_cli('show', 'T-001', '--project', 'demo', env_home=self.home)
        self.assertIn('變更軌跡', r.stdout)
        self.assertIn('pending → doing', r.stdout)
        self.assertIn('sess-a', r.stdout)
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_cli.py -k TestMarkDoneUnblocksDownstream -v`
Expected: FAIL——`mark done` 不印解阻塞提示、`show` 沒有「變更軌跡」段落

- [ ] **Step 3: 修改 `todo_cli.py`**

修改 `cmd_mark`：

```python
def cmd_mark(con, args):
    key = todo_store.resolve_ref(con, args.ref)
    todo_store.set_status(con, key, args.status, by=args.by, note=args.note,
                          force=args.force)
    todo_store.write_mirror(con, args.project_resolved,
                            mirror_path(args.project_resolved))
    who = ('（已釋放認領）' if args.status == 'pending'
           else f'（by {args.by}）' if args.by else '')
    print(f'{args.ref} → {args.status}{who}')
    if args.status in ('done', 'unpick'):
        unblocked = todo_store.newly_unblocked_after(con, key)
        if unblocked:
            print(f'→ 因此變為可動手（無阻塞）：{", ".join(unblocked)}')
```

修改 `cmd_show`，在既有 body 輸出之前加入變更軌跡（放在 `item_progress_lines` 迴圈之後、`todo_line` 迴圈之前）：

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
    events = todo_store.list_events(con, key)
    if events:
        print('  變更軌跡：')
        for action, old, new, by, at in events:
            who = f'（by {by}）' if by else ''
            if action == 'status':
                print(f'    {at}  status: {old} → {new}{who}')
            elif action == 'dep_add':
                print(f'    {at}  dep_add: {new}{who}')
            elif action == 'dep_rm':
                print(f'    {at}  dep_rm: {old}{who}')
    for seq, text in con.execute(
            'SELECT seq, text FROM todo_line WHERE todo_key=? ORDER BY seq',
            (key,)):
        print(f'{seq:>3} {text}' if args.seq else text)
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_cli.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_cli.py skills/todo-audit/tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(todo-audit): mark done 提示新解阻塞條目，show 顯示變更軌跡

mark done/unpick 後若有下游條目因此變 ready，stdout 印出提示
（純資訊，不自動轉態）；show 新增「變更軌跡」段落列出完整
status/dep 變更史。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 5: `doctor` —— 懸空依賴與環狀依賴檢查

**Files:**
- Modify: `skills/todo-audit/scripts/todo_cli.py`（`cmd_doctor`）
- Test: `skills/todo-audit/tests/test_doctor.py`

**Interfaces:**
- Consumes：Task 1 的 `todo_deps.any_cycle`
- Produces：`doctor` stdout 新增懸空依賴與環狀依賴的 WARN 行

- [ ] **Step 1: 寫失敗測試（附加到 `test_doctor.py`）**

```python
class TestDoctorDependencyIntegrity(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.home = tmp / 'home'
        self.repo = tmp / 'repo'
        self.repo.mkdir(parents=True)
        self.db = _init_db(self.home, 'demo')

    def test_dangling_dep_warns_on_stdout(self):
        con = todo_store.connect(self.db)
        sid = todo_store.append_item(con, 'demo', '測試條目', '', '')
        key = con.execute('SELECT key FROM todo WHERE short_id=?',
                          (sid,)).fetchone()[0]
        con.execute(
            "INSERT INTO todo_dep(from_key,to_key,kind,created_at,created_by)"
            " VALUES(?,?,?,?,?)", (key, 'nosuchkey', 'blocks', 'now', None))
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertIn('WARN', r.stdout)
        self.assertIn('nosuchkey', r.stdout)

    def test_cyclic_blocks_warns_on_stdout(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?', (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?', (b,)).fetchone()[0]
        # 直接寫 DB 造出環（正常路徑 add_dep 會擋，這裡模擬「已經壞掉」的資料）
        for f, t in ((ak, bk), (bk, ak)):
            con.execute(
                "INSERT INTO todo_dep(from_key,to_key,kind,created_at,created_by)"
                " VALUES(?,?,?,?,?)", (f, t, 'blocks', 'now', None))
        con.commit()
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertIn('WARN', r.stdout)
        self.assertIn('環狀依賴', r.stdout)

    def test_clean_deps_do_not_warn(self):
        con = todo_store.connect(self.db)
        a = todo_store.append_item(con, 'demo', 'A', '', '')
        b = todo_store.append_item(con, 'demo', 'B', '', '')
        ak = con.execute('SELECT key FROM todo WHERE short_id=?', (a,)).fetchone()[0]
        bk = con.execute('SELECT key FROM todo WHERE short_id=?', (b,)).fetchone()[0]
        todo_store.add_dep(con, ak, bk, 'blocks')
        con.close()
        r = run_cli('doctor', '--project', 'demo',
                    env_home=self.home, cwd=self.repo)
        self.assertNotIn('todo_dep 有懸空', r.stdout)
        self.assertNotIn('環狀依賴', r.stdout)
```

- [ ] **Step 2: 確認測試失敗**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_doctor.py -k TestDoctorDependencyIntegrity -v`
Expected: FAIL——doctor 尚未印出任何依賴相關 WARN

- [ ] **Step 3: 修改 `todo_cli.py` 的 `cmd_doctor`**

在 import 區塊（`cmd_doctor` 函式內既有的 `import todo_audit` / `import todo_config` 之後）加入：

```python
    import todo_deps
```

在既有的 `spec_path`/`memory_ref` 懸空參照檢查區塊（`for sid, spec_path, memory_ref in con.execute(...)` 那段）**之後**插入：

```python
    if con is not None:
        for from_key, to_key, kind in con.execute(
                'SELECT from_key, to_key, kind FROM todo_dep'):
            for k, role in ((from_key, 'from_key'), (to_key, 'to_key')):
                if con.execute('SELECT 1 FROM todo WHERE key=?',
                              (k,)).fetchone() is None:
                    print(f'WARN todo_dep 有懸空 {role}'
                          f'（{kind} 邊指向不存在的條目）：{k}')
        for kind in ('blocks', 'parent-child'):
            edges = con.execute(
                'SELECT from_key, to_key FROM todo_dep WHERE kind=?',
                (kind,)).fetchall()
            cyc = todo_deps.any_cycle(edges)
            if cyc:
                names = ' → '.join(
                    (con.execute('SELECT short_id FROM todo WHERE key=?',
                                (k,)).fetchone() or [k])[0] for k in cyc)
                print(f'WARN {kind} 邊存在環狀依賴：{names}')
```

- [ ] **Step 4: 確認測試通過**

Run: `cd /var/repos/todos && python3 -m pytest skills/todo-audit/tests/test_doctor.py -v`
Expected: PASS，全部（含既有）測試綠燈

- [ ] **Step 5: Commit**

```bash
git add skills/todo-audit/scripts/todo_cli.py skills/todo-audit/tests/test_doctor.py
git commit -m "$(cat <<'EOF'
fix(todo-audit): doctor 檢查依賴圖的懸空邊與環狀依賴

todo_dep 指向不存在條目的邊、blocks/parent-child 邊構成的環，
現在會在 doctor stdout 印 WARN——只偵測不自動修，稽核維持
只給證據的既有原則。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```

---

### Task 6: 文件更新

**Files:**
- Modify: `docs/TODO-SYSTEM.md`

**Interfaces:**
- Consumes：Task 1-5 全部落地的功能（純文件，無程式碼介面）

- [ ] **Step 1: 在「管理指令速查」區塊的交付進度旗標說明之後加入新段落**

```markdown
# 依賴關係（blocks / related / parent-child / discovered-from）
python3 $T dep add <from> blocks <to>        # from 阻塞 to；環狀依賴會被拒絕
python3 $T dep add <from> related <to>
python3 $T dep add <from> parent-child <to>
python3 $T dep add <from> discovered-from <to>
python3 $T dep rm  <from> blocks <to>
python3 $T dep list <ref>                    # 印該條目的所有上下游關係

python3 $T list --ready                      # pending 且未被 blocks 邊卡住的條目
```

在文件補充一段說明依賴圖與變更軌跡的語意（可放在「交付進度與 status 的關係」之後）：

```markdown
## 依賴圖與變更軌跡

條目間可以記錄有向依賴關係：`blocks`（阻塞執行順序）、`related`（純資訊
性關聯）、`parent-child`（階層）、`discovered-from`（做這條時發現了那
條，記錄來源）。只有 `blocks`/`parent-child` 在寫入時做環狀依賴檢查，
偵測到會拒絕並附上具體的環路徑。

`blocked` **不是** `status` 的第五個值——它是依賴圖算出的動態視圖，用
`list --ready` 查「pending 且未被 blocks 邊卡住」的條目。`mark done`/
`unpick` 時若有下游因此變 ready，會印出提示，但**不會自動改動它們的
status**——動不動手仍由人決定。

`show <ref>` 會列出該條目完整的變更軌跡（status 轉換與依賴增刪，
append-only，不會被覆寫）。`doctor` 會檢查依賴圖的懸空邊與環狀依賴，
只 WARN 不自動修——詳細設計見
`docs/specs/2026-08-22-todo-dependency-graph-design.md`。
```

- [ ] **Step 2: 確認文件無語法問題（人工檢視即可，無自動化測試）**

Run: `cd /var/repos/todos && git diff docs/TODO-SYSTEM.md`
Expected: diff 內容與上方一致，Markdown 格式正確

- [ ] **Step 3: Commit**

```bash
git add docs/TODO-SYSTEM.md
git commit -m "$(cat <<'EOF'
docs(todo-audit): 補齊依賴圖與變更軌跡的操作手冊說明

新增 dep 子指令、list --ready 的用法，以及 blocked 是動態視圖
（非 status 新值）、變更軌跡 append-only 的語意說明。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019pftVSDE1jfZ3btrRtTdZp
EOF
)"
```
