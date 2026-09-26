"""Prepare the dedicated Umbrel volume without recursively changing user files."""
import os
from pathlib import Path
import stat


def initialize(data: Path = Path('/data')):
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(root, 10001, 10001)
        os.fchmod(root, 0o700)
        try:
            os.mkdir('models', mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        models = os.open('models', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            os.fchown(models, 10001, 10001)
            os.fchmod(models, 0o700)
        finally:
            os.close(models)
        for name in ('evencomms.sqlite3', 'evencomms.sqlite3-wal', 'evencomms.sqlite3-shm'):
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError(f'{name} must be a regular file without hard links')
                os.fchown(descriptor, 10001, 10001)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
    finally:
        os.close(root)


if __name__ == '__main__':
    initialize()
