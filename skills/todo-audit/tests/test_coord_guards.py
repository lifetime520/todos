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


class TestLastPromptSource(unittest.TestCase):
    """這一輪是誰叫醒的：peer（cross-session 訊息）或 Cast 本人打字。"""

    def _write(self, recs):
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    PEER = {'type': 'user', 'message': {'content': 'Another Claude session sent a message: '
            '<cross-session-message from="uds:/x.sock" from-name="demo #13">【轉達】改接 T-1</cross-session-message>'}}
    CAST = {'type': 'user', 'message': {'content': '幫我看一下 T-1'}}
    TOOL = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}}
    NOTI = {'type': 'user', 'message': {'content': '<task-notification><status>completed</status></task-notification>'}}
    ASST = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '好'}]}}

    def test_peer_woke_this_turn(self):
        # 2026-11-18 實例：#13 派 T-1004 給 #4，#4 中途用 AskUserQuestion 直接問 Cast
        p = self._write([self.CAST, self.ASST, self.PEER, self.ASST, self.TOOL, self.NOTI, self.ASST])
        self.assertEqual(g.last_prompt_source(p), 'peer')

    def test_peer_message_flagged_meta_still_counts(self):
        # 真實 transcript：cross-session 訊息的 user 紀錄帶 isMeta=True（#4 4d890a11… 11:08:00）
        peer = dict(self.PEER, isMeta=True)
        self.assertEqual(g.last_prompt_source(self._write([self.CAST, peer, self.TOOL])), 'peer')

    def test_cast_typed_mid_turn_queued_command(self):
        # 審查實證：session 忙碌時 Cast 打的字只記成 attachment/queued_command，origin.kind=human
        q = {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': '這題直接問我',
             'origin': {'kind': 'human'}}}
        self.assertEqual(g.last_prompt_source(self._write([self.PEER, self.ASST, q, self.TOOL])), 'user')

    def test_peer_mid_turn_queued_command(self):
        q = {'type': 'attachment', 'attachment': {'type': 'queued_command', 'origin': {'kind': 'peer'},
             'prompt': '<cross-session-message from-name="demo #13">【轉達】改派</cross-session-message>'}}
        noti = {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': '<task-notification>x',
                'origin': {'kind': 'task-notification'}}}
        self.assertEqual(g.last_prompt_source(self._write([self.CAST, q, noti, self.TOOL])), 'peer')

    def test_half_line_and_odd_records_skipped(self):
        p = self._write([self.CAST, {'type': 'user', 'message': 'not-a-dict'},
                         {'type': 'user', 'message': {'content': [{'type': 'text', 'text': None}]}}])
        with open(p, 'a') as f:
            f.write('{"type": "user", "message": {"content": "半行')
        self.assertEqual(g.last_prompt_source(p), 'user')

    def test_cast_typed_this_turn(self):
        p = self._write([self.PEER, self.ASST, self.CAST, self.TOOL])
        self.assertEqual(g.last_prompt_source(p), 'user')

    def test_text_list_content_and_meta_skipped(self):
        meta = {'type': 'user', 'isMeta': True, 'message': {'content': '<system-reminder>x</system-reminder>'}}
        summ = {'type': 'user', 'isCompactSummary': True, 'message': {'content': 'This session is being continued'}}
        cast = {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '照做'}]}}
        self.assertEqual(g.last_prompt_source(self._write([cast, meta, summ])), 'user')

    def test_missing_or_empty(self):
        self.assertIsNone(g.last_prompt_source('/nonexistent/x.jsonl'))
        self.assertIsNone(g.last_prompt_source(self._write([self.TOOL])))
        self.assertIsNone(g.last_prompt_source(None))


