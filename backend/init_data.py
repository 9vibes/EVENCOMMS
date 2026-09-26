"""Prepare dedicated Umbrel data and optional image-managed config volumes."""
import argparse
import os
from pathlib import Path
import secrets
import stat


ROOT = Path(__file__).resolve().parent.parent


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


def install_configs(target: Path, source: Path = ROOT / 'infra'):
    if target == Path('.') or '..' in target.parts:
        raise ValueError('Config directory must be nonempty and contain no parent traversal')
    # Linux treats //data as /data, although pathlib retains the double-slash anchor.
    target = Path('/' + os.path.abspath(target).lstrip('/'))
    for protected in (Path('/data'), source.resolve()):
        if target.is_relative_to(protected) or protected.is_relative_to(target):
            raise ValueError('Config directory must not overlap data or source directories')
    configs = {name: (source / name).read_bytes() for name in ('nginx.conf', 'mediamtx.yml')}
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(target.anchor, directory_flags)
    try:
        # Walk every component without following links, including parent directories.
        for part in target.parts[1:]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=root)
            except FileExistsError:
                pass
            child = os.open(part, directory_flags, dir_fd=root)
            os.close(root)
            root = child
        for name in configs:
            try:
                info = os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError(f'{name} must be a regular file without symbolic or hard links')
        os.fchmod(root, 0o755)
        for name, content in configs.items():
            temporary = f'.{name}.{secrets.token_hex(16)}.tmp'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=root)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(content)
                    output.flush()
                    os.fchmod(output.fileno(), 0o644)
                    os.fsync(output.fileno())
                os.replace(temporary, name, src_dir_fd=root, dst_dir_fd=root)
                os.fsync(root)
            finally:
                try:
                    os.unlink(temporary, dir_fd=root)
                except FileNotFoundError:
                    pass
    finally:
        os.close(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', help='Install image-managed configs into this dedicated directory')
    args = parser.parse_args(argv)
    if args.config_dir is not None and not args.config_dir.strip():
        parser.error('--config-dir must not be empty')
    initialize()
    if args.config_dir is not None:
        install_configs(Path(args.config_dir))


if __name__ == '__main__':
    main()
