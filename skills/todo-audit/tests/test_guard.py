import json
import subprocess
import unittest
from pathlib import Path

GUARD = Path.home() / '.claude' / 'hooks' / 'todo-guard.sh'
HOME = str(Path.home())


def call(tool, tool_input):
    payload = json.dumps({'tool_name': tool, 'tool_input': tool_input})
    return subprocess.run(['bash', str(GUARD)], input=payload,
                          capture_output=True, text=True)


class TestGuard(unittest.TestCase):
    """PreToolUse hook：擋直接存取，放行官方入口，且不得誤擋無關操作。

    對應 spec §9.1 的白名單邊界表，逐列一條測試。
    """

    def test_deny_read_of_todo_md(self):
        r = call('Read', {'file_path': f'{HOME}/.claude/todos/tradingbot.md'})
        self.assertEqual(r.returncode, 2)
        self.assertIn('todo_cli.py', r.stderr)

    def test_deny_grep_into_todos_dir(self):
        r = call('Grep', {'pattern': 'P0', 'path': f'{HOME}/.claude/todos'})
        self.assertEqual(r.returncode, 2)

    def test_deny_bash_cat_of_todo(self):
        r = call('Bash', {'command': f'cat {HOME}/.claude/todos/tradingbot.md'})
        self.assertEqual(r.returncode, 2)

    def test_deny_bash_sqlite3_direct(self):
        r = call('Bash', {'command':
                          f'sqlite3 {HOME}/.claude/todos/.audit/tradingbot.sqlite "SELECT 1"'})
        self.assertEqual(r.returncode, 2)

    def test_allow_official_cli(self):
        r = call('Bash', {'command':
                          'python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py list'})
        self.assertEqual(r.returncode, 0)

    def test_allow_helper_scripts(self):
        r = call('Bash', {'command': 'bash ~/.claude/hooks/todo-add.sh "x" "y" "z"'})
        self.assertEqual(r.returncode, 0)

    def test_does_not_overblock_unrelated_grep(self):
        # 誤擋回歸：搜尋 hook 原始碼是正當操作
        r = call('Bash', {'command': f'grep -r todos {HOME}/.claude/hooks/'})
        self.assertEqual(r.returncode, 0)

    def test_does_not_overblock_other_projects(self):
        r = call('Read', {'file_path': '/tmp/todos/something.md'})
        self.assertEqual(r.returncode, 0)

    def test_does_not_overblock_ordinary_file(self):
        r = call('Read', {'file_path': '/Users/x/project/src/main.py'})
        self.assertEqual(r.returncode, 0)

    def test_deny_chained_bypass(self):
        # 串接繞過：前半直接讀，後半掛個官方入口充門面
        r = call('Bash', {'command':
                          f'cat {HOME}/.claude/todos/tradingbot.md && '
                          'python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py list'})
        self.assertEqual(r.returncode, 2, '串接繞過必須被擋')

    def test_deny_subshell_bypass(self):
        r = call('Bash', {'command':
                          f'echo $(cat {HOME}/.claude/todos/tradingbot.md)'})
        self.assertEqual(r.returncode, 2)

    def test_allow_audit_with_explicit_db_path(self):
        # todo_audit.py 吃 .sqlite 路徑是官方用法，必須放行
        r = call('Bash', {'command':
                          'python3 ~/.claude/skills/todo-audit/scripts/todo_audit.py '
                          f'{HOME}/.claude/todos/.audit/tradingbot.sqlite .'})
        self.assertEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
