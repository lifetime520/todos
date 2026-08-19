# todos — Claude Code 跨 session 待辦系統

給 Claude Code 用的待辦事項系統：**每專案一份 sqlite，跨 session 持久化，讀取路徑強制帶上「這筆資料有多新」的標籤。**

解決的是一個具體問題 —— agent 在 session A 記下的待辦，到 session B 時已經過期，但它讀起來完整、格式漂亮，於是不會被質疑。

> **2026-08-08 實證**：188 條待辦中 55 條的錨點已失效、52 條從未複驗，而照抄出去的清單裡就混著一條標的檔案已被刪除的條目。

## 設計理念

**1. 過期資料要自己帶著標籤出現**

條目裡的行號、數字、檔案路徑都是「寫下當時」的快照。系統不假設它們還成立，而是每次讀取都比對現行 codebase，逐條標上 `ALIVE` / `TOUCHED` / `PARTIAL_GONE`，並在輸出頂端寫明距上次稽核多久。

**2. 稽核器是分流器，不是判官**

`todo-audit` 抽出條目裡的機器錨點（`file:line`、符號、SQL 表、config key、commit），比對 codebase 與 git pickaxe，輸出**候選＋證據**。

它答不了「描述與現況是否相反」—— 那只有讀了程式碼才知道。實測對 9 條人工確認已過期的條目只召回 3 條（33%），所以：

> **任何情況下都不得依它的輸出自動刪除條目。** 誤判成「已過期」是靜默資料遺失 —— 沒有錯誤訊息，沒人會發現。

**3. 直接讀取一律擋下**

`todo-guard.sh` 是 PreToolUse hook，攔截所有工具參數中出現 `~/.claude/todos/` 的呼叫（`cat` / `grep` / `Read` / `sqlite3` 皆擋）。因為直接讀會繞過新鮮度標註，把快照當現況用。

官方入口 `todo_cli.py` 的 sqlite 連線發生在子行程內部，不經 hook，故天然放行。

**4. 多 session 防撞車**

`doing` 狀態強制 `--by <身分>`。條目一旦是他人的 `doing`，**任何** status 變更都被擋下（exit 7）—— 不只是重複認領。

因為 B 把 A 正在做的條目標成 `done`，比重複認領更糟：A 還在寫程式，條目已從清單消失，而 A 不會收到任何通知。

沒有 TTL 也沒有心跳 —— 判斷對方 session 死活是人的責任，工具只保證資訊齊全、且沒人會在無意識下覆蓋別人。

## 資料不在這個 repo

**本 repo 只版控「工具」，不版控「待辦內容」。**

待辦資料在 `~/.claude/todos/.audit/{project}.sqlite`，`{project}` = git repo root 的 `basename`（非 git repo 不啟動本系統）。

`.gitignore` 嚴格排除所有 `*.sqlite` / `*.view.md` / 遷移快照 —— 待辦內文含各專案的檔案路徑、架構細節與未修漏洞，不該出現在公開 repo。

## 組成

```
hooks/
  todo-startup.sh    SessionStart —— 注入待辦「摘要與新鮮度」，不注入清單全文
  todo-reminder.sh   Stop —— session 結束時提醒仍掛在自己名下的項目
  todo-guard.sh      PreToolUse —— 擋掉對 ~/.claude/todos/ 的一切直接存取
  todo-add.sh        新增條目（薄殼，轉呼 todo_store.py）
  todo-done.sh       標記完成（薄殼；標 done 而非刪除，完成記錄保留）

skills/todo-audit/
  SKILL.md           skill 定義與使用說明
  scripts/
    todo_cli.py      唯一官方讀寫入口，輸出自帶新鮮度與逐條 state
    todo_store.py    sqlite 儲存層、無損解析、短 ID 配發
    todo_audit.py    錨點抽取與取證比對
    migrate_md_to_db.py   舊版 markdown → sqlite 一次性遷移
  tests/             104 個 unittest

docs/
  TODO-SYSTEM.md     完整操作手冊：格式規範、三態語意、指令速查
```

## 安裝

Claude Code 只從 `~/.claude/hooks/` 與 `~/.claude/skills/` 載入，所以用 symlink 指回這個 repo —— 改完立即生效，不會有兩份不同步。

```bash
REPO="$(pwd)"   # 在本 repo root 執行

# 先備份既有檔案（若有）
for f in todo-add.sh todo-done.sh todo-guard.sh todo-reminder.sh todo-startup.sh; do
  [ -e ~/.claude/hooks/$f ] && [ ! -L ~/.claude/hooks/$f ] \
    && mv ~/.claude/hooks/$f ~/.claude/hooks/$f.pre-symlink
  ln -sfn "$REPO/hooks/$f" ~/.claude/hooks/$f
done

[ -e ~/.claude/skills/todo-audit ] && [ ! -L ~/.claude/skills/todo-audit ] \
  && mv ~/.claude/skills/todo-audit ~/.claude/skills/todo-audit.pre-symlink
ln -sfn "$REPO/skills/todo-audit" ~/.claude/skills/todo-audit

mkdir -p ~/.claude/docs
ln -sfn "$REPO/docs/TODO-SYSTEM.md" ~/.claude/docs/TODO-SYSTEM.md
```

接著在 `~/.claude/settings.json` 註冊三個 hook：

| 事件 | 指令 |
|---|---|
| `SessionStart` | `bash ~/.claude/hooks/todo-startup.sh` |
| `Stop` | `bash ~/.claude/hooks/todo-reminder.sh` |
| `PreToolUse` | `bash ~/.claude/hooks/todo-guard.sh` |

## 使用

```bash
T=~/.claude/skills/todo-audit/scripts/todo_cli.py

# 新專案第一次使用（唯一會建庫的指令）
python3 $T init

# 讀（輸出自帶新鮮度與逐條 state）
python3 $T list [--section urgent|decision|normal|later] [--doing] [--by 誰]
python3 $T search <關鍵字> [--all]
python3 $T show <T-NNN|關鍵字> [--seq]
python3 $T similar <T-NNN>
python3 $T dump [--all] [--format md|json]

# 寫
bash ~/.claude/hooks/todo-add.sh "標題" "🏷️  keyword" "💡  上次做到哪：X，下一步：Y"
python3 $T note <T-NNN> "🔍  複驗結果…"
python3 $T edit <T-NNN> --title "新標題"

# 狀態
python3 $T mark <T-NNN> doing --by <誰>
python3 $T mark <T-NNN> pending --by <誰>
python3 $T audit
```

**為什麼建庫要用獨立指令**：讓任何讀寫指令自動建庫，會在專案名打錯時靜默生出一個空庫，
把「打錯字」偽裝成「這個專案還沒有待辦」—— 這兩者的正確反應相反，而錯的那個不會有任何錯誤訊息。
所以 `init` 之外的指令找不到 DB 一律 exit 2 並指路，不自作主張。

完整指令與格式規範見 [`docs/TODO-SYSTEM.md`](docs/TODO-SYSTEM.md)。

## 測試

```bash
cd skills/todo-audit && python3 -m unittest discover -s tests -v
```

不需要 pytest，標準庫 unittest 即可。

## 環境需求

Python 3（標準庫，無外部依賴）、bash、git。開發與驗證環境為 macOS + Python 3.12。
