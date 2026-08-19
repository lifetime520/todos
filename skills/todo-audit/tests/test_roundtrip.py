import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / 'scripts'))
sys.path.insert(0, str(TESTS))
import todo_store
from fixtures import all_projects, latest_snapshot


class TestRoundTrip(unittest.TestCase):
    """render(parse(t)) == t，對真實資料逐 byte 相同。

    這是遷移的第一道硬閘門：渲染不出原檔，就代表 DB 存不下全部資訊。

    輸入來自 .audit/ 的遷移備份而非 ~/.claude/todos/*.md —— 後者已於
    Phase 4 刪除，若還指著它，測試會全部靜默 skip 而報 OK。
    """

    def _assert_identical(self, path):
        original = path.read_text(encoding='utf-8')
        out = todo_store.render(todo_store.parse_md_lossless(original))
        if out != original:
            a, b = original.split('\n'), out.split('\n')
            for i in range(max(len(a), len(b))):
                x = a[i] if i < len(a) else '<EOF>'
                y = b[i] if i < len(b) else '<EOF>'
                if x != y:
                    self.fail(f'{path.name} 第 {i+1} 行不符\n'
                              f'  原始: {x!r}\n  渲染: {y!r}')
        self.assertEqual(out, original)

    def test_fixtures_exist(self):
        """守衛：沒有 fixture 就是保護失效，必須紅燈而不是靜默 skip。"""
        projects = all_projects()
        self.assertTrue(projects, '找不到任何 pre-migrate 備份 —— round-trip 保護已失效')
        for p in projects:
            self.assertIsNotNone(latest_snapshot(p), f'{p} 缺備份')

    def test_all_real_projects_roundtrip(self):
        projects = all_projects()
        self.assertTrue(projects, '無 fixture')
        for p in projects:
            with self.subTest(project=p):
                self._assert_identical(latest_snapshot(p))


if __name__ == '__main__':
    unittest.main()
