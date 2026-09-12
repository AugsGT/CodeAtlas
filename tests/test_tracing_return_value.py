"""Unit tests for return-value capture on traced calls (graph/models.py's
RuntimeSpan.return_value): a call that raises no exception but produces
the wrong (or no) result is invisible to status/error_message alone -
this is what lets CodeAtlas actually answer "why is there no output."
"""

from codeatlas.telemetry.tracing import make_in_memory_tracer, to_runtime_span, traced


def test_traced_captures_return_value_on_success():
    tracer, exporter = make_in_memory_tracer()

    @traced(tracer)
    def add(a, b):
        return a + b

    result = add(2, 3)

    assert result == 5
    span = to_runtime_span(exporter.get_finished_spans()[0])
    assert span.return_value == "5"
    assert span.status == "OK"


def test_traced_captures_none_return_value():
    """The exact shape of a silent bug: no exception, but the call
    returns None because of a missing return statement."""
    tracer, exporter = make_in_memory_tracer()

    @traced(tracer)
    def forgot_to_return(a, b):
        total = a + b
        # no return

    result = forgot_to_return(2, 3)

    assert result is None
    span = to_runtime_span(exporter.get_finished_spans()[0])
    assert span.return_value == "None"
    assert span.status == "OK"
    assert span.error_message == ""


def test_traced_does_not_set_return_value_on_exception():
    tracer, exporter = make_in_memory_tracer()

    @traced(tracer)
    def boom():
        raise ValueError("nope")

    try:
        boom()
    except ValueError:
        pass

    span = to_runtime_span(exporter.get_finished_spans()[0])
    assert span.status == "ERROR"
    assert "ValueError" in span.error_message
    assert span.return_value == ""


def test_traced_truncates_large_return_values():
    tracer, exporter = make_in_memory_tracer()

    @traced(tracer)
    def big():
        return list(range(1000))

    big()

    span = to_runtime_span(exporter.get_finished_spans()[0])
    assert len(span.return_value) <= 520  # 500 + truncation marker
    assert span.return_value.endswith("...(truncated)")


def test_traced_handles_unrepresentable_return_value():
    class Unrepresentable:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    tracer, exporter = make_in_memory_tracer()

    @traced(tracer)
    def weird():
        return Unrepresentable()

    weird()

    span = to_runtime_span(exporter.get_finished_spans()[0])
    assert "unrepresentable" in span.return_value.lower()
