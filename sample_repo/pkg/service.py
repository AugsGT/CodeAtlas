from codeatlas.telemetry.tracing import traced
from pkg.tracing_setup import tracer
from pkg.math_utils import add, square


class Calculator:
    @traced(tracer)
    def compute(self, a, b):
        total = add(a, b)
        return square(total)


@traced(tracer)
def run():
    calc = Calculator()
    return calc.compute(2, 3)
