"""REQ-1：掃描範圍改為三層 config（builtin < user-global < per-repo），deep merge + provenance。

這份測試釘住 `.castpower/todo-audit-cfg/requirements.md` 的 REQ-1 七條驗收。

介面契約（尚不存在，本檔案預期在 import 階段就 ImportError —— 合格的 RED）：
    import todo_config
    config, provenance = todo_config.load_config(repo_root, defaults, home=None)

    - `defaults` 由呼叫端（todo_audit.py）傳入自己既有的 SEARCH_DIRS/SCAN_EXTS 常數，
      todo_config.py 本身不重複硬編一份 BTSE 預設值 —— 避免兩份常數日後漂移。
      這是本檔對「一字不改，只改成作為 builtin 層被讀取」的具體化解讀，見 report 第 3 節。
    - `config` = {'search_dirs': [...], 'scan_exts': [...]}（deep merge 後的最終值）
    - `provenance` = {'search_dirs': 'builtin'|'user-global'|'per-repo', 'scan_exts': ...}
      （層級名稱逐字取自 requirements.md:27）
    - per-repo config 路徑固定為 `<repo>/.claude/todo-audit.json`
    - user-global config 路徑固定為 `<home>/.claude/todo-audit.json`
    - 非法 JSON 只讓該層失效，其餘層照常合併；該次呼叫必須印出可辨識的警告
      （不預設印到哪個 stream，測試合併 stdout+stderr 判定）
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import todo_audit  # noqa: E402
import todo_config  # noqa: E402  -- 尚未存在，import 本身就是這份測試的第一道 RED


BUILTIN_SEARCH_DIRS = ['agent/src/main', 'core/src/main', 'exchange/src/main',
                        'web/src', 'web/scripts', 'scripts']
BUILTIN_SCAN_EXTS = ['.java', '.ts', '.tsx', '.js', '.mjs', '.cjs', '.sql',
                      '.gradle', '.css', '.sh', '.properties']


def _defaults():
    # 每次回傳新 dict/list，避免 deep merge 實作萬一原地修改輸入而讓測試互相污染
    return {'search_dirs': list(BUILTIN_SEARCH_DIRS),
            'scan_exts': list(BUILTIN_SCAN_EXTS)}


class ConfigLayerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.repo.mkdir()
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_cfg(self, base, content):
        p = base / '.claude' / 'todo-audit.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return p


class TestLoadConfigLayering(ConfigLayerFixture):

    def test_no_config_provenance_all_builtin(self):
        # REQ-1 驗收 7：無任何 config 檔時，provenance 應指出兩個鍵都來自 builtin
        config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        self.assertEqual(prov, {'search_dirs': 'builtin', 'scan_exts': 'builtin'})

    def test_no_config_values_match_legacy_constants_verbatim(self):
        # REQ-1 驗收 1：BTSE 零回歸保護 —— 無 config 時逐項等於 ed493c1 的常數
        config, _ = todo_config.load_config(self.repo, _defaults(), home=self.home)
        self.assertEqual(config['search_dirs'], todo_audit.SEARCH_DIRS)
        self.assertEqual(sorted(config['scan_exts']), sorted(todo_audit.SCAN_EXTS))

    def test_per_repo_overrides_search_dirs_only_scan_exts_stays_builtin(self):
        # REQ-1 驗收 2：deep merge —— 只設 search_dirs 時 scan_exts 仍為內建 11 個
        self._write_cfg(self.repo, json.dumps({'search_dirs': ['hooks', 'scripts']}))
        config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        self.assertEqual(config['search_dirs'], ['hooks', 'scripts'])
        self.assertEqual(sorted(config['scan_exts']), sorted(BUILTIN_SCAN_EXTS))
        self.assertEqual(len(config['scan_exts']), 11)
        self.assertEqual(prov['search_dirs'], 'per-repo')
        self.assertEqual(prov['scan_exts'], 'builtin')

    def test_per_repo_wins_over_user_global_on_conflict(self):
        # REQ-1 驗收 3：同鍵衝突，per-project 勝
        self._write_cfg(self.home, json.dumps({'search_dirs': ['from-user-global']}))
        self._write_cfg(self.repo, json.dumps({'search_dirs': ['from-per-repo']}))
        config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        self.assertEqual(config['search_dirs'], ['from-per-repo'])
        self.assertEqual(prov['search_dirs'], 'per-repo')

    def test_illegal_json_in_one_layer_does_not_crash_and_warns(self):
        # REQ-1 驗收 4：非法 JSON 不得靜默吞掉，須印可辨識警告；該層失效，其餘層（此處只剩 builtin）照常生效
        bad = self._write_cfg(self.repo, '{not valid json,,,')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        combined = out.getvalue() + err.getvalue()
        self.assertTrue(combined.strip(), '非法 JSON 必須印出可辨識的警告，不得靜默吞掉')
        self.assertIn(str(bad), combined, '警告應指出是哪個檔案壞掉，否則使用者無從修起')
        self.assertEqual(config['search_dirs'], BUILTIN_SEARCH_DIRS)
        self.assertEqual(prov['search_dirs'], 'builtin')

    def test_user_global_valid_survives_per_repo_illegal_json(self):
        # REQ-1 驗收 5（G-5）：per-repo 壞掉不得拖累 user-global —— 最終值是
        # 「user-global 疊在內建上」，不是退回純內建
        self._write_cfg(self.home, json.dumps({'search_dirs': ['from-user-global']}))
        self._write_cfg(self.repo, '{{{ 壞掉的 json')
        config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        self.assertEqual(config['search_dirs'], ['from-user-global'])
        self.assertEqual(prov['search_dirs'], 'user-global')

    def test_search_dirs_as_number_layer_invalid_falls_back_to_builtin(self):
        # Stage 7 第四輪裁決 / REQ-1 驗收 4（新增判準）：search_dirs 不是
        # list[str]（此例是數字）時，該層視為非法，比照非法 JSON 處理——
        # 印 WARN、該層失效、其餘層（此處只剩 builtin）照常合併。
        # 這正是實跑會炸 TypeError 的那個型別。
        bad = self._write_cfg(self.repo, json.dumps({'search_dirs': 5}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        combined = out.getvalue() + err.getvalue()
        self.assertTrue(combined.strip(), '型別錯誤必須印出可辨識的警告，不得靜默吞掉')
        self.assertIn(str(bad), combined, '警告應指出是哪個檔案壞掉')
        self.assertIn('型別', combined, '警告措辭應區分「型別錯誤」與「JSON 語法錯誤」，避免使用者搞混')
        self.assertEqual(config['search_dirs'], BUILTIN_SEARCH_DIRS)
        self.assertEqual(prov['search_dirs'], 'builtin')

    def test_search_dirs_as_string_layer_invalid_falls_back_to_builtin(self):
        # 常見手誤：忘了寫成陣列，寫成裸字串。若不驗型別，這個字串會被
        # 當可迭代物逐字元展開成 5 個單字元「目錄」，且不 crash——比
        # crash 更危險的靜默失效，是本次裁決要堵的主要案例。
        bad = self._write_cfg(self.repo, json.dumps({'search_dirs': 'hooks'}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        combined = out.getvalue() + err.getvalue()
        self.assertTrue(combined.strip(), '型別錯誤必須印出可辨識的警告，不得靜默吞掉')
        self.assertIn(str(bad), combined)
        self.assertEqual(config['search_dirs'], BUILTIN_SEARCH_DIRS,
                          'search_dirs 不得被當成可迭代物逐字元展開，該層必須整層失效')
        self.assertEqual(prov['search_dirs'], 'builtin')

    def test_scan_exts_list_with_non_string_element_layer_invalid(self):
        # scan_exts 雖是 list，但元素非字串（例如混進數字）同樣算型別
        # 錯誤——驗收要求「list 裡每個元素都必須是 str」。
        bad = self._write_cfg(self.repo, json.dumps({'scan_exts': [1, 2]}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        combined = out.getvalue() + err.getvalue()
        self.assertTrue(combined.strip(), '型別錯誤必須印出可辨識的警告，不得靜默吞掉')
        self.assertIn(str(bad), combined)
        self.assertEqual(sorted(config['scan_exts']), sorted(BUILTIN_SCAN_EXTS))
        self.assertEqual(prov['scan_exts'], 'builtin')

    def test_underscore_prefixed_keys_excluded_from_runtime(self):
        # REQ-1 驗收 6：`_` 前綴鍵是文件，merge 時必須被 strip 掉
        self._write_cfg(self.repo, json.dumps({
            '_comment': '這是給人看的說明，不該進 runtime 值',
            'search_dirs': ['hooks'],
        }))
        config, prov = todo_config.load_config(self.repo, _defaults(), home=self.home)
        self.assertNotIn('_comment', config)
        self.assertNotIn('_comment', prov)
        self.assertEqual(config['search_dirs'], ['hooks'])


class TestCollectSourceFilesConsumesConfig(unittest.TestCase):
    """驗證的是可觀察行為（掃到哪些檔案），不是 todo_config 內部合併邏輯 —— 那是上面那組測試的職責。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        # 兩個內建預設目錄下各放一個檔案
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'scripts' / 'legacy.sh').write_text('# x', encoding='utf-8')
        (self.repo / 'web' / 'src').mkdir(parents=True)
        (self.repo / 'web' / 'src' / 'other.ts').write_text('// x', encoding='utf-8')
        # 一個不在任何內建目錄下的檔案
        (self.repo / 'hooks').mkdir(parents=True)
        (self.repo / 'hooks' / 'newdir.sh').write_text('# y', encoding='utf-8')
        # 副檔名不合格的檔案，即使目錄合格也不該被收
        (self.repo / 'hooks' / 'ignore.txt').write_text('x', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _names(files):
        return {p.name for p in files}

    def test_no_config_scans_only_legacy_search_dirs(self):
        # REQ-1 驗收 1
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            files, _ = todo_audit.collect_source_files(self.repo)
        names = self._names(files)
        self.assertIn('legacy.sh', names)
        self.assertIn('other.ts', names)
        self.assertNotIn('newdir.sh', names)

    def test_per_repo_config_narrows_scan_to_configured_dirs(self):
        # REQ-1 驗收 2：設定後只掃設定的目錄，原本內建目錄下的檔案要被排除
        cfg_dir = self.repo / '.claude'
        cfg_dir.mkdir()
        (cfg_dir / 'todo-audit.json').write_text(
            json.dumps({'search_dirs': ['hooks', 'scripts']}), encoding='utf-8')
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            files, _ = todo_audit.collect_source_files(self.repo)
        names = self._names(files)
        self.assertIn('newdir.sh', names)
        self.assertIn('legacy.sh', names)
        self.assertNotIn('other.ts', names, 'web/src 已不在生效的 search_dirs 內，不該再被掃到')
        self.assertNotIn('ignore.txt', names, 'AND 條件不能被 config 覆寫掉 —— 副檔名仍須合格')


if __name__ == '__main__':
    unittest.main()
