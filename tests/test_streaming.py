"""Length-prefixed record streaming must survive being read back after a
clean write, and must degrade gracefully (stop, not raise) on a
truncated trailing record - exactly what's left on disk if the writing
process is killed mid-write by a hard execution timeout."""

import io

from codeatlas.execution.streaming import append_record, read_records


def test_round_trips_multiple_records(tmp_path):
    path = tmp_path / "records.bin"
    with open(path, "ab") as f:
        append_record(f, b"first")
        append_record(f, b"second")
        append_record(f, b"")

    with open(path, "rb") as f:
        records = list(read_records(f))
    assert records == [b"first", b"second", b""]


def test_truncated_trailing_record_is_ignored_not_raised():
    buf = io.BytesIO()
    append_record(buf, b"complete")
    complete_bytes = buf.getvalue()
    # Simulate a kill mid-write: header present, but body cut short.
    truncated = complete_bytes + append_and_return_header(b"partial") + b"pa"

    records = list(read_records(io.BytesIO(truncated)))
    assert records == [b"complete"]


def append_and_return_header(data: bytes) -> bytes:
    buf = io.BytesIO()
    append_record(buf, data)
    return buf.getvalue()[: -len(data)]
