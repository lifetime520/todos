# 待辦事項系統操作手冊

> 核心規則在 `~/.claude/CLAUDE.md`。本檔是完整操作細節。

## 儲存層（2026-08-08 遷移）

| 角色 | 位置 | 誰能碰 |
|---|---|---|
| **真相來源** | `~/.claude/todos/.audit/{project}.sqlite` | 只有 `todo_cli.py` |
| 唯讀鏡像 | `~/.claude/todos/{project}.view.md` | 人的眼睛；Claude 被 hook 擋 |
| 閘門 | `~/.claude/hooks/todo-guard.sh`（PreToolUse） | 擋整個 `~/.claude/todos/` |

**為什麼要擋**：條目裡的行號與數字都是稽核當下的快照。直接 `grep`／`Read` 拿到的內容看起來完整、格式漂亮，於是不會被質疑 —— 2026-08-08 實證：188 條中 55 條已標紅、52 條從未複驗，而照抄出去的「最新 11 條」裡就混著一條 `PARTIAL_GONE`。
官方入口的每個輸出都強制附上新鮮度與逐條 state，讓過期資料自己帶著標籤出現。

hook 的判定是「路徑出現在工具參數裡」，不是「行程讀了那個檔」——
`todo_cli.py` 子行程內部的 `sqlite3.connect()` 不經 hook，所以官方入口天然放行，不需要額外權限機制。

## `{project}` 認定規則（Cast 2026-05-24 拍板，遷移後不變）

1. PWD 必須有 `.git` 目錄（必須在 git repo root）→ 否則 todo 系統不啟動
2. `{project}` = `basename $(pwd)`（linked worktree 會反解回主 worktree）
3. 專案綁定記在 DB 的 `doc_meta`（`project_path` / `git_remote`）—— 這是舊 md 首行 marker `<!-- project_path: ... -->` 的 DB 版
4. 若同名專案在不同路徑（例如 `/A/tradingbot` vs `/B/tradingbot`）—— 當第二個專案要寫入時，**自動偵測綁定衝突並阻擋寫入**（`todo_store.bind_project`），提示用戶改名其中一個專案。

## 決策點落地規則

**任何決定「之後再做」的事情，必須在當下用 bash 寫入待辦，不可只停留在對話中。**

觸發條件（符合任一即寫入）：

- 提出了方向但沒有立刻實作
- 任務因為相依性暫時擱置
- 發現了問題但這個 session 來不及修
- 說了「下一步」但沒有馬上執行
- 架構決策需要用戶確認後才能繼續

## 完成判準（缺一不可）

- build 通過 + 測試綠燈 + 用戶確認
- **若該條標註了「runtime 症狀層驗收」等後置驗收 → commit 只是「程式落地」，尚未完成，不得移除。`commit ≠ done`。**

不要留著已完成的項目，保持列表只有真正 pending 的事情。

## 交付進度與 status 的關係

`status`（pending/doing/done/unpick）與交付進度 `progress` 位元旗標是
兩個正交的維度：前者答「誰在做、要不要做」，後者答「做到哪個階段」。
七個位元互不強制順序，各自獨立可點。**進度全滿時單向自動轉
`status=done`**（`unpick` 的條目不受此影響）；反過來手動 `mark done`
不會強制把進度一併點滿——不是每條 todo 都走得完整個 pipeline。

`doctor` 會檢查每條 `--spec`/`--memory` 參照的檔案是否存在，壞掉的
參照印 WARN，避免靜默過期。詳細設計見
`docs/specs/2026-08-22-todo-progress-bitmask-design.md`。

`list` 是掃視用的清單，不印進度與 spec/memory ——要看某條的細節請用
`show <T-NNN>` 或 `dump`。

## 依賴圖與變更軌跡

條目間可以記錄有向依賴關係：`blocks`（阻塞執行順序）、`related`（純資訊性關聯）、
`parent-child`（階層）、`discovered-from`（做這條時發現了那條，記錄來源）。只有
`blocks`/`parent-child` 在寫入時做環狀依賴檢查（兩者**分開**跑，不合併成同一張
圖——父任務被自己子任務的 `blocks` 邊卡住是合法情境），偵測到會拒絕並附上具體
的環路徑（`T-NNN` 形式，不是「有環」三個字）。

`blocked` **不是** `status` 的第五個值——它是依賴圖算出的動態視圖，用
`list --ready` 查「pending 且未被 `blocks` 邊卡住」的條目。`mark done`/`unpick`
時若有下游因此變 ready，會印出提示（例如「因此變為可動手（無阻塞）：T-051」），
但**不會自動改動它們的 status**——動不動手仍由人決定。

