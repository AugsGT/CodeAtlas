# This file contains code for running an arbitrary repository in a Docker container for isolated execution.

import os
import shutil
import subprocess
import tempfile
import uuid

from . import entrypoint as entrypoint_mod
from .base import ExecutionResult, read_bootstrap_output

IMAGE_NAME = "codeatlas-sandbox:latest"
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DOCKERFILE_PATH = os.path.join(os.path.dirname(__file__), "docker", "Dockerfile")

DEFAULT_EXECUTE_TIMEOUT_S = 30
DEFAULT_PREPARE_TIMEOUT_S = 120
DEFAULT_MEMORY_MB = 512
DEFAULT_CPUS = 1.0
_DOCKER_OVERHEAD_BUFFER_S = 15  # extra wall-clock slack given to `docker run` itself beyond the container's own timeout


def docker_availability():
    """Checks if Docker is installed and available on the system."""
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False, "the `docker` CLI is not installed or not on PATH."
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run `docker info`: {exc}"
    if result.returncode != 0:
        return False, f"Docker daemon is not available: {result.stderr.strip() or result.stdout.strip()}"
    return True, "Docker is available."


def image_exists():
    """Checks if the Docker sandbox image exists."""
    result = subprocess.run(["docker", "image", "inspect", IMAGE_NAME], capture_output=True, text=True)
    return result.returncode == 0


def build_image():
    """Builds the Docker sandbox image. The build context is the CodeAtlas project root."""
    result = subprocess.run(
        ["docker", "build", "-f", _DOCKERFILE_PATH, "-t", IMAGE_NAME, _PROJECT_ROOT],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to build sandbox image:\n{result.stdout}\n{result.stderr}")


class DockerExecutor:
    def __init__(self, memory_mb=DEFAULT_MEMORY_MB, cpus=DEFAULT_CPUS,
                 execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S, prepare_timeout_s=DEFAULT_PREPARE_TIMEOUT_S):
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.execute_timeout_s = execute_timeout_s
        self.prepare_timeout_s = prepare_timeout_s

    def run(self, repo_root: str) -> ExecutionResult:
        """Runs the repository in a Docker container."""
        available, detail = docker_availability()
        if not available:
            return ExecutionResult(success=False, reason=f"Docker sandbox unavailable: {detail}")

        repo_root = os.path.abspath(repo_root)
        entry = entrypoint_mod.detect_entrypoint(repo_root)
        if not entry.found:
            return ExecutionResult(success=False, reason=f"could not determine a safe entry point: {entry.reason}")

        if not image_exists():
            try:
                build_image()
            except RuntimeError as exc:
                return ExecutionResult(success=False, reason=str(exc))

        workspace = tempfile.mkdtemp(prefix="codeatlas_sandbox_")
        deps_dir = os.path.join(workspace, "deps")
        output_dir = os.path.join(workspace, "output")
        os.makedirs(deps_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        dependency_install_log = ""
        dependency_warning = ""
        manifest = entrypoint_mod.detect_dependencies(repo_root)
        if manifest.kind == "unsupported":
            dependency_warning = manifest.note
        elif manifest.requirements:
            ok, log = self._prepare_dependencies(repo_root, manifest, deps_dir, workspace)
            dependency_install_log = log
            if not ok:
                dependency_warning = "dependency installation failed; running with only the standard library available."

        container_name = f"codeatlas-exec-{uuid.uuid4().hex[:12]}"
        run_args = self._build_execute_args(container_name, repo_root, deps_dir, output_dir, entry)

        timed_out = False
        exit_code = None
        stdout, stderr = "", ""
        try:
            proc = subprocess.run(
                run_args, capture_output=True, text=True,
                timeout=self.execute_timeout_s + _DOCKER_OVERHEAD_BUFFER_S,
            )
            exit_code = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            subprocess.run(["docker", "kill", container_name], capture_output=True)
        finally:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        return self._collect_result(
            entry, output_dir, exit_code, timed_out, stdout, stderr,
            dependency_install_log, dependency_warning,
        )

    def _build_execute_args(self, container_name, repo_root, deps_dir, output_dir, entry):
        """Builds the arguments for running the Docker container."""
        args = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", "none",
            f"--memory={self.memory_mb}m",
            f"--cpus={self.cpus}",
            "--pids-limit=256",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs=/tmp:rw,size=64m",
            "-v", f"{repo_root}:/repo:ro",
            "-v", f"{deps_dir}:/deps:ro",
            "-v", f"{output_dir}:/output:rw",
            "--env", "PYTHONPATH=/deps",
            IMAGE_NAME,
            "--repo-root", "/repo", "--output-dir", "/output",
        ]
        if entry.callable_spec:
            args += ["--callable", f"{entry.callable_spec[0]}:{entry.callable_spec[1]}"]
        elif entry.module:
            args += ["--module", entry.module]
        else:
            args += ["--script", entry.script_path]
        return args

    def _prepare_dependencies(self, repo_root, manifest, deps_dir, workspace):
        """Prepares the dependencies for the Docker container."""
        if manifest.kind == "requirements_txt":
            requirements_arg = ["-r", f"/repo/{manifest.path}"]
            mount_repo_for_prepare = ["-v", f"{repo_root}:/repo:ro"]
        else:
            req_file = os.path.join(workspace, "requirements.generated.txt")
            with open(req_file, "w", encoding="utf-8") as f:
                f.write("\n".join(manifest.requirements))
            requirements_arg = ["-r", "/generated/requirements.generated.txt"]
            mount_repo_for_prepare = ["-v", f"{workspace}:/generated:ro"]

        args = [
            "docker", "run", "--rm", "--entrypoint", "pip",
            *mount_repo_for_prepare,
            "-v", f"{deps_dir}:/deps:rw",
            IMAGE_NAME,
            "install", "--no-cache-dir", "--target", "/deps", *requirements_arg,
        ]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=self.prepare_timeout_s)
            return proc.returncode == 0, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            return False, f"dependency installation timed out after {self.prepare_timeout_s}s"

    def _collect_result(self, entry, output_dir, exit_code, timed_out, stdout, stderr,
                         dependency_install_log, dependency_warning):
        """Collects the result of running the Docker container."""
        return ExecutionResult(
            success=True,
            isolation="docker",
            entrypoint_source=entry.source,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            dependency_install_log=dependency_install_log,
            dependency_warning=dependency_warning,
            **read_bootstrap_output(output_dir),
        )