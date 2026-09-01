---
name: todo-audit
description: Use when auditing a project todo list for stale entries, detecting duplicate/related todos, deciding which todos to merge or remove, or when any todo needs to be read, listed, added, or marked. The todo store is `~/.claude/todos/.audit/{project}.sqlite` and direct reads of `~/.claude/todos/` are blocked by a PreToolUse hook — all access goes through `scripts/todo_cli.py`, whose output carries audit freshness and a per-item state tag. Extracts machine-verifiable anchors (file:line, symbols, SQL tables, config keys, commits), cross-checks them against the live codebase and git history, and produces evidence-backed triage — it does not decide on its own. Keywords: 待辦稽核, 過期 todo, stale todo, todo 重複偵測, 列出待辦, anchor verification, pickaxe.
---

# todo-audit

為「每專案一檔 markdown」的待辦系統做**取證式稽核**：抽出條目裡的機器錨點，比對現況與 git 歷史，把上百條的人工複驗量壓到可處理的規模。

## 這是分流器，不是判官

它能回答的：錨點還在不在、行號漂了沒、條目寫下後這塊碼被誰動過。
它**答不了**：描述與現況是否相反 —— 那只有讀了程式碼才知道。

> **實測召回率：對 9 條人工確認已過期的條目，只標出 3 條（33%）。**
> 所以它的輸出是**優先級排序＋證據包**，不是篩選結果。全部條目仍需人（或 L1 agent）過目，只是先看哪些、帶著什麼證據看，由它決定。

**任何情況下都不得依它的輸出自動刪除條目。** 誤判成「已過期」是靜默資料遺失 —— 沒有錯誤訊息，沒人會發現。

## 使用

**2026-08-08 起真相來源是 sqlite，不是 md。** 日常一律走 `todo_cli.py`：

```bash
T=~/.claude/skills/todo-audit/scripts/todo_cli.py

# 建（新專案第一次使用，唯一會建庫的指令）
python3 $T init

# 讀
python3 $T list [--section urgent|decision|normal|later] [--doing] [--by 誰]
python3 $T search <關鍵字> [--all]        # 全文搜尋，大小寫不敏感
python3 $T show <T-NNN|關鍵字> [--seq]
python3 $T similar <T-NNN> [--top 10]
python3 $T dump [--all] [--format md|json] [--section ...]

# 寫
python3 $T note <T-NNN> "🔍  複驗結果…"          # 追加一行
python3 $T edit <T-NNN> --title "新標題"         # key 與稽核歷史自動搬移
python3 $T edit <T-NNN> "🏷️  新內容" --line 1
python3 $T rm   <T-NNN> [--line N | --force]
python3 $T mark <T-NNN> {pending|doing|done|unpick} [--note "..."] [--by 誰] [--force]
python3 $T flag <T-NNN> {set|clear|toggle} <name>    # name ∈ implemented/reviewed/committed/compiled/tested/live_tested/deployed
python3 $T edit <T-NNN> --spec "docs/specs/xxx.md" --memory ".claude/memory/xxx.md"
python3 $T edit <T-NNN> --section urgent            # 人工搬章節；合法值 urgent/decision/normal/later

python3 $T audit          # 轉呼下面的 todo_audit.py
```

### `edit --section`：為什麼需要這個指令

`list --section` 讀的是 DB 裡 `section` 欄位（`WHERE section=?`），而這個欄位是由 md 的
章節標頭（heading，如 `## 🔴 緊急`）決定，**不是**由標題裡塞的 `[P0]`/`[P2]` 之類關鍵字決定
——判定邏輯裡 heading 優先於標題關鍵字。過去只能靠 `--title` 塞這類標記，其實從未生效，
因為條目仍留在原本的 heading 底下。`edit --section` 直接改寫 heading（同步更新 `section`
欄位），是**唯一**能把一條待辦搬去別的優先序章節的方式；它是人工裁示優先序，不是稽核判定，
`todo-audit` 不會因為這個欄位自動改變任何條目的狀態。

