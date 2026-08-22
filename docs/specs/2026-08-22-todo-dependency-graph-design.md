# Todo 依賴圖與變更軌跡設計

> 狀態：待使用者複審
> 關聯：`skills/todo-audit/scripts/todo_store.py`、`todo_cli.py`、`todo_audit.py`
> 前置閱讀：`docs/TODO-SYSTEM.md`、`docs/specs/2026-08-22-todo-progress-bitmask-design.md`（`status`/`progress` 既有規則，本次同樣不動）
> 觸發：以 Claude Code 原生 Tasks 系統與其設計靈感來源 Beads（steveyegge/beads）為競品標竿，
> 做功能完整性落差研究後的升級方案（研究過程見對話中已發布的《Todo-Audit 強化藍圖》）

## 1. 背景與動機

競品研究確認兩件事：

1. **Claude Code 原生 Tasks**（`~/.claude/tasks/`，`CLAUDE_CODE_TASK_LIST_ID` 綁定）已具備
   持久化 + 依賴阻塞（`addBlockedBy`/`addBlocks`）+ 認領（owner）機制，跟本專案的
   `doing` 鎖高度重疊。但官方文件證實：**Task 工具集在 Opus 4.8/Sonnet 5/Fable 5/
   Mythos 5 等新模型上預設不開放**，需手動 `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` 或
   `--allowedTools` 才能用——它不是「已取代 TodoWrite 的現況」，是選配功能。
2. **Beads**（Tasks 的設計靈感來源，功能比 Tasks 更完整）核心賣點是**依賴關係圖**
   （`blocks`/`related`/`parent-child`/`discovered-from` 四種邊）+ `bd ready` 查詢
   「無阻塞、可動手」的任務——這是本專案目前**完全沒有**的能力，每條待辦互相獨立，
   多條待辦之間若有先後關係，只能靠自由文字描述，機器讀不出來。

本專案的功能落差不在「稽核」（那塊已經比兩者都深，見前一輪研究），而在**任務間關係**
與**完整變更史**這兩塊。本次設計補上這兩塊，同時明確保留已驗證有效的既有原則
（見第 9 節「非目標」）——特別是 Beads 的 `bd doctor --fix` 自動修復路線，**明確不採用**，
本專案的稽核維持「只給證據，不自動改」。

## 2. 範圍界定

**In scope：**
- 新增 `todo_dep` 表：條目間的有向關係（`blocks`/`related`/`parent-child`/`discovered-from`）
- 新增 `todo_event` 表：append-only 變更軌跡，MVP 涵蓋 `status` 轉換與 `dep` 增刪
- `todo_cli.py` 新增 `dep` 子指令（`add`/`rm`/`list`）
- `list --ready`：pending 且未被 `blocks` 邊阻塞的條目
- `mark done` 時，掃一次「誰在等這條」，把新變 ready 的下游條目印到 stdout（純提示，不改狀態）
- `dep add` 對 `blocks`/`parent-child` 做環狀依賴檢查，偵測到就拒絕並報錯
- `show <ref>` 新增顯示依賴關係與變更軌跡
- `doctor` 新增依賴完整性檢查（懸空 `to_key`/`from_key`、環狀依賴）——**偵測不修復**

**Out of scope（本次不做，理由見下）：**
- 不做 `doctor --fix` 自動修復——見第 9 節，這是刻意的哲學分歧，不是漏做
- 不把 `blocked` 做成新的 `status` enum 值——`blocked` 是依賴圖算出來的動態視圖，
  `status` 欄位維持 `pending`/`doing`/`done`/`unpick` 四值不變（沿用位元旗標設計已定的
  「不動 status」原則）
- `todo_event` 本次只記 `status` 與 `dep` 兩類事件，不記每個欄位（`title`/`flag`/
  `spec_path` 等）的變更——欄位級全覆蓋是合理的下一步，但一次做全部會讓每個既有
  mutator 都要改，範圍過大；先覆蓋「多 session 交接」最需要的兩類事件
- 不做 MCP server 介面——Beads 有，但這是獨立的整合面向（讓非 Claude Code 的工具也能
  讀寫），跟本次「功能完整性」的落差無關，且需要額外的協定層決定，留待未來單獨評估
- 不做檔案鎖（file locking）/ git worktree 隔離——Beads/Agent Teams 的這塊解決的是
  「多 agent 同時改同一批程式碼檔案」，本專案的條目是知識/決策記錄不是檔案執行單位，
  語意套不上
- 不把真相來源改成 git-tracked（Beads 的做法）——本專案待辦資料刻意不進 git（含檔案
  路徑、架構細節、未修漏洞等敏感內容），這是既有原則，本次維持

## 3. 資料模型變更

沿用既有的 idempotent migration 模式：

```sql
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
```

