# execution

`execution` is a Python project designed to help find the main entry point and dependencies of any repository layout. It provides utilities for detecting entry points and managing dependencies across various Python projects.

## Project Structure

- **entrypoint.py**: This file contains the core functionality for identifying the main entry point and dependencies in a Python project.
  - **Classes**:
    - `EntrypointResult`: Represents the result of detecting an entry point, including details like the path to the entry script and any associated metadata.
    - `DependencyManifest`: Represents a manifest of dependencies detected in the repository, including direct and transitive dependencies.
  - **Functions**:
    - `_top_level_py_files(repo_root)`: Identifies top-level Python files within the repository.
    - `_top_level_packages(repo_root)`: Identifies top-level packages within the repository.
    - `_has_main_guard(filepath)`: Checks if a file has a guard against running as a script (e.g., `if __name__ == "__main__":`).
    - `_top_level_syntax_errors(repo_root)`: Detects syntax errors in top-level Python files.
    - `_detect_console_script(repo_root)`: Detects console scripts defined in the repository's setup configuration.
    - `detect_entrypoint(repo_root: str) -> EntrypointResult`: Detects and returns the main entry point of the project.
    - `detect_dependencies(repo_root: str) -> DependencyManifest`: Detects and returns a manifest of dependencies for the project.

## Setup/Usage

To use this project, simply import the necessary classes and functions from `entrypoint.py` in your Python script. For example:

```python
from execution.entrypoint import detect_entrypoint, detect_dependencies

repo_root = "/path/to/your/repository"
entrypoint_result = detect_entrypoint(repo_root)
dependency_manifest = detect_dependencies(repo_root)

print(entrypoint_result)
print(dependency_manifest)
```

This will help you identify the main entry point and dependencies of your Python project.