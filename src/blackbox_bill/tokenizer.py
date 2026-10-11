"""Tokenizer-aware token figures.

Claude 4.7+ ships a new tokenizer that emits roughly 30% more tokens for
the same text. A cost ledger that only knows legacy token counts will
under-report what the same workload looks like under the new tokenizer —
and vice versa.

This module does NOT re-tokenize anything (stdlib-only, no model
artifacts): it tags token figures with the tokenizer that produced them
and converts between the two with a configurable factor so every report
can show *both* figures side by side.
"""

TOKENIZERS = {
    "legacy": {
        "label": "legacy tokenizer (Claude <=4.5)",
        "factor": 1.0,
    },
    "claude-4.7": {
        "label": "Claude 4.7+ tokenizer",
        "factor": 1.3,
    },
}

DEFAULT_FACTOR = 1.3  # claude-4.7 emits ~30% more tokens than legacy


def resolve(name, factor=None):
    """Return (name, label, factor) for a tokenizer name and optional override.

    ``factor`` is the legacy->claude-4.7 conversion ratio (how many 4.7
    tokens one legacy token becomes). It defaults to the ratio between
    the registered tokenizers (1.3 for legacy input, 1.0 when the input
    is already measured under claude-4.7). Raises ValueError on unknown
    tokenizer names or non-positive factors.
    """
    if name not in TOKENIZERS:
        raise ValueError(
            "unknown tokenizer %r (choose: %s)"
            % (name, ", ".join(sorted(TOKENIZERS))))
    entry = TOKENIZERS[name]
    if factor is None:
        # The ratio between the two tokenizers, independent of which one
        # the input was measured under.
        factor = (TOKENIZERS["claude-4.7"]["factor"]
                  / TOKENIZERS["legacy"]["factor"])
    f = float(factor)
    if f <= 0:
        raise ValueError("tokenizer factor must be positive, got %r" % factor)
    return name, entry["label"], f


def to_legacy(tokens, name, factor=None):
    """Convert a token count measured under ``name`` back to legacy tokens."""
    _, _, f = resolve(name, factor)
    if name == "legacy":
        return tokens
    return tokens / f


def to_claude47(tokens, name, factor=None):
    """Convert a token count measured under ``name`` to Claude 4.7+ tokens."""
    return to_legacy(tokens, name, factor) * resolve(name, factor)[2]


def both(tokens, name, factor=None):
    """Return (legacy_tokens, claude47_tokens, label, factor) for display."""
    _, label, f = resolve(name, factor)
    legacy = to_legacy(tokens, name, f)
    return legacy, legacy * f, label, f


def fmt_tokens(n):
    """Compact token formatting: 1.2M / 34.5K / 123."""
    n = float(n)
    if n >= 1_000_000:
        return "%.2fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1fK" % (n / 1_000)
    return "%d" % int(n)


def dual_line(tokens, name, factor=None):
    """One-line dual-figure rendering, e.g. '1.20M (legacy) ~= 1.56M (claude-4.7, x1.3)'."""
    legacy, c47, _, f = both(tokens, name, factor)
    return "%s (legacy) ~= %s (claude-4.7, x%.2g)" % (
        fmt_tokens(legacy), fmt_tokens(c47), f)
