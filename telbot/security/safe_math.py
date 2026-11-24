"""Resource-bounded AST mathematical expression evaluator (no eval())."""

from __future__ import annotations

import ast
import math
import operator

MAX_EXPR_CHARS = 256
MAX_AST_NODES = 64
MAX_EXPONENT = 1000
MAX_RESULT_MAGNITUDE_LOG10 = 300
MAX_FACTORIAL_N = 170

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _bounded_factorial(n: int | float) -> int:
    if isinstance(n, bool) or not isinstance(n, int):
        if isinstance(n, float) and n.is_integer():
            n = int(n)
        else:
            raise ValueError("factorial requires an integer")
    if n < 0 or n > MAX_FACTORIAL_N:
        raise ValueError(f"factorial input must be between 0 and {MAX_FACTORIAL_N}")
    return math.factorial(n)


_SAFE_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

_SAFE_CALLABLES = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
    "factorial": _bounded_factorial,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
}


def _checked_pow(base: int | float, exp: int | float) -> int | float:
    if abs(exp) > MAX_EXPONENT:
        raise ValueError("Exponent exceeds safe limit")
    if abs(base) > 1 and exp > 0:
        if exp * math.log10(abs(base)) > MAX_RESULT_MAGNITUDE_LOG10:
            raise ValueError("Power result exceeds safe magnitude")
    result = base**exp
    if isinstance(result, complex):
        raise ValueError("Complex results are not supported")
    return result


def safe_eval_math(expr: str) -> str:
    """Safely evaluate basic arithmetic and math functions with strict DoS bounds."""
    cleaned = (expr or "").strip()
    if not cleaned or len(cleaned) > MAX_EXPR_CHARS:
        raise ValueError("Expression empty or too long")

    tree = ast.parse(cleaned, mode="eval")
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_AST_NODES:
        raise ValueError("Expression too complex")

    def _eval(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("Unsupported literal")
            if isinstance(node.value, float) and not math.isfinite(node.value):
                raise ValueError("Non-finite literal")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Pow):
                val = _checked_pow(left, right)
            else:
                val = _SAFE_OPS[type(node.op)](left, right)
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError("Result overflow")
            return val
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name) and node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_CALLABLES:
            if node.keywords:
                raise ValueError("Keyword arguments are not supported")
            fn = _SAFE_CALLABLES[node.func.id]
            args = [_eval(a) for a in node.args]
            val = fn(*args)
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError("Result overflow")
            return val
        raise ValueError("Unsupported expression")

    result = _eval(tree)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("Non-finite result")
    return str(result)
