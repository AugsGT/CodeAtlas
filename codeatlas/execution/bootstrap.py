# This file is the entry point for running CodeAtlas inside a sandbox. It sets up tracing, runs the detected entry point, and captures telemetry.

import argparse
import importlib
import json
import os
import runpy
import sys
import tempfile
import traceback

from .tracer import ENV_REPO_ROOT, ENV_SPANS_OUTPUT, SITECUSTOMIZE_SOURCE, make_streaming_tracer

SPANS_FILENAME = "spans.otlp"
CRASH_FILENAME = "crash.json"

def _enable_otel_auto_instrumentation():
    """Enables automatic instrumentation for OpenTelemetry. This helps in capturing higher-level spans (like HTTP requests or database calls) that might not be captured by function tracing alone. Returns a list of instrumentor names that were successfully enabled."""
    enabled = []
    try:
        from importlib.metadata import entry_points
        instrumentor_entry_points = entry_points(group="opentelemetry_instrumentor")
    except Exception:  # noqa: BLE001 - metadata lookup failing shouldn't block execution
        return enabled

    for ep in instrumentor_entry_points:
        try:
            instrumentor_class = ep.load()
            instrumentor_class().instrument()
            enabled.append(ep.name)
        except Exception:  # noqa: BLE001 - one bad/incompatible instrumentor shouldn't block others
            continue
    return enabled

def _install_child_process_propagation(repo_root, spans_output_path):
    """Sets up the environment for any Python child process this run spawns. It points these child processes to use the same tracer by modifying the PYTHONPATH and setting environment variables."""
    sitecustomize_dir = tempfile.mkdtemp(prefix="codeatlas_sitecustomize_")
    with open(os.path.join(sitecustomize_dir, "sitecustomize.py"), "w", encoding="utf-8") as f:
        f.write(SITECUSTOMIZE_SOURCE)

    os.environ[ENV_REPO_ROOT] = repo_root
    os.environ[ENV_SPANS_OUTPUT] = spans_output_path
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = sitecustomize_dir + (os.pathsep + existing if existing else "")

def _run_target(args):
    """Runs the target code based on the arguments provided. It can run a module, script, or callable."""
    if args.module:
        runpy.run_module(args.module, run_name="__main__", alter_sys=True)
    elif args.script:
        script_path = os.path.join(args.repo_root, args.script)
        runpy.run_path(script_path, run_name="__main__")
    elif args.callable:
        module_name, _, func_name = args.callable.partition(":")
        module = importlib.import_module(module_name)
        getattr(module, func_name)()
    else:
        raise ValueError("exactly one of --module, --script, --callable must be given")

def main(argv=None):
    """Main function that sets up the environment and runs the target code. It also handles tracing and error handling."""
    parser = argparse.ArgumentParser(description="CodeAtlas sandbox execution bootstrap")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--module")
    parser.add_argument("--script")
    parser.add_argument("--callable")
    parser.add_argument("target_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)
    spans_output_path = os.path.join(args.output_dir, SPANS_FILENAME)

    sys.path.insert(0, args.repo_root)
    os.chdir(args.repo_root)
    if args.target_args and args.target_args[0] == "--":
        sys.argv = [sys.argv[0]] + args.target_args[1:]

    enabled_instrumentors = _enable_otel_auto_instrumentation()
    _install_child_process_propagation(args.repo_root, spans_output_path)

    provider, repo_tracer = make_streaming_tracer(args.repo_root, spans_output_path)
    repo_tracer.install()

    exit_code = 0
    try:
        _run_target(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    except BaseException as exc:  # noqa: BLE001 - deliberately broad: any failure becomes evidence
        exit_code = 1
        crash = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        with open(os.path.join(args.output_dir, CRASH_FILENAME), "w", encoding="utf-8") as f:
            json.dump(crash, f)
    finally:
        repo_tracer.uninstall()
        provider.shutdown()
        with open(os.path.join(args.output_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump({
                "span_count": repo_tracer.span_count,
                "truncated": repo_tracer.truncated,
                "otel_instrumentors_enabled": enabled_instrumentors,
            }, f)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()