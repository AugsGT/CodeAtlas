"""Repository-agnostic entry-point and dependency detection.

Automatic execution must not assume `main.py`, `sample_repo`, a specific
package name, or a specific directory layout. This module inspects an
arbitrary Python repository and decides, deterministically, how it could
be safely started — or reports clearly that it can't, per the project
requirement to "report that clearly rather than pretending execution
succeeded" instead of guessing.

Detection order (most explicit/reliable first):

  1. `[project.scripts]` in pyproject.toml (PEP 621 console scripts) — if
     there is exactly one entry, its "module:function" target is the
     entry point. Ambiguous (more than one) is reported, not guessed.
  2. A `__main__.py` inside exactly one top-level package — runnable via
     `python -m <package>`.
  3. Exactly one top-level `*.py` file that has an
     `if __name__ == "__main__":` guard (excluding `setup.py` and
     `conftest.py`, which are tooling files, not application entry
     points). More than one such candidate is ambiguous and reported
     rather than picked arbitrarily.

Dependency detection is separate and best-effort: requirements.txt and
PEP 621 `[project.dependencies]` are supported; a Pipfile/poetry.lock is
noted but not parsed (reported as an unsupported manifest so the caller
can decide to run without installing anything rather than silently
skipping dependencies with no explanation).
"""

import ast
import os
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:  # Python < 3.11 fallback, not expected given requires-python
    tomllib = None

_EXCLUDED_ROOT_SCRIPTS = {"setup.py", "conftest.py"}
_EXCLUDED_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}


@dataclass
class EntrypointResult:
    found: bool
    # Exactly one of these is set when found is True.
    module: str | None = None          # run via `python -m <module>`
    script_path: str | None = None     # run via `python <script_path>` (relative to repo_root)
    callable_spec: tuple | None = None  # (module, function) run via import + call
    source: str = ""    # human-readable description of how this was chosen
    reason: str = ""    # populated when found is False: why nothing was chosen


@dataclass
class DependencyManifest:
    kind: str  # "requirements_txt" | "pyproject" | "unsupported" | "none"
    path: str | None = None       # relative to repo_root, when applicable
    requirements: list = field(default_factory=list)
    note: str = ""


def _top_level_py_files(repo_root):
    return sorted(
        f for f in os.listdir(repo_root)
        if f.endswith(".py") and os.path.isfile(os.path.join(repo_root, f))
    )


def _top_level_packages(repo_root):
    packages = []
    for name in sorted(os.listdir(repo_root)):
        full = os.path.join(repo_root, name)
        if name in _EXCLUDED_DIR_NAMES or not os.path.isdir(full):
            continue
        if os.path.isfile(os.path.join(full, "__init__.py")):
            packages.append(name)
    return packages


def _has_main_guard(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return False

    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__":
            comparators = [c.value for c in test.comparators if isinstance(c, ast.Constant)]
            if "__main__" in comparators:
                return True
    return False


def _top_level_syntax_errors(repo_root):
    """Which top-level scripts couldn't even be parsed - a candidate with
    a genuine `if __name__ == '__main__':` guard is still correctly
    rejected by _has_main_guard if the file has a syntax error, since
    there's no way to check for the guard (or run the file at all)
    without a valid parse. Surfaced separately so the final "no entry
    point found" reason can say WHY a file that looks runnable wasn't
    picked, instead of just that nothing was - a real point of user
    confusion when the only file in a repo has both a real syntax error
    and a real __main__ guard below it."""
    errors = {}
    for filename in _top_level_py_files(repo_root):
        if filename in _EXCLUDED_ROOT_SCRIPTS:
            continue
        full = os.path.join(repo_root, filename)
        try:
            with open(full, "r", encoding="utf-8") as f:
                ast.parse(f.read(), filename=full)
        except SyntaxError as exc:
            errors[filename] = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        except (OSError, UnicodeDecodeError):
            continue
    return errors


def _detect_console_script(repo_root):
    pyproject_path = os.path.join(repo_root, "pyproject.toml")
    if not os.path.isfile(pyproject_path) or tomllib is None:
        return None, None

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError if hasattr(tomllib, "TOMLDecodeError") else Exception):
        return None, None

    scripts = (data.get("project") or {}).get("scripts") or {}
    if len(scripts) != 1:
        return None, scripts

    (_, target), = scripts.items()
    if ":" not in target:
        return None, scripts
    module, _, func = target.partition(":")
    return (module, func), scripts