### 認領守衛（多 session 防撞車）

`doing` **強制** `--by`。沒有身分的認領擋不住任何人 —— 舊版 `--by` 預設 `claude`，
全部 session 同名，等於沒有名字。

**條目一旦是他人的 `doing`，任何 status 變更都被擋下**（exit 7），不只是重複認領。
守衛範圍刻意放寬到 `done` / `unpick` / `pending`：B 把 A 正在做的條目標成 done
比重複認領更糟 —— A 還在寫程式，條目已從清單消失，而 A 不會收到任何通知。
唯一例外：`flag` 補滿七個交付進度旗標時會自動把 status 轉為 done，這不經過這道守衛
（是工作自然做完，不是搶認領），`status_by` 會保留原認領者。

```bash
python3 $T mark T-003 doing --by session-alpha    # 認領
python3 $T list --doing --by session-alpha        # session 結束前查自己名下
python3 $T mark T-003 pending --by session-alpha  # 釋放（status_by 會被清空）
python3 $T mark T-003 doing --by session-beta --force   # 接管，先確認對方真的結束了
```

讀取路徑一律顯示 `(doing by 誰 · 多久前)` —— 沒有「多久前」就分不出這是活著的
session 還是三天前的殘留。**沒有 TTL 也沒有心跳**：判斷死活是人的責任，
工具只保證資訊齊全、且不會有人在無意識下覆蓋別人。

⚠️ `edit --title` 會改變 `todo_key`（它是 `sha1(date|title)`）。
`todo_line` / `anchor` / `probe` / `verdict` 會一起搬過去，稽核歷史不會斷 ——
但這是 `edit_title()` 特地處理的，**不要繞過它直接 UPDATE title**。

直接呼叫稽核器（吃 `.sqlite`；仍接受 `.md` 走 legacy 解析，供遷移驗證比對）：

```bash
python3 ~/.claude/skills/todo-audit/scripts/todo_audit.py \
    ~/.claude/todos/.audit/$(basename $(git rev-parse --show-toplevel)).sqlite \
    . [--json out.jsonl] [--db PATH | --no-db]
```

DB 在 `~/.claude/todos/.audit/{project}.sqlite` —— 天然按專案隔離，**且不在 git 樹內**（放進 repo 會被 `git clean -fdx` 清掉，這個坑已經有人踩過）。

⚠️ `~/.claude/todos/` 整個目錄由 `todo-guard.sh`（PreToolUse hook）擋住直接存取。
`cat`／`grep`／`Read`／`sqlite3` 打那個路徑一律 deny —— 直接讀會繞過新鮮度標註，
把過期快照當現況用。官方入口的輸出一律自帶「上次稽核距今多久」與每條的 state。

## 快速查詢 API

除 `--groups`（分層聚類，約 1.2s）外都**不跑 git、不掃 codebase**，只讀 todo 檔，各約 0.12s。

| 指令 | 回答什麼 |
|---|---|
| `--stats` | 總條目數、可自動複驗比例、等人拍板數、依優先級與月份分佈 |
| `--groups [max_df]` | 有幾群、每群的共同錨點與成員（預設 `max_df=8`） |
| `--batch` | 依產出日期分批，量測每批內聚度（同一次 task 的產物） |
| `--dims` | 目前啟用的相似度演算法、向量檢索狀態、可用 embedding 模型與維數 |
| `--similar "標題"` | 新條目 vs 既有條目的相似提示（`todo-add.sh` 已自動呼叫） |

**`--batch` 補的是 `--groups` 的盲區。** 工作流程有真實的批次結構：一個 task 在 D 日執行 → 產出一批 todo → commit 落在 D+1/D+2。實測同日產出的條目彼此錨點相似度是相隔 >7 天者的 **2.53 倍**（高相似占比 3.91 倍），且隨日期距離**單調遞減**。