class TestAskGuard(unittest.TestCase):
    def test_impl_woken_by_peer_is_denied_and_redirected(self):
        out = g.ask_guard('impl', woke_by='peer', window='demo #13')
        self.assertEqual(out['permissionDecision'], 'deny')
        r = out['permissionDecisionReason']
        self.assertIn('demo #13', r)
        self.assertIn('派工時加的限制', r)

    def test_impl_cast_typed_or_unknown_not_denied(self):
        self.assertIsNone(g.ask_guard('impl', woke_by='user', window='demo #13'))
        self.assertIsNone(g.ask_guard('impl', woke_by=None, window='demo #13'))
        self.assertIsNone(g.ask_guard('impl', woke_by='peer', window=None))

    def test_only_impl_role_denied_when_peer_woke(self):
        # #11（改進計劃，直屬 Cast）、#2（一般）、未登記角色被 peer 叫醒時仍可問 Cast
        for role in ('improve', 'research', None):
            self.assertIsNone(g.ask_guard(role, woke_by='peer', window='demo #13'), role)

    def test_window_is_denied(self):
        out = g.ask_guard('window')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('純文字', out['permissionDecisionReason'])

    def test_window_told_to_decide_on_own_recommendation(self):
        # 2026-11-18 Cast：「他都已經有了答案，根本不需要問我」
        r = g.ask_guard('window')['permissionDecisionReason']
        self.assertIn('照建議執行', r)
        self.assertIn('Cast 可推翻', r)

    def test_non_window_not_denied(self):
        self.assertIsNone(g.ask_guard('impl'))
        self.assertIsNone(g.ask_guard(None))


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

    def test_castpower_ledger_word_silent(self):
        # 2026-11-16 實際誤觸發：castpower 的 ledger（run 紀錄檔）與倉位帳本無關
        self.assertIsNone(g.design_docs_guard('Agent', '審查 cmd_seal：ledger 回補判定與 accheck PASS', self.root))

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


class TestPeerMessageGuard(unittest.TestCase):
    """B 案（D-048）：peer 送窗口要帶標籤；窗口不回純確認。"""
    W = 'tradingbot #13'

    def test_untagged_peer_to_window_denied_with_inbox_hint(self):
        out = g.peer_message_guard('tradingbot #1', self.W, self.W, 'T-1 已推送，等 tester #900。')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('inbox post', out['permissionDecisionReason'])

    def test_tagged_peer_to_window_allowed(self):
        for tag in ('問題', '阻塞', '交付', '轉達', '彙整', '緊急'):
            self.assertIsNone(g.peer_message_guard('tradingbot #1', self.W, self.W, f'  【{tag}】T-1 …'), tag)

    def test_tag_variants_allowed(self):
        for m in ('【交付 T-1021】PASS', '【T-1021 交付】x', '#3：【交付】x', '** 【問題】x', '[交付] x'):
            self.assertIsNone(g.peer_message_guard('tradingbot #1', self.W, self.W, m), m)

    def test_tag_late_in_text_not_counted(self):
        m = 'T-1 已推送，等 tester #900，之後如果有' + '【問題】' + '再說'
        self.assertIsNotNone(g.peer_message_guard('tradingbot #1', self.W, self.W, m))

    def test_peer_to_non_window_allowed(self):
        self.assertIsNone(g.peer_message_guard('tradingbot #1', 'tradingbot #3', self.W, '隨便'))
        self.assertIsNone(g.peer_message_guard('tradingbot #1', None, self.W, '隨便'))

    def test_no_window_configured_allowed(self):
        self.assertIsNone(g.peer_message_guard('tradingbot #1', self.W, None, '隨便'))

    def test_window_pure_ack_denied(self):
        for m in ('收到', '【#13】收到，謝謝。', '了解 👍', 'OK，知悉', 'ok!', '好的～'):
            out = g.peer_message_guard(self.W, 'tradingbot #1', self.W, m)
            self.assertEqual(out['permissionDecision'], 'deny', m)

    def test_window_substantive_reply_allowed(self):
        for m in ('token 已換新', '收到，T-1021 我這邊接手', '收到，T-1021 已部署完成 PASS', 'OK 已 merge',
                  '不好', '好，停', '好，等', '收到，改', '好，可', '了解，否', '好？', 'OK?', '收到？','收到，採 (a)。', '【#13 裁示 T-1016】採 (a)，附三點：…', '收到，請先改測試再送 tester。',
                  '收到。' + '說明' * 40, '了解，你那邊卡在哪？'):
            self.assertIsNone(g.peer_message_guard(self.W, 'tradingbot #1', self.W, m), m)


