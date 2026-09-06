from codeatlas.graph.paths import canonical_path_key


def test_backslash_and_forward_slash_paths_match():
    windows_style = "C:\\Storage\\CodeAtlas\\sample_repo\\pkg\\math_utils.py"
    posix_style = "C:/Storage/CodeAtlas/sample_repo/pkg/math_utils.py"
    assert canonical_path_key(windows_style) == canonical_path_key(posix_style)


def test_case_differences_match():
    lower = "c:/storage/codeatlas/sample_repo/pkg/math_utils.py"
    mixed = "C:/Storage/CodeAtlas/Sample_Repo/PKG/Math_Utils.py"
    assert canonical_path_key(lower) == canonical_path_key(mixed)


def test_duplicate_separators_are_collapsed():
    assert canonical_path_key("C:/Storage//CodeAtlas///pkg/math_utils.py") == \
        canonical_path_key("C:/Storage/CodeAtlas/pkg/math_utils.py")


def test_trailing_slash_is_ignored():
    assert canonical_path_key("C:/Storage/CodeAtlas/pkg/") == canonical_path_key("C:/Storage/CodeAtlas/pkg")


def test_different_files_do_not_match():
    assert canonical_path_key("C:/repo/pkg/math_utils.py") != canonical_path_key("C:/repo/pkg/service.py")


def test_empty_path_is_empty_key():
    assert canonical_path_key("") == ""


def test_real_existing_file_resolves_symlink_equivalent_representations(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "mod.py"
    real_file.write_text("x = 1")

    link_dir = tmp_path / "link"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Symlink creation needs a privilege this environment may not grant
        # (e.g. unelevated Windows) — the pure string-normalization layer
        # is covered by the other tests either way.
        import pytest
        pytest.skip("symlink creation not permitted in this environment")

    linked_file = link_dir / "mod.py"
    assert canonical_path_key(str(real_file)) == canonical_path_key(str(linked_file))
