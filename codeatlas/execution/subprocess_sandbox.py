# Subprocess-based execution fallback, used when the Docker sandbox isn't available. Same detect-entry-point -> run bootstrap.py -> decode-output flow as DockerExecutor, just without a container.

import os
import subprocess
import sys
import tempfile

from . import entrypoint as entrypoint_mod
from .base import ExecutionResult, read_bootstrap_output

DEFAULT_TIMEOUT_S = 30
_PROCESS_OVERHEAD_BUFFER_S = 5

ISOLATION_WARNING = (
    "Docker was not available, so this ran as a plain restricted subprocess instead: "
    "no network isolation, no memory/CPU limits, and it shares this machine's filesystem "
    "permissions. Start Docker for stronger isolation."
)


class SubprocessExecutor:
    def __init__(self, timeout_s=DEFAULT_TIMEOUT_S):
        # Initialize the executor with a default timeout
        self.timeout_s = timeout_s

    def run(self, repo_root: str) -> ExecutionResult:
        # Run the code in the given repository root and return the result
        repo_root = os.path.abspath(repo_root)
        entry = entrypoint_mod.detect_entrypoint(repo_root)
        if not entry.found:
            return ExecutionResult(success=False, reason=f"could not determine a safe entry point: {entry.reason}")

        manifest = entrypoint_mod.detect_dependencies(repo_root)
        dependency_warning = ""
        if manifest.kind in ("requirements_txt", "pyproject"):
            dependency_warning = (
                "this repository declares dependencies, but the subprocess fallback does not install "
                "them (only the Docker sandbox does) - it ran against whatever is already available in "
                "CodeAtlas's own Python environment, so imports may fail."
            )
        elif manifest.kind == "unsupported":
            dependency_warning = manifest.note

        output_dir = tempfile.mkdtemp(prefix="codeatlas_subprocess_exec_")
        args = self._build_args(repo_root, output_dir, entry)

        timed_out = False
        exit_code = None
        stdout, stderr = "", ""
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, cwd=repo_root,
                timeout=self.timeout_s + _PROCESS_OVERHEAD_BUFFER_S,
            )
            exit_code = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _decode(exc.stdout)
            stderr = _decode(exc.stderr)

        return ExecutionResult(
            success=True,
            isolation="subprocess",
            isolation_warning=ISOLATION_WARNING,
            entrypoint_source=entry.source,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            dependency_warning=dependency_warning,
            **read_bootstrap_output(output_dir),
        )

    def _build_args(self, repo_root, output_dir, entry):
        # Build the command arguments to run the bootstrap script
        args = [
            sys.executable, "-m", "codeatlas.execution.bootstrap",
            "--repo-root", repo_root, "--output-dir", output_dir,
        ]
        if entry.callable_spec:
            args += ["--callable", f"{entry.callable_spec[0]}:{entry.callable_spec[1]}"]
        elif entry.module:
            args += ["--module", entry.module]
        else:
            args += ["--script", entry.script_path]
        return args


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""