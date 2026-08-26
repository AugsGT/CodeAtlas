"""Static analysis backend for Python, built on the stdlib `ast` module.

This is the only supported language for now. Name resolution is
deterministic and intra-repo only — no imported-package introspection,
no type inference:

  - a plain call `foo()` resolves to a function/class in the same
    module first, then to an unambiguous same-named entity elsewhere in
    the analyzed repo (skipped if the name is ambiguous or unknown)
  - `self.method()` resolves to a method on the immediately enclosing
    class
  - `var.method()` resolves only when `var` was assigned in the same
    function from a direct `var = ClassName(...)` call to a known class
    (no control-flow or reassignment tracking)
  - calls to names imported via `from a.b import foo` resolve using the
    import to locate the source module, provided a.b is inside the
    analyzed repo
  - calls at module or class-body level (no enclosing function/method)
    are not attributed to any caller and are skipped
  - relative imports (`from . import x`) are not resolved
  - imports of modules outside the analyzed repo produce no
    DEPENDS_ON edge
"""

import ast
import os

from ..graph.models import CodeEntity, Module
from ..graph.paths import canonical_path_key
from .base import AnalysisResult, CallEdge, StaticAnalyzer
from .quality import check_source


class PythonAstAnalyzer(StaticAnalyzer):
    def analyze(self, repo_root: str) -> AnalysisResult:
        repo_root = os.path.abspath(repo_root)
        result = AnalysisResult()

        py_files = self._find_python_files(repo_root)

        parsed = {}  # rel_path -> ast.Module
        entity_by_id = {}  # entity id -> CodeEntity
        entities_by_name = {}  # simple name -> list of entity ids
        module_by_dotted_name = {}  # dotted module name -> rel_path

        for filepath in py_files:
            rel_path = self._rel_path(filepath, repo_root)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source, filename=filepath)
            except SyntaxError as exc:
                # Still recorded as a Module (with no entities/calls/
                # depends_on, since none could be extracted) rather than
                # silently vanishing from the graph - a file that doesn't
                # even parse is itself something worth being able to ask
                # about ("what's happening in this file?"), not a reason
                # to pretend it doesn't exist.
                result.modules.append(Module(
                    path=rel_path, name=os.path.basename(rel_path), language="python",
                    parse_error=f"SyntaxError: {exc.msg} (line {exc.lineno})",
                    abs_path=canonical_path_key(filepath),
                ))
                continue
            except OSError:
                continue  # unreadable file (permissions, race with deletion, ...) - not a code problem to report

            parsed[rel_path] = tree
            module_by_dotted_name[self._module_dotted_name(rel_path)] = rel_path
            result.modules.append(
                Module(path=rel_path, name=os.path.basename(rel_path), language="python",
                       abs_path=canonical_path_key(filepath))
            )

            collector = _EntityCollector(rel_path, canonical_path_key(filepath))
            collector.visit(tree)
            for entity in collector.entities:
                result.entities.append(entity)
                result.contains.append((rel_path, entity.id))
                entity_by_id[entity.id] = entity
                entities_by_name.setdefault(entity.name, []).append(entity.id)

            for diagnostic in check_source(tree, filepath):
                result.diagnostics.append((rel_path, diagnostic))

        depends_on_seen = set()
        for rel_path, tree in parsed.items():
            import_map = _collect_import_map(tree, module_by_dotted_name)

            for target_path in _resolve_import_targets(tree, module_by_dotted_name):
                edge = (rel_path, target_path)
                if target_path != rel_path and edge not in depends_on_seen:
                    depends_on_seen.add(edge)
                    result.depends_on.append(edge)

            call_collector = _CallCollector(rel_path, import_map, entity_by_id, entities_by_name)
            call_collector.visit(tree)
            result.calls.extend(call_collector.calls)

        return result

    @staticmethod
    def _find_python_files(repo_root):
        found = []
        for dirpath, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [d for d in dirnames if d not in (".venv", "__pycache__", ".git")]
            for filename in filenames:
                if filename.endswith(".py"):
                    found.append(os.path.join(dirpath, filename))
        return sorted(found)

    @staticmethod
    def _rel_path(filepath, repo_root):
        return os.path.relpath(filepath, repo_root).replace(os.sep, "/")

    @staticmethod
    def _module_dotted_name(rel_path):
        if rel_path.endswith("/__init__.py"):
            rel_path = rel_path[: -len("/__init__.py")]
        else:
            rel_path = rel_path[: -len(".py")]
        return rel_path.replace("/", ".")


