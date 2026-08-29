"""Canonical path keys for cross-platform, cross-tool identity matching.

Static analysis and OpenTelemetry can each describe the same file with a
different string: backslashes vs forward slashes, different drive-letter
casing, symlinked vs real locations. Both sides must reduce a filepath to
the same key before comparing, or matching silently fails whenever the
two tools' path conventions merely *look* different.
"""

import os


def canonical_path_key(path: str) -> str:
    """Reduce a filepath to a key that's equal for two strings describing
    the same file, regardless of separator style, case, or (when the path
    exists on this machine) symlinks.

    Two layers:
      1. If the path exists on disk, resolve it with realpath() so a
         symlink/junction and its target compare equal.
      2. Always normalize separators to "/", collapse duplicates, drop a
         trailing slash, and lowercase — this is what makes a Windows
         backslash path and a POSIX forward-slash path for the same
         relative location compare equal even when the path doesn't (or
         can't, e.g. in a test) exist on disk.
    """
    if not path:
        return ""

    # If the path exists on disk, resolve it to its real path
    if os.path.exists(path):
        path = os.path.realpath(path)

    # Normalize separators to "/", collapse duplicates, drop trailing slash, and lowercase
    normalized = path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    normalized = normalized.rstrip("/")
    return normalized.lower()