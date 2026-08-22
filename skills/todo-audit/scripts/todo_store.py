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
    # 條目所屬的 md 章節標頭原文。section 是它的有損投影
    # （🟡 待辦 與 ⚪ 觀察/技術債 都塌成 normal），故原文另存一份。
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


# md 章節標頭 → section。標頭裡的顏色 emoji 是穩定的分類訊號。
_HEADING_SECTION = (
    ('🔴', 'urgent'),
    ('🟠', 'decision'),
    ('🟡', 'normal'),
    ('⚪', 'later'),
    ('🟢', 'later'),
)


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
    """寫入條目與版面。既有 status 不被覆蓋 —— 遷移可重跑。"""
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
    # pending = 無人持有。留著舊 status_by 會讓消費端把「上一個做過的人」
    # 讀成「現任擁有者」；status_at 則保留，它此時的語意是「何時釋放」。
    owner = None if status == 'pending' else by
    con.execute('UPDATE todo SET status=?, status_by=?, status_at=?, status_note=?'
                ' WHERE key=?',
                (status, owner, datetime.now().isoformat(timespec='seconds'),
                 note, key))
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
_KEYED_TABLES = ('todo_line', 'anchor', 'probe', 'verdict')


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
    con.execute(
        'UPDATE todo SET key=?, title=?, raw_title=?, section=? WHERE key=?',
        (new_key, full_title, f'- [ ] {full_title}',
         _section_of(full_title, heading), key))
    con.commit()
    return new_key


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
