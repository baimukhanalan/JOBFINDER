"""Allowlisted calculation tree; never eval/exec model output."""
from decimal import Decimal, localcontext
from fractions import Fraction
import re


def calculate(node):
    def run(value, numeric, depth=0):
        if depth > 12:
            raise ValueError("calculation depth")
        if isinstance(value, str):
            if len(value) > 64 or not re.fullmatch(r"-?\d+(\.\d+)?", value):
                raise ValueError("invalid number")
            return numeric(value)
        if not isinstance(value, dict) or set(value) != {"op", "args"}:
            raise ValueError("invalid calculation")
        op, args = value["op"], value["args"]
        sizes = {"add": 2, "sub": 2, "mul": 2, "div": 2, "percent": 2, "proportion": 3}
        if op not in sizes or not isinstance(args, list) or len(args) != sizes[op]:
            raise ValueError("unsupported operation")
        a = [run(v, numeric, depth + 1) for v in args]
        if op == "add": return a[0] + a[1]
        if op == "sub": return a[0] - a[1]
        if op == "mul": return a[0] * a[1]
        if op == "div": return a[0] / a[1]
        if op == "percent": return a[0] * a[1] / 100
        return a[1] * a[2] / a[0]
    try:
        with localcontext() as context:
            context.prec = 50
            result = run(node, Decimal)
            exact = run(node, Fraction)
            check = Decimal(exact.numerator) / Decimal(exact.denominator)
            if not result.is_finite() or abs(result - check) > Decimal("1e-40") * max(1, abs(check)):
                raise ValueError("independent numeric check failed")
            return result
    except (ArithmeticError, TypeError, KeyError) as error:
        raise ValueError("invalid calculation") from error


UNITS = {"m": ("length", Decimal(1)), "km": ("length", Decimal(1000)),
         "cm": ("length", Decimal("0.01")), "ft": ("length", Decimal("0.3048")),
         "s": ("time", Decimal(1)), "min": ("time", Decimal(60)),
         "h": ("time", Decimal(3600)), "kg": ("mass", Decimal(1)),
         "g": ("mass", Decimal("0.001"))}


def convert_unit(value, source, target):
    if source not in UNITS or target not in UNITS or UNITS[source][0] != UNITS[target][0]:
        raise ValueError("incompatible or unknown units")
    return calculate({"op": "div", "args": [
        {"op": "mul", "args": [value, str(UNITS[source][1])]}, str(UNITS[target][1])]})


def match_number(q, value):
    matches = []
    for option in q.options:
        label = option.label.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", label) and Decimal(label) == value:
            matches.append(option.id)
    if len(matches) != 1:
        raise ValueError("numeric option absent or ambiguous")
    return matches[0]


def table_number(q, table, row, column):
    if any(type(i) is not int or i < 0 for i in (table, row, column)):
        raise ValueError("nonnegative cell indices required")
    try:
        value = q.tables[table].rows[row][column]
        return calculate(value)
    except (IndexError, TypeError) as error:
        raise ValueError("invalid table cell") from error
