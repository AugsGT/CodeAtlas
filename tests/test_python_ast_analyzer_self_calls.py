import textwrap

from codeatlas.analysis.python_ast import PythonAstAnalyzer


def analyze_source(tmp_path, source):
    (tmp_path / "mod.py").write_text(textwrap.dedent(source))
    return PythonAstAnalyzer().analyze(str(tmp_path))


def test_resolves_self_method_call(tmp_path):
    result = analyze_source(tmp_path, """
        class Widget:
            def helper(self):
                return 1

            def main(self):
                return self.helper()
    """)

    calls = {(c.caller_id, c.callee_id) for c in result.calls}
    assert ("mod.py::Widget.main", "mod.py::Widget.helper") in calls


def test_skips_module_level_calls_with_no_caller(tmp_path):
    result = analyze_source(tmp_path, """
        def foo():
            return 1

        foo()
    """)

    assert result.calls == []


def test_skips_unresolvable_attribute_calls(tmp_path):
    result = analyze_source(tmp_path, """
        def use(obj):
            return obj.method_of_unknown_type()
    """)

    assert result.calls == []
