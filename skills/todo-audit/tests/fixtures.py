"""測試用的真實資料來源。

Phase 4（2026-08-09）刪掉 `~/.claude/todos/*.md` 之後，round-trip 與 parity
測試就找不到輸入了，會全部靜默 skip —— 測試報 OK 但什麼都沒驗，比紅燈更危險。

故改用 `.audit/{project}-pre-migrate-*.md` 這些遷移備份當 fixture：
它們是真實資料的快照、永久保存、且正是遷移當下驗證過 byte-identical 的那份。

但 `.audit/` 只存在於做過遷移的本機——新機器、CI runner、任何協作者的環境都沒有這個
目錄，測試套件在那些環境下會永遠紅燈（`all_projects()`==[] 導致 `test_fixtures_exist`
失敗，`latest_snapshot('tradingbot') is None` 導致 `test_audit_parity` 整批失敗）。
故加一層「合成墊底」：`FIXTURES_DIR` 下手工構造、不含任何真實待辦內容的 `.md`，
檔名固定為 `{project}.md`（不帶時間戳——合成資料沒有「最新一份」的概念，時間戳
只會誤導後續讀者以為它是某次遷移的快照）。

兩段解析語意（`latest_snapshot()`）：
1. 本機真實備份 `{project}-pre-migrate-*.md` 存在 → 用它（`sorted()[-1]`，真實優先）。
2. 不存在 → 回退到檢入的合成 fixture `FIXTURES_DIR/{project}.md`。
3. 兩者皆無 → 回 `None`。**守衛：呼叫端必須把這當紅燈處理，不得改成 skip**——
   一旦兩個來源都拿不到東西，代表 round-trip / parity 保護本身已經失效。

`all_projects()` 回傳兩個來源的聯集（依 project 名稱去重，不看檔名格式差異）。
"""
from pathlib import Path

AUDIT = Path.home() / '.claude' / 'todos' / '.audit'
FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'


def latest_snapshot(project):
    """回傳該專案可用的快照：真實 pre-migrate 備份優先，合成 fixture 墊底。

    兩者皆無時回 None —— 呼叫端（test_roundtrip.py / test_audit_parity.py）
    刻意不 skipTest，讓這個 None 直接炸出紅燈。
    """
    snaps = sorted(AUDIT.glob(f'{project}-pre-migrate-*.md'))
    if snaps:
        return snaps[-1]
    synthetic = FIXTURES_DIR / f'{project}.md'
    return synthetic if synthetic.exists() else None


def all_projects():
    """兩個來源的聯集：真實備份反推的專案名 ∪ 合成 fixture 的專案名。"""
    names = set()
    for p in AUDIT.glob('*-pre-migrate-*.md'):
        stem = p.name.rsplit('-pre-migrate-', 1)[0]
        if not stem.endswith('.view'):
            names.add(stem)
    if FIXTURES_DIR.is_dir():
        for p in FIXTURES_DIR.glob('*.md'):
            names.add(p.stem)
    return sorted(names)