兩張表都用 `CREATE TABLE IF NOT EXISTS`（比照 `BASE_SCHEMA` 既有六表的模式，不放進
`MIGRATIONS` 的 `ALTER TABLE` 清單——這是新表不是新欄位）。

`kind` 合法值：`blocks` / `related` / `parent-child` / `discovered-from`，未知值一律
`raise ValueError`（比照 `todo_flags.py` 對未知 `name` 的既有處理方式）。

`todo_dep` 的方向語意：`(from_key, to_key, 'blocks')` 讀作「`from_key` 阻塞
`to_key`」——`to_key` 要等 `from_key` 完成（`done`/`unpick`）才算 ready。
`parent-child` 讀作「`from_key` 是 `to_key` 的父項」。`discovered-from` 讀作
「`to_key` 是做 `from_key` 時發現的」（Beads 的 provenance 概念）。`related` 無方向
語意上的強制，但仍存成一列，查詢時雙向都要納入。

## 4. 依賴圖運算 API

新檔 `skills/todo-audit/scripts/todo_deps.py`，只放純函式（不碰 DB），比照
`todo_flags.py` 的隔離原則：

```python
KINDS = {'blocks', 'related', 'parent-child', 'discovered-from'}

def validate_kind(kind) -> None: ...          # 不在 KINDS 內 raise ValueError

def has_cycle(edges, new_from, new_to) -> bool:
    """edges: [(from_key, to_key)] 既有的 blocks/parent-child 邊。
    新增 (new_from, new_to) 後，從 new_to 出發能否走回 new_from——
    用 DFS，純函式、不碰 DB，方便獨立單元測試。"""

def is_ready(todo_status, blocker_statuses) -> bool:
    """todo_status 是 pending 且所有 blocker 的 status 都是 done/unpick 時回 True。
    blocker_statuses: 該條目所有 `blocks` 入邊來源的 status list。"""

def newly_unblocked(done_key, all_edges, all_statuses) -> list:
    """done_key 剛轉 done/unpick 後，回傳因此變 ready 的下游 key 清單，
    供 mark done 時印出提示用。"""
```

`todo_store.py` 負責把 DB 資料轉成這些函式要的純資料結構（`edges`/`statuses`），
運算邏輯全部留在 `todo_deps.py`，跟 `todo_flags.py` 的分工方式一致。

## 5. CLI 介面

```bash
T=~/.claude/skills/todo-audit/scripts/todo_cli.py

python3 $T dep add <A> blocks <B>          # A 阻塞 B；環狀依賴會被拒絕
python3 $T dep add <A> related <B>
python3 $T dep add <A> parent-child <B>
python3 $T dep add <A> discovered-from <B>
python3 $T dep rm  <A> blocks <B>
python3 $T dep list <ref>                  # 印該條目的所有上下游關係

python3 $T list --ready                    # pending 且未被 blocks 邊卡住的條目
```

`dep add` 內部呼叫 `todo_store.add_dep(con, from_key, to_key, kind, by)`：先用
`todo_deps.validate_kind` 檢查合法性，`blocks`/`parent-child` 再跑
`todo_deps.has_cycle`，通過才寫入並在同一 transaction 寫一筆 `todo_event`
（`action='dep_add'`，`new_value=f"{kind}:{to_key}"`）。偵測到環狀依賴時，指令
回報具體的環（例如 `T-003 → T-007 → T-003`），不只是「有環」三個字。

## 6. 與 mark done 的整合（提示，不是自動狀態轉換）

`mark <ref> done` 現有邏輯不變（狀態轉換、擁有者檢查、寫 `todo_event`
`action='status'` 一列，見第 7 節）。**新增**的部分是轉態成功後，額外查詢
`todo_deps.newly_unblocked()`，若有結果就在 stdout 多印一段：

```
✓ T-042 已標記 done
→ 因此變為可動手（無阻塞）：T-051, T-058
```

這是單純的資訊提示，**不觸發任何自動狀態變更**——比照第 2 節「不做自動化」的
原則，人要不要動手接著做，仍由人決定。`mark pending`/`unpick` 同樣觸發這個檢查
（`unpick` 也算「阻塞解除」，因為下游不用再等它真的做完）。

## 7. todo_event 寫入時機（MVP 範圍：status + dep）

- `set_status()` 每次成功轉態後多寫一列：`action='status'`，
  `old_value`/`new_value` 是轉態前後的 `status`，`by`/`at` 沿用該次呼叫的
  `--by` 與時間戳。**這張表取代第一輪研究提案的獨立 `claim_log`**——語意更廣，
  一張表涵蓋 claim 軌跡，不需要兩張功能重疊的表。
- `dep add`/`dep rm` 各寫一列（見第 5 節）。
- 寫入失敗（例如 `ClaimConflict` 擋下的轉態）**不寫 event**——只記真正發生的
  變更，不記被拒絕的嘗試，避免軌跡表混進雜訊。

