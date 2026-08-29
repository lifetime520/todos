"""REQ-2：`fixtures.latest_snapshot()` / `fixtures.all_projects()` 的
兩段解析語意——「真實優先、合成墊底」。

**契約（給 implementer 的名字約定）**：`tests/fixtures.py` 目前只有
`AUDIT`（真實備份根目錄）一個模組層常數。本檔案要求它再新增一個模組層常數
`FIXTURES_DIR`，指向檢入 repo 的合成 fixture 根目錄
（`skills/todo-audit/tests/fixtures/`），且底下用固定檔名
`{project}.md`（不帶時間戳，G-3 裁決）。兩個常數都必須是模組層變數（不是
函式內硬編路徑），才能被 `unittest.mock.patch.multiple` 整組換成
`tempfile` 目錄——這是不觸碰 `~/.claude/todos/.audit/` 真實資料的唯一方式。

全部測試都用 `tempfile.TemporaryDirectory()` 建立假的「真實備份目錄」與
「合成 fixture 目錄」，patch 掉 `fixtures.AUDIT` 與 `fixtures.FIXTURES_DIR`
這兩個根目錄後才呼叫 `latest_snapshot()`/`all_projects()`。不依賴、不觸碰
真實的 `~/.claude/todos/.audit/`。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / 'scripts'))
sys.path.insert(0, str(TESTS))
import fixtures  # noqa: E402


class TestLatestSnapshotTwoStageResolution(unittest.TestCase):
    """`latest_snapshot(project)`：① 真實備份 ② 合成 fixture ③ None。"""

    def setUp(self):
        self._real_tmp = tempfile.TemporaryDirectory()
        self._synth_tmp = tempfile.TemporaryDirectory()
        self.real_dir = Path(self._real_tmp.name)
        self.synth_dir = Path(self._synth_tmp.name)

    def tearDown(self):
        self._real_tmp.cleanup()
        self._synth_tmp.cleanup()

    def _patched(self):
        """把 fixtures.py 的兩個根目錄常數換成本測試的 tempfile 目錄。

        兩個常數都必須「已存在」於 fixtures 模組上，patch.multiple 預設
        不允許新建屬性（create=True 才會允許）——這正是我們要的：
        FIXTURES_DIR 在功能實作前不存在，patch 這一步就會直接
        AttributeError，是預期的 RED，不是測試寫錯。
        """
        return patch.multiple(fixtures, AUDIT=self.real_dir,
                               FIXTURES_DIR=self.synth_dir)

    # REQ-2：真實備份存在時，latest_snapshot() 回真實那份，不是 fixture。
    def test_real_backup_wins_over_fixture_when_both_exist(self):
        real_file = self.real_dir / 'tradingbot-pre-migrate-20260101-000000.md'
        real_file.write_text('real backup content', encoding='utf-8')
        synth_file = self.synth_dir / 'tradingbot.md'
        synth_file.write_text('synthetic fixture content', encoding='utf-8')

        with self._patched():
            result = fixtures.latest_snapshot('tradingbot')

        self.assertEqual(result, real_file)

    # REQ-2：真實備份不存在時，回退到檢入的合成 fixture。
    def test_falls_back_to_fixture_when_no_real_backup(self):
        synth_file = self.synth_dir / 'tradingbot.md'
        synth_file.write_text('synthetic fixture content', encoding='utf-8')
        # 真實備份目錄存在但是空的（模擬乾淨環境/新機器/CI runner）。

        with self._patched():
            result = fixtures.latest_snapshot('tradingbot')

        self.assertEqual(result, synth_file)

    # REQ-2：兩者皆無 → None（反向驗收自動化版，取代人工改名目錄）。
    def test_returns_none_when_neither_real_nor_fixture_exists(self):
        # real_dir、synth_dir 兩個 tempfile 目錄都是空的。

        with self._patched():
            result = fixtures.latest_snapshot('tradingbot')

        self.assertIsNone(result)


class TestAllProjectsUnion(unittest.TestCase):
    """`all_projects()`：兩個來源的聯集，不重複計數。"""

    def setUp(self):
        self._real_tmp = tempfile.TemporaryDirectory()
        self._synth_tmp = tempfile.TemporaryDirectory()
        self.real_dir = Path(self._real_tmp.name)
        self.synth_dir = Path(self._synth_tmp.name)

    def tearDown(self):
        self._real_tmp.cleanup()
        self._synth_tmp.cleanup()

    def _patched(self):
        return patch.multiple(fixtures, AUDIT=self.real_dir,
                               FIXTURES_DIR=self.synth_dir)

    # REQ-2：只有合成 fixture 時，all_projects() 仍回非空（不是空 list）。
    def test_nonempty_when_only_synthetic_fixture_present(self):
        (self.synth_dir / 'tradingbot.md').write_text('x', encoding='utf-8')
        # 真實備份目錄是空的。

        with self._patched():
            result = fixtures.all_projects()

        self.assertIn('tradingbot', result)

    # REQ-2：同一專案兩個來源都有時，all_projects() 不重複計數。
    def test_project_present_in_both_sources_counted_once(self):
        (self.real_dir / 'tradingbot-pre-migrate-20260101-000000.md').write_text(
            'r', encoding='utf-8')
        (self.synth_dir / 'tradingbot.md').write_text('s', encoding='utf-8')

        with self._patched():
            result = fixtures.all_projects()

        self.assertEqual(result.count('tradingbot'), 1)


if __name__ == '__main__':
    unittest.main()