它抓得到 `--groups` 抓不到的東西：用詞不同、錨點只部分重疊的重複條目。實例 —— `2026-08-06` 那批內聚度 11.2×，直接框出「Quartz trigger 相位未對齊」與「[P2] 策略排程 trigger 相位不對齊」這組真重複；純錨點共現抓不牢它們。無錨點的條目完全落在 `--groups` 之外，但它們仍屬於某個批次。

⚠️ 這是**強化訊號但弱主導**：4 倍是相對值，絕對值仍低（2.8% vs 0.7%）。當次要權重或 tie-breaker 用，別拿它當分群主依據。

**`--groups` 用分層階層聚類**（L1 錨點 @0.10 → L2 文字收殘餘 @0.12），實測 **41 群 / 最大 12 條(6%) / 覆蓋 92% / 輪廓 0.713**，約 1.2s。

這是試過三種做法後的結論，兩個極端都不行：

| 做法 | 結果 |
|---|---|
| 連通分量（傳遞閉包） | 巨型分量 **65 條** —— A–B 相關、B–C 相關但 A–C 無關時，橋接節點把不相干的串成一坨。有共用基礎設施的 codebase 幾乎必然發生 |
| 共同錨點（完全不傳遞） | 另一個極端：78 群太碎，覆蓋僅 56% |
| **average-linkage 階層聚類** | 在兩者之間最佳化「群內密、群間疏」，群數由資料決定不需指定 k |

**⚠️ 日期訊號刻意不進相似度矩陣。** 日期相似度是**稠密**的（任兩條都非零），等於加一層全連接弱邊，average linkage 會把它們黏成 93 條的巨型群，輪廓從 0.706 崩到 0.241。日期是**強化訊號但不是結構訊號** —— 當排序 tie-breaker 可以（見 `--batch`），當分群依據不行。

群標籤用**群內最高頻錨點**（`AI_AGENT(7/12)` = 這群 12 條裡 7 條碰它），不用交集 —— average linkage 不要求全員共用同一錨點，取交集會常常落空。

**`--dims` 會誠實回報「向量未啟用」。** 實測 `bge-small-zh` 對 todo↔commit 配對 top-3 僅 20%，輸給零依賴字元 3-gram 的 60% —— 本語料的主訊號是**英文識別字逐字匹配**（`historicalKlines` 在 todo 與 commit 裡一字不差），純中文模型會把識別字切成無意義 subword。要重試請換中英雙語模型（`jina-embeddings-v2-base-zh`，dim=768）；venv 已移除（曾佔 261MB / 8035 檔在 skill 目錄內，而 skill 目錄是被掃描的路徑），需先 `python3 -m venv` + `pip install fastembed`。

## 三態的意義

| 狀態 | 含義 | 怎麼處理 |
|---|---|---|
| `ALL_GONE` | 所有錨點都消失 | 高信心已落地或已重構，優先複審 |
| `PARTIAL_GONE` | 部分錨點消失 | 需人看：可能是重構搬家，也可能已完成 |
| `TOUCHED` | 錨點在，但**稀有符號**在條目日期後被 commit 動過 | 最可能已過期，附 commit 清單 |
| `ALIVE` | 錨點完好且無人動過 | 大機率仍成立 |
| `NO_ANCHOR` | 抽不到錨點 | 不可自動複驗，純人工 |

## 血淚教訓（都是實測撞出來的，別重蹈）

