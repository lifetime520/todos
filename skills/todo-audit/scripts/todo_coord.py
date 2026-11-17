"""多 session 協作：角色、題目擁有者、裁決紀錄、看板（協定 v2，2026-11-14）。

為什麼有這個模組：todo 認領的單位是「條目」，但派工實際上走對話。結果
「這題誰在做」只存在各 session 的記憶裡，轉達者與執行者會同時做同一題
（S9b-F 資金費，#11／#13，2026-11-13）。這裡把「誰負責什麼」、「Cast 裁決過
什麼」落進共用 DB，讓任何 session 一條指令就查得到。

存活判斷不另存：直接讀 Claude Code 自己維護的 ~/.claude/sessions/<pid>.json，
再用 /proc/<pid> 確認程序還在。表結構定義在 todo_store.BASE_SCHEMA。
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path.home() / '.claude' / 'sessions'

ROLES = {
    'window': '窗口',
    'research': '研究',
    'impl': '實作',
    'improve': '改進計劃',
    'general': '一般',
}

_TITLE_SEP = '｜'


def _now():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def short_name(name):
    """「tradingbot #3｜實作｜←#13」→「tradingbot #3」。角色寫在標題後綴，
    改名不改前綴，因此角色表以前綴為鍵。"""
    return (name or '').split(_TITLE_SEP)[0].strip()


# ---------------------------------------------------------------- 角色

def role_set(con, name, role, reports_to=None):
    if role not in ROLES:
        raise ValueError(f'未知角色：{role}（可用：{"、".join(ROLES)}）')
    con.execute(
        'INSERT INTO coord_role(name, role, reports_to, updated_at) VALUES(?,?,?,?)'
        ' ON CONFLICT(name) DO UPDATE SET role=excluded.role,'
        ' reports_to=excluded.reports_to, updated_at=excluded.updated_at',
        (short_name(name), role, short_name(reports_to) or None, _now()))
    con.commit()


def role_get(con, name):
    r = con.execute('SELECT role, reports_to FROM coord_role WHERE name=?',
                    (short_name(name),)).fetchone()
    return {'role': r[0], 'reports_to': r[1]} if r else None


def window_name(con):
    r = con.execute("SELECT name FROM coord_role WHERE role='window'"
                    ' ORDER BY updated_at DESC LIMIT 1').fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- 題目擁有者

# 沒有待辦編號的紀錄無法跟著待辦結案自動釋放，只能靠到期：超過這個時數沒有再
# owner set 就不再列出（owner list --all 仍看得到）。還在做就重新 set 一次。
OWNER_NOID_TTL_H = 48

_ID_RE = re.compile(r'T-\d+')
_LEAD_ID_RE = re.compile(r'\s*(T-\d+)(?=\s|$)')


def _norm_ids(todo_ids, topic):
    """回傳正規化的編號字串（「T-1, T-2」），沒有編號回 None。
    未帶 todo_ids 時取題目開頭的 T-NNN —— 歸屬能否隨待辦結案自動釋放，
    全看這個欄位有沒有值。"""
    ids = _ID_RE.findall(todo_ids or '')
    if not ids:
        m = _LEAD_ID_RE.match(topic or '')
        ids = [m.group(1)] if m else []
    seen = []
    for x in ids:
        if x not in seen:
            seen.append(x)
    return ', '.join(seen) or None


def owner_set(con, topic, owner, role=None, todo_ids=None, by=None):
    """登記題目擁有者。同一組待辦編號只留一筆：題目文字換了寫法視為同一題，
    舊紀錄在同一交易內刪除（T-990 曾因文字不同登記兩筆）。"""
    topic = topic.strip()
    ids = _norm_ids(todo_ids, topic)
    if ids:
        want = set(ids.split(', '))
        for t, other in con.execute(
                'SELECT topic, todo_ids FROM coord_owner WHERE topic<>? AND todo_ids IS NOT NULL',
                (topic,)).fetchall():
            if set(_ID_RE.findall(other)) == want:
                con.execute('DELETE FROM coord_owner WHERE topic=?', (t,))
    con.execute(
        'INSERT INTO coord_owner(topic, owner, role, todo_ids, updated_at, updated_by)'
        ' VALUES(?,?,?,?,?,?) ON CONFLICT(topic) DO UPDATE SET owner=excluded.owner,'
        ' role=excluded.role, todo_ids=excluded.todo_ids,'
        ' updated_at=excluded.updated_at, updated_by=excluded.updated_by',
        (topic, short_name(owner), role, ids, _now(), by))
    con.commit()


def _expired(row, now=None):
    if row['todo_ids']:
        return False
    try:
        age = ((now or datetime.now()) - datetime.fromisoformat(row['updated_at'])).total_seconds()
    except (TypeError, ValueError):
        return True  # 時間壞掉的無編號紀錄沒有任何依據說它還在做
    return age > OWNER_NOID_TTL_H * 3600


def owner_who(con, keyword, include_expired=False):
    kw = f'%{keyword.strip().lower()}%'
    cur = con.execute(
        'SELECT topic, owner, role, todo_ids, updated_at, updated_by FROM coord_owner'
        " WHERE lower(topic) LIKE ? OR lower(coalesce(todo_ids,'')) LIKE ?"
        ' ORDER BY updated_at DESC', (kw, kw))
    keys = ('topic', 'owner', 'role', 'todo_ids', 'updated_at', 'updated_by')
    out = []
    for r in cur.fetchall():
        d = dict(zip(keys, r))
        d['expired'] = _expired(d)
        if include_expired or not d['expired']:
            out.append(d)
    return out


def owner_list(con, include_expired=True):
    """完整登記。預設含過期紀錄（帶 expired 旗標、CLI 會標 ⌛）：castpower 徵用閘門
    解析這份輸出，錯誤方向只能往「列出」偏，過期只在 who 與看板隱藏。"""
    return owner_who(con, '', include_expired=include_expired)


def owner_rm(con, topic):
    n = con.execute('DELETE FROM coord_owner WHERE topic=?', (topic.strip(),)).rowcount
    con.commit()
    return n > 0


def owner_prune(con, dry_run=False):
    """清掉自動釋放上線前留下的殘留：待辦已結案的編號從紀錄拿掉（拿光就刪），
    同一組編號的重複紀錄只留最新一筆，未帶 todo_ids 的紀錄補上題目開頭的編號。
    回傳每個動作的說明。冪等。dry_run 時整批 rollback，只回報會做什麼。"""
    msgs = []
    closed = {r[0] for r in con.execute(
        "SELECT short_id FROM todo WHERE status IN ('done','unpick') AND short_id IS NOT NULL")}
    rows = con.execute('SELECT topic, todo_ids FROM coord_owner'
                       ' ORDER BY updated_at DESC').fetchall()
    seen = {}
    for topic, ids in rows:
        norm = _norm_ids(ids, topic)
        if norm != ids:
            con.execute('UPDATE coord_owner SET todo_ids=? WHERE topic=?', (norm, topic))
            msgs.append(f'補編號：{topic} → {norm}')
        if not norm:
            continue
        parts = norm.split(', ')
        rest = [x for x in parts if x not in closed]
        if not rest:
            con.execute('DELETE FROM coord_owner WHERE topic=?', (topic,))
            msgs.append(f'刪除（待辦已結案）：{topic}')
            continue
        if rest != parts:
            norm = ', '.join(rest)
            con.execute('UPDATE coord_owner SET todo_ids=? WHERE topic=?', (norm, topic))
            msgs.append(f'拿掉已結案編號：{topic} → {norm}')
        k = frozenset(rest)
        if k in seen:
            con.execute('DELETE FROM coord_owner WHERE topic=?', (topic,))
            msgs.append(f'刪除重複（保留「{seen[k]}」）：{topic}')
        else:
            seen[k] = topic
    if dry_run:
        con.rollback()
    else:
        con.commit()
    return msgs or ['沒有需要清理的紀錄。']


# ---------------------------------------------------------------- 孤兒認領

WS_ROOT = Path(os.environ.get('TODO_WS_ROOT') or Path.home() / 'WorkSpace' / 'ws')


def _ws_exists(root, name, project=None):
    """session-ws 的工作區目錄是 <專案>-<名稱>。知道專案就精確比對；不知道才退回
    後綴比對（會把 ws:t2 誤認成 tradingbot-coinm-t2 —— 寧可少報孤兒，不誤報）。"""
    if (root / name).is_dir():
        return True
    if project:
        return (root / f'{project}-{name}').is_dir()
    return any(p.is_dir() for p in root.glob(f'*-{name}'))


def orphan_claims(con, ws_root=None):
    """doing 認領者是 ws:<名稱>，但工作區目錄已不存在的條目。只列出、不改狀態：
    工作區刪掉不代表沒在等驗收（實作完、等部署後驗收的 doing 也長這樣），
    所以附上最後活動時間（狀態變更或 note），交給人判斷。"""
    root = Path(ws_root or WS_ROOT)
    if not root.is_dir():
        return []  # 這台機器沒有工作區根目錄：無從判斷，不能把每條都報成孤兒
    pp = project_path_of(con)
    project = Path(pp).name if pp else None
    out = []
    for key, sid, title, by, at in con.execute(
            "SELECT key, short_id, title, status_by, status_at FROM todo"
            " WHERE status='doing' AND status_by LIKE 'ws:%' ORDER BY sort_order").fetchall():
        name = by[3:]
        if not name or _ws_exists(root, name, project):
            continue
        last = con.execute('SELECT MAX(at) FROM todo_event WHERE todo_key=?', (key,)).fetchone()[0]
        out.append({'short_id': sid, 'title': title, 'by': by, 'claimed_at': at,
                    'last_activity': max(filter(None, (last, at)), default=None)})
    return out


# ---------------------------------------------------------------- 收件匣（B 案，D-048）

# peer 不需窗口回應的回報（進度、已照辦、PASS 通知）寫這裡，窗口被任何理由喚醒時
# 由 window-inbox.sh 一次注入。實測（#13 transcript 11-06～11-16）：153 段 peer 喚醒中
# 「讀完沒回」＋「只回收到」占 31% cache read。積壓超過這個分鐘數、且這段時間內沒彙整過，
# 下一則 post 會要求寄件者另送一則【彙整】喚醒窗口（系統排程無法送 SendMessage）。
INBOX_FLUSH_MIN = 60
INBOX_MAX_CHARS = 2000  # 單則上限；長內容寫檔後附路徑
_TS = '%Y-%m-%dT%H:%M:%S'


def _minutes(a, b):
    return int((datetime.strptime(b, _TS) - datetime.strptime(a, _TS)).total_seconds() // 60)


def inbox_post(con, body, frm, to=None, todo_ids=None, now=None):
    body = (body or '').strip()
    if not body:
        raise ValueError('內容不得為空')
    if len(body) > INBOX_MAX_CHARS:
        raise ValueError(f'內容 {len(body)} 字，超過單則上限 {INBOX_MAX_CHARS} 字；長內容請寫檔後附路徑')
    to = short_name(to) if to else window_name(con)
    if not to:
        raise ValueError('未設定窗口 session，請以 --to 指定收件者')
    now = now or _now()
    con.execute('INSERT INTO coord_inbox(to_name, from_name, body, todo_ids, created_at)'
                ' VALUES(?,?,?,?,?)', (to, short_name(frm) or None, body, todo_ids, now))
    unread, oldest = con.execute('SELECT COUNT(*), MIN(created_at) FROM coord_inbox'
                                 ' WHERE to_name=? AND read_at IS NULL', (to,)).fetchone()
    last = con.execute('SELECT flushed_at FROM coord_inbox_flush WHERE to_name=?', (to,)).fetchone()
    oldest_min = _minutes(oldest, now)
    flush = (oldest_min >= INBOX_FLUSH_MIN
             and (not last or _minutes(last[0], now) >= INBOX_FLUSH_MIN))
    if flush:
        con.execute('INSERT OR REPLACE INTO coord_inbox_flush VALUES(?,?)', (to, now))
    con.commit()
    return {'to': to, 'unread': unread, 'oldest_min': oldest_min, 'flush': flush}


_INBOX_KEYS = ('id', 'from_name', 'body', 'todo_ids', 'created_at')


def inbox_take_unread(con, to, max_chars=3000, mark=True):
    """取未讀（舊到新），總字數不超過 max_chars（至少一則）。mark=True 時取到的標為已讀；
    hook 用 mark=False，先印出注入內容再呼叫 inbox_mark_read，被中斷時寧可重複注入也不遺失。"""
    rows = con.execute(f'SELECT {", ".join(_INBOX_KEYS)} FROM coord_inbox'
                       ' WHERE to_name=? AND read_at IS NULL ORDER BY id',
                       (short_name(to),)).fetchall()
    out, used = [], 0
    for r in rows:
        if out and used + len(r[2]) > max_chars:
            break
        out.append(dict(zip(_INBOX_KEYS, r)))
        used += len(r[2])
    if out and mark:
        inbox_mark_read(con, [i['id'] for i in out])
    return out


def inbox_mark_read(con, ids):
    con.executemany('UPDATE coord_inbox SET read_at=? WHERE id=? AND read_at IS NULL',
                    [(_now(), i) for i in ids])
    con.commit()


def inbox_list(con, to=None, unread_only=False, limit=50):
    q = f'SELECT {", ".join(_INBOX_KEYS)}, to_name, read_at FROM coord_inbox WHERE 1=1'
    args = []
    if to:
        q += ' AND to_name=?'
        args.append(short_name(to))
    if unread_only:
        q += ' AND read_at IS NULL'
    q += ' ORDER BY id DESC LIMIT ?'
    args.append(limit)
    return [dict(zip(_INBOX_KEYS + ('to_name', 'read_at'), r)) for r in con.execute(q, args)]


_UDS_PID_RE = re.compile(r'^uds:.*/(\d+)\.sock$')
_REF_RE = re.compile(r'\s*\[[^\]]*\]\s*$')


def resolve_target(to, sessions_dir=None):
    """SendMessage 的 to（名稱、帶 [ref] 的名稱、uds 位址）→ session 短名；解析不到回 None。"""
    to = (to or '').strip()
    if not to:
        return None
    m = _UDS_PID_RE.match(to)
    if m:
        f = Path(sessions_dir or SESSIONS_DIR) / f'{m.group(1)}.json'
        try:
            return short_name(json.loads(f.read_text()).get('name')) or None
        except (OSError, ValueError, AttributeError):
            return None
    return short_name(_REF_RE.sub('', to)) or None


# ---------------------------------------------------------------- 裁決

_DEC_KEYS = ('id', 'decided_at', 'topic', 'cast_quote', 'summary',
             'supersedes', 'asked_by', 'status')


def _next_decision_id(con):
    nums = [int(r[0][2:]) for r in con.execute('SELECT id FROM coord_decision')
            if re.fullmatch(r'D-\d+', r[0])]
    return f'D-{(max(nums) if nums else 0) + 1:03d}'


def decision_add(con, topic, quote, summary=None, supersedes=None,
                 asked_by=None, decided_at=None, id=None):
    """新增一條裁決；supersedes 指定的舊條同一交易內改成 superseded。
    舊條不存在就整筆拒絕 —— 寫進一條「取代了不存在的東西」的裁決，比沒寫更糟。"""
    # 取號、取代、寫入放在同一個 IMMEDIATE 交易：兩個 session 同時寫時，
    # 後到者等鎖，不會撞號，也不會把同一條舊裁決取代兩次（兩條互相衝突的
    # 裁決同時 active 正是協定要消除的事）。
    con.commit()
    con.execute('BEGIN IMMEDIATE')
    try:
        did = id or _next_decision_id(con)
        if supersedes:
            n = con.execute("UPDATE coord_decision SET status='superseded'"
                            " WHERE id=? AND status='active'", (supersedes,)).rowcount
            if n != 1:
                raise ValueError(f'要取代的裁決不存在或已被取代：{supersedes}')
        con.execute(
            'INSERT INTO coord_decision(id, decided_at, topic, cast_quote, summary,'
            " supersedes, asked_by, status) VALUES(?,?,?,?,?,?,?,'active')",
            (did, decided_at or _now(), topic.strip(), quote, summary,
             supersedes, asked_by))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return did


def decision_list(con, topic=None, include_all=False):
    q = f'SELECT {", ".join(_DEC_KEYS)} FROM coord_decision WHERE 1=1'
    params = []
    if not include_all:
        q += " AND status='active'"
    if topic:
        q += ' AND (lower(topic) LIKE ? OR lower(cast_quote) LIKE ?'
        q += " OR lower(coalesce(summary,'')) LIKE ?)"
        kw = f'%{topic.strip().lower()}%'
        params += [kw, kw, kw]
    q += ' ORDER BY id'
    return [dict(zip(_DEC_KEYS, r)) for r in con.execute(q, params).fetchall()]


def _terms(text):
    """抽比對用的詞：英數連續字串（≥3）與中文二字片段。不靠任何語意模型。"""
    text = (text or '').lower()
    terms = set(re.findall(r'[a-z0-9][a-z0-9_\-.]{2,}', text))
    for run in re.findall(r'[一-鿿]{2,}', text):
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def decision_match(con, text, limit=3):
    """與 text 共用詞最多的 active 裁決（至少命中 2 個詞），依命中數排序。"""
    want = _terms(text)
    scored = []
    for d in decision_list(con):
        hit = len(want & _terms(' '.join(filter(None, (d['topic'], d['cast_quote'],
                                                       d['summary'])))))
        if hit >= 2:
            scored.append((hit, d))
    scored.sort(key=lambda x: (-x[0], x[1]['id']))
    return [d for _, d in scored[:limit]]


# ---------------------------------------------------------------- session 與看板

def _alive(pid):
    return Path(f'/proc/{pid}').exists()


def _in_project(cwd, project_path):
    if not project_path:
        return True
    cwd = cwd or ''
    name = Path(project_path).name
    return (cwd == project_path or cwd.startswith(project_path.rstrip('/') + '/')
            or f'/WorkSpace/ws/{name}-' in cwd)


def live_sessions(sessions_dir=None, project_path=None):
    out = []
    for f in sorted(Path(sessions_dir or SESSIONS_DIR).glob('*.json')):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(d.get('pid'), int) and _alive(d['pid'])
                and _in_project(d.get('cwd'), project_path)):
            out.append(d)
    return out


def name_for_session_id(session_id, sessions_dir=None):
    for d in live_sessions(sessions_dir):
        if d.get('sessionId') == session_id:
            return d.get('name')
    return None


def name_for_current_process(sessions_dir=None):
    """沿父程序往上找，找到第一個有 sessions/<pid>.json 的就是自己所屬的 session。"""
    sdir = Path(sessions_dir or SESSIONS_DIR)
    pid = os.getpid()
    for _ in range(12):
        f = sdir / f'{pid}.json'
        if f.exists():
            try:
                return json.loads(f.read_text()).get('name')
            except (OSError, ValueError):
                return None
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
            pid = int(stat.rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if pid <= 1:
            return None
    return None


def project_path_of(con):
    r = con.execute("SELECT v FROM doc_meta WHERE k='project_path' LIMIT 1").fetchone()
    return r[0] if r else None


def board_rows(con, sessions_dir=None):
    owners = {}
    for o in owner_list(con, include_expired=False):
        owners.setdefault(o['owner'], []).append(o['topic'])
    rows = []
    for s in live_sessions(sessions_dir, project_path_of(con)):
        full = s.get('name') or '(未命名)'
        sn = short_name(full)
        r = role_get(con, sn)
        role = ROLES.get(r['role']) if r else None
        warn = None
        if not r:
            warn = '未指派角色'
        elif _TITLE_SEP not in full:
            warn = f'標題缺角色，應改為「{sn}{_TITLE_SEP}{role}」'
        rows.append({'name': sn, 'title': full, 'role': role,
                     'reports_to': r['reports_to'] if r else None,
                     'status': s.get('status'), 'topics': owners.get(sn, []),
                     'warn': warn})
    rows.sort(key=lambda x: x['name'])
    return rows


def board_text(con, sessions_dir=None, max_chars=600):
    win = window_name(con)
    lines = ['【誰在做什麼】' + (f'設計／決策問題送 {win}' if win else '⚠ 未設定窗口 session')]
    for r in board_rows(con, sessions_dir):
        parts = [r['name'], r['role'] or '?']
        if r['reports_to']:
            parts.append(f'←{r["reports_to"]}')
        if r['topics']:
            parts.append('負責：' + '、'.join(r['topics']))
        line = '・' + ' ｜ '.join(parts)
        if r['warn']:
            line += f' ⚠ {r["warn"]}'
        lines.append(line)
    orphans = orphan_claims(con)
    if orphans:  # 放在標題後：看板超長會從尾端截斷
        lines.insert(1, f'⚠ 孤兒認領 {len(orphans)} 條（工作區已不存在）：'
                     + '、'.join(o['short_id'] for o in orphans[:6])
                     + ('…' if len(orphans) > 6 else '') + '（todo_cli.py orphans）')
    text = '\n'.join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 1] + '…'
    return text
