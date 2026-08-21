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

# Stage 7 第四輪裁決（2026-08-21）：只對這兩個鍵做型別檢查，值必須是
# list[str]。不建完整 schema 驗證框架 —— 其他未知鍵不受此規則限制，
# 那是既有的「未知鍵靜默併入」行為，不在本次修復範圍。
TYPED_LIST_KEYS = ('search_dirs', 'scan_exts')


def config_paths(repo_root, home=None):
    """回傳 (per-repo path, user-global path) 這兩個固定位置，不論是否存在。

    集中在這裡是刻意的：load_config() 與呼叫端（例如零命中警告訊息判斷
    「config 檔到底存不存在」）都要用同一份路徑解析，避免兩處各自猜一次
    home 目錄而彼此漂移。
    """
    home = Path(home) if home is not None else Path.home()
    return Path(repo_root) / CONFIG_RELPATH, home / CONFIG_RELPATH


def _typed_key_error(layer):
    """檢查 TYPED_LIST_KEYS 內的鍵（若存在於這一層）是否為 list[str]。

    回傳 (key, value) 描述第一個型別錯誤的鍵；全部合法則回傳 None。
    只驗 search_dirs/scan_exts 這兩個鍵 —— 其他未知鍵不受此規則限制，
    那是既有的「未知鍵靜默併入」行為，這裡刻意不動〔Stage 7 第四輪裁決〕。
    """
    for key in TYPED_LIST_KEYS:
        if key not in layer:
            continue
        v = layer[key]
        if not isinstance(v, list) or not all(isinstance(item, str) for item in v):
            return key, v
    return None


def _load_layer(path):
    """讀單一層。回傳 (layer, warning)：

    - layer：該層的 dict（已剔除 `_` 前綴鍵），或 None（檔案不存在／非法內容）
    - warning：None（無異狀），或人類可讀的一行字串，描述這一層為何被忽略
      （含檔案路徑與原因）——給呼叫端往上傳遞用，不需要重新解析 stderr。

    非法內容仍印出警告（含檔案路徑）到 stderr，但不拋例外 —— 該層當作
    不存在，其餘層照常合併，不得因為一層壞掉就連帶忽略別層。

    「非法內容」涵蓋兩種情況，處理方式統一（都是印 WARN + 回 (None, warning)，
    呼叫端 load_config() 不需要知道是哪一種）：
    - JSON 語法錯誤（原有邏輯）
    - search_dirs/scan_exts 型別錯誤（Stage 7 第四輪裁決新增）：值必須是
      list[str]，否則實跑會在下游炸 TypeError（數字）或被當可迭代物
      逐字元展開（字串），且後者不 crash、doctor 卻誤報 OK——比 crash
      更危險的靜默失效。
    """
    if not path.exists():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        warning = f'設定檔 {path} 不是合法 JSON（{e}），已忽略此層，其餘層照常生效'
        print(f'WARN: {warning}', file=sys.stderr)
        return None, warning
    if not isinstance(raw, dict):
        warning = f'設定檔 {path} 內容須是 JSON object，已忽略此層'
        print(f'WARN: {warning}', file=sys.stderr)
        return None, warning
    layer = {k: v for k, v in raw.items() if not k.startswith('_')}
    type_error = _typed_key_error(layer)
    if type_error is not None:
        key, value = type_error
        warning = (f'設定檔 {path} 的 "{key}" 型別錯誤'
                   f'（須為字串陣列 list[str]，實際為 {type(value).__name__}：{value!r}），'
                   f'已忽略此層，其餘層照常生效')
        print(f'WARN: {warning}', file=sys.stderr)
        return None, warning
    return layer, None


def load_config(repo_root, defaults, home=None):
    """三層 deep merge，回傳 (config, provenance, warnings)。

    - config：合併後的最終值，鍵集合與 `defaults` 相同
    - provenance：每個鍵最終由哪一層決定 —— 'builtin' / 'user-global' / 'per-repo'
      （逐字對應 requirements.md 的層級命名），供 `doctor` 直接消費，
      不必自行重做一次三層探測
    - warnings：list[str]，每一層被忽略（JSON 語法錯誤或型別錯誤）時的
      人類可讀說明，依 user-global → per-repo 的檢查順序排列；沒有任何
      一層失效則為空 list。呼叫端（例如 `doctor`）不需要重新解析 stderr
      字串就能知道「哪一層被拒絕、為什麼」——這正是 Stage 7 第五輪要堵的
      缺口：先前只印到 stderr，合併後若 fallback 到的下一層剛好命中檔案，
      doctor 的 stdout 就會印出一行乾淨的 OK，完全看不出使用者的 config
      其實被拒絕過。
    """
    per_repo_path, user_global_path = config_paths(repo_root, home)

    config = dict(defaults)
    provenance = {k: 'builtin' for k in defaults}
    warnings = []

    for layer_name, path in (('user-global', user_global_path),
                             ('per-repo', per_repo_path)):
        layer, warning = _load_layer(path)
        if warning is not None:
            warnings.append(warning)
        if not layer:
            continue
        for k, v in layer.items():
            config[k] = v
            provenance[k] = layer_name

    return config, provenance, warnings


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
