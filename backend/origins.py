"""Opt-in support for the installed Even app's observed loopback origin."""

import re

from starlette.middleware.cors import CORSMiddleware


def is_even_localhost_origin(origin: str | None) -> bool:
    # Match the whole origin, not a hostname substring or arbitrary loopback host.
    if not isinstance(origin, str):
        return False
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", origin)
    return match is not None and int(match[1]) <= 65535


class EvenCORSMiddleware(CORSMiddleware):
    def __init__(self, app, *, allow_even_localhost: bool = False, **kwargs):
        super().__init__(app, **kwargs)
        self.allow_even_localhost = allow_even_localhost

    def is_allowed_origin(self, origin: str) -> bool:
        return (super().is_allowed_origin(origin)
                or (self.allow_even_localhost and is_even_localhost_origin(origin)))
