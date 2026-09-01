#!/usr/bin/env python3
"""todo-audit L0：從 todo.md 抽取機器可驗證的錨點，批次比對現況，輸出三態分流。

定位：這是「分流器」不是「判官」。它只能回答「錨點還在不在、行號漂了沒」，
回答不了「描述與現況是否相反」—— 那類只有讀了才知道，交給 L1/L2。

用法：
    python3 todo_audit.py <todo.md> <repo_root> [--json out.jsonl] [--db PATH | --no-db] | --similar "標題" | --stats | --groups [max_df] | --batch | --dims | --verdict JSON | --verdicts
"""
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import todo_config

# ── anchor 抽取規則 ────────────────────────────────────────────────
# 檔案錨點的副檔名清單。**可由 per-repo config 的 `anchor_exts` 覆寫**，見 set_anchor_exts()。
#
# 副檔名清單漏一個就漏一整類錨點——實測 `.gradle` 缺席，導致 fastTest N 常數那 3 條
# 重複條目全部抽不到錨點而配不上對。
#
# 為什麼要做成可設定，而不是直接把 md 加進預設值：這份清單決定「todo 文字裡什麼
# 字串算是檔案錨點」，那取決於該專案的產物是什麼。純文件 repo（skill／規格庫）的
# 主體是 markdown，`SKILL.md:284` 這種引用在預設清單下抽不出任何錨點，稽核對它
# 幾乎失效（實測 cast-power 3 條待辦全部落在 NO_ANCHOR）。但**全域加 md 不安全**：
# 實測 tradingbot 的 233 條待辦會多出 21 個查無此檔的錨點——`~/.claude/…` 底下的
# 檔案，以及 analysis.md／bindings.md 這類跑完就刪的 workspace 產物。而檔案錨點
# **沒有**符號那條「從未存在於 git 歷史 → 不算 GONE 訊號」的過濾（見 build_checks()），
# 所以那 21 個會直接變成假 GONE，正是本工具最怕的「把仍成立的待辦標成可移除」。
#
# 兩條 regex 共用同一份清單。它們原本各有一份且**不一致**（RE_FILE_LINE 少了 `js`）；
# 統一後等於補上 js，實測對 cast-power／todos／tradingbot 三個 repo 的 file_line
# 錨點數皆為 0 變化，是行為中性的整併。
ANCHOR_EXTS = ('java', 'ts', 'tsx', 'js', 'sql', 'gradle', 'properties',
               'css', 'mjs', 'cjs', 'sh', 'yml', 'yaml')


def _build_file_regexes(exts):
    """由副檔名清單組出 (file:line, 裸檔名) 兩條 regex。

    兩者的字元集刻意不同：file:line 允許 `/`（引用常帶路徑），裸檔名不允許
    （避免把路徑片段吃進來）。這是既有行為，本次整併不動它。
    """
    alt = '|'.join(re.escape(e) for e in exts)
    return (
        re.compile(rf'([A-Za-z0-9_/.\-]+\.(?:{alt})):(\d+)'),
        re.compile(rf'\b([A-Za-z0-9_\-]+\.(?:{alt}))\b'),
    )


# file:line —— 最強的錨點，同時給出存在性與位置
RE_FILE_LINE, RE_FILE = _build_file_regexes(ANCHOR_EXTS)


def set_anchor_exts(exts):
    """依 config 重建兩條檔案錨點 regex。

    刻意改寫 module 級 global，而不是把 exts 一路傳進 extract_anchors()：
    extract_anchors() 有多個呼叫點（稽核主流程、similar_mode 的寫入前重複提示、
    api_batch、api_groups）。只改主流程會讓「寫入前提示」與「稽核」用不同的錨點
    定義，那種不一致比 global 難看的問題嚴重得多——同一條 todo 在兩個地方會被
    算出不同的錨點集合。
    """
    global RE_FILE_LINE, RE_FILE
    RE_FILE_LINE, RE_FILE = _build_file_regexes(exts)
# SQL 表名／欄位：要求含底線，避免把一般大寫英文詞誤判
RE_SQL = re.compile(r'\b([A-Z][A-Z0-9]*_[A-Z0-9_]{2,})\b')
# config key：三段以上的點分小寫路徑，如 ai.signal.producer.interval-ms
RE_CONFIG = re.compile(r'\b([a-z][a-z0-9]*(?:[.\-][a-z0-9]+){2,})\b')
# Java 類別名：靠命名後綴辨識，避免把一般英文詞誤判成類別
RE_CLASS = re.compile(
    r'\b([A-Z][A-Za-z0-9]*(?:Service|Controller|Repository|Job|Runner|Executor|Manager|'
    r'Config|Client|Handler|Calculator|Assessor|Sizer|Guard|Monitor|Tracker|Filter|'
    r'Aggregator|Producer|Consumer|Scheduler|Resolver|Reconciler|Ledger|Engine|Factory|Interceptor))\b'
)
# backtick 包住的識別字（方法名、常數、欄位）
RE_BACKTICK = re.compile(r'`([A-Za-z_][A-Za-z0-9_]{3,})`')
# commit hash：必須含至少一個 a-f 字母，且前後不接英數/小數點
# （否則 0.07786353 這種價格數字會被當成 commit）
RE_COMMIT = re.compile(r'(?<![0-9a-zA-Z.])([0-9a-f]{7,10})(?![0-9a-zA-Z.])')

# 掃描範圍：只看生產與前端原始碼。
# 這兩個值現在是 builtin 層 —— 沒有任何 config 檔時逐字沿用（BTSE 零回歸保護）；
# 專案可用 <repo>/.claude/todo-audit.json 或 ~/.claude/todo-audit.json 覆寫
# （見 todo_config.load_config()）。值本身一字不改。
SEARCH_DIRS = ['agent/src/main', 'core/src/main', 'exchange/src/main', 'web/src', 'web/scripts', 'scripts']
SCAN_EXTS = {'.java', '.ts', '.tsx', '.js', '.mjs', '.cjs', '.sql', '.gradle', '.css', '.sh', '.properties'}
SKIP_DIRS = {'build', 'node_modules', '.git', 'dist', 'target', 'keyConfig'}

# ── 機敏檔禁訪（專案最高原則）──────────────────────────────────────
# 掃描型工具的通病：範圍由副檔名＋目錄定義，而機敏檔恰好落在裡面。
# 「只是建索引」不構成讀取豁免——實測本腳本初版就因 SCAN_EXTS 含 .properties
# 而讀進了 application.properties。任何全樹掃描都必須有明確排除清單。
SECRET_FILES = {
    'application.properties',
    'application-prod.properties',
    'application-dev.properties',
    'gradle.properties',
}


def is_secret(p):
    return p.name in SECRET_FILES or 'keyConfig' in p.parts

# 行號漂移超過這個值就標 DRIFT（描述可能已過期）
DRIFT_THRESHOLD = 15



# ── 持久化 ────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL, todo_file TEXT, repo TEXT,
  todo_count INT, symbol_count INT, hit_rate REAL, degraded INT);

-- key 用 date+title 的 hash，不用行號：行號每次編輯都會漂，
-- 但標題改了本來就該視為新條目。
CREATE TABLE IF NOT EXISTS todo(
  key TEXT PRIMARY KEY, date TEXT, title TEXT,
  first_seen_run INT, last_seen_run INT, content_hash TEXT);

CREATE TABLE IF NOT EXISTS anchor(
  todo_key TEXT, kind TEXT, ref TEXT, recorded_line INT,
  PRIMARY KEY(todo_key, kind, ref));