**1. 零命中優先懷疑工具，不是懷疑資料。**
`rg` 在部分環境是 Claude Code 注入的 **shell function** 而非可執行檔，`subprocess` 會拿到 `FileNotFoundError`。初版把它 `except` 吞掉 → 136 個符號全零命中 → **75 條真待辦被標成「載體全消失、可移除」**。
→ 本工具改為純 Python 掃描（零外部依賴），並在命中率 < 30% 時**直接中止**而非回報空結果。
**但只在符號數 ≥ 4 時中止**：0.30 這個門檻在 n ≤ 3 時會退化成「命中數是不是 0」（1/3 = 33% 已高於門檻），而小樣本零命中經常是良性的——符號改了名，或住在 `scan_exts` 之外的檔案。樣本太小時改走 WEAK_AUDIT 降級：照常產出結果，但每一條的狀態都顯示為 `WEAK_AUDIT`，假陽性的 `ALL_GONE` 一樣不可能被當成「可移除」。**這道保護沒有被拿掉，只是換成不會癱瘓整份稽核的形式。**
實測兩起同日、獨立 repo 的誤中止，根因都是**掃描範圍**而非掃描層故障（`0/1` 與 `1/5`），所以 FATAL 訊息現在會先印出目前的 `search_dirs`／`scan_exts` 與該改哪個 config 檔。

**2. 檔案存在性與符號存在性需要不同掃描範圍。**
`build.gradle` 在 repo root、測試在 `src/test` —— 只掃 `src/main` 會把它們判成「檔案已消失」。但符號掃描**必須**只看生產碼，否則「只剩測試在用、生產零呼叫端」這種死碼特徵就驗不出來。

**2b. 「什麼字串算檔案錨點」也是專案相依的，而且不能全域放寬。**
`RE_FILE`／`RE_FILE_LINE` 的副檔名清單沒有 `md`，所以純文件 repo（skill／規格庫）裡
`SKILL.md:284` 這種引用抽不出任何錨點，稽核對它幾乎失效——實測 cast-power 的 3 條待辦全部落在 `NO_ANCHOR`。
但**全域加 `md` 不安全**：實測 tradingbot 的 233 條待辦會多出 **21 個查無此檔的錨點**
（`~/.claude/…` 底下的檔、以及 `analysis.md`／`bindings.md` 這類跑完就刪的 workspace 產物）。
關鍵在下面這條不對稱——檔案錨點**沒有**符號那條「從未存在於 git 歷史 → 不算 GONE 訊號」的過濾
（`build_checks()` 對 file 是 `OK if hits else GONE`），所以那 21 個會直接變成假 GONE。
→ 改成 per-repo 可設定的 `anchor_exts`（預設清單一字未改），需要的 repo 自己在
`.claude/todo-audit.json` 開。**opt-in 的理由不是保守，是量出來的**：同一個改動在 A repo 是修復、在 B repo 是 21 個假 GONE。

**3. 錨點存活 ≠ 命題成立。**
主流的過期型態是「**符號還在、狀態變了**」。例：todo 說 `recordPnl` 零呼叫端是死碼，而它現在已經被接上 —— 符號完好，命題已死。純錨點驗證對此完全無感（召回 1/9）。這是加入 git pickaxe 的原因。

**4. 掃描型工具必須有機敏檔排除清單。**
範圍由副檔名＋目錄定義，而機敏檔恰好落在裡面。初版 `SCAN_EXTS` 含 `.properties` → 讀進了 `application.properties`。「只是建索引」不構成讀取豁免。
→ `git log -p` 尤其危險：diff 會整段吐出檔案明文，必須用 **git pathspec 在來源端排除**，只在讀檔端過濾擋不住。

**5. 訊號強度與符號稀有度成正比。**
`setScale` / `OrderService` 這種散佈幾十處的通用符號，在任何時間窗內幾乎必然被動到 —— 把 58% 的條目染成紅旗。而 `recordPnl` 只出現在兩個檔案，它被動到就是強訊號。
→ 只採計 `df <= 8` 的稀有符號，TOUCHED 從 114 條降到 40 條。**粒度不對的訊號比沒有訊號更糟，因為它看起來像證據。**

