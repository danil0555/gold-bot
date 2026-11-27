"""Guard the static BingX futures universe against a known retired symbol."""

import ast
from pathlib import Path


def test_futures_symbol_universe_excludes_unavailable_ton_contract():
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SYMBOLS"
                for target in node.targets)
    )
    symbols = ast.literal_eval(assignment.value)

    assert "TON-USDT" not in symbols
    assert len(symbols) == 14
    assert len(symbols) == len(set(symbols))
    assert all(symbol.endswith("-USDT") for symbol in symbols)
