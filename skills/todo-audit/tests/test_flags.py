import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import todo_flags


class TestFlags(unittest.TestCase):
    def test_has_set_clear_toggle_roundtrip(self):
        p = 0
        self.assertFalse(todo_flags.has(p, 'implemented'))
        p = todo_flags.set_(p, 'implemented')
        self.assertTrue(todo_flags.has(p, 'implemented'))
        p = todo_flags.toggle(p, 'implemented')
        self.assertFalse(todo_flags.has(p, 'implemented'))
        p = todo_flags.set_(p, 'implemented')
        p = todo_flags.clear(p, 'implemented')
        self.assertFalse(todo_flags.has(p, 'implemented'))

    def test_unknown_flag_name_raises(self):
        with self.assertRaises(ValueError):
            todo_flags.has(0, 'not_a_flag')
        with self.assertRaises(ValueError):
            todo_flags.set_(0, 'not_a_flag')
        with self.assertRaises(ValueError):
            todo_flags.clear(0, 'not_a_flag')
        with self.assertRaises(ValueError):
            todo_flags.toggle(0, 'not_a_flag')

    def test_is_complete_requires_all_seven_bits(self):
        p = 0
        for name in todo_flags.ORDER[:-1]:
            p = todo_flags.set_(p, name)
        self.assertFalse(todo_flags.is_complete(p))
        p = todo_flags.set_(p, todo_flags.ORDER[-1])
        self.assertTrue(todo_flags.is_complete(p))
        self.assertEqual(p, todo_flags.ALL_FLAGS)

    def test_all_flags_equals_127(self):
        self.assertEqual(todo_flags.ALL_FLAGS, 127)
        self.assertEqual(len(todo_flags.FLAGS), 7)
        self.assertEqual(set(todo_flags.FLAGS), set(todo_flags.ORDER))

    def test_flags_are_distinct_powers_of_two(self):
        values = sorted(todo_flags.FLAGS.values())
        self.assertEqual(values, [1, 2, 4, 8, 16, 32, 64])

    def test_summary_lists_only_set_flags_in_fixed_order(self):
        p = todo_flags.set_(todo_flags.set_(0, 'deployed'), 'implemented')
        self.assertEqual(todo_flags.summary(p), ['implemented', 'deployed'])

    def test_none_progress_treated_as_zero(self):
        self.assertFalse(todo_flags.has(None, 'implemented'))
        self.assertEqual(todo_flags.summary(None), [])
        self.assertFalse(todo_flags.is_complete(None))


if __name__ == '__main__':
    unittest.main()
