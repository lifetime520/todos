"""三層 config 合併：builtin < user-global < per-repo（高者勝）。

參考 remember plugin 的 lib-memory-dir.sh 三層合併模式，讓 todo-audit 的
掃描範圍在 BTSE 以外的專案也能設定，而不是永遠沿用硬編碼的 Gradle 模組路徑。

- per-repo config：`<repo_root>/.claude/todo-audit.json`
- user-global config：`<home>/.claude/todo-audit.json`
- 兩者皆缺時，整份沿用呼叫端傳入的 `defaults`（BTSE 既有常數）—— 本檔案
  刻意不重複硬編一份預設值，避免與 todo_audit.py 的 SEARCH_DIRS/SCAN_EXTS
  兩處漂移。

deep merge：以鍵為單位覆寫，不是整份 dict 取代 —— 只設 search_dirs 時，
scan_exts 仍取上一層的值。

非法 JSON 只讓「該層」失效，其餘層照常合併（該層當作不存在處理）；
呼叫端會看到一則印到 stderr 的警告，指出是哪個檔案壞掉。

`_` 前綴的鍵是給人看的文件（如 `_comment`），merge 時一律剔除，不進 runtime 值。
"""
import json
import sys
from pathlib import Path

CONFIG_RELPATH = Path('.claude') / 'todo-audit.json'


def config_paths(repo_root, home=None):
    """回傳 (per-repo path, user-global path) 這兩個固定位置，不論是否存在。

    集中在這裡是刻意的：load_config() 與呼叫端（例如零命中警告訊息判斷
    「config 檔到底存不存在」）都要用同一份路徑解析，避免兩處各自猜一次
    home 目錄而彼此漂移。
    """
    home = Path(home) if home is not None else Path.home()
    return Path(repo_root) / CONFIG_RELPATH, home / CONFIG_RELPATH


def _load_layer(path):
    """讀單一層。回傳該層的 dict（已剔除 `_` 前綴鍵），或 None（檔案不存在／非法內容）。

    非法內容印出警告（含檔案路徑），但不拋例外 —— 該層當作不存在，
    其餘層照常合併，不得因為一層壞掉就連帶忽略別層。
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f'WARN: 設定檔 {path} 不是合法 JSON（{e}），已忽略此層，其餘層照常生效',
              file=sys.stderr)
        return None
    if not isinstance(raw, dict):
        print(f'WARN: 設定檔 {path} 內容須是 JSON object，已忽略此層', file=sys.stderr)
        return None
    return {k: v for k, v in raw.items() if not k.startswith('_')}


def load_config(repo_root, defaults, home=None):
    """三層 deep merge，回傳 (config, provenance)。

    - config：合併後的最終值，鍵集合與 `defaults` 相同
    - provenance：每個鍵最終由哪一層決定 —— 'builtin' / 'user-global' / 'per-repo'
      （逐字對應 requirements.md 的層級命名），供 `doctor` 直接消費，
      不必自行重做一次三層探測
    """
    per_repo_path, user_global_path = config_paths(repo_root, home)

    config = dict(defaults)
    provenance = {k: 'builtin' for k in defaults}

    for layer_name, path in (('user-global', user_global_path),
                             ('per-repo', per_repo_path)):
        layer = _load_layer(path)
        if not layer:
            continue
        for k, v in layer.items():
            config[k] = v
            provenance[k] = layer_name

    return config, provenance


# 兩份呼叫端各自加前綴格式化輸出範例 config 用的示範值 —— 逐字一致，
# 供 zero_hit_diagnosis() 回傳，避免兩處各自寫一份範例字串而漂移。
EXAMPLE_CONFIG = {'search_dirs': ['src', 'scripts'], 'scan_exts': ['.py']}


def zero_hit_diagnosis(repo_root, config, provenance, home=None):
    """G-2 判定規則：search_dirs 零命中時，區分「未設定」與「設定但零命中」
    兩種前因，回傳結構化資訊。

    這段規則被 `todo_audit.py`（降級警告）與 `todo_cli.py`（doctor）兩處
    消費 —— 判定邏輯只准在這裡實作一次，兩邊各自加前綴、格式化成自己的
    輸出風格，不准各自重做一份判定〔finding 3〕。

    回傳 dict：
      - configured: bool，per-repo 或 user-global 任一 config 檔是否存在
      - search_dirs: 目前生效的 search_dirs 值
      - source: search_dirs 的 provenance（'builtin'/'user-global'/'per-repo'）
      - per_repo_path / user_global_path: 兩個固定位置（Path），不論是否存在
      - example: 建議的範例 config 內容（dict，呼叫端自行序列化成字串）
    """
    per_repo_path, user_global_path = config_paths(repo_root, home)
    configured = per_repo_path.exists() or user_global_path.exists()
    return {
        'configured': configured,
        'search_dirs': config['search_dirs'],
        'source': provenance.get('search_dirs', 'builtin'),
        'per_repo_path': per_repo_path,
        'user_global_path': user_global_path,
        'example': EXAMPLE_CONFIG,
    }
