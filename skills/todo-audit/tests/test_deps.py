"""REQ 編號的作用域：本檔的 REQ-n 是「新增依賴圖純函式模組 todo_deps」那次交付（commit 078fd3f）的需求編號。
本 repo 的 REQ 編號**逐檔案局部有效** —— 不同測試檔的 REQ-1 指涉
完全不同的需求，不要跨檔對照。原始需求文件住在交付當下的 castpower
工作目錄（`.castpower/`，被 gitignore 完全排除、不進版控），所以這裡
指的是 **commit**：`git show 078fd3f` 永遠查得到，路徑不會。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_deps  # noqa: E402  (path 必須先 insert 才能 import)


def _is_valid_cycle_path(path, known_edges, extra_edge):
    """驗證 path 是不是一條「真的」環：頭尾同一個節點，且每一段
    (path[i], path[i+1]) 都對應到既有邊或剛插入的那條新邊。

    這比只檢查「有沒有回傳值」/「某些節點有沒有出現」更嚴謹——
    REQ-2 要求錯誤訊息附「具體的環路徑」，若 find_cycle/any_cycle
    回傳的 path 不是真的沿著邊走出來的合法環，呼叫端組出的錯誤訊息
    就會誤導使用者（例如把不相鄰的兩點接在一起）。
    """
    if not path or path[0] != path[-1]:
        return False
    edge_set = set(known_edges)
    edge_set.add(extra_edge)
    return all((a, b) in edge_set for a, b in zip(path, path[1:]))


class TestValidateKind(unittest.TestCase):
    # REQ-1
    def test_known_kinds_pass(self):
        for k in ('blocks', 'related', 'parent-child', 'discovered-from'):
            todo_deps.validate_kind(k)  # 不 raise 即通過

    # REQ-1
    def test_unknown_kind_raises_with_offending_value_in_message(self):
        # 錯誤訊息要帶上實際傳入的壞值，不能只是籠統的「kind 不合法」——
        # 否則使用者打錯字（例如 blocks 打成 block）除錯時看不出是哪個值錯了。
        with self.assertRaisesRegex(ValueError, 'bogus'):
            todo_deps.validate_kind('bogus')

    # REQ-1（邊界：大小寫視為不同值，不做寬鬆比對）
    def test_case_variant_of_known_kind_raises(self):
        with self.assertRaises(ValueError):
            todo_deps.validate_kind('Blocks')

    # REQ-1（邊界：空字串不是合法 kind）
    def test_empty_string_kind_raises(self):
        with self.assertRaises(ValueError):
            todo_deps.validate_kind('')


class TestFindCycle(unittest.TestCase):
    # REQ-2 / REQ-10（G-1：find_cycle 回傳的必須是可組出具體訊息的路徑，不是純 bool）
    def test_direct_cycle_returns_valid_path(self):
        # A blocks B，現在要加 B blocks A —— 直接互相阻塞
        edges = [('A', 'B')]
        cyc = todo_deps.find_cycle(edges, 'B', 'A')
        self.assertIsNotNone(cyc)
        self.assertIsInstance(cyc, list)
        self.assertTrue(_is_valid_cycle_path(cyc, edges, ('B', 'A')))
        self.assertIn('A', cyc)
        self.assertIn('B', cyc)

    # REQ-2
    def test_indirect_cycle_returns_valid_path(self):
        # A blocks B blocks C，現在要加 C blocks A —— 三節點環
        edges = [('A', 'B'), ('B', 'C')]
        cyc = todo_deps.find_cycle(edges, 'C', 'A')
        self.assertIsNotNone(cyc)
        self.assertTrue(_is_valid_cycle_path(cyc, edges, ('C', 'A')))
        for node in ('A', 'B', 'C'):
            self.assertIn(node, cyc)

    # REQ-2（邊界：條目依賴自己，A blocks A，一定要被當成環擋下）
    def test_self_dependency_is_a_cycle(self):
        cyc = todo_deps.find_cycle([], 'A', 'A')
        self.assertIsNotNone(cyc)

    # REQ-2（邊界：合法的鑽石形依賴不該被誤判成環）
    def test_diamond_shape_is_not_a_false_positive(self):
        # A blocks B、A blocks C、B blocks D、C blocks D 是合法的鑽石形，
        # 加 D blocks E 不該被誤判成環
        edges = [('A', 'B'), ('A', 'C'), ('B', 'D'), ('C', 'D')]
        self.assertIsNone(todo_deps.find_cycle(edges, 'D', 'E'))

    # REQ-2（邊界：跟既有邊完全無關的新邊不該被誤判）
    def test_unrelated_edge_addition_has_no_cycle(self):
        edges = [('A', 'B')]
        self.assertIsNone(todo_deps.find_cycle(edges, 'C', 'D'))

    # REQ-2（邊界：空 edges 加第一條邊不可能成環）
    def test_empty_edges_never_cycles(self):
        self.assertIsNone(todo_deps.find_cycle([], 'A', 'B'))


class TestAnyCycle(unittest.TestCase):
    # REQ-2 / REQ-8（doctor 用的全圖複查，路徑一樣要是真的環）
    def test_detects_cycle_in_existing_edge_set(self):
        edges = [('A', 'B'), ('B', 'C'), ('C', 'A')]
        cyc = todo_deps.any_cycle(edges)
        self.assertIsNotNone(cyc)
        self.assertTrue(_is_valid_cycle_path(cyc, edges, edges[0]))

    # REQ-2（邊界：DB 裡殘留的自我依賴，doctor 複查也要抓得到）
    def test_self_loop_edge_detected(self):
        edges = [('A', 'A')]
        self.assertIsNotNone(todo_deps.any_cycle(edges))

    # REQ-2（邊界：合法鑽石形的全圖複查不該誤判成環）
    def test_dag_has_no_cycle(self):
        edges = [('A', 'B'), ('A', 'C'), ('B', 'D'), ('C', 'D')]
        self.assertIsNone(todo_deps.any_cycle(edges))

    # REQ-2（邊界：空圖没有環）
    def test_empty_edges_has_no_cycle(self):
        self.assertIsNone(todo_deps.any_cycle([]))


class TestIsReady(unittest.TestCase):
    # REQ-3
    def test_pending_with_no_blockers_is_ready(self):
        self.assertTrue(todo_deps.is_ready('pending', []))

    # REQ-3
    def test_pending_with_all_blockers_done_or_unpick_is_ready(self):
        self.assertTrue(todo_deps.is_ready('pending', ['done', 'unpick']))

    # REQ-3
    def test_pending_with_one_blocker_still_pending_is_not_ready(self):
        self.assertFalse(todo_deps.is_ready('pending', ['done', 'doing']))

    # REQ-3 / REQ-5（status 只有四值互斥，非 pending 的條目不會出現在 ready 佇列）
    def test_non_pending_status_is_never_ready(self):
        self.assertFalse(todo_deps.is_ready('doing', []))
        self.assertFalse(todo_deps.is_ready('done', []))
        self.assertFalse(todo_deps.is_ready('unpick', []))

    # REQ-3（邊界：blocker 狀態查不到，例如懸空邊指向不存在的條目，
    # 不該被寬鬆地當成「沒有 blocker」而誤判成 ready）
    def test_blocker_with_unknown_status_is_not_ready(self):
        self.assertFalse(todo_deps.is_ready('pending', ['done', None]))


class TestNewlyUnblocked(unittest.TestCase):
    # REQ-4
    def test_direct_downstream_becomes_ready(self):
        # A blocks B；A 剛轉 done，B 沒有其他 blocker，應該變 ready
        edges = [('A', 'B')]
        statuses = {'A': 'done', 'B': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), ['B'])

    # REQ-4（mark unpick 也要能觸發解阻塞提示，不是只有 done）
    def test_unpick_also_unblocks_downstream(self):
        edges = [('A', 'B')]
        statuses = {'A': 'unpick', 'B': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), ['B'])

    # REQ-4
    def test_still_blocked_by_other_blocker_is_excluded(self):
        # A blocks C、X blocks C；A 剛轉 done，但 X 還沒完成，C 不該出現
        edges = [('A', 'C'), ('X', 'C')]
        statuses = {'A': 'done', 'X': 'pending', 'C': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), [])

    # REQ-4（out-of-scope 條款：只看直接下游，不做遞移預告）
    def test_indirect_downstream_not_included(self):
        # A blocks B blocks C —— A 完成只解 B，不遞移去看 C
        edges = [('A', 'B'), ('B', 'C')]
        statuses = {'A': 'done', 'B': 'pending', 'C': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), ['B'])

    # REQ-4
    def test_non_pending_downstream_excluded(self):
        edges = [('A', 'B')]
        statuses = {'A': 'done', 'B': 'doing'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), [])

    # REQ-4（邊界：完全沒有下游時回傳空 list，不是 None 或例外）
    def test_no_downstream_returns_empty_list(self):
        edges = [('X', 'Y')]
        statuses = {'A': 'done', 'X': 'pending', 'Y': 'pending'}
        self.assertEqual(todo_deps.newly_unblocked('A', edges, statuses), [])

    # REQ-4（邊界：一次完成同時解開多條下游，全部要回傳）
    def test_multiple_downstream_all_ready_are_returned(self):
        edges = [('A', 'B'), ('A', 'C')]
        statuses = {'A': 'done', 'B': 'pending', 'C': 'pending'}
        result = todo_deps.newly_unblocked('A', edges, statuses)
        self.assertEqual(sorted(result), ['B', 'C'])


if __name__ == '__main__':
    unittest.main()
