"""Tiny regex router: (method, path) -> (handler, path-params)."""
from __future__ import annotations

import re


class Router:
    def __init__(self):
        self.routes = []  # (method, compiled_regex, handler)

    def add(self, method: str, pattern: str, handler) -> None:
        # {name} -> named capture group matching a single path segment
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self.routes.append((method.upper(), re.compile("^" + regex + "$"), handler))

    def match(self, method: str, path: str):
        for m, regex, handler in self.routes:
            if m != method.upper():
                continue
            mo = regex.match(path)
            if mo:
                return handler, mo.groupdict()
        return None, None