`show <T-NNN>` 會列出該條目完整的變更軌跡（`status` 轉換與依賴增刪，append-only、
新到舊排序，不會被覆寫）。被 `ClaimConflict` 擋下的轉態嘗試不寫入軌跡——只記真正
發生的變更。`doctor` 會檢查依賴圖的懸空邊（指向不存在的條目）與環狀依賴，只
`WARN` 不自動修——稽核維持「只給證據，人決定」的既有原則，詳細設計見
`docs/specs/2026-08-22-todo-dependency-graph-design.md`。

## 稽核：`todo-audit` skill

待辦會過期，而且**過期的 P0 比沒有 P0 更危險** —— 它持續消耗注意力，還讓真正活著的 P0 顯得不緊急。（實證：`Gate 3 daily-loss 是死碼` 這條 P0 在被修好三天後仍掛在清單上。）

### 何時跑

| 時機 | 為什麼 |
|---|---|
| **動工前**（要處理某條 todo） | 條目內的數字與行號都是快照。先確認它還成立，別做一件別的 session 已經做掉的事 |
| **移除任何條目前** | **強制**。必須有錨點／commit 證據，禁止憑印象刪 |
| **定期整理**（距上次 >7 天，或條目 >100） | 防止再次累積到讀不動（曾累積到 198 條，其中 93 條堆在未分組區） |

```bash
python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py audit
# 等價於直接呼叫（吃 .sqlite，不再吃 .md）：
python3 ~/.claude/skills/todo-audit/scripts/todo_audit.py \
    ~/.claude/todos/.audit/$(basename $(pwd)).sqlite .
```

DB 在 `~/.claude/todos/.audit/{project}.sqlite` —— 按專案隔離，且**不在 git 樹內**（放進 repo 會被 `git clean -fdx` 清掉）。增量已支援：條目沒改、程式碼沒動就跳過。

`todo_audit.py` 仍接受 `.md` 路徑（走 legacy `parse_todos`），這條路徑保留給遷移驗證與回歸比對用 —— 兩條輸入路徑的分流結果必須完全一致，那是「換輸入層而語義不變」的證據。

### 完成與擱置

`done` 與 `unpick` 的條目**不進稽核**（稽核的對象是待辦，不是完成記錄），也不出現在鏡像裡，但留在 DB 中可用 `dump --all` 查。

- `done` —— 完成。舊版 `todo-done.sh` 是直接刪行，完成記錄全丟；現在留存。
- `unpick` —— 看過了，這次不撿。**`--note` 必填**：無理由的擱置與遺忘沒有區別，三個月後你不會知道當初為什麼跳過。它解的是「標紅條目每次稽核重複出現」——2026-08-08 有 52 條標紅條目從未被複驗過，標了 unpick 才不會反覆吃掉注意力。
- `doing` —— 認領中，記 `status_by`。shared master 多 session 併發下，這是唯一能防兩個 session 撿同一條的機制。
  **條目一旦是他人的 `doing`，任何 status 變更都被擋下（exit 7）**——把別人正在做的條目標成 `done` 比重複認領更糟：
  對方還在寫程式，條目已從清單消失且不會收到通知。讀取路徑一律顯示 `(doing by 誰 · 多久前)`；
  **沒有 TTL 也沒有心跳**，判斷對方 session 死活是人的責任，工具只保證資訊齊全、不會有人無意識覆蓋別人。

`rm --force` 是**抹除**，與上述三者不同：它連記錄一起刪掉，用於誤建的條目與測試垃圾。
真的做完了請用 `done`，別用 `rm`。

### 複驗結果要寫回去

查證完一條待辦，用 `note` 把結論追加成 `🔍` 行。**這不是可選的**——
複驗成果沒留痕，下個 session 只能從頭再查一次。實測 192 條裡只有 5 條有 `🔍` 記錄，
而機器標紅、從未經人複驗的有 52 條。`freshness()` 的 `unreviewed` 計數就是數這個：
它只認 `todo_line.marker = '🔍'`。

### 三態語意

| 狀態 | 含義 |
|---|---|
| `ALL_GONE` | 所有錨點都消失 —— 高信心已落地或已重構 |
| `PARTIAL_GONE` | 部分錨點消失 —— 可能是重構搬家，也可能已完成 |
| `TOUCHED` | 錨點在，但**稀有符號**在條目日期後被 commit 動過 —— 最可能已過期 |
| `ALIVE` | 錨點完好且無人動過 —— 大機率仍成立 |
| `NO_ANCHOR` | 抽不到錨點 —— 不可自動複驗 |