-- 每次驗證留一筆，才回答得了「這條的錨點是什麼時候開始漂的」
CREATE TABLE IF NOT EXISTS probe(
  run_id INT, todo_key TEXT, state TEXT, anchor_count INT,
  gone TEXT, touched TEXT, commits_since TEXT,
  PRIMARY KEY(run_id, todo_key));

-- L1/L2 的判定與理由。分流器只寫 probe，判定一律另外落這裡，
-- 讓「機器觀察」與「誰做了什麼結論」在資料層就分開。
CREATE TABLE IF NOT EXISTS verdict(
  run_id INT, todo_key TEXT, tier TEXT, call TEXT, reason TEXT,
  decided_at TEXT, PRIMARY KEY(run_id, todo_key, tier));

-- 「某符號是否從未存在於 git 歷史」查一次要一趟 git log -S --all（慢），
-- 但這是**單調事實**：從未存在的可能變成存在（那時它會直接進 sym_index，
-- 不再走這條路徑），已存在的不會變回從未存在。所以快取永久有效。
CREATE TABLE IF NOT EXISTS symbol_history(
  symbol TEXT PRIMARY KEY, never_existed INT, checked_at TEXT);

CREATE INDEX IF NOT EXISTS ix_probe_state ON probe(state);
"""


def todo_key(t):
    return hashlib.sha1(f"{t['date']}|{t['title']}".encode()).hexdigest()[:16]


def persist(db_path, todo_file, repo, todos, results, symbols, hit_rate, started_at,
            degraded=False):
    """寫入 sqlite。回傳 (run_id, 內容有變動的條目數)。

    `degraded`：這次稽核的 search_dirs 是否零命中而降級為全 repo 掃描（run 級旗標，
    見 todo_store.state_of()）。既有 DB（建於本欄位新增之前）走 ALTER TABLE
    就地升級 —— 與 todo_store.MIGRATIONS 的機制一致，供 todo_store.connect()
    開啟同一個 DB 時不需要重新遷移。
    """
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    try:
        con.execute('ALTER TABLE run ADD COLUMN degraded INT')
    except sqlite3.OperationalError as e:
        if 'duplicate column name' not in str(e):
            raise
    cur = con.cursor()
    cur.execute('INSERT INTO run(started_at,todo_file,repo,todo_count,symbol_count,hit_rate,degraded)'
                ' VALUES(?,?,?,?,?,?,?)',
                (started_at, str(todo_file), str(repo), len(todos), len(symbols), hit_rate,
                 1 if degraded else 0))
    run_id = cur.lastrowid

    changed = 0
    for t, r in zip(todos, results):
        k = todo_key(t)
        chash = hashlib.sha1(('\n'.join(t['body'])).encode()).hexdigest()[:16]
        prev = cur.execute('SELECT content_hash FROM todo WHERE key=?', (k,)).fetchone()
        if prev is None:
            cur.execute('INSERT INTO todo VALUES(?,?,?,?,?,?)',
                        (k, t['date'], t['title'], run_id, run_id, chash))
            changed += 1
        else:
            if prev[0] != chash:
                changed += 1
            cur.execute('UPDATE todo SET last_seen_run=?, content_hash=? WHERE key=?',
                        (run_id, chash, k))
        cur.execute('DELETE FROM anchor WHERE todo_key=?', (k,))
        for c in r['checks']:
            cur.execute('INSERT OR IGNORE INTO anchor VALUES(?,?,?,?)',
                        (k, c['kind'], c['ref'], None))
        cur.execute('INSERT OR REPLACE INTO probe VALUES(?,?,?,?,?,?,?)',
                    (run_id, k, r['state'], r['anchor_count'],
                     json.dumps(r['gone'], ensure_ascii=False),
                     json.dumps(r.get('touched_symbols', []), ensure_ascii=False),
                     json.dumps(r['commits_since'], ensure_ascii=False)))
    con.commit()
    con.close()
    return run_id, changed


# 提議性語境：todo 常寫「應改用 X」「建議抽出 Y」「改名為 Z」，那些名字**從未存在**。
# 把它們當錨點會讓「查無此符號」被誤讀成「載體消失」——實測 PARTIAL_GONE 因此
# 精確度接近 0（16 條逐條查證，0 條真的可移除）。
# 只過濾「提議動詞緊鄰識別字」的情形，避免誤殺同句提到的真實符號。
RE_PROPOSAL = re.compile(
    r'(應改|應該改|建議|改用|改名|抽出|正解是|修法|宜改|可改|改為|改成|應為|'
    r'新增一個|加一個|換成|收斂成|重構成|拆成|例如|比照)[^。；\n]{0,14}$'
)


def _is_proposed(text, start):
    """識別字前 14 字元內出現提議動詞 → 視為提議中的名字，不當錨點。"""
    return bool(RE_PROPOSAL.search(text[max(0, start - 20):start]))


def _loc(todo, width=4):
    """行號欄位。sqlite 來源的條目未必有 md 行號（`todo_line` 缺列時為
    None），舊的 f'{line:4d}' 會直接 TypeError 炸掉整份報告。"""
    ln = todo['line']
    return f'L{ln:{width}d}' if ln is not None else '-'.rjust(width + 1)


def parse_todos(path):
    """解析 markdown，每個 `- [ ]` 連同其續行成為一筆。"""
    todos, cur = [], None
    for lineno, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if raw.startswith('- [ ]'):
            if cur:
                todos.append(cur)
            title = raw[6:].strip()
            m = re.match(r'\[(\d{4}-\d{2}-\d{2})\]', title)
            cur = {'line': lineno, 'title': title, 'body': [],
                   'date': m.group(1) if m else '1970-01-01'}
        elif cur is not None and (raw.startswith('  >') or raw.startswith('  ')):
            cur['body'].append(raw.strip())
        elif raw.startswith('#') or raw.startswith('- [x]'):
            if cur:
                todos.append(cur)
            cur = None
    if cur:
        todos.append(cur)
    return todos


def load_todos(source):
    """統一輸入層。source 為 .md 走 legacy 解析，為 .sqlite 走 DB。

    兩條路徑的回傳形狀必須完全相同 —— 見 tests/test_audit_parity.py。
    這是遷移的唯一接縫：換掉輸入來源，其餘 900 行不動。

    DB 路徑只取 pending/doing —— 稽核的對象是待辦，已完成或已擱置的
    條目不該再消耗注意力（md 時代它們是被直接刪掉的，看不到也就不必篩）。
    """
    source = Path(source)
    if source.suffix == '.md':
        return parse_todos(source)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import todo_store
    con = todo_store.connect(source)
    try:
        parsed = todo_store.load_parsed(con, source.stem,
                                        statuses=('pending', 'doing'))
        return todo_store.to_audit_shape(parsed)
    finally:
        con.close()


def extract_anchors(todo):
    """從標題＋續行抽出所有錨點，去重。"""
    text = todo['title'] + ' ' + ' '.join(todo['body'])
    a = {'file_line': [], 'file': [], 'symbol': [], 'commit': []}

    for m in RE_FILE_LINE.finditer(text):
        a['file_line'].append((m.group(1), int(m.group(2))))
    seen_fl = {f for f, _ in a['file_line']}
    for m in RE_FILE.finditer(text):
        if m.group(1) not in seen_fl and not _is_proposed(text, m.start()):
            a['file'].append(m.group(1))
    for m in RE_CLASS.finditer(text):
        if not _is_proposed(text, m.start()):
            a['symbol'].append(m.group(1))
    for m in RE_BACKTICK.finditer(text):
        tok = m.group(1)
        # 過濾掉純中文/純數字/明顯不是識別字的
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', tok) and not _is_proposed(text, m.start()):
            a['symbol'].append(tok)
    for m in RE_COMMIT.finditer(text):
        a['commit'].append(m.group(1))
    for m in RE_SQL.finditer(text):
        a['symbol'].append(m.group(1))
    for m in RE_CONFIG.finditer(text):
        # 排除檔名（已由 RE_FILE 處理）與版號
        tok = m.group(1)
        if not re.search(r'\.(java|ts|tsx|js|sql|sh|gradle|properties|css|yml|yaml|md|cjs|mjs)$', tok):
            a['symbol'].append(tok)

    for k in a:
        if k == 'file_line':
            a[k] = sorted(set(a[k]))
        else:
            a[k] = sorted(set(a[k]))
    return a


def collect_source_files(repo, config=None):
    """一次走訪全 repo，建立兩個用途不同的索引。

    兩者的範圍刻意不同 —— 這是實測踩出來的：
    - by_name（檔案存在性）必須涵蓋全 repo。build.gradle 在 root、*Test.java 在
      src/test，若只掃 src/main 會把它們判成「檔案已消失」。
    - files（符號掃描）只看生產碼，範圍由三層 config 合併後的 search_dirs/scan_exts
      決定（見 todo_config.load_config()）。把測試碼算進來的話，「符號只剩測試在用、
      生產零呼叫端」這種死碼特徵就驗不出來 —— 而那正是本 repo 反覆出現的缺陷型態。

    索引只建一次。原本每條 todo 各跑一次 rglob，198 條就是 198 次全樹遍歷。

    `config` 由呼叫端（main()）預先合併好傳入，避免每次呼叫都重新讀一次
    config 檔；未傳入時（例如單元測試直接呼叫本函式）就地合併一次。
    """
    if config is None:
        defaults = {'search_dirs': list(SEARCH_DIRS), 'scan_exts': list(SCAN_EXTS)}
        config, _, _ = todo_config.load_config(repo, defaults)
    search_dirs = config['search_dirs']
    scan_exts = set(config['scan_exts'])

    prod_files, by_name = [], defaultdict(list)
    prod_roots = tuple(str(repo / d) for d in search_dirs)
    for p in repo.rglob('*'):
        if not p.is_file() or SKIP_DIRS & set(p.parts):
            continue
        by_name[p.name].append(p)   # 僅記路徑，供存在性判定，不讀內容
        if is_secret(p):
            continue                # 機敏檔永不進入會被 read_text 的清單
        if p.suffix in scan_exts and str(p).startswith(prod_roots):
            prod_files.append(p)
    return prod_files, by_name



def _warn_zero_hit_degrade(repo, config, provenance):
    """REQ-2 零命中降級警告。訊息依 G-2 區分兩種前因，修復動作不同：
      (a) 找不到任何 config 檔 → 印「未設定」，指引為「建立 config」
      (b) config 檔存在但合併後仍零命中 → 不印「未設定」，
          改印實際生效的 search_dirs 值與其來源層級，指引為「檢查 config 內容」

    判定規則本身在 todo_config.zero_hit_diagnosis()（與 todo_cli.py 的
    doctor 共用，不各自重做一份〔finding 3〕），這裡只負責用自己的
    前綴／文案格式化輸出。
    """
    diag = todo_config.zero_hit_diagnosis(repo, config, provenance)
    print('⚠️  WEAK_AUDIT：search_dirs 零命中，已降級為全 repo 掃描 —— '
          '死碼偵測（GONE 判定）本次失效，結果不可盡信。')
    if diag['configured']:
        print(f'   目前生效的 search_dirs = {diag["search_dirs"]}'
              f'（來源：{diag["source"]}）')
        print(f'   請檢查設定內容是否正確：{diag["per_repo_path"]} 或 {diag["user_global_path"]}')
    else:
        print(f'   本專案未設定 search_dirs（找不到 {diag["per_repo_path"]} 或 {diag["user_global_path"]}）')
        print(f'   建立設定檔以縮小掃描範圍，例如 {diag["per_repo_path"]}：')
        print(f'   {json.dumps(diag["example"])}')
    print()


def _warn_small_sample_degrade(repo, config, provenance, symbols, sym_index, files):
    """符號樣本太小、命中率不具鑑別力時的降級警告。

    與 _warn_zero_hit_degrade() 分開的理由是前因不同、修復動作也不同：
    零命中是「search_dirs 一個檔都沒掃到」；這裡是「檔案掃到了，但待辦裡的
    符號錨點太少，命中率無法用來判斷掃描層有沒有故障」。兩者共用同一份
    config 診斷（zero_hit_diagnosis），因為最可能的修復都是調整掃描範圍
    —— 實測兩起小樣本 FATAL 的根因都是掃描範圍，不是掃描器故障。
    """
    diag = todo_config.zero_hit_diagnosis(repo, config, provenance)
    print(f'⚠️  WEAK_AUDIT：本次只有 {len(symbols)} 個符號錨點、命中 {len(sym_index)} 個'
          f'（掃描 {len(files)} 個檔案），樣本太小，命中率無法用來判斷掃描層是否故障。')
    print('   已降級照常產出結果 —— 但死碼偵測（GONE 判定）本次不可盡信，'
          '每一條的狀態都會顯示為 WEAK_AUDIT。')
    if diag['configured']:
        print(f'   目前生效的 search_dirs = {diag["search_dirs"]}（來源：{diag["source"]}）')
        print(f'   符號若其實存在、只是不在掃描範圍內，請檢查：{diag["per_repo_path"]}')
    else:
        print(f'   本專案未設定 search_dirs（找不到 {diag["per_repo_path"]}），沿用內建預設。')
        print(f'   若這個 repo 的佈局與預設不同，建立設定檔即可：{json.dumps(diag["example"])}')
    print()


def build_commit_pool(repo, since):
    """全部 commit message，供「這條待辦是不是已經被做掉了」比對。"""
    out = subprocess.run(['git', 'log', f'--since={since}', '--date=short',
                          '--format=%h|%ad|%s'],
                         cwd=repo, capture_output=True, text=True, check=True).stdout
    return [l.split('|', 2) for l in out.splitlines() if l.count('|') >= 2]


def _grams(s, n=3):
    s = re.sub(r'\s+', '', s.lower())
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def match_commits(title, pool, since_date, topk=3, floor=0.08):
    """字元 3-gram 比對 todo 標題 ↔ commit message。

    為什麼是字元 n-gram 而不是語義 embedding：實測本語料的主訊號是**英文識別字
    的逐字匹配**（`historicalKlines` 在 todo 與 commit 裡一字不差），而非中文語義。
    純中文 embedding 反而把識別字切成無意義 subword，top-3 從 60% 掉到 20%。
    模型選型要跟著語料特性走，不跟著「比較先進」走。

    只回窗內（條目日期之後）的 commit——之前的 commit 不可能「做掉」這條。
    """
    tq = _grams(title)
    if not tq:
        return []
    scored = []
    for h, d, s in pool:
        if d <= since_date:
            continue
        g = _grams(s)
        sc = len(tq & g) / max(1, len(tq | g))
        if sc >= floor:
            scored.append((round(sc, 3), h, d, s))
    return sorted(scored, reverse=True)[:topk]


def build_symbol_touch_index(repo, since, symbols):
    """一次 `git log -p`，建立 {符號: [(hash, date, subject), ...]}。

    等價於對每個符號跑 `git log -S<符號>`（pickaxe），但只啟動一個進程。

    為什麼一定要符號級而非檔案級：實測檔案級把 58% 的條目標成「碰過」——
    OrderService.java 上千行、幾乎每天被改，「這個檔案動過」對「這條 todo
    關心的那幾行動過沒」幾乎零資訊量。粒度不對的訊號比沒有訊號更糟，
    因為它看起來像證據。

    機敏檔用 git pathspec 在來源端排除：diff 內容會整段吐出檔案明文，
    只在讀檔端過濾擋不住這條路徑。
    """
    idx = defaultdict(set)
    pattern = re.compile(r'\b(' + '|'.join(re.escape(s) for s in sorted(symbols)) + r')\b')
    try:
        out = subprocess.run(
            ['git', 'log', f'--since={since}', '--date=short',
             '--format=@@@%h|%ad|%s', '-p', '--unified=0', '--no-color',
             '--', '.',
             ':(exclude)*application*.properties',
             ':(exclude)*gradle.properties',
             ':(exclude)keyConfig/*'],
            cwd=repo, capture_output=True, text=True, timeout=300, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        sys.exit(f'FATAL: git log -p 失敗（{type(e).__name__}）——不可降級為空索引，'
                 '否則「查無變更」會被誤讀成「這塊碼沒動過」')

    # 純註解／文件 commit 對 pickaxe 是假訊號：refactor(test) 之類會大量增刪
    # 含符號名的「註解行」，與真的改邏輯在 diff 上無法區分。實測它們佔
    # TOUCHED 引用 commit 的 58%，且有 11 條的證據全部來自這類 commit。
    NOISE = re.compile(r'^(docs|chore)[(:]|^refactor\(test\)')
    cur = None
    for line in out.splitlines():
        if line.startswith('@@@'):
            h, d, s = line[3:].split('|', 2)
            cur = None if NOISE.match(s) else (h, d, s)
        elif cur and line[:1] in ('+', '-') and not line[:3] in ('+++', '---'):
            for m in pattern.finditer(line):
                idx[m.group(1)].add(cur)
    return {k: sorted(v, key=lambda c: c[1], reverse=True) for k, v in idx.items()}


def build_symbol_index(symbols, files):
    """純 Python 掃描，建 {symbol: [(file, line), ...]}。

    刻意不呼叫外部 grep/rg：`rg` 在部分環境是 shell function 而非可執行檔，
    subprocess 會拿到 FileNotFoundError。零外部依賴才不會因環境差異靜默退化。
    """
    if not symbols:
        return {}
    index = defaultdict(list)
    pattern = re.compile(r'\b(' + '|'.join(re.escape(s) for s in sorted(symbols)) + r')\b')
    for p in files:
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if not pattern.search(text):
            continue  # 整檔無命中，跳過逐行掃描
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in pattern.finditer(line):
                index[m.group(1)].append((str(p), lineno))
    return index


# 一個符號在生產碼出現超過這個處數，就視為「通用符號」，其 touch 訊號無鑑別力
RARE_SYMBOL_MAX_DF = 8


def collect_touches(anchors, touch_index, sym_index, since_date):
    """條目寫下之後，有哪些 commit 真的動到了它的「特徵符號」。

    只採計稀有符號（df <= RARE_SYMBOL_MAX_DF）。這是實測結論：
    setScale / OrderService 這類散佈幾十處的通用符號，在任何窗口內幾乎必然
    被動到，把 58% 的條目染成紅旗；而 recordPnl 這種只出現在兩個檔案的符號，
    被動到就是強訊號（Gate 3 死碼→已通電，正是這樣現形的）。

    訊號強度與符號稀有度成正比——與分群用的 IDF 加權是同一個統計問題。
    """
    touches = {}
    for s in anchors['symbol']:
        df = len(sym_index.get(s, []))
        if df == 0 or df > RARE_SYMBOL_MAX_DF:
            continue
        later = [c for c in touch_index.get(s, []) if c[1] > since_date]
        if later:
            touches[s] = later
    return touches


def never_existed(repo, symbols, db_path=None):
    """用 git pickaxe 全歷史判斷：哪些符號**從未存在**於這個 repo。

    「查無此符號」有兩種意義，先前被混為一談：
      曾存在後消失 → 真被移除或重構，條目可能已完成（有訊號）
      從未存在     → todo 作者自創的描述性稱呼（零訊號）
    實測 shutdownAfterGateExit / TERMINAL_ORDER_STATUSES 屬後者，
    它們讓 PARTIAL_GONE 充滿無效紅旗。

    只對已判定 GONE 的符號查，數量少，成本可控。
    """
    out, cached, con = set(), {}, None
    if db_path:
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA)
        cached = dict(con.execute('SELECT symbol, never_existed FROM symbol_history'))
    todo_syms = [s for s in symbols if s not in cached]
    out |= {s for s in symbols if cached.get(s) == 1}
    for s in todo_syms:
        try:
            r = subprocess.run(['git', 'log', '--all', '--oneline', '-S', s, '-1'],
                               cwd=repo, capture_output=True, text=True, timeout=30)
            never = 1 if not r.stdout.strip() else 0
        except Exception:
            never = 0     # 查不到就當它存在——保守，不製造假的「從未存在」
        if never:
            out.add(s)
        if con:
            con.execute('INSERT OR REPLACE INTO symbol_history VALUES(?,?,?)',
                        (s, never, _now()))
    if con:
        con.commit(); con.close()
    if todo_syms:
        print(f'  （git 全歷史查詢 {len(todo_syms)} 個新符號，'
              f'{len(symbols)-len(todo_syms)} 個命中快取）')
    return out


def verify(todo, anchors, repo, sym_index, by_name, commit_set, never=frozenset()):
    """對照現況，產生 anchor 級的驗證結果。"""
    checks = []

    for f, ln in anchors['file_line']:
        hits = by_name.get(Path(f).name, [])
        if not hits:
            checks.append({'kind': 'file_line', 'ref': f'{f}:{ln}', 'state': 'GONE'})
        else:
            p = hits[0]
            total = sum(1 for _ in p.open(encoding='utf-8', errors='ignore'))
            checks.append({
                'kind': 'file_line', 'ref': f'{f}:{ln}',
                'state': 'OK' if ln <= total else 'DRIFT',
                'detail': f'檔案共 {total} 行' + ('（記載行號已超出）' if ln > total else ''),
            })

    for f in anchors['file']:
        hits = by_name.get(f, [])
        checks.append({
            'kind': 'file', 'ref': f,
            'state': 'OK' if hits else 'GONE',
            'detail': str(hits[0]) if hits else '',
        })

    for s in anchors['symbol']:
        hits = sym_index.get(s, [])
        if hits:
            st, detail = 'OK', f'{len(hits)} 處命中'
        elif s in never:
            # 從未存在 ≠ 消失。標成 OK 不是說它成立，是說它**不構成 GONE 訊號**
            st, detail = 'OK', '從未存在於 git 歷史（描述性稱呼，非錨點）'
        else:
            st, detail = 'GONE', '曾存在但已消失'
        checks.append({'kind': 'symbol', 'ref': s, 'state': st, 'detail': detail})

    for c in anchors['commit']:
        checks.append({
            'kind': 'commit', 'ref': c,
            'state': 'OK' if c in commit_set else 'GONE',
        })

    return checks


def classify(checks, touches):
    """收斂成條目級狀態。保守優先：有任何存活跡象就不判 GONE。

    注意 TOUCHED 的位階：它排在 ALIVE 之前，因為實測顯示「錨點存活」對過期
    幾乎沒有判別力（對 9 條已知過期只命中 1 條），而「條目寫下後這塊碼被動過」
    才是主流過期型態的指紋。
    """
    if not checks:
        return 'NO_ANCHOR'
    states = [c['state'] for c in checks]
    if all(s == 'GONE' for s in states):
        return 'ALL_GONE'          # 載體全消失
    if any(s == 'GONE' for s in states):
        return 'PARTIAL_GONE'      # 部分錨點消失
    if touches:
        return 'TOUCHED'           # 錨點在，但這塊碼在條目之後被改過 → 最可能已過期
    if any(s == 'DRIFT' for s in states):
        return 'DRIFT'
    return 'ALIVE'                 # 錨點完好且無人動過，大機率仍成立



def similar_mode(todo_path, query, topn=5, exclude_title=None):
    """單條查詢：新條目 vs 既有條目的相似度。供寫入前的重複提示。

    刻意只做文字與錨點比對，不跑 git log -p —— 這條路徑要在人打字的
    節奏內回應，不能花 20 秒。

    輸出是**提示**：高相似不等於重複。實測錨點共現滿分的兩條
    （preview 超時 / dry-run 超時）是同一件事的兩半，兩個都得做。
    """
    todos = load_todos(todo_path)
    # 查既有條目的相似項時，把它自己排除 —— 否則第一名永遠是自己（1.0），
    # 白佔一個名額。寫入前的重複提示情境沒有 exclude_title，行為不變。
    if exclude_title:
        todos = [t for t in todos if t['title'] != exclude_title]
    qa = extract_anchors({'title': query, 'body': [], 'date': '9999-12-31'})
    qset = set(qa['symbol']) | {f for f, _ in qa['file_line']} | set(qa['file'])
    qg = _grams(query)

    def aset_of(t):
        a = extract_anchors(t)
        return set(a['symbol']) | {f for f, _ in a['file_line']} | set(a['file'])

    # IDF：錨點在多少條目中出現過。build.gradle 這種散佈全清單的檔案鑑別力低，
    # 共用它不代表兩條相關——實測「Spring context 碎片化」與「fastTest N 脫鉤」
    # 僅因共用 build.gradle 就拿到滿分。
    all_sets = [aset_of(t) for t in todos]
    df = defaultdict(int)
    for s in all_sets:
        for x in s:
            df[x] += 1

    def w(x):
        return 1.0 if df[x] <= 1 else 1.0 / df[x] ** 0.5

    scored = []
    for t, aset in zip(todos, all_sets):
        inter = qset & aset
        # 用「共用了多少資訊量」而非「共用比例」：Jaccard 對「兩邊各只有一個
        # 錨點且相同」必給滿分，即使那錨點是散佈全清單的 build.gradle。
        # 改成權重總和後，單一常見錨點只拿部分分數，多個罕見錨點才逼近 1.0。
        anchor_j = min(1.0, sum(w(x) for x in inter)) if inter else 0.0
        g = _grams(t['title'])
        text_j = len(qg & g) / max(1, len(qg | g)) if qg else 0.0
        # 取兩者較大者：錨點與文字是互斥的訊號源，任一夠強就值得提示
        score = max(anchor_j, text_j)
        if score >= 0.12:
            # 存格式化後的行號而非原值：本函式把 todo dict 拆成 tuple，
            # _loc() 的 dict 簽名在這裡對不上，當初就漏用了它。
            scored.append((round(score, 3), round(anchor_j, 3), round(text_j, 3),
                           _loc(t), t['date'], t['title'], sorted(qset & aset)[:4]))

    # 只拿三個分數欄位當 key。裸 sort 會在同分時往下比第四欄，而 md 遷移來的
    # 條目有整數行號、append_item() 建的沒有（它的 INSERT 沒列 md_line），
    # 新舊同分就 int 比 None 而炸。同分先後改由 sort 的穩定性決定 —— 比拿一個
    # 半數為 NULL 的欄位當 tiebreaker 更可預期。
    scored.sort(key=lambda r: r[:3], reverse=True)
    if not scored:
        print('✓ 未發現相似的既有條目')
        return
    print(f'⚠️  發現 {len(scored)} 條相似的既有條目 —— 這是提示，不是阻擋。')
    print('   高相似 ≠ 重複：同一件事的兩半也會高分，那種情況兩條都該留。\n')
    for sc, aj, tj, loc, date, title, shared in scored[:topn]:
        print(f'  [{sc}] {loc} [{date}] {title[:60]}')
        print(f'        錨點={aj} 文字={tj}' + (f'  共用: {", ".join(shared)}' if shared else ''))
    print('\n  處置：(a) 合併進既有條目  (b) 另立新條並互相 link  (c) 忽略提示')



# ── 快速查詢 API（皆不跑 git，秒回）────────────────────────────────
def api_stats(todo_path):
    """1) 條目統計：總數、依日期與優先級標記分佈、錨點覆蓋率。"""
    todos = load_todos(todo_path)
    anchors = [extract_anchors(t) for t in todos]
    with_anchor = sum(1 for a in anchors if any(a[k] for k in a))
    by_month = defaultdict(int)
    by_pri = defaultdict(int)
    for t in todos:
        by_month[t['date'][:7]] += 1
        m = re.search(r'\[(P[0-3])\]', t['title'])
        by_pri[m.group(1) if m else '未標'] += 1
    blocked = sum(1 for t in todos if '拍板' in t['title'])

    print(f'總計          {len(todos)} 條')
    print(f'可自動複驗    {with_anchor} 條（{with_anchor/max(1,len(todos)):.0%}）'
          f' / 無錨點 {len(todos)-with_anchor} 條')
    print(f'等人拍板      {blocked} 條')
    print('\n依優先級：')
    for k in sorted(by_pri, key=lambda x: (x == '未標', x)):
        print(f'  {k:6s} {by_pri[k]:4d}')
    print('\n依月份：')
    for k in sorted(by_month):
        print(f'  {k}  {by_month[k]:4d}')
    return len(todos)


def _anchor_sets(todos):
    out = []
    for t in todos:
        a = extract_anchors(t)
        out.append(set(a['symbol']) | {f for f, _ in a['file_line']} | set(a['file']))
    return out


def _sim_matrices(todos, sets):
    """回傳 (錨點相似度 A, 文字相似度 T)。

    刻意**不**把日期放進矩陣：實測日期相似度是稠密的（任兩條都非零），
    等於在圖上加一層全連接弱邊，average linkage 會把它們黏成 93 條的巨型群，
    輪廓係數從 0.706 崩到 0.241。日期是強化訊號但不是結構訊號——
    當 tie-breaker（排序）可以，當分群依據不行。
    """
    import math
    n = len(todos)
    df = defaultdict(int)
    for s in sets:
        for x in s:
            df[x] += 1
    W = {x: (1.0 if c <= 1 else 1.0 / math.sqrt(c)) for x, c in df.items()}
    G = [_grams(t['title']) for t in todos]
    A = [[0.0] * n for _ in range(n)]
    T = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            inter = sets[i] & sets[j]
            a = min(1.0, sum(W[x] for x in inter)) if inter else 0.0
            gi, gj = G[i], G[j]
            tt = len(gi & gj) / max(1, len(gi | gj))
            A[i][j] = A[j][i] = a
            T[i][j] = T[j][i] = tt
    return A, T


def _agglomerate(S, idxs, thresh):
    """Average-linkage 階層聚類。相似度低於 thresh 即停止合併——
    群數由資料決定，不需預先指定 k。"""
    cl = {i: [i] for i in idxs}
    act = list(idxs)
    while len(act) > 1:
        best, bi, bj = -1.0, None, None
        for ai, i in enumerate(act):
            ci = cl[i]
            for j in act[ai + 1:]:
                cj = cl[j]
                v = sum(S[a][b] for a in ci for b in cj) / (len(ci) * len(cj))
                if v > best:
                    best, bi, bj = v, i, j
        if best < thresh:
            break
        cl[bi] += cl[bj]; del cl[bj]; act.remove(bj)
    return list(cl.values())


def api_groups(todo_path, max_df=8):
    """2) 智慧分群：分層階層聚類。

    L1 錨點（高精度，輪廓 0.713）→ L2 文字收殘餘（高覆蓋）。

    為什麼不是連通分量：傳遞閉包會產生巨型分量（實測 192 條裡一群 65 條），
    A–B 相關、B–C 相關但 A–C 無關時橋接節點把不相干的串成一坨。
    為什麼不是「共同錨點」：那是另一個極端，完全不做傳遞，78 群太碎且覆蓋僅 56%。
    Average-linkage 在兩者之間最佳化「群內密、群間疏」，實測 41 群 / 最大 12 條
    (6%) / 覆蓋 92%。
    """
    todos = load_todos(todo_path)
    sets = _anchor_sets(todos)
    n = len(todos)
    A, T = _sim_matrices(todos, sets)

    L1 = [g for g in _agglomerate(A, list(range(n)), 0.10) if len(g) > 1]
    covered = {i for g in L1 for i in g}
    rest = [i for i in range(n) if i not in covered]
    L2 = [g for g in _agglomerate(T, rest, 0.12) if len(g) > 1]

    groups = sorted([(g, '⚓') for g in L1] + [(g, '✎') for g in L2],
                    key=lambda gg: -len(gg[0]))
    inside = sum(len(g) for g, _ in groups)
    print(f'分層階層聚類（L1 錨點 @0.10 → L2 文字 @0.12）')
    print(f'總條目 {n} | 群 {len(groups)} 個 | 涵蓋 {inside} 條（{inside/n:.0%}）'
          f' | 孤立 {n-inside} 條')
    print('⚓=錨點群（高精度）  ✎=文字群（收殘餘）\n')
    for gi, (g, tag) in enumerate(groups[:20], 1):
        # 群標籤用「群內最高頻錨點」而非交集：average linkage 不要求全員
        # 共用同一錨點（那是共同錨點法的特性），取交集會常常落空。
        freq = defaultdict(int)
        for i in g:
            for x in sets[i]:
                freq[x] += 1
        top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        ds = sorted({todos[i]['date'] for i in g})
        label = ', '.join(f'{x}({c}/{len(g)})' for x, c in top) if top else '（無錨點群）'
        print(f'群{gi:<2}({len(g)}條) {tag} {label}   {ds[0]}~{ds[-1]}')
        for i in sorted(g, key=lambda i: todos[i]['date'], reverse=True)[:3]:
            print(f'      {_loc(todos[i], 4)} {todos[i]["title"][:56]}')
        if len(g) > 3:
            print(f'      … 另 {len(g)-3} 條')
    if len(groups) > 20:
        print(f'\n… 另 {len(groups)-20} 群')
    return len(groups)


def api_batch(todo_path):
    """2b) 批次視圖：依產出日期分批，並量測每批的內聚度。

    依據是實測的工作流程結構：一個 task 在 D 日執行 → 產出一批 todo →
    後續 commit 落在 D+1/D+2。同日產出的條目彼此錨點相似度是相隔 >7 天者的
    2.53 倍（高相似占比 3.91 倍），且隨日期距離單調遞減。

    ⚠️ 這是**強化訊號但弱主導**：4 倍是相對值，絕對值仍低（2.8% vs 0.7%）。
    當次要權重或 tie-breaker 用，不要拿它當分群主依據。

    價值在於它抓得到錨點抽不到的條目——無錨點條目完全落在 --groups 之外，
    但它們仍屬於某個批次。
    """
    todos = load_todos(todo_path)
    sets = _anchor_sets(todos)
    df = defaultdict(int)
    for s in sets:
        for x in s:
            df[x] += 1
    w = lambda x: 1.0 if df[x] <= 1 else 1.0 / df[x] ** 0.5

    def sim(i, j):
        inter = sets[i] & sets[j]
        return min(1.0, sum(w(x) for x in inter)) if inter else 0.0

    # 全體基準：隨機兩條的平均相似度
    idx = [i for i in range(len(todos)) if sets[i]]
    pairs = [(i, j) for a, i in enumerate(idx) for j in idx[a + 1:]]
    base = (sum(sim(i, j) for i, j in pairs) / len(pairs)) if pairs else 0.0

    by_date = defaultdict(list)
    for i, t in enumerate(todos):
        by_date[t['date']].append(i)

    rows = []
    for d, members in by_date.items():
        if len(members) < 2:
            continue
        ps = [(i, j) for a, i in enumerate(members) for j in members[a + 1:]]
        coh = sum(sim(i, j) for i, j in ps) / len(ps)
        shared = set.intersection(*[sets[i] for i in members if sets[i]]) \
            if any(sets[i] for i in members) else set()
        rows.append((d, members, coh, shared))

    rows.sort(key=lambda r: -r[2])
    singles = sum(1 for m in by_date.values() if len(m) == 1)
    print(f'總條目 {len(todos)} | 批次 {len(by_date)} 個（{len(rows)} 個含 2+ 條，'
          f'{singles} 個單條）')
    print(f'全體基準相似度 {base:.4f} —— 批內內聚度高於此值代表該批確實是同一次 task 的產物\n')
    for d, members, coh, shared in rows[:15]:
        ratio = coh / base if base > 1e-9 else 0.0
        flag = '🔥 高內聚' if ratio >= 2.0 else ('· 中等' if ratio >= 1.0 else '  鬆散')
        print(f'{d}  {len(members):>2} 條  內聚度 {coh:.4f} ({ratio:.1f}×) {flag}'
              + (f'  ⚓ {", ".join(sorted(shared)[:2])}' if shared else ''))
        for i in members[:3]:
            print(f'     {_loc(todos[i])} {todos[i]["title"][:58]}')
        if len(members) > 3:
            print(f'     … 另 {len(members)-3} 條')
    return len(rows)


def api_dims():
    """3) 向量能力：列出可用 embedding 模型與維數，並誠實標註啟用狀態。"""
    venv_py = Path.home() / '.claude/skills/todo-audit/.venv/bin/python'
    print('目前啟用的相似度演算法：字元 3-gram Jaccard（零依賴，非向量）')
    print('向量檢索狀態：**未啟用**')
    print('  理由（實測 2026-08-08）：bge-small-zh 對 todo↔commit 配對 top-3 僅 20%,')
    print('  輸給零依賴 3-gram 的 60%。本語料主訊號是英文識別字逐字匹配，')
    print('  純中文模型會把識別字切成無意義 subword。\n')
    if not venv_py.exists():
        print('fastembed 未安裝（無 venv）。安裝後本指令會列出可用模型與維數。')
        return 0
    code = ('from fastembed import TextEmbedding\n'
            'rows=TextEmbedding.list_supported_models()\n'
            'ds=sorted({r["dim"] for r in rows})\n'
            'print("已安裝 fastembed，支援維數：", ", ".join(map(str,ds)))\n'
            'print()\n'
            'print("多語/中文可用模型：")\n'
            '[print(f"  {r[\'model\']:<56} dim={r[\'dim\']}")\n'
            ' for r in rows if any(k in r["model"].lower()\n'
            '   for k in ("multilingual","zh","m3","e5"))]\n')
    r = subprocess.run([str(venv_py), '-c', code], capture_output=True, text=True)
    print(r.stdout or r.stderr[:400])
    return 0



# ── 裁決記錄 ──────────────────────────────────────────────────────
VERDICT_CALLS = ('REMOVED', 'REWRITTEN', 'KEPT', 'DEFERRED', 'MERGED')


def record_verdicts(db_path, payload, tier='L2'):
    """把人工裁決寫進 verdict 表。

    為什麼要存：每筆裁決都是一個標註樣本（「這條確實被 commit X 做掉」）。
    實測顯示特徵層 SVM 融合要勝過不學習的 RRF，需要 30-50 條標註涵蓋
    「識別字主導」與「語義主導」兩類異質查詢——而這些標註本來就會在
    每次整理時產生，不存就白丟了。

    查找走 DB 的 todo 表而非 todo 檔：已移除的條目不在檔案裡，
    但它的裁決正是最有價值的標註。
    """
    if not Path(db_path).exists():
        sys.exit(f'FATAL: {db_path} 不存在——先跑一次完整稽核建立 DB')
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    cur = con.cursor()
    run_id = cur.execute('SELECT MAX(id) FROM run').fetchone()[0]
    now = _now()

    ok = miss = 0
    for v in payload:
        call = v['call'].upper()
        if call not in VERDICT_CALLS:
            sys.exit(f'FATAL: 未知裁決 {call}，可用：{", ".join(VERDICT_CALLS)}')
        rows = cur.execute(
            'SELECT key,title FROM todo WHERE title LIKE ?',
            (f"%{v['match']}%",)).fetchall()
        if len(rows) != 1:
            print(f'  ⚠ 跳過（比對到 {len(rows)} 筆，需恰好 1 筆）: {v["match"][:40]}')
            miss += 1
            continue
        key, title = rows[0]
        reason = v.get('reason', '')
        if v.get('evidence'):
            reason = f"{reason}｜證據: {v['evidence']}"
        cur.execute('INSERT OR REPLACE INTO verdict VALUES(?,?,?,?,?,?)',
                    (run_id, key, tier, call, reason, now))
        ok += 1
        print(f'  ✓ {call:<10} {title[:52]}')
    con.commit(); con.close()
    print(f'\n寫入 {ok} 筆，跳過 {miss} 筆（run#{run_id}）')
    return ok


def list_verdicts(db_path):
    """列出已累積的裁決 —— 這是學習型融合的訓練資料量。"""
    if not Path(db_path).exists():
        print('DB 不存在'); return 0
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    rows = con.execute(
        'SELECT v.call, COUNT(*) FROM verdict v GROUP BY v.call ORDER BY 2 DESC').fetchall()
    total = sum(n for _, n in rows)
    print(f'已累積裁決 {total} 筆')
    for call, n in rows:
        print(f'  {call:<10} {n:>4}')
    # 帶證據的才算可用標註（能對應到具體 commit）
    withev = con.execute(
        "SELECT COUNT(*) FROM verdict WHERE reason LIKE '%證據:%'").fetchone()[0]
    print(f'\n其中帶 commit 證據 {withev} 筆 —— 這些才是可用的訓練樣本')
    print(f'學習型融合門檻約 30-50 筆，目前進度 {withev}/30')
    for r in con.execute('SELECT t.title, v.call, v.reason, v.decided_at '
                         'FROM verdict v JOIN todo t ON t.key=v.todo_key '
                         'ORDER BY v.decided_at DESC LIMIT 12'):
        print(f'\n  [{r[1]}] {r[0][:62]}\n     {r[2][:88]}')
    con.close()
    return total


def _now():
    import datetime
    return datetime.datetime.now().isoformat(timespec='seconds')


def default_db(repo):
    """DB 與 todo 檔同源：~/.claude/todos/.audit/{project}.sqlite

    刻意不放在 repo 內——`git clean -fdx` 會清掉 git-ignored 路徑，
    這個專案已經真實損失過一次工作區資料。
    """
    try:
        name = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=repo,
                              capture_output=True, text=True, check=True).stdout.strip()
        name = Path(name).name
    except Exception:
        name = Path(repo).resolve().name
    d = Path.home() / '.claude' / 'todos' / '.audit'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'{name}.sqlite'


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    todo_path, repo = Path(sys.argv[1]), Path(sys.argv[2])

    # config 必須在**任何** extract_anchors() 之前載入：anchor_exts 決定錨點怎麼抽，
    # 而下面 --similar／--batch／--groups 這些早退分支也會抽錨點。載入放在這裡，
    # 是為了讓所有分支共用同一份錨點定義（見 set_anchor_exts() 的說明）。
    defaults = {'search_dirs': list(SEARCH_DIRS), 'scan_exts': list(SCAN_EXTS),
                'anchor_exts': list(ANCHOR_EXTS)}
    config, provenance, _ = todo_config.load_config(repo, defaults)
    set_anchor_exts(config['anchor_exts'])

    if '--stats' in sys.argv:
        api_stats(todo_path); return
    if '--groups' in sys.argv:
        i = sys.argv.index('--groups')
        th = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and \
            sys.argv[i + 1].isdigit() else 8
        api_groups(todo_path, th); return
    if '--verdicts' in sys.argv:
        list_verdicts(default_db(repo)); return
    if '--verdict' in sys.argv:
        raw = sys.argv[sys.argv.index('--verdict') + 1]
        data = json.loads(Path(raw).read_text(encoding='utf-8')
                          if Path(raw).exists() else raw)
        record_verdicts(default_db(repo), data); return

    if '--batch' in sys.argv:
        api_batch(todo_path); return
    if '--dims' in sys.argv:
        api_dims(); return

    if '--similar' in sys.argv:
        similar_mode(todo_path, sys.argv[sys.argv.index('--similar') + 1])
        return

    todos = load_todos(todo_path)
    all_anchors = [extract_anchors(t) for t in todos]

    # config 已在 main() 開頭載入（anchor_exts 要早於 extract_anchors），這裡直接用。
    files, by_name = collect_source_files(repo, config)
    # REQ-2：search_dirs 零命中不再 sys.exit FATAL —— 改為降級掃全 repo，
    # 並全程標記為 WEAK_AUDIT（run 級旗標，見 persist()/todo_store.state_of()）。
    degraded = not files
    if degraded:
        _warn_zero_hit_degrade(repo, config, provenance)
        files, by_name = collect_source_files(repo, dict(config, search_dirs=['']))

    symbols = {s for a in all_anchors for s in a['symbol']}
    sym_index = build_symbol_index(symbols, files)

    # ── fail loud：分流器最危險的失效模式是「工具沒跑成功卻回報零命中」，
    # 那會讓一整批仍然成立的條目被判成「載體已消失、可移除」。
    # 已知案例：rg 在某些環境是 shell function，subprocess 拿到 FileNotFoundError
    # 而被 except 吞掉 → 136 個符號全零命中 → 75 條假陽性 ALL_GONE。
    #
    # 降級（degraded）狀態下跳過這個門檻〔finding 1〕：該檢查的語意是
    # 「掃描層故障」，但降級是 search_dirs 零命中後的已知後果 —— 全 repo
    # 掃描出的檔案副檔名多半不在 scan_exts 之列（該 repo 本來就不是
    # BTSE 佈局，才會走到降級），低命中率在這裡是預期結果、已經被
    # _warn_zero_hit_degrade() 警告過，不是「故障」，用同一道門檻攔截
    # 會讓 REQ-2 驗收 1（降級後不得 FATAL）失效。
    # 這道門檻只有在「命中率」真的是一個率的時候才成立。0.30 這個值在
    # len(symbols) <= 3 時會退化成「命中數是不是 0」—— 因為 1/3 = 33% 已經高於
    # 門檻，n<=3 時唯一能觸發它的情形就是零命中。而零命中在小樣本下經常是良性的
    # （符號被改名、或符號住在 scan_exts 之外的檔案），拿它當「掃描層故障」的
    # 證據並中止整份稽核，是把雜訊當訊號。n>=4 起 1/4 = 25% 才開始能觸發，
    # 這個率到那時才有鑑別力。
    #
    # 實測兩起，同一天、兩個獨立 repo，根因都是**掃描範圍**而非掃描器故障：
    #   cast-power  0/1 = 0%  —— 純文件 repo，符號住在無副檔名的 scripts/castpower
    #   todos       1/5 = 20% —— search_dirs 排除了 tests/（見 commit 60a80b0）
    # 兩起都以 FATAL 收場、完全拿不到稽核結果；後者還得讀原始碼才找得到根因。
    #
    # 樣本太小時改走既有的 WEAK_AUDIT 降級，而不是放行：run 標記為降級後，
    # todo_store.state_of() 會把每一條的狀態蓋成 WEAK_AUDIT，假陽性的 ALL_GONE
    # 不可能被當成「可移除」—— 這道門檻原本要防的正是這件事，保護沒有被拿掉，
    # 只是換成不會癱瘓整份稽核的形式。n 夠大時 FATAL 行為完全不變。
    MIN_SYMBOLS_FOR_RATE = 4
    hit_rate = len(sym_index) / len(symbols) if symbols else 1.0
    if not degraded and symbols and hit_rate < 0.30:
        if len(symbols) < MIN_SYMBOLS_FOR_RATE:
            _warn_small_sample_degrade(repo, config, provenance,
                                       symbols, sym_index, files)
            degraded = True
        else:
            sys.exit(
                f'FATAL: 符號索引命中率僅 {hit_rate:.0%}（{len(sym_index)}/{len(symbols)}），'
                f'掃描了 {len(files)} 個檔案。\n'
                '這個比率不合理 —— 幾乎確定是掃描層故障而非「符號真的都不存在」。\n'
                '中止以免把故障誤判成「可移除」。\n'
                f'先查掃描範圍再查掃描器：目前 search_dirs = {config["search_dirs"]}，'
                f'scan_exts = {sorted(config["scan_exts"])}。\n'
                f'符號若其實存在、只是不在這個範圍內，改 {repo}/.claude/todo-audit.json 即可。'
            )

    commits = {c for a in all_anchors for c in a['commit']}
    commit_set = set()
    for c in commits:
        r = subprocess.run(['git', 'cat-file', '-e', f'{c}^{{commit}}'],
                           cwd=repo, capture_output=True)
        if r.returncode == 0:
            commit_set.add(c)

    earliest = min((t['date'] for t in todos), default='2026-01-01')
    touch_index = build_symbol_touch_index(repo, earliest, symbols)
    commit_pool = build_commit_pool(repo, earliest)

    dbp = None if '--no-db' in sys.argv else (
        Path(sys.argv[sys.argv.index('--db') + 1])
        if '--db' in sys.argv else default_db(repo))

    gone_syms = {s for a in all_anchors for s in a['symbol'] if s not in sym_index}
    never = never_existed(repo, gone_syms, dbp)
    if gone_syms:
        print(f'查無符號 {len(gone_syms)} 個，其中 {len(never)} 個從未存在於 git 歷史'
              f'（描述性稱呼，已排除為假 GONE 訊號）\n')

    results, tally = [], defaultdict(int)
    for t, a in zip(todos, all_anchors):
        checks = verify(t, a, repo, sym_index, by_name, commit_set, never)
        touches = collect_touches(a, touch_index, sym_index, t['date'])
        state = classify(checks, touches)
        tally[state] += 1
        # 攤平成「條目寫下後動過這塊碼的 commit」清單，去重後按日期排序
        cs = {c for v in touches.values() for c in v}
        results.append({
            'line': t['line'], 'title': t['title'], 'date': t['date'], 'state': state,
            'anchor_count': len(checks),
            'gone': [c['ref'] for c in checks if c['state'] == 'GONE'],
            'touched_symbols': sorted(touches),
            'commits_since': sorted(cs, key=lambda c: c[1], reverse=True)[:8],
            'likely_done_by': match_commits(t['title'], commit_pool, t['date']),
            'checks': checks,
        })

    if dbp is not None:
        run_id, changed = persist(dbp, todo_path, repo, todos, results,
                                  symbols, hit_rate, _now(), degraded=degraded)
        print(f'sqlite → {dbp}  run#{run_id}  內容有變動 {changed} 條\n')

    out = None
    if '--json' in sys.argv:
        out = Path(sys.argv[sys.argv.index('--json') + 1])
        out.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in results),
                       encoding='utf-8')

    print(f'解析 {len(todos)} 條 | 掃描 {len(files)} 檔 | '
          f'符號 {len(symbols)} 個，索引命中 {len(sym_index)} 個（{hit_rate:.0%}）\n')
    print(f'git pickaxe 窗口：{earliest} 起，{len(touch_index)}/{len(symbols)} 個符號在窗內被動過\n')
    print('條目級分流：')
    for k in ['ALL_GONE', 'PARTIAL_GONE', 'TOUCHED', 'DRIFT', 'ALIVE', 'NO_ANCHOR']:
        print(f'  {k:14s} {tally[k]:4d}')
    if out:
        print(f'\n明細 → {out}')

    strong = sorted((r for r in results if r['likely_done_by']
                     and r['likely_done_by'][0][0] >= 0.13),
                    key=lambda r: -r['likely_done_by'][0][0])
    if strong:
        print(f'\n=== 疑似已被 commit 做掉（3-gram >= 0.13）· {len(strong)} 條 ===')
        for r in strong[:25]:
            print(f"  {_loc(r)} [{r['date']}] {r['title'][:54]}")
            for sc, h, d, s in r['likely_done_by'][:2]:
                print(f"        [{sc}] {h} {d} {s[:62]}")

    for label, state in [('ALL_GONE（載體全消失）', 'ALL_GONE'),
                         ('TOUCHED（錨點在，但條目寫下後這塊碼被改過 → 最可能已過期）', 'TOUCHED')]:
        sel = [r for r in results if r['state'] == state]
        if not sel:
            continue
        print(f'\n=== {label} · {len(sel)} 條 ===')
        for r in sorted(sel, key=lambda r: -len(r['commits_since']))[:20]:
            print(f"  {_loc(r)} [{r['date']}] {r['title'][:56]}")
            if r['gone']:
                print(f"        消失: {', '.join(r['gone'][:4])}")
            for h, d, s in r['commits_since'][:3]:
                print(f"        ↳ {h} {d} {s[:66]}")


if __name__ == '__main__':
    main()
