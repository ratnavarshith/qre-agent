import sys
from pathlib import Path

import pytest

from qre_agent import load_assumptions
from qre_agent.compiler import binary

sys.path.insert(0, str(Path(__file__).parent))


def pytest_collection_modifyitems(config, items):
    """Skip tests marked `compiler` when the bicycle binaries are missing."""
    a = load_assumptions()
    try:
        binary(a, "bicycle_compiler"), binary(a, "bicycle_numerics")
        return
    except FileNotFoundError:
        pass
    skip = pytest.mark.skip(
        reason=f"bicycle compiler not found in {a.compiler_dir} (set QRE_COMPILER_DIR)"
    )
    for item in items:
        if "compiler" in item.keywords:
            item.add_marker(skip)
