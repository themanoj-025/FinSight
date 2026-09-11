"""Pattern library sub-package — internal implementation modules.

Import these from ``finance_agent`` (or the ``finance_agent.fraud_patterns``
barrel) rather than importing this package directly.
"""

from finance_agent.patterns_pkg.ctx import PatternCtx
from finance_agent.patterns_pkg.registry import (
    gen_pattern,
    inject_focal_patterns,
    pattern_names,
)

__all__ = [
    "PatternCtx",
    "gen_pattern",
    "inject_focal_patterns",
    "pattern_names",
]