**6. 純註解／文件 commit 是 pickaxe 的假訊號，必須在來源端濾掉。**
`refactor(test): 精簡測試註解` 這類 commit 會大量增刪**含符號名的註解行**，pickaxe 無法與「真的改邏輯」區分。實測它們佔 TOUCHED 引用 commit 的 **58%**，且有 11 條的證據**全部**來自這類 commit ——純假訊號。
→ 過濾 `^(docs|chore)[(:]` 與 `^refactor\(test\)` 後 TOUCHED 從 39 降到 29。形狀與教訓 5 相同（訊號源要過濾），差別是 5 過濾「哪些符號」，這條過濾「哪些 commit」。

**11. 「查無此符號」是兩種相反情況，混在一起會讓整個狀態失去意義。**
- **曾存在後消失** → 真被移除或重構，條目可能已完成（有訊號）
- **從未存在** → todo 作者自創的描述性稱呼，如 `shutdownAfterGateExit`、`TERMINAL_ORDER_STATUSES`（零訊號）

`PARTIAL_GONE` 把兩者都算作「錨點消失」，導致實測精確度**接近 0**（16 條逐條查證，0 條真可移除）。
→ 用 `git log -S --all` 判定「從未存在」並排除，`PARTIAL_GONE` 從 38 降到 24。同輪加了提議性語境過濾（「應改用 X」「建議抽出 Y」附近的識別字不當錨點），因為 todo 本來就常寫提議中的名字。
→ 這道查詢慢（40 個符號約 65s），但「從未存在」是**單調事實**（不會變回來），快取進 `symbol_history` 表後回到 22s。

**12. 有命中也要看清 context，不只零命中要懷疑。**
查「AI provider 守衛是否已掃 `scripts/*.sh`」時 grep 命中 `scripts`，我判已完成 —— 實際那是 `gradleBuildScripts()` 裡的 Java 區域變數 `List<Path> scripts`，收集的是 `.gradle` 檔，與 shell script 無關。**符號名撞名比想像中常見**，尤其 `scripts`/`config`/`client` 這類通用詞。

**13. 變異檢查本身會被 `__pycache__` 給出假綠 —— 取證工具自己需要取證。**
「把被測物改壞 → 確認測試變紅 → 改回來 → 確認變綠」是本 repo 驗證斷言可否證性的主力手段。但 CPython 判斷 `.pyc` 是否失效，看的是來源檔的 **mtime（秒級）＋ size**，而典型的變異恰好兩個都不變：`return 'urgent'` → `return 'normal'` 是**等長替換**（size 不變），且改壞、跑、改回往往在**同一秒**內完成（mtime 不變）。結果是還原後仍載入變異過的 bytecode，**「還原後全綠」這個結論是假的，且毫無徵兆**。
→ 跑變異檢查一律加 `PYTHONPYCACHEPREFIX=/tmp/pyc`，或刻意讓變異前後長度不同。

**⚠️ 它是間歇性的，這才是真正危險的地方。** 2026-09-02 受控重現：把兩次寫入對齊到同一整數秒（`int(st_mtime)` 相等）＋等長替換，還原後 `_section_of("[P0] x")` 仍回傳變異值 `'normal'`。但**同樣的操作在跨過秒邊界時完全正常** —— 第一次隨手試就沒重現。所以「我跑過一次沒事」不構成這個方法可靠的證據；你只是剛好沒踩到。
→ 更一般的教訓：**這一整份「血淚教訓」都建立在實測之上，而實測本身也有失效模式。** 一個用來產生證據的流程若沒被驗證過，它產出的證據就只是看起來像證據。2026-09-01 一次驗收實地踩到，該次的部分變異結論在換成隔離 cache 重跑前都不可信。

**7. TOUCHED 的實測精確度是 14%，不是憑感覺的 36%。**
逐條查證 28 條 TOUCHED，只有 4 條真的可移除。我先前推估 36% 是拿**人工挑過的可疑條目**算出來的比例去套機器標記的母體——選擇偏誤，高估三倍。
→ 規劃複審工作量時用 **14%**：`TOUCHED × 0.14` 才是實際可移除數。PARTIAL_GONE 尚未實測，但性質上（多為重構搬家）預期更低。