def detect_entrypoint(repo_root: str) -> EntrypointResult:
    repo_root = os.path.abspath(repo_root)

    callable_spec, scripts = _detect_console_script(repo_root)
    if callable_spec is not None:
        return EntrypointResult(
            found=True,
            callable_spec=callable_spec,
            source=f"pyproject.toml [project.scripts] -> {callable_spec[0]}:{callable_spec[1]}",
        )
    if scripts and len(scripts) > 1:
        return EntrypointResult(
            found=False,
            reason=f"pyproject.toml declares {len(scripts)} console scripts ({sorted(scripts)}); "
                   "cannot determine which one to run automatically.",
        )

    packages_with_main = [
        pkg for pkg in _top_level_packages(repo_root)
        if os.path.isfile(os.path.join(repo_root, pkg, "__main__.py"))
    ]
    if len(packages_with_main) == 1:
        pkg = packages_with_main[0]
        return EntrypointResult(found=True, module=pkg, source=f"{pkg}/__main__.py -> python -m {pkg}")
    if len(packages_with_main) > 1:
        return EntrypointResult(
            found=False,
            reason=f"multiple top-level packages have __main__.py ({sorted(packages_with_main)}); "
                   "cannot determine which one to run automatically.",
        )

    candidates = []
    for filename in _top_level_py_files(repo_root):
        if filename in _EXCLUDED_ROOT_SCRIPTS:
            continue
        full = os.path.join(repo_root, filename)
        if _has_main_guard(full):
            candidates.append(filename)

    if len(candidates) == 1:
        return EntrypointResult(
            found=True, script_path=candidates[0],
            source=f"{candidates[0]} has an `if __name__ == '__main__':` guard",
        )
    if len(candidates) > 1:
        return EntrypointResult(
            found=False,
            reason=f"multiple top-level scripts have an `if __name__ == '__main__':` guard "
                   f"({sorted(candidates)}); cannot determine which one to run automatically.",
        )

    parse_errors = _top_level_syntax_errors(repo_root)
    if parse_errors:
        details = "; ".join(f"{name} ({err})" for name, err in sorted(parse_errors.items()))
        return EntrypointResult(
            found=False,
            reason="no runnable entry point was found. A top-level script can't be checked "
                   f"for a `__main__` guard (or run at all) if it doesn't parse: {details}. "
                   "Fix the syntax error(s) first, or add a working console script, "
                   "__main__.py, or top-level script with an `if __name__ == '__main__':` guard.",
        )

    return EntrypointResult(
        found=False,
        reason="no console script, __main__.py, or top-level script with an "
               "`if __name__ == '__main__':` guard was found.",
    )


def detect_dependencies(repo_root: str) -> DependencyManifest:
    repo_root = os.path.abspath(repo_root)

    requirements_path = os.path.join(repo_root, "requirements.txt")
    if os.path.isfile(requirements_path):
        with open(requirements_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f]
        requirements = [line for line in lines if line and not line.startswith("#")]
        return DependencyManifest(kind="requirements_txt", path="requirements.txt", requirements=requirements)

    pyproject_path = os.path.join(repo_root, "pyproject.toml")
    if os.path.isfile(pyproject_path) and tomllib is not None:
        try:
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
            deps = (data.get("project") or {}).get("dependencies") or []
            if deps:
                return DependencyManifest(kind="pyproject", path="pyproject.toml", requirements=list(deps))
        except Exception:  # noqa: BLE001 - malformed pyproject.toml, treat as no manifest
            pass

    for unsupported in ("Pipfile", "poetry.lock", "environment.yml"):
        if os.path.isfile(os.path.join(repo_root, unsupported)):
            return DependencyManifest(
                kind="unsupported", path=unsupported,
                note=f"{unsupported} was found but is not parsed; the sandbox will run without "
                     "installing its dependencies.",
            )

    return DependencyManifest(kind="none", note="no dependency manifest found; the repository "
                                                  "appears to use only the standard library.")
