"""檔案錨點的副檔名清單改為 per-repo 可設定（anchor_exts）。

背景：RE_FILE / RE_FILE_LINE 的副檔名清單沒有 md，所以 `SKILL.md`、`SKILL.md:284`
這類引用在任何掃描設定下都抽不出錨點。對純文件 repo（skill／規格庫）而言，稽核
實質上只驗得到符號錨點——實測 cast-power 的 3 條待辦全部落在 NO_ANCHOR。

但**全域加 md 不安全**，這是本檔存在的理由。實測三個 repo：
  cast-power  有檔案錨點的條目 0→3，新增 4 個錨點，其中 1 個查無此檔
  todos       有檔案錨點的條目 0→6，新增 6 個錨點，其中 1 個查無此檔
  tradingbot  有檔案錨點的條目 98→125，新增 56 個錨點，**其中 21 個查無此檔**
tradingbot 那 21 個是 `~/.claude/…` 底下的檔案、以及 analysis.md／bindings.md 這類
跑完就刪的 workspace 產物。而檔案錨點**沒有**符號那條「從未存在於 git 歷史 →
不算 GONE 訊號」的過濾（build_checks() 對 file 是 `OK if hits else GONE`），
所以那 21 個會直接變成假 GONE——正是本工具最怕的「把仍成立的待辦標成可移除」。

因此做成 opt-in：預設清單一字不改，需要的 repo 自己在 .claude/todo-audit.json 開。

本檔釘住四件事：
  1. 不設 anchor_exts 時，行為與改動前完全相同（md 不是錨點）
  2. 設了之後，裸檔名與 file:line 兩種形式都抽得到
  3. 型別檢查涵蓋 anchor_exts（非 list[str] → 該層被拒，其餘層照常生效）
  4. 錨點定義全流程一致——不能只有稽核吃到 config，寫入前的重複提示卻用舊定義
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import todo_audit  # noqa: E402
import todo_config  # noqa: E402


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True,
                   capture_output=True, text=True)


class TestDefaultsUnchanged(unittest.TestCase):
    """改動前後的預設行為必須逐字相同 —— 這條若鬆掉，等於偷偷改了所有 repo。"""

    def setUp(self):
        todo_audit.set_anchor_exts(todo_audit.ANCHOR_EXTS)

    def test_md_is_not_an_anchor_by_default(self):
        a = todo_audit.extract_anchors(
            {'title': '修 `SKILL.md` 與 `docs/TODO-SYSTEM.md`', 'body': []})
        self.assertEqual(a['file'], [], 'md 預設不得成為檔案錨點')

    def test_md_file_line_is_not_an_anchor_by_default(self):
        a = todo_audit.extract_anchors({'title': '見 SKILL.md:284', 'body': []})
        self.assertEqual(a['file_line'], [], 'md:行號 預設不得成為錨點')

    def test_known_code_exts_still_extract(self):
        a = todo_audit.extract_anchors(
            {'title': '改 `Foo.java` 與 build.gradle:42', 'body': []})
        self.assertIn('Foo.java', a['file'])
        self.assertIn(('build.gradle', 42), a['file_line'])

    def test_js_now_extracts_in_file_line_form(self):
        """兩條 regex 原本的副檔名清單不一致：RE_FILE_LINE 少了 js。

        統一成單一 ANCHOR_EXTS 後等於補上它。實測對 cast-power／todos／
        tradingbot 三個 repo 的 file_line 錨點數皆為 0 變化，是行為中性的整併，
        但仍明文釘住，免得日後被當成沒人注意到的漂移。
        """
        a = todo_audit.extract_anchors({'title': '見 client.js:12', 'body': []})
        self.assertIn(('client.js', 12), a['file_line'])


class TestAnchorExtsOverride(unittest.TestCase):

    def tearDown(self):
        todo_audit.set_anchor_exts(todo_audit.ANCHOR_EXTS)

    def test_md_extracts_once_enabled(self):
        todo_audit.set_anchor_exts(list(todo_audit.ANCHOR_EXTS) + ['md'])
        a = todo_audit.extract_anchors(
            {'title': '修 `SKILL.md`，見 EVIDENCE.md:284', 'body': []})
        self.assertIn('SKILL.md', a['file'])
        self.assertIn(('EVIDENCE.md', 284), a['file_line'])

    def test_narrowing_the_list_takes_effect(self):
        """設定是覆寫不是附加 —— 縮小清單也要真的生效，否則使用者無法排除誤判型副檔名。"""
        todo_audit.set_anchor_exts(['md'])
        a = todo_audit.extract_anchors(
            {'title': '改 `Foo.java` 與 `SKILL.md`', 'body': []})
        self.assertIn('SKILL.md', a['file'])
        self.assertNotIn('Foo.java', a['file'], 'java 已不在清單內，不該再被抽出')

    def test_regex_special_chars_in_ext_are_escaped(self):
        """副檔名來自使用者的 config，不能直接拼進 regex 當 pattern。"""
        todo_audit.set_anchor_exts(['a.b'])
        a = todo_audit.extract_anchors({'title': '檔案 `x.a.b` 與 `xaab`', 'body': []})
        self.assertIn('x.a.b', a['file'])
        self.assertNotIn('xaab', a['file'], '"." 必須被跳脫成字面點，不得當萬用字元')


class TestAnchorExtsTypeChecked(unittest.TestCase):
    """anchor_exts 要和 search_dirs／scan_exts 受同一套型別檢查保護：
    字串會被當可迭代物逐字元展開，不 crash 但語意全錯 —— 那種靜默失效最危險。"""

    def test_anchor_exts_in_typed_keys(self):
        self.assertIn('anchor_exts', todo_config.TYPED_LIST_KEYS)

    def test_string_value_rejects_that_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            (repo / '.claude').mkdir(parents=True)
            (repo / '.claude' / 'todo-audit.json').write_text(
                json.dumps({'anchor_exts': 'md'}), encoding='utf-8')
            home = root / 'home'
            home.mkdir()
            defaults = {'anchor_exts': ['java']}
            cfg, prov, warns = todo_config.load_config(repo, defaults, home=home)
            self.assertEqual(cfg['anchor_exts'], ['java'], '型別錯誤的那一層必須被忽略')
            self.assertEqual(prov['anchor_exts'], 'builtin')
            self.assertTrue(any('anchor_exts' in w for w in warns),
                            '被拒絕的原因要能被呼叫端讀到，不能只印 stderr')


class TestConfigPlumbedThroughRealRun(unittest.TestCase):
    """端到端：config 真的有接到主流程，而不是只有單元層可用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.repo.mkdir()
        _git(self.repo, 'init', '-q')
        _git(self.repo, 'config', 'user.email', 'test@example.com')
        _git(self.repo, 'config', 'user.name', 'test')
        _git(self.repo, 'config', 'commit.gpgsign', 'false')
        (self.repo / 'scripts').mkdir()
        (self.repo / 'scripts' / 'tool.sh').write_text('#!/bin/sh\n', encoding='utf-8')
        (self.repo / 'HANDBOOK.md').write_text('# handbook\n', encoding='utf-8')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-q', '-m', 'init')
        self.md = self.root / 'todo.md'
        self.md.write_text(
            '- [ ] [2026-01-01] 更新 `HANDBOOK.md` 的章節\n', encoding='utf-8')
        self.db = self.root / 'audit.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        env = {'HOME': str(self.home),
               'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / 'todo_audit.py'), str(self.md),
             str(self.repo), '--db', str(self.db)],
            capture_output=True, text=True, env=env)

    def _write_cfg(self, obj):
        (self.repo / '.claude').mkdir(exist_ok=True)
        (self.repo / '.claude' / 'todo-audit.json').write_text(
            json.dumps(obj), encoding='utf-8')

    def test_without_config_item_has_no_anchor(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r'NO_ANCHOR\s+1',
                         f'沒開 anchor_exts 時 md 不該成為錨點\n{r.stdout}')

    def test_with_config_item_gets_a_live_file_anchor(self):
        self._write_cfg({'anchor_exts': list(todo_audit.ANCHOR_EXTS) + ['md']})
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r'ALIVE\s+1',
                         f'開了之後 HANDBOOK.md 應被抽成錨點且查得到檔\n{r.stdout}')
        self.assertNotRegex(r.stdout, r'NO_ANCHOR\s+1')