另有「疑似已被 commit 做掉」清單（todo 標題 ↔ commit message 的字元 3-gram 比對），直接指出是哪個 commit 做掉的。

### 三條使用紀律

1. **輸出是候選＋證據，不是結論。** 實測對 9 條已知過期只召回 3 條（33%）—— pickaxe 結構上抓不到「新增檔案交付」造成的過期。**不得依它自動刪除條目。**
2. **零命中優先懷疑工具，不是懷疑資料。** 實測 `rg` 在本機是 shell function，subprocess 拿到 `FileNotFoundError` 被靜默吞掉 → 136 個符號全零命中 → **75 條真待辦被標成「載體全消失、可移除」**。工具已改為命中率 <30% 直接中止，但這個思維習慣要保留。
3. **高共現 ≠ 重複。** 偵測到高相似只提示、列出既有條目，**不自動合併或忽略**。實測錨點共現滿分的兩條（`preview 超時` / `dry-run 超時`）其實是同一件事的兩半，兩個都得做；自動合併會吃掉一條。

## 待辦格式規範

每個項目必須包含三層資訊，以確保跨 session 的回憶品質：

```
# {Project} Pending

## 🔴 緊急
- [ ] [YYYY-MM-DD] 任務標題（一行，動詞開頭）
  > 🏷️  sprint1, okhttp5, auth          ← 關鍵詞（用於快速分類與搜索）
  > 💡  上次做到哪：已完成 X，卡在 Y，下一步是 Z   ← 回憶提示詞

## 🟠 待拍板決策
## 🟡 一般
## ⚪ 之後再看
```

- **關鍵詞原則**：用 2-5 個技術術語或模組名稱，讓 grep 搜索有意義。
- **提示詞原則**：「上次做到哪 + 卡點 + 下一步」三段式，不超過一行 80 字。

### 章節符號與 section 的對應

標頭裡的顏色 emoji 是 `_section_of()`（`todo_store.py`）唯一的分類依據，
**四個 section 一一對應四個符號**：

| 標頭符號 | section | `list --section` 用的值 |
|---|---|---|
| 🔴 | `urgent` | `urgent` |
| 🟠 | `decision` | `decision` |
| 🟡 | `normal` | `normal` |
| ⚪ | `later` | `later` |

⚠️ **標頭符號後面的文字可以自由命名**（`## 🔴 立即處理（P0 / 資金安全）`
與 `## 🔴 緊急` 都會判成 `urgent`）—— 判定只看符號，不看文字。上面的示範
用的是 `edit --section` 搬章節時會寫入的 canonical 措辭。

⚠️ **`🟢` 也會塌成 `later`**，那是歷史寫法的相容處理（早期文件用 🟢 表示
「未來規劃」）。新寫的 md 請用 `⚪`；`🟢` 仍讀得進來，但 `edit --section`
不會產出它。

> 這張表在 2026-09-02 之前是錯的：本節示範原本寫 `🔴 決策待定` /
> `🟡 待辦任務` / `🟢 未來規劃`，既漏了 🟠 與 ⚪，又把 🔴 說成「決策待定」
> —— 而程式碼裡 🔴 是 `urgent`、`🟠` 才是 `decision`。照著舊示範寫出來的
> md，`_section_of()` 會判成與作者意圖不同的 section。

## 管理指令速查

