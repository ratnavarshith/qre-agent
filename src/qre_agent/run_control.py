"""What a demo run reports and obeys (demo.py), without importing the agent: the server needs it at
startup, before the estimators are loaded."""

import secrets
import threading


class Cancelled(Exception):
    """The run was stopped from outside (timeout or the client left)."""


class Hooks:
    """`emit(event)` reports progress and may be called from the run's thread; `cancel` is a
    threading.Event that another thread sets to stop the run."""

    def __init__(self, emit, cancel=None):
        self.emit, self.cancel, self.steps = emit, cancel or threading.Event(), 0

    def check(self):
        if self.cancel.is_set():
            raise Cancelled

    def sleep(self, seconds):
        """A retry's wait, cut short by a cancel."""
        if self.cancel.wait(seconds):
            raise Cancelled


def new_run_id():
    return "d-" + secrets.token_hex(4)