class TestAnchorDefinitionIsGlobal(unittest.TestCase):
    """錨點定義必須全流程一致。set_anchor_exts() 改的是 module 級 global，
    正是為了讓 similar_mode（寫入前重複提示）與稽核主流程看到同一組錨點；
    若哪天改成只在主流程套用，同一條 todo 在兩個地方會被算出不同錨點集合。"""

    def tearDown(self):
        todo_audit.set_anchor_exts(todo_audit.ANCHOR_EXTS)

    def test_module_regexes_are_rebuilt(self):
        before = todo_audit.RE_FILE
        todo_audit.set_anchor_exts(list(todo_audit.ANCHOR_EXTS) + ['md'])
        self.assertIsNot(todo_audit.RE_FILE, before, 'RE_FILE 應被重建')
        self.assertTrue(todo_audit.RE_FILE.search('SKILL.md'))
        self.assertTrue(todo_audit.RE_FILE_LINE.search('SKILL.md:1'))

    def test_reset_restores_default(self):
        todo_audit.set_anchor_exts(list(todo_audit.ANCHOR_EXTS) + ['md'])
        todo_audit.set_anchor_exts(todo_audit.ANCHOR_EXTS)
        self.assertIsNone(todo_audit.RE_FILE.search('SKILL.md'))


if __name__ == '__main__':
    unittest.main()
