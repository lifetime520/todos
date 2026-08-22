"""待辦條目的交付進度位元旗標。

純函式，不匯入 sqlite3、不碰 DB —— 方便獨立單元測試，也讓
todo_store.py 之外的呼叫端（未來若有）不需要拉整條 DB 依賴鏈。
"""

FLAGS = {
    'implemented':  1 << 0,   # 實作完成
    'reviewed':     1 << 1,   # code review 完成
    'committed':    1 << 2,   # 已 commit
    'compiled':     1 << 3,   # build/編譯通過
    'tested':       1 << 4,   # 自動化測試綠燈
    'live_tested':  1 << 5,   # 實際跑起來驗證過（curl/手動操作）
    'deployed':     1 << 6,   # 已部署
}

# 固定顯示順序 —— summary() 依此排序，不受點擊順序影響，
# 因為旗標本身只記有無、不記時間先後。
ORDER = ('implemented', 'reviewed', 'committed', 'compiled',
         'tested', 'live_tested', 'deployed')

ALL_FLAGS = sum(FLAGS.values())  # 127


def _bit(name):
    try:
        return FLAGS[name]
    except KeyError:
        raise ValueError(f'unknown progress flag: {name}')


def has(progress, name):
    return bool((progress or 0) & _bit(name))


def set_(progress, name):
    return (progress or 0) | _bit(name)


def clear(progress, name):
    return (progress or 0) & ~_bit(name)


def toggle(progress, name):
    return (progress or 0) ^ _bit(name)


def is_complete(progress):
    p = progress or 0
    return (p & ALL_FLAGS) == ALL_FLAGS


def summary(progress):
    p = progress or 0
    return [name for name in ORDER if p & FLAGS[name]]