class TestInboxInject(unittest.TestCase):
    def test_format(self):
        items = [{'id': 1, 'from_name': 'tradingbot #1', 'body': 'A', 'todo_ids': 'T-1',
                  'created_at': '2026-11-17T10:00:00'},
                 {'id': 2, 'from_name': 'tradingbot #3', 'body': 'B', 'todo_ids': None,
                  'created_at': '2026-11-17T10:05:00'}]
        t = g.inbox_inject_text(items)
        self.assertIn('收件匣 2 則', t)
        self.assertIn('10:00 tradingbot #1（T-1）：A', t)
        self.assertIn('10:05 tradingbot #3：B', t)

    def test_empty_none(self):
        self.assertIsNone(g.inbox_inject_text([]))


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

    def test_ask_guard_denies_impl_woken_by_peer(self):
        t = self.proj / 'peer.jsonl'
        t.write_text(json.dumps({'type': 'user', 'message': {'content':
            '<cross-session-message from-name="demo #13">【轉達】x</cross-session-message>'}}) + '\n')
        out = self._run('ask-window-guard.sh', {'session_id': 'sid-imp', 'transcript_path': str(t),
                        'tool_input': {'questions': [{'question': '要授權嗎？', 'header': 'x'}]}})
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('demo #13', out['permissionDecisionReason'])

    def test_ask_guard_only_hints_impl(self):
        out = self._run('ask-window-guard.sh', {'session_id': 'sid-imp',
                        'tool_input': {'questions': [{'question': '選哪個？', 'header': 'x'}]}})
        self.assertNotIn('permissionDecision', out)
        self.assertIn('demo #13', out['additionalContext'])

    def test_peer_message_guard_hook(self):
        deny = self._run('peer-message-guard.sh', {'session_id': 'sid-imp', 'tool_name': 'SendMessage',
                         'tool_input': {'to': 'demo #13', 'message': 'T-1 已推送'}})
        self.assertEqual(deny['permissionDecision'], 'deny')
        self.assertIsNone(self._run('peer-message-guard.sh', {'session_id': 'sid-imp', 'tool_name': 'SendMessage',
                          'tool_input': {'to': 'demo #13｜窗口', 'message': '【問題】T-1 選哪個？'}}))
        ack = self._run('peer-message-guard.sh', {'session_id': 'sid-win', 'tool_name': 'SendMessage',
                        'tool_input': {'to': 'demo #1', 'message': '收到'}})
        self.assertEqual(ack['permissionDecision'], 'deny')

    def test_window_inbox_hook_injects_once(self):
        import todo_store, todo_coord
        con = todo_store.connect(self.home / '.claude/todos/.audit/demo.sqlite')
        todo_coord.inbox_post(con, '已推送待 tester', 'demo #1', todo_ids='T-9')
        con.close()
        imp = self._run('window-inbox.sh', {'session_id': 'sid-imp'})
        self.assertIsNone(imp)  # 非窗口不注入、也不標已讀
        out = self._run('window-inbox.sh', {'session_id': 'sid-win'})
        self.assertIn('已推送待 tester', out['additionalContext'])
        self.assertIsNone(self._run('window-inbox.sh', {'session_id': 'sid-win'}))

    def test_hooks_fail_open_on_garbage_input(self):
        import subprocess
        for hook in ('ask-window-guard.sh', 'design-docs-guard.sh',
                     'peer-message-guard.sh', 'window-inbox.sh'):
            r = subprocess.run(['bash', str(self.REPO / 'hooks' / hook)], cwd=self.proj,
                               input='not json{', capture_output=True, text=True, env=self.env)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, ''), hook)

    def test_design_docs_guard_deny_and_hint(self):
        deny = self._run('design-docs-guard.sh', {'tool_name': 'Agent', 'cwd': str(self.proj),
                         'tool_input': {'description': '三方共審', 'prompt': '歸屬公式已定，不要建議推翻'}})
        self.assertEqual(deny['permissionDecision'], 'deny')
        hint = self._run('design-docs-guard.sh', {'tool_name': 'SendMessage', 'cwd': str(self.proj),
                         'tool_input': {'to': 'x', 'message': '資金費歸屬怎麼拆'}})
        self.assertIn('position-attribution-model.md', hint['additionalContext'])


if __name__ == '__main__':
    unittest.main()