```bash
T=~/.claude/skills/todo-audit/scripts/todo_cli.py

# 新專案第一次使用 —— 唯一會建庫的指令
# （其餘指令找不到 DB 一律 exit 2 並指路，不自動建庫：
#   打錯專案名時靜默建空庫，會把「打錯字」偽裝成「沒有待辦」）
python3 $T init

# 查看待辦（必須在 git repo root。輸出自帶新鮮度與逐條 state）
python3 $T list [--section urgent|decision|normal|later] [--doing]
python3 $T search <關鍵字> [--all]      # 全文搜尋（標題＋內文，大小寫不敏感）
python3 $T show <T-NNN|關鍵字> [--seq]  # 多筆命中列候選，不自動選第一筆
python3 $T similar <T-NNN>              # 找相關條目（提示，不自動合併）
python3 $T dump [--all] [--format json] # 全量輸出，每條自帶 state 標籤

# 修改條目內容
python3 $T note <T-NNN> "🔍  2026-08-09 複驗：…"   # 追加一行
python3 $T edit <T-NNN> --title "新標題"           # 日期前綴自動保留
python3 $T edit <T-NNN> "🏷️  新內容" --line 1      # 改某行（序號見 show --seq）
python3 $T rm   <T-NNN> --line 2                   # 刪某行
python3 $T rm   <T-NNN> --force                    # 永久刪除整條

# 交付進度位元旗標（實作/review/commit/compile/test/live_tested/deploy）
python3 $T flag <T-NNN> set <name>      # 例：flag T-042 set reviewed
python3 $T flag <T-NNN> clear <name>
python3 $T flag <T-NNN> toggle <name>
# name ∈ implemented / reviewed / committed / compiled / tested / live_tested / deployed
# 七個旗標全數點滿時，status 會自動轉為 done（unpick 條目不受影響）

# 規格文件 / session memory 參照（只存路徑字串，不驗證存在；doctor 會檢查）
# spec_path 與 memory_ref 皆相對 repo root 解析，絕對路徑原樣使用
python3 $T edit <T-NNN> --spec "docs/specs/2026-08-22-xxx-design.md"
python3 $T edit <T-NNN> --memory "memory/xxx.md"

# 人工搬章節（優先序裁示，非稽核判定）。合法值 urgent/decision/normal/later，
# 對應 heading 符號 🔴/🟠/🟡/⚪。list --section 讀的是 DB 的 section 欄位，
# 而該欄位由 md 章節標頭決定、heading 優先於標題裡的 [P0]/[P2] 之類關鍵字 ——
# 過去只能靠 --title 塞這種標記其實從未生效，這條指令才是真正能搬動優先序的方式。
python3 $T edit <T-NNN> --section urgent

# 依賴關係（blocks / related / parent-child / discovered-from）
python3 $T dep add <from> blocks <to>        # from 阻塞 to；環狀依賴會被拒絕並附具體環路徑
python3 $T dep add <from> related <to>
python3 $T dep add <from> parent-child <to>
python3 $T dep add <from> discovered-from <to>
python3 $T dep rm  <from> blocks <to>
python3 $T dep list <T-NNN>                  # 印該條目的所有上下游依賴關係

python3 $T list --ready                      # 只列 pending 且未被 blocks 邊卡住的條目

# 新增（自動 git check + 專案綁定衝突偵測 + 相似度提示）
bash ~/.claude/hooks/todo-add.sh "任務" "🏷️  tag" "💡  recall"

# 狀態流轉
bash ~/.claude/hooks/todo-done.sh "標題行的一段關鍵字"   # 等同 mark ... done
python3 $T mark <T-NNN> doing --by <session識別碼>
python3 $T mark <T-NNN> unpick --note "理由（必填）"

# 稽核
python3 $T audit
python3 $T stats

# 列出所有專案的待辦數
python3 -c "
import sqlite3, pathlib
for db in sorted(pathlib.Path.home().glob('.claude/todos/.audit/*.sqlite')):
    n = sqlite3.connect(db).execute(
        \"SELECT COUNT(*) FROM todo WHERE sort_order IS NOT NULL\"
        \" AND status IN ('pending','doing')\").fetchone()[0]
    print(f'{db.stem}: {n} 項')
"
```

## 三個禁用寫法（都踩過）

| 禁用 | 為什麼 | 改用 |
|---|---|---|
| `cat` / `grep` / `Read` / `sqlite3` 打 `~/.claude/todos/` | 繞過稽核新鮮度標註，把過期快照當現況用。2026-08-08 實證：188 條中 55 條已標紅、52 條從未複驗，照抄出去的清單裡就混著一條 `PARTIAL_GONE`。**已由 `todo-guard.sh`（PreToolUse）硬擋** | `todo_cli.py list` / `show` / `dump` |
| `echo >> ~/.claude/todos/...` | 同名 project 不同路徑時會誤寫到別人的待辦；且真相來源已是 sqlite，寫 md 不會被任何人讀到 | `todo-add.sh`（git 驗證 + 綁定衝突偵測） |
| `sed -i '' '/關鍵字/d'` | todo 是多行區塊（`- [ ]` 標題 + `> 🏷️` + `> 💡` + `> ⚠️`），raw sed 只刪標題行，留下續行成為**孤兒**；且會刪掉完成記錄 | `todo-done.sh`（改標 `done`，歷史留存；命中 >1 條時拒動避免誤標） |
