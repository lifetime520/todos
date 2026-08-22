# Todo 條目交付進度位元旗標設計

> 狀態：待使用者複審
> 關聯：`skills/todo-audit/scripts/todo_store.py`、`todo_cli.py`
> 前置閱讀：`docs/TODO-SYSTEM.md`（三態語意、`status` 欄位既有規則）

## 1. 背景與動機

現有 `todo.status` 是單一字串 enum（`pending`/`doing`/`done`/`unpick`），管的是
「誰在做、要不要做」這件事，四者互斥。這組語意運作良好，**本次設計完全不動它**。

真正卡住的是另一件事：CLAUDE.md 規則 #4／#8 要求的交付驗收（build 通過、測試
綠燈、實際 curl/操作驗證）現在沒有機器可讀的落地位置，只能靠 `status_note`
手寫一句話（例如「等 runtime 驗收」），下個 session 得重新讀文字才知道卡在
哪一步、卡了多久。稽核系統的 `probe.state`（ALIVE/TOUCHED/…）也是類似情況：
是否已複驗只靠 `todo_line.marker='🔍'` 的字串比對現算，不是條目本身的欄位。

需求收斂後的範圍：新增一組**可疊加、不強制順序**的「交付進度」位元旗標，
疊加在既有 `status` 之上；再加兩個 nullable 參照欄位（`spec_path`、
`memory_ref`），讓條目能指向外部的規格文件與 session 記憶。

## 2. 範圍界定

**In scope：**
- 新增 `todo.progress`（INTEGER，位元旗標）與對應的純函式運算 API
- 新增 `todo.spec_path`、`todo.memory_ref`（TEXT，nullable，純路徑參照）
- `progress` 七旗標全數點滿時，自動將 `status` 轉為 `done`（單向觸發）
- `todo_cli.py` 新增 `flag` 子指令；`edit` 支援 `--spec`／`--memory`
- `list`/`show`/`dump` 輸出加入進度視覺化
- `doctor` 檢查 `spec_path`／`memory_ref` 是否為存在的檔案，不存在則 WARN

**Out of scope（本次確認過不需要）：**
- 不新增「提示詞」欄位——現有 `💡` 回憶提示詞 marker 已足夠
- 不新增「note」欄位——現有 `append_note`／`status_note`／`🔍` 複驗記錄已足夠
- 不強制 pipeline 位元之間的完成順序
- 不把 `pending`/`doing`/`unpick` 併入同一個大位元遮罩
- `mark done` 手動下達時，不強制把 `progress` 一併點滿

## 3. 資料模型變更

沿用 `todo_store.py` 既有的 idempotent migration 模式（`MIGRATIONS` 清單，
`ALTER TABLE` + 捕捉 `duplicate column name`）：

```sql
ALTER TABLE todo ADD COLUMN progress INT DEFAULT 0
ALTER TABLE todo ADD COLUMN spec_path TEXT
ALTER TABLE todo ADD COLUMN memory_ref TEXT
```

`progress` 為 `NULL` 時視同 `0`（尚未開始任何階段）——查詢與顯示層一律用
`COALESCE(progress, 0)`，避免舊資料因欄位剛加入而是 `NULL` 造成位元運算炸掉。

## 4. 位元旗標定義與運算 API

新檔 `skills/todo-audit/scripts/todo_flags.py`，只放純函式（不碰 DB、不匯入
`sqlite3`），方便獨立單元測試：

```python
FLAGS = {
    'implemented':  1 << 0,   # 實作完成
    'reviewed':     1 << 1,   # code review 完成
    'committed':    1 << 2,   # 已 commit
    'compiled':     1 << 3,   # build/編譯通過
    'tested':       1 << 4,   # 自動化測試綠燈
    'live_tested':  1 << 5,   # 實際跑起來驗證過（curl/手動操作，對應 CLAUDE.md #8）
    'deployed':     1 << 6,   # 已部署
}
ALL_FLAGS = sum(FLAGS.values())  # 127

def has(progress, name) -> bool: ...      # progress & FLAGS[name] != 0
def set_(progress, name) -> int: ...      # progress | FLAGS[name]
def clear(progress, name) -> int: ...     # progress & ~FLAGS[name]
def toggle(progress, name) -> int: ...    # progress ^ FLAGS[name]
def is_complete(progress) -> bool: ...    # progress & ALL_FLAGS == ALL_FLAGS
def summary(progress) -> list[str]: ...   # 依 FLAGS 定義順序回傳已點的旗標名稱
```

`name` 不在 `FLAGS` 內一律 `raise ValueError`——不猜、不忽略，與
`todo_store.set_status` 對未知 status 的既有處理方式一致。

## 5. CLI 介面

```bash
T=~/.claude/skills/todo-audit/scripts/todo_cli.py

python3 $T flag <ref> set <name>      # 例：flag T-042 set reviewed
python3 $T flag <ref> clear <name>
python3 $T flag <ref> toggle <name>

python3 $T edit <ref> --spec "docs/specs/2026-08-22-xxx-design.md"
python3 $T edit <ref> --memory "memory/xxx.md"
```

`flag` 指令內部呼叫 `todo_store.set_progress(con, key, op, name)`：讀出目前
`progress`、透過 `todo_flags` 算出新值、寫回、若觸發完成則連動改 `status`
（見第 6 節），全程一個 transaction。

`--spec`／`--memory` 只存字串，**不驗證路徑存在**（寫入當下文件可能還沒建
好，例如先登記路徑、稍後才補檔）——存在性檢查交給 `doctor`（第 7 節）。

## 6. 與既有 status 的整合規則

