"""Estimates for one circuit on two architectures. The estimators are imported on first use, so
importing a submodule (the demo server, the sandbox broker) doesn't pull in qiskit and QDK."""

from .assumptions import Assumptions, load_assumptions

__all__ = ["Assumptions", "estimate_bicycle", "estimate_surface", "load_assumptions"]


def __getattr__(name):
    if name == "estimate_bicycle":
        from .bicycle import estimate_bicycle

        return estimate_bicycle
    if name == "estimate_surface":
        from .surface import estimate_surface

        return estimate_surface
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