class _EntityCollector(ast.NodeVisitor):
    """First pass: find every function/method/class in a module and
    assign it a CodeEntity, stashing the id on the ast node itself
    (`_codeatlas_entity_id`) so the second pass can reuse it without
    recomputing the same qualname logic.
    """

    def __init__(self, rel_path, abs_path_key):
        self.rel_path = rel_path
        self.abs_path_key = abs_path_key
        self.module_dotted_name = PythonAstAnalyzer._module_dotted_name(rel_path)
        self.entities = []
        self._qualname_stack = []

    def visit_ClassDef(self, node):
        self._add_entity(node, kind="class")
        self._qualname_stack.append(node.name)
        self.generic_visit(node)
        self._qualname_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function_like(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function_like(node)

    def _visit_function_like(self, node):
        kind = "method" if self._qualname_stack else "function"
        self._add_entity(node, kind=kind)
        self._qualname_stack.append(node.name)
        self.generic_visit(node)
        self._qualname_stack.pop()

    def _add_entity(self, node, kind):
        qualname = ".".join([*self._qualname_stack, node.name])
        entity_id = f"{self.rel_path}::{qualname}"
        entity = CodeEntity(
            id=entity_id,
            qualified_name=f"{self.module_dotted_name}.{qualname}",
            name=node.name,
            kind=kind,
            module_path=self.rel_path,
            start_line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
            abs_path=self.abs_path_key,
            local_qualname=qualname,
        )
        node._codeatlas_entity_id = entity_id
        self.entities.append(entity)


def _collect_import_map(tree, module_by_dotted_name):
    """Map local name -> entity-resolvable (module rel_path, original name)
    for `from a.b import name [as local]` where a.b is inside the repo.
    Relative imports and `import x` are not represented here (calls via
    module attribute access, e.g. `x.foo()`, are not resolved).
    """
    import_map = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module_rel_path = module_by_dotted_name.get(node.module)
            if module_rel_path is None:
                continue
            for alias in node.names:
                local_name = alias.asname or alias.name
                import_map[local_name] = (module_rel_path, alias.name)
    return import_map


def _resolve_import_targets(tree, module_by_dotted_name):
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            target = module_by_dotted_name.get(node.module)
            if target:
                targets.append(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = module_by_dotted_name.get(alias.name)
                if target:
                    targets.append(target)
    return targets


class _CallCollector(ast.NodeVisitor):
    """Second pass: walk the tree tracking which CodeEntity (function or
    method) currently encloses us, and resolve ast.Call nodes to callee
    CodeEntity ids where possible.
    """

    def __init__(self, rel_path, import_map, entity_by_id, entities_by_name):
        self.rel_path = rel_path
        self.import_map = import_map
        self.entity_by_id = entity_by_id
        self.entities_by_name = entities_by_name
        self.calls = []
        self._caller_stack = []  # entity ids of enclosing functions/methods
        self._class_stack = []  # entity ids of enclosing classes
        self._local_var_class = {}  # var name -> class entity id, current function only

    def visit_ClassDef(self, node):
        class_entity_id = getattr(node, "_codeatlas_entity_id", None)
        self._class_stack.append(class_entity_id)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function_like(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function_like(node)

    def _visit_function_like(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)

        entity_id = getattr(node, "_codeatlas_entity_id", None)
        outer_local_vars = self._local_var_class
        self._local_var_class = {}
        if entity_id:
            self._caller_stack.append(entity_id)
        for stmt in node.body:
            self.visit(stmt)
        if entity_id:
            self._caller_stack.pop()
        self._local_var_class = outer_local_vars

    def visit_Assign(self, node):
        self.generic_visit(node)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            class_id = self._resolve_constructor_call(node.value)
            if class_id:
                self._local_var_class[node.targets[0].id] = class_id

    def visit_Call(self, node):
        self.generic_visit(node)
        if not self._caller_stack:
            return  # module/class-body level call: no CodeEntity caller
        callee_id = self._resolve_callee(node.func)
        if callee_id:
            self.calls.append(CallEdge(
                caller_id=self._caller_stack[-1],
                callee_id=callee_id,
                call_line=node.lineno,
            ))

    def _resolve_constructor_call(self, node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            candidate_id = f"{self.rel_path}::{node.func.id}"
            entity = self.entity_by_id.get(candidate_id)
            if entity and entity.kind == "class":
                return candidate_id
        return None

    def _resolve_callee(self, func_node):
        if isinstance(func_node, ast.Name):
            return self._resolve_name_call(func_node.id)
        if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
            receiver = func_node.value.id
            if receiver == "self" and self._class_stack and self._class_stack[-1]:
                candidate_id = f"{self._class_stack[-1]}.{func_node.attr}"
                if candidate_id in self.entity_by_id:
                    return candidate_id
                return None
            class_id = self._local_var_class.get(receiver)
            if class_id:
                candidate_id = f"{class_id}.{func_node.attr}"
                if candidate_id in self.entity_by_id:
                    return candidate_id
        return None

    def _resolve_name_call(self, name):
        # 1. imported from a known in-repo module
        imported = self.import_map.get(name)
        if imported:
            module_rel_path, original_name = imported
            candidate_id = f"{module_rel_path}::{original_name}"
            if candidate_id in self.entity_by_id:
                return candidate_id

        # 2. same-module entity (function or class constructor)
        same_module_id = f"{self.rel_path}::{name}"
        if same_module_id in self.entity_by_id:
            return same_module_id

        # 3. unambiguous same-named entity anywhere else in the repo
        candidates = self.entities_by_name.get(name, [])
        if len(candidates) == 1:
            return candidates[0]

        return None
