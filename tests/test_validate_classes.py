"""The class-structure check in tools/validate_classes.py finds what it should."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "validate_classes.py"
_spec = importlib.util.spec_from_file_location("validate_classes", _TOOL)
validate_classes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_classes)


def _problems(source: str):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sample.py"
        path.write_text(source, encoding="utf-8")
        return validate_classes.check_file(path)


class ValidateClassesTests(unittest.TestCase):
    def test_a_normal_class_passes(self):
        classes, errors = _problems(
            'class A:\n    """Doc."""\n\n    def __init__(self):\n        pass\n'
        )
        self.assertEqual((classes, errors), (1, []))

    def test_two_inits_in_one_class_are_reported(self):
        _, errors = _problems(
            "class A:\n"
            "    def __init__(self):\n        pass\n"
            "    def __init__(self, x):\n        pass\n"
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("2 __init__ methods", errors[0])

    def test_a_string_left_behind_in_a_class_body_is_reported(self):
        _, errors = _problems(
            'class A:\n    """Doc."""\n\n    x = 1\n\n    """Orphaned."""\n'
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("orphaned string literal", errors[0])

    def test_a_syntax_error_is_reported_not_raised(self):
        classes, errors = _problems("class A(:\n    pass\n")
        self.assertEqual(classes, 0)
        self.assertIn("SyntaxError", errors[0])

    def test_the_integration_itself_is_clean(self):
        folder = Path(__file__).resolve().parent.parent / "custom_components" / "mg_saic"
        total = 0
        for path in sorted(folder.glob("*.py")):
            classes, errors = validate_classes.check_file(path)
            self.assertEqual(errors, [], str(path))
            total += classes
        self.assertGreater(total, 0)

    def test_the_script_exits_non_zero_when_something_is_wrong(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "bad.py").write_text(
                "class A:\n    def __init__(self):\n        pass\n"
                "    def __init__(self):\n        pass\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_classes.main(["x", folder]), 1)
            (Path(folder) / "bad.py").write_text("class A:\n    pass\n", encoding="utf-8")
            self.assertEqual(validate_classes.main(["x", folder]), 0)


if __name__ == "__main__":
    unittest.main()
