"""Private token files: 32..256 ASCII hex characters, optionally followed by one LF.

Kept identical in backend and codex_bridge, which ship in separate images.
"""

import os
from pathlib import Path
import re
import stat


def read_token(descriptor, *, check_permissions=True):
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 32 <= info.st_size <= 257
            or (check_permissions and info.st_mode & 0o026)):
        raise ValueError("Invalid CODEX_BRIDGE_TOKEN_FILE")
    raw = os.read(descriptor, 258)
    after = os.fstat(descriptor)
    if (len(raw) != info.st_size or not re.fullmatch(rb"[0-9A-Fa-f]{32,256}\n?", raw)
            or (after.st_nlink, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (info.st_nlink, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
        raise ValueError("Invalid CODEX_BRIDGE_TOKEN_FILE")
    return raw.removesuffix(b"\n").decode("ascii")


def load_token_file(filename):
    """Bounded, no-follow read; never expose OS errors, paths or contents."""
    try:
        if not isinstance(filename, str) or not 1 <= len(os.fsencode(filename)) <= 4096 or "\0" in filename:
            raise ValueError()
        path = Path(filename)
        if not path.is_absolute() or ".." in path.parts or not path.name or filename.endswith("/"):
            raise ValueError()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open("/", flags)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, flags, dir_fd=root)
                os.close(root)
                root = child
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            try:
                return read_token(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(root)
    except (OSError, ValueError):
        raise ValueError("Invalid CODEX_BRIDGE_TOKEN_FILE") from None