- `progress` 與 `status` 是兩個獨立欄位，語意上正交：`status` 答「誰在做、
  要不要做」，`progress` 答「做到哪個階段」。
- **單向自動觸發**：`set_progress` 寫入後，若 `todo_flags.is_complete(new_progress)`
  為真，且目前 `status` 不是 `unpick`／`done`，則**直接** `UPDATE todo SET
  status='done', status_at=?`，**保留原本的 `status_by` 不變**、**不經過
  `set_status()` 的 `ClaimConflict` 擁有者比對**。
  - 理由：若條目當下是 A 認領的 `doing`，而補滿最後一個旗標的操作來自另一
    個呼叫端（例如 CI 幫忙點 `deployed`），這不是「B 要搶 A 的認領」，而是
    A 的工作自然做完了——用 `set_status` 的擁有者檢查去擋這個轉態，會讓
    自動完成在多 session 情境下無故失敗。`status_by` 保持原值，代表「這條
    是誰做完的」這個歷史事實不因誰按下最後一個旗標而改寫。
  - `unpick` 的條目即使七個旗標都點滿也不自動轉態——`unpick` 是「決定不做」
    的終局狀態，語意上不該被進度覆蓋。
  - 若目前已經是 `done`，觸發是 no-op（避免重複寫入 `status_at` 洗掉原本
    的完成時間）。
- **反方向不連動**：手動 `mark <ref> done` 完全比照現有邏輯執行，**不**強制
  把 `progress` 補滿。理由：不是每條 todo 都走得完整個七階段 pipeline（例如
  純決策、純文件類條目沒有「編譯」「部署」的概念），硬灌假進度會誤導顯示。
  這代表「`status='done'` 但 `progress` 未滿」是合法且會出現的狀態，顯示層
  要能處理這種情況（見第 8 節），不能假設兩者永遠同步。

## 7. doctor 整合

`todo_audit.py` 的 doctor 檢查新增一項：對每筆 `spec_path`／`memory_ref` 非
空的條目，用 `Path(ref).exists()`（相對於 repo root 解析）確認檔案存在；
不存在則在 doctor stdout 印一行 WARN，格式比照近期 config 值型別驗證的 WARN
輸出（`a5ebf58`／`ec2beeb` 那個模式）——**印在 stdout，不能只丟 stderr**，
避免重蹈「config 被拒但 doctor 印乾淨 OK」的静默回報問題。

## 8. 顯示格式

`list`/`show`/`dump` 在既有 state 標籤之後，加一行進度視覺化：

```
T-042  [P0]  修正 XXX 的 race condition
  進度：✅實作 ✅review ⬜commit ⬜compile ⬜test ⬜live ⬜deploy
  spec: docs/specs/2026-08-22-xxx-design.md
  memory: memory/xxx.md
```

- 旗標順序固定依 `FLAGS` 定義順序（實作→review→commit→compile→test→
  live→deploy），不因點擊順序改變顯示順序——旗標本身不記順序，只記有無。
- `spec`/`memory` 兩行只在對應欄位非空時顯示，避免每條都印空行洗版。
- `status='done'` 但 `progress` 未滿的條目，進度行照常顯示部分勾選——不
  特殊隱藏、不偽造成全勾，維持「顯示真實資料」的既有原則。

## 9. 遷移策略

一次性 backfill（隨下一次 `connect()` 的 migration 執行，冪等）：

```sql
UPDATE todo SET progress = 127 WHERE status = 'done' AND progress IS NULL;
UPDATE todo SET progress = 0   WHERE progress IS NULL;
```

理由：既有 `done` 條目在此功能上線前就已完成，補滿七旗標維持「`done` ⟹
進度已知」的合理預設；非 `done` 的舊條目一律視為進度未知 = 0，不嘗試從
`status_note` 自由文字反推局部進度（太脆弱、容易猜錯，違反「不猜、不預設」
的既有原則）。

## 10. 測試計畫

- `todo_flags.py`：純函式單元測試——`has`/`set_`/`clear`/`toggle`/`is_complete`/
  `summary` 的正常路徑、未知 `name` 的 `ValueError`、`ALL_FLAGS` 邊界值。
- `todo_store.py`：`set_progress` 的自動轉 `done` 觸發（含 `unpick` 不觸發、
  已 `done` 時 no-op 兩個邊界）；`bind_project`／migration 冪等性（重跑
  `connect()` 兩次，`progress` 欄位不重複、不炸）。
- `todo_cli.py`：`flag` 子指令三個動作的整合測試；`edit --spec`/`--memory`
  的寫入與讀出往返。
- `todo_audit.py` doctor：`spec_path`/`memory_ref` 缺檔 WARN 的整合測試，
  比照既有 config 型別驗證 WARN 的測試寫法（`test_doctor.py`）。
- 舊資料遷移：fixture 建一個舊 schema（無 `progress` 欄位）的 DB，跑
  `connect()` 後驗證 `done` 條目 `progress=127`、其餘 `progress=0`。

## 11. 非目標／向後相容

- 不改動 `status` 欄位的既有寫入路徑、`ClaimConflict`、`write_mirror` 篩選
  條件（`WHERE status IN ('pending','doing')`）——這些邏輯完全不知道
  `progress` 欄位存在，也不需要知道。
- `hooks/todo-add.sh`、`hooks/todo-done.sh` 介面不變、行為不變。
- 舊版（未升級）的呼叫端讀到新 schema 不會出錯——`progress`/`spec_path`/
  `memory_ref` 都是新增欄位，既有 `SELECT` 語句除非明確加欄位否則不受影響。