`show <ref>` 新增一段 `變更軌跡`，依 `at` 由新到舊列出 `todo_event`，格式：

```
變更軌跡：
  2026-08-22 14:02  status: doing → done          (by session-alpha)
  2026-08-22 13:47  dep_add: blocks T-058          (by session-alpha)
  2026-08-20 09:10  status: pending → doing        (by session-alpha)
```

## 8. doctor 整合

`todo_cli.py` 的 `cmd_doctor`（既有 `spec_path`/`memory_ref` 參照檢查所在位置）
新增兩項檢查，沿用同一套 WARN 輸出格式，印在 stdout：

1. **懸空依賴**：`todo_dep` 的 `from_key`/`to_key` 若指向不存在的 `todo.key`
   （條目被 `rm --force` 永久刪除後留下的孤兒邊），WARN 並列出。
2. **環狀依賴**：理論上 `dep add` 時已擋，但允許人工直接改 DB 或未來程式碼有漏洞
   的情況下複查一次，`blocks`/`parent-child` 邊跑一次全圖環檢測，抓到就 WARN。

**明確不做**`doctor --fix` 自動清除懸空邊或自動斷環——比照第 9 節的核心原則，
稽核只負責讓問題被看見，怎麼處理是人的決定。

## 9. 非目標／刻意的哲學分歧（相對於 Tasks / Beads）

這節記錄「研究過競品做法、確認不採用」的決定，避免以後有人重新提案時要重新
論證一次：

| 項目 | Tasks / Beads 的做法 | 本專案的決定 | 理由 |
|---|---|---|---|
| 稽核發現問題後 | Beads `bd doctor --fix` 自動修 | 只 WARN，不自動改 | 「分流器不是判官」是已驗證有效的核心原則（見 README 稽核章節），自動修等於把判斷權交給啟發式，跟過去實測 33% 召回率、容易誤判的教訓相衝突 |
| 真相來源 | Beads：git-tracked JSONL | 維持 `~/.claude/todos/`，不進 git | 待辦內容含各專案檔案路徑、架構細節、未修漏洞，本來就是刻意排除進版控（見 `.gitignore`／README「資料不在這個 repo」） |
| 阻塞狀態 | Tasks 把 `blocked` 存成 `status` 的一個值 | `blocked` 是依賴圖算出的動態視圖，`status` 不新增值 | 沿用位元旗標設計已定案的「不動 status」，避免兩個獨立設計互相打架 |
| 多 agent 認領到期 | （無，兩者都沒做心跳/TTL） | 維持無 TTL、人工判斷死活 | 上一輪研究已確認這是本專案刻意設計，K8s lease 式心跳需要常駐 daemon，前提不成立 |
| 對外介面 | Beads 有 MCP server | 維持 CLI + hooks，MCP 留待未來單獨評估 | 不是本次「功能完整性」落差的一部分，屬於另一個決策維度（要不要支援非 Claude Code 的呼叫端） |

## 10. 測試計畫

- `todo_deps.py`：純函式單元測試——`validate_kind` 的未知值、`has_cycle` 的
  正例（真的成環）與反例（common diamond 形狀不誤判成環）、`is_ready`、
  `newly_unblocked` 的正常路徑與空結果邊界。
- `todo_store.py`：`add_dep` 的環狀依賴拒絕（含具體錯誤訊息內容）、重複邊的
  行為（`PRIMARY KEY` 衝突要回可讀錯誤，不是裸 `sqlite3.IntegrityError`）、
  `todo_event` 寫入的欄位正確性、`rm --force` 後懸空邊的產生（供 doctor 測試用）。
- `todo_cli.py`：`dep add/rm/list` 整合測試、`list --ready` 的過濾邏輯、
  `mark done` 印出 newly-unblocked 提示的整合測試。
- `todo_cli.py` 的 `cmd_doctor`：懸空依賴與環狀依賴兩種 WARN 的整合測試，
  比照 `test_doctor.py` 既有寫法。

## 11. 遷移策略

新表用 `CREATE TABLE IF NOT EXISTS`，舊資料庫首次 `connect()` 直接建出空表，
不需要 backfill——這兩張表本來就沒有舊資料可補（依賴關係與變更軌跡是全新概念，
不像 `progress` 欄位那樣能從既有 `status='done'` 反推初始值）。

## 12. 向後相容

- 不改動 `status`/`progress` 欄位的既有寫入路徑與 `ClaimConflict` 邏輯。
- 沒有使用 `dep` 功能的既有條目，`todo_dep`/`todo_event` 對它們就是空查詢，
  `list`/`show` 的既有輸出格式不受影響（新增段落只在有資料時顯示，比照位元
  旗標設計 `spec`/`memory` 兩行的既有慣例）。
- `hooks/todo-add.sh`、`hooks/todo-done.sh` 介面不變。
