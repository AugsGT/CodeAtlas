from codeatlas.telemetry.tracing import traced
from pkg.tracing_setup import tracer


@traced(tracer)
def add(a, b):
    return a + b


@traced(tracer)
def a_times_a(a):
    return a * a


@traced(tracer)
def square(x):
    return a_times_a(x)
