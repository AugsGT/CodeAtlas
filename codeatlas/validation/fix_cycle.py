"""The fix-validation cycle (spec section 14) and, separately, actually
applying a fix to the real repository (spec section 13: "eventually
support applying patches, but keep that separate from diagnosis").

validate_fix() applies a proposed `improved_code` snippet to a
disposable COPY of the repository (never the original), re-runs it the
same way `/api/execute` does, and reports whether the entity's
previously-observed failure is actually gone - rather than just
trusting the LLM's own claim that a fix "should" work.

apply_fix_to_repository() is the distinct, explicit, higher-stakes
action of writing that same snippet into the user's REAL source file -
never triggered by diagnosis or validation on their own, only by a
caller explicitly choosing to apply (see the /api/apply-fix route,
gated behind its own separate confirmation in the dashboard). It always
writes a timestamped backup of the original file first, so an
unsatisfactory fix can always be recovered without relying on the
user's own version control.

Both share the same line-replacement/re-indentation logic
(_compute_patched_lines) - the difference is only WHICH file(s) that
result gets written to.
"""

import os
import shutil
import tempfile
import textwrap
import time
from dataclasses import dataclass

from ..execution.pipeline import select_executor
from ..execution.streaming import read_records
from ..telemetry.otlp_receiver import parse_trace_request

DEFAULT_TIMEOUT_S = 30
_IGNORED_DIR_NAMES = (".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache")


@dataclass
class FixValidationResult:
    attempted: bool
    resolved: bool | None  # True = fixed, False = still failing, None = couldn't determine
    before_status: str = ""
    before_error: str = ""
    after_status: str = ""
    after_error: str = ""
    exit_code: int | None = None
    isolation: str = ""
    isolation_warning: str = ""
    explanation: str = ""
    reason: str = ""  # populated when attempted is False - why validation couldn't run at all


@dataclass
class ApplyFixResult:
    applied: bool
    file: str = ""
    backup_path: str = ""  # absolute path to the pre-fix copy of the file, for manual recovery
    reason: str = ""  # populated when applied is False - why nothing was written


def _before_state(repo, entity_id) -> dict:
    """The entity's most recently observed RuntimeSpan status/error -
    already sitting in the graph from real execution, so there's no need
    to re-run the ORIGINAL (unpatched) code just to establish a baseline.
    """
    rows = repo.query(
        "MATCH (e:CodeEntity {id: $id})-[:PRODUCES]->(s:RuntimeSpan) "
        "RETURN s.status, s.error_message ORDER BY s.start_time DESC LIMIT 1",
        {"id": entity_id},
    )
    if not rows:
        return {"status": "", "error_message": ""}
    status, error_message = rows[0]
    return {"status": status or "", "error_message": error_message or ""}


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _reindent(code: str, indent: str) -> str:
    """Best-effort re-indentation: the model's improved_code rarely
    matches the exact indentation of the function it's replacing
    (especially for a method nested in a class), so this strips
    whatever common leading whitespace the snippet has and reapplies the
    original block's own indentation instead of trusting the model's."""
    dedented = textwrap.dedent(code).strip("\n")
    lines = dedented.splitlines()
    reindented = "\n".join((indent + line if line.strip() else line) for line in lines)
    return reindented + "\n"


def _compute_patched_lines(lines: list, entity, improved_code: str) -> list:
    """Replace `entity`'s own line range within `lines` (a file's current
    lines) with `improved_code`, re-indented to match the block it's
    replacing. Raises ValueError if the entity's recorded line range
    doesn't fit `lines` (e.g. the file has changed since ingest) rather
    than silently corrupting it. Shared by both the disposable-copy
    validation path and the real-file apply path - they differ only in
    which file the result gets written to.
    """
    start_idx = entity.start_line - 1
    end_idx = entity.end_line  # end_line is inclusive/1-indexed, so this is the exclusive slice end
    if not (0 <= start_idx < end_idx <= len(lines)):
        raise ValueError(
            f"entity line range {entity.start_line}-{entity.end_line} is out of bounds "
            f"for {entity.module_path} ({len(lines)} lines) - the file may have changed"
        )

    original_indent = _leading_whitespace(lines[start_idx])
    patched_block = _reindent(improved_code, original_indent)
    return lines[:start_idx] + [patched_block] + lines[end_idx:]


