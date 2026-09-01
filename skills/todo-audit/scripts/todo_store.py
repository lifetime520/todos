"""待辦真相來源的 sqlite 讀寫層。

唯一碰 schema 的地方。設計約束見
~/.claude/docs/specs/2026-08-08-todo-sqlite-migration-design.md
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import todo_deps
import todo_flags

# 既有六表由 todo_audit.py 的 SCHEMA 建立；此處只做增量 migration。
# 每一項都必須是 idempotent —— connect() 每次呼叫都會全跑一遍。
MIGRATIONS = [
    "ALTER TABLE todo ADD COLUMN raw_title TEXT",
    "ALTER TABLE todo ADD COLUMN section TEXT",
    "ALTER TABLE todo ADD COLUMN group_marker TEXT",
    "ALTER TABLE todo ADD COLUMN sort_order INT",
    "ALTER TABLE todo ADD COLUMN status TEXT",
    "ALTER TABLE todo ADD COLUMN status_by TEXT",
    "ALTER TABLE todo ADD COLUMN status_at TEXT",
    "ALTER TABLE todo ADD COLUMN status_note TEXT",
    "ALTER TABLE todo ADD COLUMN short_id TEXT",
    # 條目所屬的 md 章節標頭整行。兩條寫入路徑各自負責填這欄，來源不同但
    # 格式對齊：save_parsed()（:301 INSERT、:310 UPDATE）把 parse_md_lossless()
    # 解析既有 md 檔得到的標頭原文整行寫入這欄；set_section()（人工搬章節）
    # 寫入 _SECTION_HEADING 表合成出來的 canonical 整行（如 '## 🔴 緊急'）——
    # 這行不保證對應任何 md 檔裡真實存在的行，是刻意仿照原文格式構造的。
    # 兩者共同的不變式只到「完整標頭行」這一層，不到「必為某份 md 檔的
    # 原文」——_section_of() 靠符號在整行裡出現與否判斷 section，只要整行
    # 帶對符號，來源是解析還是合成都不影響判定結果，故沒有必要為人工路徑
    # 另開分支或改存裸符號。
    # section 是 heading 的有損投影（見 _HEADING_SECTION）：🟡 待辦塌成
    # normal、⚪ 觀察/技術債塌成 later，故整行原文/canonical 另存一份，
    # 供未來想印 heading 或據以重組 md 章節的人取用完整資訊。
    "ALTER TABLE todo ADD COLUMN heading TEXT",
    # 真實 md 行號，供 todo_audit.py 的報告輸出使用
    "ALTER TABLE todo ADD COLUMN md_line INT",
    # run 級品質旗標：這次稽核的 search_dirs 是否零命中而降級為全 repo 掃描。
    # 刻意不進 probe.state（見 state_of()）—— 那會讓 freshness() 的
    # `WHERE state IN ('TOUCHED','PARTIAL_GONE')` 統計靜默失真。
    "ALTER TABLE run ADD COLUMN degraded INT",
    # 交付進度位元旗標與外部參照（見
    # docs/specs/2026-08-22-todo-progress-bitmask-design.md）。
    "ALTER TABLE todo ADD COLUMN progress INT",
    "ALTER TABLE todo ADD COLUMN spec_path TEXT",
    "ALTER TABLE todo ADD COLUMN memory_ref TEXT",
]

# 完整 schema。前六張表與 todo_audit.py 的 SCHEMA 定義相同（IF NOT EXISTS，
# 兩邊誰先跑都不衝突）；後三張是本次遷移新增的。
# 全部寫在這裡是刻意的 —— store 要能獨立建出可用的 DB，否則新專案的第一次
# 寫入會因為缺 run 表而炸在 freshness()。
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


def todo_key(date, title):
    """必須與 todo_audit.py:110 完全一致 —— 改了會讓既有 probe 歷史斷鏈。"""
    return hashlib.sha1(f"{date}|{title}".encode()).hexdigest()[:16]


def connect(db_path):
    """開啟 DB 並套用所有 migration。可重複呼叫。"""
    con = sqlite3.connect(str(db_path))
    con.executescript(BASE_SCHEMA)
    for stmt in MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError as e:
            # 欄位已存在是預期的（idempotent）；其他錯誤照拋
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


# ---------------------------------------------------------------- 保真解析

_DATE_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2})\]')
_GROUP_RE = re.compile(r'^<!--\s*⚓.*-->\s*$')
_MARKER_RE = re.compile(r'^\s*>\s*(\S+)')


# 已知 marker 白名單。用白名單而非「第一個 token」，是因為 (\S+) 會把
# 自由文字的第一個詞當成 marker —— `> 說明文字沒有 marker` 會回
# '說明文字沒有'，直接污染 todo_line.marker 這個要被查詢的欄位。
# 三個真實檔剛好 100% 續行都帶 emoji，所以測試抓不到；但 add 進來的
# 條目沒有這個保證。
MARKERS = ('💡', '🔗', '🏷️', '⚠️', '✅', '🔍', '⚖️')


def line_marker(raw):
    """抽出續行的 marker。不在白名單內一律回 None —— 不猜、不預設。"""
    m = _MARKER_RE.match(raw)
    if not m:
        return None
    tok = m.group(1)
    return tok if tok in MARKERS else None


# 章節分類的**單一定義來源**。新增或調整一種 section 只改這裡。
#
# 每列是 (section, canonical 標頭整行, 可辨識的符號…)：
#   - canonical 標頭整行 → set_section() 人工搬章節時寫進 todo.heading
#   - 可辨識的符號       → _section_of() 從既有 md 標頭反推 section
#
# 兩個方向刻意放在同一列，因為它們必須成對維護。過去這是兩張獨立的表
# （_HEADING_SECTION 與 _SECTION_HEADING），新增一種 section 要記得改
# 兩處，漏一處不會有任何測試變紅 —— 分類會靜默地只在單一方向生效。
#
# ⚠️ 這兩個方向**不是互為反函數**：later 有兩個可辨識符號（⚪ 與 🟢，
# 歷史上兩種寫法都用過），但只有一個 canonical 輸出。所以這裡是
# section → (一個輸出, 多個輸入) 的多對一結構，不能用「反轉字典」實作。
#
# ⚠️ 順序有意義：_section_of() 取第一個在標頭裡出現的符號，所以這個
# tuple 的排列順序就是判定優先序。
_SECTIONS = (
    # section,    canonical 標頭整行,   可辨識的符號
    ('urgent',   '## 🔴 緊急',        ('🔴',)),
    ('decision', '## 🟠 待拍板決策',   ('🟠',)),
    ('normal',   '## 🟡 一般',        ('🟡',)),
    ('later',    '## ⚪ 之後再看',     ('⚪', '🟢')),
)

# 以下兩張查表**衍生自** _SECTIONS，不得手動維護。
# md 章節標頭 → section。標頭裡的顏色 emoji 是穩定的分類訊號。
_HEADING_SECTION = tuple(
    (sym, section)
    for section, _heading, syms in _SECTIONS
    for sym in syms
)
# section → canonical 標頭整行（供 set_section() 使用）。
_SECTION_HEADING = {section: heading for section, heading, _syms in _SECTIONS}


def _section_of(title, heading=None):
    """四類粗分類。**章節標頭優先於標題標記。**

    人把條目搬進哪個標頭底下，是比標題殘留的 [P0]/[P2] 更強的意圖表達。
    實測 tradingbot.md：只看標題會錯 19/189 —— 例如
    `P0 AI_AGENT 風控`（P0 沒有方括號）判不到 urgent，而已被人移進
    「觀察／技術債（不急）」的條目標題仍殘留 [P0]，會被判回 urgent。
    """
    if heading:
        for sym, sec in _HEADING_SECTION:
            if sym in heading:
                return sec
    if '[P0]' in title:
        return 'urgent'
    if '[Cast 拍板]' in title:
        return 'decision'
    if '[P2]' in title:
        return 'later'
    return 'normal'


def parse_md_lossless(text):
    """保真解析。與 todo_audit.py:166 的差別：body 不 strip、保留章節與註解。

    保真的祕密在 layout —— 不重建版面，而是記住版面。layout 的每個元素是
    ('raw', 原始行) 或 ('item', sort_order)；render 照著放回去即還原原檔。
    用 sort_order 而非 key 當索引，是因為同日同標題的重複條目 key 會碰撞。
    """
    lines = text.split('\n')
    items, preamble, trailer, layout = [], [], [], []
    cur = None
    pending_group = None
    seen_first_item = False
    heading = None

    for lineno, raw in enumerate(lines, 1):
        if raw.startswith('- [ ] '):
            if cur:
                items.append(cur)
            seen_first_item = True
            title = raw[6:].strip()
            m = _DATE_RE.match(title)
            date = m.group(1) if m else '1970-01-01'
            cur = {
                'raw_title': raw, 'title': title, 'date': date,
                'key': todo_key(date, title),
                'section': _section_of(title, heading),
                'heading': heading,
                'group_marker': pending_group, 'body_raw': [],
                'sort_order': len(items),
                # 真實 md 行號。todo_audit.py 有五處（:527/666/729/973/984）
                # 把它當行號印給人看，給序位會讓報告指向錯的位置。
                'line': lineno,
            }
            layout.append(('item', cur['sort_order']))
            pending_group = None
        elif cur is not None and raw.startswith('  >'):
            cur['body_raw'].append(raw)
        else:
            if cur:
                items.append(cur)
                cur = None
            if raw.startswith('#'):
                heading = raw
            if _GROUP_RE.match(raw.strip()):
                pending_group = raw.strip()
            # 所有非條目行一律原樣進 layout —— 這就是保真的全部祕密：
            # 不重建版面，而是記住版面。
            layout.append(('raw', raw))
            (preamble if not seen_first_item else trailer).append(raw)

    if cur:
        items.append(cur)
    # 不變式：每個 item 恰好進 items 一次，且指派當下的 len(items) 就是它的
    # 最終索引。曾經在這裡重編號一次，實測（20k fuzz）證明是 no-op，改成
    # 斷言表達意圖 —— 留著迴圈會讓人以為兩處編號可能對不上。
    assert all(it['sort_order'] == i for i, it in enumerate(items)), \
        'sort_order 與 items 索引不一致'
    return {'preamble': preamble, 'items': items,
            'trailer': trailer, 'layout': layout}


def render(parsed):
    """依 layout 還原原檔。條目內容取自 items，其餘行原樣放回。

    layout 用 sort_order 索引而非 key —— 同日同標題的重複條目 key 會碰撞，
    用 key 當索引會讓其中一條被覆蓋掉。
    """
    by_order = {it['sort_order']: it for it in parsed['items']}
    out = []
    for kind, val in parsed['layout']:
        if kind == 'raw':
            out.append(val)
        else:
            it = by_order.get(val)
            if it is None:
                # render 的職責是「保真還原」，不是「產出過濾後的視圖」。
                # 曾經在這裡 continue 假裝支援刪除，實測會留下孤兒
                # <!-- ⚓ ... --> group marker 與塌成 \n\n\n 的空行，
                # 每次 done/unpick 累積一次版面漂移。
                # 過濾視圖請走 write_mirror()，它自己組版不依賴 layout。
                raise KeyError(
                    f'layout 指向不存在的條目 sort_order={val} —— '
                    'render 不支援缺條目，過濾視圖請用 write_mirror()')
            out.append(it['raw_title'])
            out.extend(it['body_raw'])
    return '\n'.join(out)


def to_audit_shape(parsed):
    """轉成 todo_audit.py:166 parse_todos() 的回傳形狀，讓其餘 900 行零改動。

    差異刻意保留：legacy 的 body 是 strip 過的，此處照做。"""
    out = []
    for it in parsed['items']:
        out.append({
            # 必須是真實 md 行號，不是序位。todo_audit.py 有五處
            # （:527/666/729/973/984）直接印 L{line} 給人看，給序位等於
            # 讓報告指向錯的位置 —— 而行號在 parse 時本來就免費拿得到。
            'line': it['line'],
            'title': it['title'],
            'date': it['date'],
            'body': [b.strip() for b in it['body_raw']],
        })
    return out


# ---------------------------------------------------------------- 持久化

def save_parsed(con, project, parsed):
    """寫入條目與版面。既有 status 不被覆蓋 —— 遷移可重跑。

    同一批出現重複 key 會直接 raise，不寫入任何一列。key 是
    sha1(date|title)，所以「同日同標題」的兩條就是同一個 key —— 而
    todo.key 是 PRIMARY KEY，第二條只會 UPDATE 掉第一條，DB 只剩一列。
    parse_md_lossless()/render() 用 sort_order 索引 layout，兩條都活著
    （見 test_store.py 的 TestKeyCollision），所以損失只發生在 md→DB
    這一段，md 檔看起來完好無損，DB 卻少了一條 —— 沒有錯誤訊息、沒人
    會發現。

    這裡選擇報錯而不是「納入 sort_order 當主鍵」或「容忍重複」，是因為
    後兩者都要動 todo.key 的 PRIMARY KEY 約束，而 anchor/probe/verdict/
    todo_line/todo_dep/todo_event 六張表都拿 todo_key 當外鍵或主鍵成分，
    且「用 key 唯一定位一條」是 show/note/mark/flag 全部指令的前提。
    為一條一次性遷移路徑付這個代價不划算。

    這也與本 repo 既有的取捨一致：`init` 之外的指令找不到 DB 一律 exit 2
    並指路，不自作主張建空庫 —— 靜默的錯誤狀態比大聲失敗糟得多。
    """
    seen = {}
    for it in parsed['items']:
        if it['key'] in seen:
            raise ValueError(
                f'同一批寫入出現重複的 todo key：{it["key"]}\n'
                f'  第 1 筆：[{seen[it["key"]]["date"]}] '
                f'{seen[it["key"]]["title"]}\n'
                f'  第 2 筆：[{it["date"]}] {it["title"]}\n'
                'key = sha1(date|title)，故同日同標題必然碰撞。todo.key 是 '
                'PRIMARY KEY，硬寫下去第二筆會覆蓋第一筆、DB 只剩一列，而 md '
                '檔看起來完好 —— 這是靜默的資料遺失，所以這裡直接中止，'
                '一列都不寫。修法：把其中一條的標題或日期改成不同的值。')
        seen[it['key']] = it

    cur = con.cursor()
    for it in parsed['items']:
        prev = cur.execute('SELECT status FROM todo WHERE key=?',
                           (it['key'],)).fetchone()
        chash = hashlib.sha1('\n'.join(it['body_raw']).encode()).hexdigest()[:16]
        if prev is None:
            # progress=0 明寫在 INSERT 裡（不是留給 connect() 的 backfill
            # 補）：backfill 只在 connect() 當下跑一次，這裡新插入的列在
            # 同一個連線的後續呼叫裡不會再經過 connect()，留 NULL 會讓剛建
            # 好的條目在下一次重連前都讀不到 0。
            cur.execute(
                'INSERT INTO todo(key,date,title,content_hash,raw_title,section,'
                'group_marker,sort_order,status,heading,md_line,progress)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (it['key'], it['date'], it['title'], chash, it['raw_title'],
                 it['section'], it['group_marker'], it['sort_order'], 'pending',
                 it.get('heading'), it.get('line'), 0))
        else:
            # status 刻意不在 UPDATE 清單內：遷移重跑不得洗掉已標的 doing/done/unpick
            cur.execute(
                'UPDATE todo SET date=?,title=?,content_hash=?,raw_title=?,'
                'section=?,group_marker=?,sort_order=?,heading=?,md_line=?'
                ' WHERE key=?',
                (it['date'], it['title'], chash, it['raw_title'], it['section'],
                 it['group_marker'], it['sort_order'], it.get('heading'),
                 it.get('line'), it['key']))
            if prev[0] is None:
                cur.execute("UPDATE todo SET status='pending' WHERE key=?",
                            (it['key'],))
        cur.execute('DELETE FROM todo_line WHERE todo_key=?', (it['key'],))
        for seq, raw in enumerate(it['body_raw']):
            cur.execute('INSERT INTO todo_line VALUES(?,?,?,?)',
                        (it['key'], seq, line_marker(raw), raw))
    cur.execute('INSERT OR REPLACE INTO doc_meta VALUES(?,?,?)',
                (project, 'layout', json.dumps(parsed['layout'], ensure_ascii=False)))
    con.commit()


def load_parsed(con, project, statuses=None):
    """從 DB 重建 parse_md_lossless 的回傳形狀。

    statuses 為 None 時取全部（render 保真還原需要全部，缺條目會拋錯）；
    給定時只取該些狀態（稽核只看 pending/doing）。
    """
    row = con.execute('SELECT v FROM doc_meta WHERE project=? AND k=?',
                      (project, 'layout')).fetchone()
    layout = [tuple(x) for x in json.loads(row[0])] if row else []
    items = []
    q = ('SELECT key,date,title,raw_title,section,group_marker,sort_order,'
         'heading,md_line FROM todo WHERE sort_order IS NOT NULL')
    params = []
    if statuses:
        q += ' AND status IN (%s)' % ','.join('?' * len(statuses))
        params.extend(statuses)
    q += ' ORDER BY sort_order'
    for r in con.execute(q, params):
        key = r[0]
        body = [b[0] for b in con.execute(
            'SELECT text FROM todo_line WHERE todo_key=? ORDER BY seq', (key,))]
        items.append({'key': key, 'date': r[1], 'title': r[2], 'raw_title': r[3],
                      'section': r[4], 'group_marker': r[5], 'sort_order': r[6],
                      'heading': r[7], 'line': r[8],
                      'body_raw': body})
    # preamble / trailer 只在 parse 時供統計除錯用，render 完全不依賴它們
    # （版面全在 layout 裡）。從 DB 重建時回空 list 是正確的，不是遺漏。
    return {'preamble': [], 'items': items, 'trailer': [], 'layout': layout}


def assign_short_ids(con):
    """配發 T-NNN。只增不重用 —— 既有的不動，新的從最大值往上加。"""
    cur = con.cursor()
    used = [r[0] for r in cur.execute(
        'SELECT short_id FROM todo WHERE short_id IS NOT NULL')]
    nxt = max((int(s.split('-')[1]) for s in used), default=0) + 1
    for (key,) in cur.execute(
            'SELECT key FROM todo WHERE short_id IS NULL'
            ' AND sort_order IS NOT NULL ORDER BY sort_order').fetchall():
        cur.execute('UPDATE todo SET short_id=? WHERE key=?',
                    (f'T-{nxt:03d}', key))
        nxt += 1
    con.commit()


# ---------------------------------------------------------------- 查詢與新鮮度

class AmbiguousRef(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__(f'{len(candidates)} 筆命中')


class ClaimConflict(Exception):
    """有人正在做這一條。刻意不繼承 ValueError —— 呼叫端要能區分
    「指令參數寫錯」（自己修）與「被別人佔著」（去問人），兩者的下一步不同。"""


def freshness(con):
    """回報稽核新鮮度。所有讀取指令都必須把它印在最前面 ——
    輸出若不帶新鮮度，讀者就分不出這是現況還是快照。"""
    row = con.execute('SELECT id, started_at FROM run ORDER BY id DESC LIMIT 1').fetchone()
    if row is None:
        return {'last_run': None, 'age_hours': None, 'flagged': 0,
                'unreviewed': 0, 'stale': True}
    run_id, started = row
    age = (datetime.now() - datetime.fromisoformat(started)).total_seconds() / 3600
    flagged = con.execute(
        "SELECT COUNT(*) FROM probe WHERE run_id=? AND state IN ('TOUCHED','PARTIAL_GONE')",
        (run_id,)).fetchone()[0]
    unreviewed = con.execute(
        "SELECT COUNT(*) FROM probe p JOIN todo t ON t.key=p.todo_key"
        " WHERE p.run_id=? AND p.state IN ('TOUCHED','PARTIAL_GONE')"
        " AND NOT EXISTS (SELECT 1 FROM todo_line l"
        "                 WHERE l.todo_key=t.key AND l.marker='🔍')",
        (run_id,)).fetchone()[0]
    return {'last_run': started, 'age_hours': age, 'flagged': flagged,
            'unreviewed': unreviewed, 'stale': age > 24 * 7}


def state_of(con, key):
    """顯示層狀態。`probe.state` 一律是 `classify()` 的真值 ——
    降級（WEAK_AUDIT）記在 run 級，這裡才依 run.degraded 做顯示取代，
    不覆蓋 probe 本身，否則 freshness() 的統計會靜默失真。"""
    row = con.execute(
        'SELECT p.state, r.degraded FROM probe p JOIN run r ON r.id = p.run_id'
        ' WHERE p.todo_key=? ORDER BY p.run_id DESC LIMIT 1',
        (key,)).fetchone()
    if row is None:
        return 'NO_AUDIT'
    state, degraded = row
    return 'WEAK_AUDIT' if degraded else state


def resolve_ref(con, ref):
    """<ref> 接受 short_id（T-042）、完整 key hash、或標題唯一子字串。

    多筆命中一律拋 AmbiguousRef 並列候選 —— 不自動選第一筆。
    自動選第一筆會讓 mark/done 打在錯的條目上，而且無聲。"""
    row = con.execute('SELECT key FROM todo WHERE short_id=? OR key=?',
                      (ref, ref)).fetchone()
    if row:
        return row[0]
    hits = con.execute(
        'SELECT short_id, key, title FROM todo WHERE title LIKE ?'
        ' AND sort_order IS NOT NULL ORDER BY sort_order',
        (f'%{ref}%',)).fetchall()
    if not hits:
        raise KeyError(ref)
    if len(hits) > 1:
        raise AmbiguousRef(hits)
    return hits[0][1]


def humanize_age(iso):
    """把時間戳轉成「距今多久」。認領資訊少了這個就無法判斷死活 ——
    三天前掛著的認領（session 早沒了）與五分鐘前的，絕對值看起來一樣。"""
    if not iso:
        return '時間不明'
    try:
        delta = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return '時間不明'
    if delta < 60:
        return '剛剛'
    if delta < 3600:
        return f'{delta / 60:.0f} 分鐘前'
    if delta < 3600 * 48:
        return f'{delta / 3600:.0f} 小時前'
    return f'{delta / 86400:.0f} 天前'


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
    # 守衛條件是「當前是他人的 doing」，不是「本次要寫 doing」——
    # B 把 A 正在做的條目直接標 done 比重複認領更糟：A 還在做，
    # 條目已從清單消失，而 A 不會收到任何通知。
    if cur[0] == 'doing' and not force and (cur[1] or '') != (by or ''):
        # 用 short_id 而非內部 key —— 訊息要讓人當場對得上清單上的編號
        raise ClaimConflict(
            f'{cur[3] or key[:8]} 已被 {cur[1] or "(未署名)"} 認領於 '
            f'{cur[2] or "時間不明"}（{humanize_age(cur[2])}）。'
            f'確定要接管就加 --force —— 先確認對方 session 真的已經結束')
    old_status = cur[0]
    # pending = 無人持有。留著舊 status_by 會讓消費端把「上一個做過的人」
    # 讀成「現任擁有者」；status_at 則保留，它此時的語意是「何時釋放」。
    owner = None if status == 'pending' else by
    con.execute('UPDATE todo SET status=?, status_by=?, status_at=?, status_note=?'
                ' WHERE key=?',
                (status, owner, datetime.now().isoformat(timespec='seconds'),
                 note, key))
    _record_event(con, key, 'status', old_status, status, by)
    con.commit()


def append_item(con, project, title, tags, note):
    """新增條目。日期一律用今天 —— 呼叫端不可指定，避免偽造歷史。"""
    date = datetime.now().strftime('%Y-%m-%d')
    full_title = f'[{date}] {title}'
    key = todo_key(date, full_title)
    raw_title = f'- [ ] {full_title}'
    mx = con.execute('SELECT MAX(sort_order) FROM todo').fetchone()[0] or 0
    # progress=0 明寫在 INSERT 裡（同 save_parsed 的理由）：backfill 只在
    # connect() 當下跑一次，這裡新插入的列在同一個連線的後續呼叫裡不會
    # 再經過 connect() —— 留 NULL 而條目在同連線內被標成 done，下次
    # connect() 的 backfill 會誤判成「上線前的舊 done」而灌成 127。
    con.execute('INSERT OR REPLACE INTO todo(key,date,title,raw_title,section,'
                'sort_order,status,progress) VALUES(?,?,?,?,?,?,?,?)',
                (key, date, full_title, raw_title, _section_of(full_title),
                 mx + 1, 'pending', 0))
    con.execute('DELETE FROM todo_line WHERE todo_key=?', (key,))
    # tags / note 由呼叫端自帶 marker —— todo-add.sh 的介面是
    # `todo-add.sh "標題" "🏷️  a, b" "💡  說明"`（寫死在 ~/.claude/CLAUDE.md），
    # 這裡再補一個 emoji 會變成 `> 🏷️  🏷️  a, b`。原版 todo-add.sh:88 也是
    # 直接 `echo "  > ${TAG_LINE}"`，原樣輸出。
    # 空字串不建行，對齊原版的 `[ -n "$TAG_LINE" ] && echo`。
    body = [f'  > {t}' for t in (tags, note) if t and t.strip()]
    for seq, raw in enumerate(body):
        con.execute('INSERT INTO todo_line VALUES(?,?,?,?)',
                    (key, seq, line_marker(raw), raw))
    con.commit()
    assign_short_ids(con)
    return con.execute('SELECT short_id FROM todo WHERE key=?',
                       (key,)).fetchone()[0]


MIRROR_BANNER = ('<!-- GENERATED by todo_cli.py — 禁止手改。'
                 '真相來源：.audit/{project}.sqlite -->')


def write_mirror(con, project, path):
    """重算唯讀鏡像。只含 pending + doing。"""
    lines = [MIRROR_BANNER.format(project=project), '',
             f'# {project} Pending', '']
    for sid, key, raw, status in con.execute(
            "SELECT short_id, key, raw_title, status FROM todo"
            " WHERE sort_order IS NOT NULL AND status IN ('pending','doing')"
            " ORDER BY sort_order"):
        tag = '' if status == 'pending' else f'  ({status})'
        lines.append(f'{raw}{tag}   <!-- {sid} -->')
        for (text,) in con.execute(
                'SELECT text FROM todo_line WHERE todo_key=? ORDER BY seq', (key,)):
            lines.append(text)
        lines.append('')
    Path(path).write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------- 專案綁定

class ProjectBindingConflict(Exception):
    """同名專案在不同路徑／remote —— 不能共用同一份待辦。"""


def bind_project(con, project, project_path, git_remote):
    """首次寫入記錄綁定，之後每次寫入前比對。

    這是 md 首行 marker（`<!-- project_path: ... | git_remote: ... -->`）
    的 DB 版。Cast 2026-05-24 拍板的規則：同名專案在不同路徑時必須拒寫，
    否則 /A/tradingbot 與 /B/tradingbot 會互相污染待辦。
    md 刪掉後那道 marker 就沒了，故綁定資訊必須先搬進 DB。
    """
    cur = con.cursor()
    saved = dict(cur.execute(
        'SELECT k, v FROM doc_meta WHERE project=? AND k IN'
        " ('project_path','git_remote')", (project,)).fetchall())
    if 'project_path' not in saved:
        cur.execute('INSERT OR REPLACE INTO doc_meta VALUES(?,?,?)',
                    (project, 'project_path', project_path))
        cur.execute('INSERT OR REPLACE INTO doc_meta VALUES(?,?,?)',
                    (project, 'git_remote', git_remote))
        con.commit()
        return
    if saved['project_path'] != project_path:
        raise ProjectBindingConflict(
            f'待辦 {project} 已綁定 {saved["project_path"]}，'
            f'當前是 {project_path} —— 兩個同名專案不能共用待辦。'
            '請改名其中一個專案。')
    sr = saved.get('git_remote')
    # remote 比對只在兩邊都非空且都不是 (no-remote) 時才做，
    # 沿用 project-resolve.sh:_verify_markers 的既有寬容度
    if sr and sr != '(no-remote)' and git_remote != '(no-remote)' \
            and sr != git_remote:
        raise ProjectBindingConflict(
            f'待辦 {project} 已綁定 remote {sr}，當前是 {git_remote}。')


def append_note(con, key, text):
    """在條目 body 末尾追加一行。

    md 時代可以直接編輯條目補充 🔍 複驗記錄／⚠️ 警告（現存清單有 4 條這樣的
    記錄），遷移到 DB 後若沒有這個入口就是功能退化 —— 而複驗成果留不下來
    正是「52 條標紅條目從未被複驗」這個問題的一半原因。

    text 需自帶 marker（如 '🔍  2026-08-09 複驗：…'），與 todo-add.sh 的
    參數慣例一致。
    """
    seq = con.execute(
        'SELECT COALESCE(MAX(seq), -1) + 1 FROM todo_line WHERE todo_key=?',
        (key,)).fetchone()[0]
    raw = f'  > {text}'
    con.execute('INSERT INTO todo_line VALUES(?,?,?,?)',
                (key, seq, line_marker(raw), raw))
    con.commit()
    return seq


def search(con, pattern, include_all=False):
    """在標題與 body 全文搜尋（大小寫不敏感）。

    直接 grep todo 檔已被 hook 擋掉，而擋掉之後沒有替代的搜尋入口，
    等於逼人用 dump 再自己過濾 —— 那正是繞過新鮮度標註的誘因。
    """
    like = f'%{pattern}%'
    q = ("SELECT DISTINCT t.short_id, t.key, t.raw_title, t.status FROM todo t"
         " LEFT JOIN todo_line l ON l.todo_key = t.key"
         " WHERE t.sort_order IS NOT NULL"
         "   AND (t.title LIKE ? COLLATE NOCASE OR l.text LIKE ? COLLATE NOCASE)")
    params = [like, like]
    if not include_all:
        q += " AND t.status IN ('pending','doing')"
    q += ' ORDER BY t.sort_order'
    return con.execute(q, params).fetchall()


# ---------------------------------------------------------------- 修改與刪除

# 帶 todo_key 外鍵的所有表。改 key 或刪條目時必須全部一起處理，
# 漏一張就留下指向不存在條目的孤兒列。
_KEYED_TABLES = ('todo_line', 'anchor', 'probe', 'verdict', 'todo_event')


def edit_title(con, key, new_title):
    """改標題。回傳新的 key。

    ⚠️ key = sha1(date|title)，改標題必然換 key。若不把 todo_line / anchor /
    probe / verdict 一起搬過去，body 會消失、稽核歷史會斷鏈 —— 而且是無聲的。
    日期前綴保持原樣（日期代表「這件事什麼時候被記下來」，改標題不該竄改它）。
    """
    row = con.execute('SELECT date, heading FROM todo WHERE key=?',
                      (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    date, heading = row
    full_title = f'[{date}] {new_title}'
    new_key = todo_key(date, full_title)

    if new_key != key:
        if con.execute('SELECT 1 FROM todo WHERE key=?', (new_key,)).fetchone():
            raise ValueError(f'已有同日同標題的條目（key={new_key}）')
        for tbl in _KEYED_TABLES:
            con.execute(f'UPDATE {tbl} SET todo_key=? WHERE todo_key=?',
                        (new_key, key))
        # todo_dep 有 from_key/to_key 兩欄，不符合 _KEYED_TABLES 統一用
        # todo_key 欄名的假設，獨立處理。不遷移的話依賴圖會斷鏈，且
        # doctor 的懸空邊 WARN 會把「條目被改名」誤報成「條目被刪除」
        # （見 requirements.md Stage 2 裁決 G-6）。
        con.execute('UPDATE todo_dep SET from_key=? WHERE from_key=?',
                    (new_key, key))
        con.execute('UPDATE todo_dep SET to_key=? WHERE to_key=?',
                    (new_key, key))
        # dep_add/dep_rm 事件的 old_value/new_value 存的是 f'{kind}:{key}'，
        # 這裡的 key 是「另一側」條目的 key，不是這則事件自己的 todo_key
        # （後者已經被上面的 _KEYED_TABLES 迴圈遷移）——本條目改名前，
        # 可能是別條事件（記在別條目名下）裡嵌的那個另一側 key，所以要
        # 全表掃 dep_add/dep_rm 事件，不能只看本條目名下的列。用
        # split(':', 1) 拆開再拼回去，不用 .replace() 整段硬換——kind
        # 欄位（如 parent-child）裡沒有冒號，但全域字串取代仍有機率誤傷
        # 剛好含相同子字串的其他欄位。
        for eid, old_v, new_v in con.execute(
                "SELECT id, old_value, new_value FROM todo_event"
                " WHERE action IN ('dep_add', 'dep_rm')").fetchall():
            changed = False
            if old_v is not None:
                parts = old_v.split(':', 1)
                if len(parts) == 2 and parts[1] == key:
                    old_v = f'{parts[0]}:{new_key}'
                    changed = True
            if new_v is not None:
                parts = new_v.split(':', 1)
                if len(parts) == 2 and parts[1] == key:
                    new_v = f'{parts[0]}:{new_key}'
                    changed = True
            if changed:
                con.execute(
                    'UPDATE todo_event SET old_value=?, new_value=? WHERE id=?',
                    (old_v, new_v, eid))
    con.execute(
        'UPDATE todo SET key=?, title=?, raw_title=?, section=? WHERE key=?',
        (new_key, full_title, f'- [ ] {full_title}',
         _section_of(full_title, heading), key))
    con.commit()
    return new_key


# _SECTION_HEADING 定義在檔案上方的 _SECTIONS 區塊（與 _HEADING_SECTION
# 一起從同一個來源衍生）—— 這裡刻意不再重複定義。


def set_section(con, key, section):
    """手動搬章節（人的優先序裁示，不是稽核判定）。

    _section_of() 裡 heading 優先於標題關鍵字 —— 過去 CLI 只能靠 --title
    塞 [P2] 之類標記碰運氣，其實不生效，因為 heading 才是真正的判定依據。
    這裡直接寫 heading（連帶同步 section 欄位），list --section 才會反映：
    rows() 是直接對 section 欄位做 WHERE section=?，不會重新用
    heading 現算 _section_of()。
    """
    heading_line = _SECTION_HEADING.get(section)
    if heading_line is None:
        raise ValueError(
            f'不支援的 section：{section}（限 {"/".join(_SECTION_HEADING)}）')
    if con.execute('SELECT 1 FROM todo WHERE key=?', (key,)).fetchone() is None:
        raise KeyError(key)
    con.execute('UPDATE todo SET heading=?, section=? WHERE key=?',
                (heading_line, section, key))
    con.commit()


def edit_line(con, key, seq, text):
    """改 body 的某一行。text 需自帶 marker。"""
    row = con.execute(
        'SELECT 1 FROM todo_line WHERE todo_key=? AND seq=?', (key, seq)).fetchone()
    if row is None:
        raise IndexError(f'條目沒有第 {seq} 行')
    raw = f'  > {text}'
    con.execute('UPDATE todo_line SET text=?, marker=? WHERE todo_key=? AND seq=?',
                (raw, line_marker(raw), key, seq))
    con.commit()


def remove_line(con, key, seq):
    """刪 body 的某一行。

    刻意不重排 seq —— 讀取一律 ORDER BY seq，留洞無害；重排反而會讓
    「第 N 行」這個指稱在別人手上失效。
    """
    cur = con.execute('DELETE FROM todo_line WHERE todo_key=? AND seq=?',
                      (key, seq))
    if cur.rowcount == 0:
        raise IndexError(f'條目沒有第 {seq} 行')
    con.commit()


def remove_item(con, key):
    """真正刪除條目及其所有關聯列。

    與 mark done/unpick 不同：那兩者保留記錄，這個是抹除。
    用於誤建的條目與測試垃圾 —— 那些留著會誤導後人。
    """
    for tbl in _KEYED_TABLES:
        con.execute(f'DELETE FROM {tbl} WHERE todo_key=?', (key,))
    # todo_dep 有 from_key/to_key 兩欄，不符合 _KEYED_TABLES 統一用
    # todo_key 欄名的假設，獨立處理（比照 edit_title() 的同款寫法）——
    # 否則刪除條目會留下懸空邊，讓 is_ready()/doctor 收到使用者自己
    # 正常操作造成的假警報。
    con.execute('DELETE FROM todo_dep WHERE from_key=?', (key,))
    con.execute('DELETE FROM todo_dep WHERE to_key=?', (key,))
    con.execute('DELETE FROM todo WHERE key=?', (key,))
    con.commit()


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
    不是「有環」三個字）。重複新增同一條邊視為錯誤——與 remove_dep/
    remove_line 等既有函式對「找不到」的處理方式對稱，不靜默吞掉
    （見 requirements.md Stage 2 裁決 G-5：靜默 no-op 會讓「打錯 kind
    後補一次正確呼叫」跟「單純重跑」混為一談）。
    """
    todo_deps.validate_kind(kind)
    for k in (from_key, to_key):
        if not _key_exists(con, k):
            raise KeyError(k)
    if kind in ('blocks', 'parent-child'):
        # G-3：只用同一種 kind 的既有邊做 DFS —— blocks 與 parent-child
        # 分開各自成圖，不合併，避免合法的「父任務被子任務的 blocks
        # 邊卡住」被誤判成死鎖。
        existing = con.execute(
            'SELECT from_key, to_key FROM todo_dep WHERE kind=?',
            (kind,)).fetchall()
        cycle = todo_deps.find_cycle(existing, from_key, to_key)
        if cycle:
            names = ' → '.join(
                con.execute('SELECT short_id FROM todo WHERE key=?',
                            (k,)).fetchone()[0] for k in cycle)
            raise ValueError(f'會造成環狀依賴：{names}')
    try:
        con.execute(
            'INSERT INTO todo_dep(from_key,to_key,kind,created_at,created_by)'
            ' VALUES(?,?,?,?,?)',
            (from_key, to_key, kind,
             datetime.now().isoformat(timespec='seconds'), by))
    except sqlite3.IntegrityError:
        # 訊息要讓人當場對得上清單上的編號，不能印內部 sha1 key——
        # 比照上面環狀依賴分支與 list_deps() 的既有寫法轉成 short_id，
        # 查無 short_id 時 fallback 用 key[:8]。
        from_sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                               (from_key,)).fetchone()
        to_sid = con.execute('SELECT short_id FROM todo WHERE key=?',
                             (to_key,)).fetchone()
        raise ValueError(
            f'{from_sid[0] if from_sid else from_key[:8]} -{kind}-> '
            f'{to_sid[0] if to_sid else to_key[:8]} 已存在')
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
    # blocker 可能已被 remove_item() 刪除（懸空邊）——查無此列時 status 視為
    # None，交給 todo_deps.is_ready() 判斷（那邊已正確處理 None：
    # 已刪除的 blocker 不算 done/unpick，條目仍算被卡住）。
    blocker_statuses = []
    for b in blockers:
        r = con.execute('SELECT status FROM todo WHERE key=?', (b,)).fetchone()
        blocker_statuses.append(r[0] if r else None)
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
