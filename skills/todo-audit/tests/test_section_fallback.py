"""REQ-2：補滿 `_section_of()` 無-heading fallback 的分支覆蓋。

背景：
`scripts/todo_store.py:149-167` 的 `_section_of(title, heading)` 在 `heading`
為 falsy 時走標題關鍵字 fallback，共四條出口：
    `[P0]` → urgent、`[Cast 拍板]` → decision、`[P2]` → later、其餘 → normal
`tests/fixtures/tradingbot.md` 現已涵蓋這四條分支各一條無 heading 的條目，
本檔逐條直接斷言 `_section_of()` 算出的 `section` 值，確保任一分支壞掉時
都有對應測試變紅。

**[G-1] 硬約束**：本檔直接讀 fixture 固定路徑，不經
`fixtures.latest_snapshot('tradingbot')`。後者的規則是「真實 `.md` 備份優先
於合成 fixture」的兩段解析（`tests/fixtures.py:30-40`），一旦某台機器存在
真的 `.md` 格式備份，測試會靜默改測別的內容——本 REQ 要補的分支覆蓋在該機器
上會失效，測試卻仍然全綠。這正是同批交付另一個 task（REQ-1）要根治的
「恆真偽裝」形狀，不可在這裡重蹈覆轍。
"""
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import todo_store  # noqa: E402

# [G-1]：固定路徑直讀，不得改成 fixtures.latest_snapshot('tradingbot')。
FIXTURE = TESTS / 'fixtures' / 'tradingbot.md'

# 四條 fallback 分支共用的標題標記，供各測試方法的「排除」判斷共用，
# 避免字面值在多處重複打錯而彼此不一致。
_MARKERS = ('[P0]', '[Cast 拍板]', '[P2]')


def _load_items():
    """讀 fixture 固定路徑並解析，回傳 `parse_md_lossless()` 的 items 清單。"""
    text = FIXTURE.read_text(encoding='utf-8')
    parsed = todo_store.parse_md_lossless(text)
    return parsed['items']


class TestSectionFallbackNoHeading(unittest.TestCase):
    """逐條斷言 `_section_of()` 在 heading 為 falsy 時的四條 fallback 分支。

    每個測試方法都要求「唯一命中」而非「至少命中」：用 `_find_unique()`
    在候選清單裡先篩出「heading 為 falsy 且符合條件」的條目，若命中數不是
    剛好 1，直接 `self.fail()`——零命中代表 fixture 缺少這條分支對應的無-
    heading 條目，命中數 >1 則代表選擇條件不夠精確、可能誤測到別條分支的
    條目。兩種情況都不能被「迴圈沒跑到所以沒 assert 到，測試靜默通過」這種
    恆真寫法蓋過去。
    """

    def _find_unique(self, predicate, description):
        items = _load_items()
        matches = [it for it in items if predicate(it)]
        if len(matches) != 1:
            titles = [it['title'] for it in matches]
            self.fail(
                f'{description}：預期在 {FIXTURE} 中找到唯一 1 條符合條件的'
                f'條目，實際找到 {len(matches)} 條（{titles}）。'
                '若為 0 條，代表 fixture 缺少這條 fallback 分支對應的無-'
                'heading 條目（REQ-2）。'
            )
        return matches[0]

    # REQ-2 —— 驗證 [P0] 分支：對應 fixture 中無 heading 的條目
    # （tradingbot.md:5）。
    def test_p0_without_heading_falls_back_to_urgent(self):
        item = self._find_unique(
            lambda it: not it['heading'] and '[P0]' in it['title'],
            '標題含 [P0] 且無 heading 的條目',
        )
        # 同時明確斷言 heading 為 falsy——否則萬一 _find_unique 的篩選條件
        # 寫錯（例如漏掉 `not it['heading']`），這個測試可能誤測到「有
        # heading 但標題剛好也含 [P0]」的條目，那就沒驗到 fallback 路徑本身。
        # fixture 裡確實有這種條目：標題「已被降級為 later 的事項」那條，
        # 它落在 ⚪ 標頭底下、標題卻殘留 [P0]（刻意不寫行號——這份 fixture
        # 每次新增條目行號都會漂，本次交付就已經讓它從 :37 移到 :49）。
        self.assertFalse(
            item['heading'],
            f'條目 {item["title"]!r} 的 heading 不是 falsy，'
            '這個測試驗的是「無 heading 時走標題 fallback」，選錯條目了',
        )
        self.assertEqual(
            item['section'], 'urgent',
            f'標題含 [P0] 且無 heading 的條目 {item["title"]!r}，'
            f'section 應為 urgent，實際為 {item["section"]!r}',
        )

    # REQ-2 —— 驗證 [Cast 拍板] 分支：對應 fixture 中無 heading 的條目。
    def test_cast_paiban_without_heading_falls_back_to_decision(self):
        item = self._find_unique(
            lambda it: not it['heading'] and '[Cast 拍板]' in it['title'],
            '標題含 [Cast 拍板] 且無 heading 的條目',
        )
        self.assertFalse(
            item['heading'],
            f'條目 {item["title"]!r} 的 heading 不是 falsy，選錯條目了',
        )
        self.assertEqual(
            item['section'], 'decision',
            f'標題含 [Cast 拍板] 且無 heading 的條目 {item["title"]!r}，'
            f'section 應為 decision，實際為 {item["section"]!r}',
        )

    # REQ-2 —— 驗證 [P2] 分支：對應 fixture 中無 heading 的條目。
    def test_p2_without_heading_falls_back_to_later(self):
        item = self._find_unique(
            lambda it: not it['heading'] and '[P2]' in it['title'],
            '標題含 [P2] 且無 heading 的條目',
        )
        self.assertFalse(
            item['heading'],
            f'條目 {item["title"]!r} 的 heading 不是 falsy，選錯條目了',
        )
        self.assertEqual(
            item['section'], 'later',
            f'標題含 [P2] 且無 heading 的條目 {item["title"]!r}，'
            f'section 應為 later，實際為 {item["section"]!r}',
        )

    # REQ-2 —— 驗證預設（無特殊標記）分支：對應 fixture 中無 heading 的條目。
    def test_unmarked_title_without_heading_falls_back_to_normal(self):
        item = self._find_unique(
            lambda it: not it['heading']
            and not any(marker in it['title'] for marker in _MARKERS),
            '標題不含 [P0]/[Cast 拍板]/[P2] 任何標記，且無 heading 的條目',
        )
        self.assertFalse(
            item['heading'],
            f'條目 {item["title"]!r} 的 heading 不是 falsy，選錯條目了',
        )
        self.assertEqual(
            item['section'], 'normal',
            f'標題無特殊標記且無 heading 的條目 {item["title"]!r}，'
            f'section 應為 normal（預設分支），實際為 {item["section"]!r}',
        )


if __name__ == '__main__':
    unittest.main()
