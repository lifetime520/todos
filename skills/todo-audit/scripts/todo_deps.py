"""待辦條目間的依賴關係運算。

純函式，不匯入 sqlite3、不碰 DB —— 方便獨立單元測試，也讓
todo_store.py 之外的呼叫端不需要拉整條 DB 依賴鏈（比照 todo_flags.py
的隔離原則）。
"""

KINDS = {'blocks', 'related', 'parent-child', 'discovered-from'}


def validate_kind(kind):
    """驗證 kind 是否為合法值，未知值 raise ValueError。"""
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
    有漏洞」的情況。
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
