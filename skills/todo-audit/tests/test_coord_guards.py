"""coord_guards：窗口流程守衛（2026-11-16，#13 績效診斷後 Cast 核准四項改進的 1～3 項）。

實證（#13 transcript b78b35ae…，診斷報告在
~/.superpowers/diagnosing-superpowers/b78b35ae-0016-4f7c-9ac4-430642a4bde4/report.md）：
1. 窗口 17 次 AskUserQuestion 合計卡住 3302 分鐘，期間 26 則 peer 訊息排隊。
2. peer 喚醒段占 63.5% 呼叫；每次喚醒首呼叫平均重讀 20.8 萬 token，峰值 40 萬。
3. S9b 歸屬設計被否決 5 輪：第 4 次糾正後才讀 position-attribution-model.md；
   共審 prompt 把文件明列的陷阱公式鎖成「不要建議推翻」。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import coord_guards as g


class TestAskGuard(unittest.TestCase):
    def test_window_is_denied(self):
        out = g.ask_guard('window')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('純文字', out['permissionDecisionReason'])

    def test_non_window_not_denied(self):
        self.assertIsNone(g.ask_guard('impl'))
        self.assertIsNone(g.ask_guard(None))


class TestContextTokens(unittest.TestCase):
    def _write(self, records, pad=0):
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(json.dumps(r) + '\n')
        if pad:
            f.write(json.dumps({'type': 'user', 'blob': 'x' * pad}) + '\n')
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def test_last_assistant_usage_summed(self):
        p = self._write([
            {'type': 'assistant', 'message': {'usage': {
                'input_tokens': 1, 'cache_read_input_tokens': 10, 'cache_creation_input_tokens': 5}}},
            {'type': 'assistant', 'message': {'usage': {
                'input_tokens': 2, 'cache_read_input_tokens': 200000, 'cache_creation_input_tokens': 3000}}},
            {'type': 'user', 'message': {'content': 'hi'}},
        ])
        self.assertEqual(g.context_tokens(p), 203002)

    def test_skips_trailing_huge_non_assistant_line(self):
        p = self._write([{'type': 'assistant', 'message': {'usage': {
            'input_tokens': 0, 'cache_read_input_tokens': 160000, 'cache_creation_input_tokens': 0}}}],
            pad=300_000)
        self.assertEqual(g.context_tokens(p), 160000)

    def test_missing_file_or_no_usage_is_none(self):
        self.assertIsNone(g.context_tokens('/nonexistent/x.jsonl'))
        self.assertIsNone(g.context_tokens(self._write([{'type': 'user'}])))


class TestContextGuard(unittest.TestCase):
    def test_window_over_threshold_warns(self):
        msg = g.context_guard('window', 210000)
        self.assertIn('21 萬', msg)
        self.assertIn('/remember', msg)

    def test_under_threshold_or_not_window_silent(self):
        self.assertIsNone(g.context_guard('window', 149999))
        self.assertIsNone(g.context_guard('window', None))
        self.assertIsNone(g.context_guard('impl', 400000))


class TestDesignDocs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'docs/knowledge').mkdir(parents=True)
        (root / 'docs/audit').mkdir(parents=True)
        (root / 'docs/knowledge/position-attribution-model.md').write_text('x')
        (root / 'docs/audit/PITFALLS.md').write_text('x')
        self.root = str(root)

    def test_attribution_keywords_hint_existing_docs(self):
        out = g.design_docs_guard('Agent', '請審 S9b 的倉位歸屬與記帳設計', self.root)
        self.assertNotIn('permissionDecision', out)
        self.assertIn('docs/knowledge/position-attribution-model.md', out['additionalContext'])
        self.assertIn('docs/audit/PITFALLS.md', out['additionalContext'])

    def test_funding_keyword_also_hints(self):
        out = g.design_docs_guard('SendMessage', '資金費拆帳要怎麼算', self.root)
        self.assertIn('decision list', out['additionalContext'])

    def test_unrelated_text_silent(self):
        self.assertIsNone(g.design_docs_guard('Agent', '修 UI 按鈕顏色', self.root))

    def test_no_docs_in_project_silent(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(g.design_docs_guard('Agent', '倉位歸屬設計審查', empty))

    def test_review_prompt_locking_premise_denied(self):
        out = g.design_docs_guard('Agent', '共審歸屬設計稿。人工倉位公式已定，不要建議推翻。', self.root,
                                  review_label='協調三方共審 S9b 設計稿 general-purpose')
        self.assertEqual(out['permissionDecision'], 'deny')

    def test_impl_dispatch_mentioning_audit_path_and_reviewer_not_denied(self):
        # code-reviewer 審查重現的誤擋：實作派工含 docs/audit/ 路徑、code-reviewer、驗收條件
        text = ('實作 T-1030 歸屬修正，Cast 已核准參數不得推翻；先讀 docs/audit/PITFALLS.md，'
                'commit 前走 code-reviewer，驗收條件見 spec')
        out = g.design_docs_guard('Agent', text, self.root, review_label='T-1030 歸屬修正實作 general-purpose')
        self.assertNotEqual(out.get('permissionDecision'), 'deny')

    def test_reviewer_subagent_type_counts_as_review(self):
        out = g.design_docs_guard('Agent', '歸屬設計：不得質疑帳本公式', self.root,
                                  review_label='看設計 pr-review-toolkit:code-reviewer')
        self.assertEqual(out['permissionDecision'], 'deny')

    def test_lock_phrase_in_sendmessage_not_denied(self):
        # 對實作者說「照 plan 做、不要推翻設計」是正常派工，不擋
        out = g.design_docs_guard('SendMessage', '歸屬照 plan 實作，不要推翻設計', self.root)
        self.assertNotEqual(out.get('permissionDecision'), 'deny')

    def test_lock_phrase_in_non_review_agent_not_denied(self):
        out = g.design_docs_guard('Agent', '實作歸屬模組，不要推翻既有介面', self.root)
        self.assertNotEqual(out.get('permissionDecision'), 'deny')


class TestHooksEndToEnd(unittest.TestCase):
    """直接跑 hooks/*.sh，HOME 指向假家目錄、skill 連回本 checkout。"""
    REPO = Path(__file__).resolve().parents[3]

    def setUp(self):
        import os, subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / 'home'
        skills = self.home / '.claude' / 'skills'
        skills.mkdir(parents=True)
        (skills / 'todo-audit').symlink_to(self.REPO / 'skills' / 'todo-audit')
        audit = self.home / '.claude' / 'todos' / '.audit'
        audit.mkdir(parents=True)
        self.proj = base / 'demo'
        (self.proj / 'docs/knowledge').mkdir(parents=True)
        (self.proj / 'docs/knowledge/position-attribution-model.md').write_text('x')
        subprocess.run(['git', 'init', '-q', str(self.proj)], check=True)
        sys.path.insert(0, str(self.REPO / 'skills/todo-audit/scripts'))
        import todo_store, todo_coord
        con = todo_store.connect(audit / 'demo.sqlite')
        todo_coord.role_set(con, 'demo #13', 'window')
        todo_coord.role_set(con, 'demo #1', 'impl')
        con.close()
        sess = self.home / '.claude' / 'sessions'
        sess.mkdir()
        for sid, name in (('sid-win', 'demo #13｜窗口'), ('sid-imp', 'demo #1｜實作')):
            (sess / f'{sid}.json').write_text(json.dumps(
                {'pid': os.getpid(), 'sessionId': sid, 'name': name, 'cwd': str(self.proj)}))
        self.env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}

    def _run(self, hook, payload):
        import subprocess
        r = subprocess.run(['bash', str(self.REPO / 'hooks' / hook)], cwd=self.proj,
                           input=json.dumps(payload), capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)['hookSpecificOutput'] if r.stdout.strip() else None

    def test_ask_guard_denies_window(self):
        out = self._run('ask-window-guard.sh', {'session_id': 'sid-win',
                        'tool_input': {'questions': [{'question': '選哪個？', 'header': 'x'}]}})
        self.assertEqual(out['permissionDecision'], 'deny')

    def test_ask_guard_only_hints_impl(self):
        out = self._run('ask-window-guard.sh', {'session_id': 'sid-imp',
                        'tool_input': {'questions': [{'question': '選哪個？', 'header': 'x'}]}})
        self.assertNotIn('permissionDecision', out)
        self.assertIn('demo #13', out['additionalContext'])

    def test_context_guard_warns_window_once(self):
        t = self.proj / 't.jsonl'
        t.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {
            'input_tokens': 0, 'cache_read_input_tokens': 250000, 'cache_creation_input_tokens': 0}}}) + '\n')
        payload = {'session_id': 'sid-win', 'transcript_path': str(t)}
        first = self._run('window-context-guard.sh', payload)
        self.assertIn('25 萬', first['additionalContext'])
        self.assertIsNone(self._run('window-context-guard.sh', payload))  # 30 分鐘內不重複
        self.assertIsNone(self._run('window-context-guard.sh',
                                    {'session_id': 'sid-imp', 'transcript_path': str(t)}))

    def test_hooks_fail_open_on_garbage_input(self):
        import subprocess
        for hook in ('ask-window-guard.sh', 'window-context-guard.sh', 'design-docs-guard.sh'):
            r = subprocess.run(['bash', str(self.REPO / 'hooks' / hook)], cwd=self.proj,
                               input='not json{', capture_output=True, text=True, env=self.env)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, ''), hook)
        missing = self._run('window-context-guard.sh',
                            {'session_id': 'sid-win', 'transcript_path': '/nonexistent.jsonl'})
        self.assertIsNone(missing)

    def test_design_docs_guard_deny_and_hint(self):
        deny = self._run('design-docs-guard.sh', {'tool_name': 'Agent', 'cwd': str(self.proj),
                         'tool_input': {'description': '三方共審', 'prompt': '歸屬公式已定，不要建議推翻'}})
        self.assertEqual(deny['permissionDecision'], 'deny')
        hint = self._run('design-docs-guard.sh', {'tool_name': 'SendMessage', 'cwd': str(self.proj),
                         'tool_input': {'to': 'x', 'message': '資金費歸屬怎麼拆'}})
        self.assertIn('position-attribution-model.md', hint['additionalContext'])


if __name__ == '__main__':
    unittest.main()
