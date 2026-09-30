"""Late-bound handles to the active services for router modules.

Each name forwards attribute access to the current :class:`Services`
instance, so a maintenance action that swaps the face store (and the
clustering/worker bound to it) is visible to every router immediately.
"""

from __future__ import annotations

from .services.container import current


class _ServiceProxy:
    __slots__ = ("_attr",)

    def __init__(self, attr: str):
        self._attr = attr

    def _target(self):
        return getattr(current(), self._attr)

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __repr__(self):
        return f"<service proxy {self._attr}>"


def services():
    return current()


db = _ServiceProxy("db")
config = _ServiceProxy("config")
engine = _ServiceProxy("engine")
cluster = _ServiceProxy("cluster")
worker = _ServiceProxy("worker")
store = _ServiceProxy("store")
vectors = _ServiceProxy("vectors")
models = _ServiceProxy("models")
video_compat = _ServiceProxy("video_compat")