**8. 特徵權重異常時，先問「有沒有機制能解釋」，再判定是不是洩漏。**
做特徵層 SVM 融合時，`day_gap`（commit 與 todo 的日期距離）權重最大（+1.09），我判定它是過擬合捷徑 —— 還編得出合理說法（「5 條正解剛好都在 todo 之後不久」）。但移除它之後 MRR 從 0.512 **降到** 0.505、某案例排名從 222 **惡化到** 291。**手上有反證卻照原假設解讀，是確認偏誤。**
真相是它編碼了工作流程的真實結構（task 在 D 日跑 → 產出一批 todo → commit 落在 D+1/D+2），事後量測證實同日條目相關性是遠日的 2.53 倍。
→ 純資料視角**分辨不出「捷徑」與「真實機制」**——兩者在數字上長得一模一樣。這類結構只有領域知識能提供，所以遇到說不出所以然的強相關，先去問懂這個流程的人。

**9. 小樣本下，不學習的融合勝過學習型融合。**
5 條 ground truth 做 leave-one-out，特徵層 SVM 融合的 top-3 只有 60%，輸給固定公式 RRF 的 80%。根因是**樣本異質性**：有的條目靠識別字逐字匹配取勝（3-gram 第 1、cosine 第 14），有的靠純語義（cosine 第 1、3-gram 第 14），兩類需要相反的權重，而 linear SVM 只能學一組全域 `w`。訓練資料愈「一致」，對異類樣本愈致命。
→ 樣本量不足以涵蓋所有子型態時，**RRF 這種不學習的融合天然讓每條各取所長**。要讓學習型融合勝出，粗估需要 30–50 條標註。`verdict` 表就是為此累積用的。

**10. 試了兩個極端就下「做不到」的結論，是過早放棄。**
分群我先試連通分量（全傳遞）→ 巨型分量 65 條；再試共同錨點（零傳遞）→ 78 群太碎、覆蓋 56%。兩次失敗後我判定「自動分群做不到」，退回四類粗分類 + 工具查詢 —— 結果那個妥協把 167 條（87%）擠進單一章節，比原問題更糟。
真相是**那兩個極端之間整片沒試**：average-linkage 階層聚類最佳化「群內密、群間疏」，群數由資料決定，一次就給出 41 群 / 最大 6% / 覆蓋 92% / 輪廓 0.713。
→ 失敗的是**參數選擇**（傳遞性 0% 或 100%），不是**方法類別**。下結論前先問：我試的是不是同一個維度的兩端？中間有沒有東西？

## 資料模型

`run` / `todo` / `anchor` / `probe` / `verdict` 五張表。設計要點：

- `todo.key` 用 `date+title` 的 hash，**不用行號** —— 行號每次編輯都會漂
- `probe` 每次驗證留一筆，才回答得了「這條的錨點是什麼時候開始漂的」
- `verdict` 與 `probe` 分開 —— **機器觀察**與**誰下了什麼結論**在資料層就該分離
- `content_hash` 支援增量：條目沒改、程式碼沒動就跳過

## 已知缺口

pickaxe 抓不到兩類過期，這是結構性的：

1. **新增檔案／新功能造成的過期** —— 既有符號沒動，diff 裡看不到
2. **抽取規則涵蓋不到的錨點** —— CSS 變數 `--text-disabled`、非 ASCII 標記

補法是 **todo ↔ commit message 語義配對**（「這條待辦是不是已經被某個 commit 做掉了」）。零依賴的字元 3-gram 已證明訊號存在但鑑別力不足 —— 實測 `historicalKlines 改 allowlist` 命中 0.244，是噪音底線的 3 倍，但同義不同詞的案例（「狀態持久化」vs「狀態接新表」）只有 0.083。這是 embedding 唯一不可替代的位置，**尚未實作**。