def apply_patch_to_temp_workspace(repo_root: str, entity, improved_code: str) -> str:
    """Copy repo_root into a fresh temp directory and replace `entity`'s
    own line range in its module with `improved_code` there. The original
    repository is never opened for writing.
    """
    repo_root = os.path.abspath(repo_root)
    workspace = tempfile.mkdtemp(prefix="codeatlas_fix_validate_")
    dest = os.path.join(workspace, "repo")
    shutil.copytree(repo_root, dest, ignore=shutil.ignore_patterns(*_IGNORED_DIR_NAMES))

    target_file = os.path.join(dest, *entity.module_path.split("/"))
    if not os.path.isfile(target_file):
        shutil.rmtree(workspace, ignore_errors=True)
        raise ValueError(f"target file not found in workspace copy: {entity.module_path}")

    with open(target_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    try:
        new_lines = _compute_patched_lines(lines, entity, improved_code)
    except ValueError:
        shutil.rmtree(workspace, ignore_errors=True)
        raise

    with open(target_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return dest


def apply_fix_to_repository(repo, repo_root: str, entity_id: str, improved_code: str) -> ApplyFixResult:
    """Write `improved_code` into entity_id's REAL source file, in place
    of its current line range - unlike validate_fix/
    apply_patch_to_temp_workspace, this modifies the user's actual
    repository. Always writes a timestamped backup of the original file
    first (never overwritten on a repeat apply, so each attempt keeps
    its own recovery point) before touching the real file. This is a
    deliberate, explicit action - never triggered automatically by
    diagnosis or validation, only by a caller (see /api/apply-fix)
    choosing to apply a specific fix.

    Same line-range-based limitation as validate_fix: if the file has
    changed since the entity's start_line/end_line were recorded (ingest
    time), the bounds check in _compute_patched_lines catches a grossly
    out-of-range case, but can't detect the file having changed in a way
    that still happens to fit - re-ingesting before applying a fix keeps
    this accurate.
    """
    if not improved_code or not improved_code.strip():
        return ApplyFixResult(applied=False, reason="no improved_code was proposed to apply")

    entity = repo.get_code_entity(entity_id)
    if entity is None:
        return ApplyFixResult(applied=False, reason=f"unknown entity: {entity_id!r}")

    repo_root = os.path.abspath(repo_root)
    target_file = os.path.join(repo_root, *entity.module_path.split("/"))
    if not os.path.isfile(target_file):
        return ApplyFixResult(applied=False, reason=f"file not found: {entity.module_path}")

    with open(target_file, "r", encoding="utf-8") as f:
        original_lines = f.readlines()

    try:
        new_lines = _compute_patched_lines(original_lines, entity, improved_code)
    except ValueError as exc:
        return ApplyFixResult(applied=False, reason=str(exc))

    backup_path = f"{target_file}.codeatlas-backup-{int(time.time())}"
    with open(backup_path, "w", encoding="utf-8") as f:
        f.writelines(original_lines)

    with open(target_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return ApplyFixResult(applied=True, file=entity.module_path, backup_path=backup_path)


def _decode_spans(paths):
    spans = []
    for path in paths:
        with open(path, "rb") as f:
            for record in read_records(f):
                spans.extend(parse_trace_request(record))
    return spans


def _after_state(execution_result, entity) -> dict:
    if not execution_result.success:
        return {"status": "", "error_message": "", "exit_code": None, "crash": None,
                "reason": execution_result.reason}

    span_files = (
        ([execution_result.spans_path] if execution_result.spans_path else [])
        + list(execution_result.child_spans_paths)
    )
    spans = _decode_spans(span_files)

    target_name = entity.local_qualname.rsplit(".", 1)[-1] or entity.name
    matching = [s for s in spans if s.code_function == target_name]
    matching.sort(key=lambda s: s.start_time, reverse=True)

    latest = matching[0] if matching else None
    return {
        "status": latest.status if latest else "",
        "error_message": (latest.error_message if latest else "") or "",
        "exit_code": execution_result.exit_code,
        "crash": execution_result.crash,
    }


def _compare(before: dict, after: dict) -> bool | None:
    if before.get("status") != "ERROR":
        # Nothing failing was on record for this entity to begin with -
        # there's no "before" failure to confirm was resolved.
        return None
    if after.get("status") == "OK":
        return True
    if after.get("status") == "ERROR":
        return False
    if after.get("crash"):
        # The entity's own span didn't run/resolve, but the process
        # crashed some other way - still failing, just differently.
        return False
    return None  # the patched run produced no matching span at all - inconclusive, not a verdict


def _explain(before: dict, after: dict, resolved: bool | None) -> str:
    if resolved is True:
        return (
            f"Previously failed with {before.get('error_message') or 'an error'}; "
            "after applying the proposed fix, the same call completed successfully."
        )
    if resolved is False:
        if after.get("error_message"):
            return f"Still failing after the proposed fix: {after['error_message']}"
        if after.get("crash"):
            return f"The process crashed after the proposed fix: {after['crash'].get('type')}: {after['crash'].get('message')}"
        return "Still failing after the proposed fix."
    return "Could not determine whether the fix resolved the issue from this run's evidence."


def validate_fix(repo, repo_root: str, entity_id: str, improved_code: str,
                  timeout_s: int = DEFAULT_TIMEOUT_S) -> FixValidationResult:
    """Apply `improved_code` to a disposable copy of repo_root in place
    of `entity_id`'s current source, re-run the repository, and report
    whether that entity's previously-recorded failure is gone. Never
    modifies repo_root itself - see apply_patch_to_temp_workspace.
    """
    if not improved_code or not improved_code.strip():
        return FixValidationResult(attempted=False, resolved=None,
                                    reason="no improved_code was proposed to validate")

    entity = repo.get_code_entity(entity_id)
    if entity is None:
        return FixValidationResult(attempted=False, resolved=None,
                                    reason=f"unknown entity: {entity_id!r}")

    before = _before_state(repo, entity_id)

    try:
        workspace = apply_patch_to_temp_workspace(repo_root, entity, improved_code)
    except (OSError, ValueError) as exc:
        return FixValidationResult(attempted=False, resolved=None, reason=f"could not apply patch: {exc}")

    try:
        executor = select_executor()
        execution_result = executor.run(workspace)
        after = _after_state(execution_result, entity)
    finally:
        shutil.rmtree(os.path.dirname(workspace), ignore_errors=True)

    if not execution_result.success:
        return FixValidationResult(
            attempted=False, resolved=None,
            reason=f"could not re-run the patched workspace: {execution_result.reason}",
        )

    resolved = _compare(before, after)
    return FixValidationResult(
        attempted=True,
        resolved=resolved,
        before_status=before.get("status", ""),
        before_error=before.get("error_message", ""),
        after_status=after.get("status", ""),
        after_error=after.get("error_message", ""),
        exit_code=after.get("exit_code"),
        isolation=execution_result.isolation,
        isolation_warning=execution_result.isolation_warning,
        explanation=_explain(before, after, resolved),
    )
