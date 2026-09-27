from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading

import pytest

from backend import init_data
from backend.init_data import initialize, initialize_codex_auth, install_configs


@pytest.fixture(autouse=True)
def no_ownership_change(monkeypatch):
    if os.geteuid() != 0:
        monkeypatch.setattr(os, 'fchown', lambda fd, uid, gid: None)


def test_initialize_fixed_roots_and_database_only(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    models = data / 'models'
    models.mkdir()
    cached = models / 'cached-model'
    cached.write_text('cached')
    cached.chmod(0o640)
    database = data / 'evencomms.sqlite3'
    database.write_text('existing database')
    for _ in range(2):
        initialize(data)
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
        assert stat.S_IMODE(models.stat().st_mode) == 0o700
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        assert stat.S_IMODE(cached.stat().st_mode) == 0o640
        assert database.read_text() == 'existing database'


def test_initialize_creates_new_model_directory(tmp_path):
    initialize(tmp_path / 'data')
    assert (tmp_path / 'data/models').is_dir()


@pytest.mark.parametrize('entry', ['data', 'models', 'evencomms.sqlite3'])
def test_initialize_refuses_symlinks(tmp_path, entry):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o755)
    data = tmp_path / 'data'
    if entry == 'data':
        data.symlink_to(outside, target_is_directory=True)
    else:
        data.mkdir()
        (data / entry).symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        initialize(data)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_initialize_refuses_database_hardlink(tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('must remain untouched')
    outside.chmod(0o644)
    data = tmp_path / 'data'
    data.mkdir()
    os.link(outside, data / 'evencomms.sqlite3')
    with pytest.raises(RuntimeError, match='hard links'):
        initialize(data)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


@pytest.fixture
def config_source(tmp_path):
    source = tmp_path / 'infra'
    source.mkdir()
    (source / 'nginx.conf').write_bytes(b'proxy_set_header Host $http_host;\n'
                                      b'map $http_upgrade $connection_upgrade {}\n')
    (source / 'mediamtx.yml').write_bytes(b'authHTTPAddress: http://server:8000/internal/media/auth\n')
    (source / 'unmanaged').write_text('must not be copied')
    return source


def test_install_configs_updates_only_managed_files(tmp_path, config_source, monkeypatch, capsys):
    target = tmp_path / 'config'
    target.mkdir(mode=0o700)
    custom = target / 'custom.conf'
    custom.write_bytes(b'private custom config')
    custom.chmod(0o600)
    outside = tmp_path / 'outside'
    outside.write_text('private outside content')
    (target / 'custom-link').symlink_to(outside)
    os.link(outside, target / 'custom-hardlink')
    custom_info = custom.stat()
    outside_info = outside.stat()

    def no_chown(*args):
        pytest.fail('Config installation must not change ownership')

    monkeypatch.setattr(os, 'fchown', no_chown)
    monkeypatch.setattr(os, 'chown', no_chown)
    monkeypatch.setenv('http_host', 'must-not-be-substituted')
    previous_umask = os.umask(0o077)
    try:
        for attempt in range(3):
            if attempt == 2:
                (config_source / 'nginx.conf').write_bytes(b'updated $http_host $backend;\n')
                (target / 'mediamtx.yml').write_text('stale config')
            install_configs(target, config_source)
            assert stat.S_IMODE(target.stat().st_mode) == 0o755
            for name in ('nginx.conf', 'mediamtx.yml'):
                assert (target / name).read_bytes() == (config_source / name).read_bytes()
                assert stat.S_IMODE((target / name).stat().st_mode) == 0o644
            assert {p.name for p in target.iterdir()} == {
                'nginx.conf', 'mediamtx.yml', 'custom.conf', 'custom-link', 'custom-hardlink',
            }
            assert custom.stat() == custom_info
            assert outside.stat() == outside_info
    finally:
        os.umask(previous_umask)
    assert custom.read_bytes() == b'private custom config'
    assert outside.read_text() == 'private outside content'
    assert capsys.readouterr() == ('', '')


def test_install_configs_uses_bundled_source(tmp_path):
    target = tmp_path / 'config'
    install_configs(target)
    for name in ('nginx.conf', 'mediamtx.yml'):
        assert (target / name).read_bytes() == (init_data.ROOT / 'infra' / name).read_bytes()
    assert b'$http_host' in (target / 'nginx.conf').read_bytes()


@pytest.mark.parametrize('ancestor', [False, True])
def test_install_configs_refuses_directory_symlinks(tmp_path, config_source, ancestor):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    target = tmp_path / 'config'
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        install_configs(target / 'nested' if ancestor else target, config_source)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('name', ['nginx.conf', 'mediamtx.yml'])
@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'hardlink', 'directory', 'fifo'])
def test_install_configs_refuses_unsafe_destinations(tmp_path, config_source, name, kind):
    outside = tmp_path / 'outside'
    outside.write_bytes(b'private outside content')
    outside.chmod(0o600)
    target = tmp_path / 'config'
    target.mkdir(mode=0o700)
    destination = target / name
    if kind == 'symlink':
        destination.symlink_to(outside)
    elif kind == 'dangling':
        destination.symlink_to(tmp_path / 'missing')
    elif kind == 'hardlink':
        os.link(outside, destination)
    elif kind == 'directory':
        destination.mkdir()
    else:
        os.mkfifo(destination)
    before = outside.stat()
    with pytest.raises(RuntimeError, match='regular file') as error:
        install_configs(target, config_source)
    assert 'private outside content' not in str(error.value)
    assert outside.stat() == before
    assert outside.read_bytes() == b'private outside content'
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert {p.name for p in target.iterdir()} == {name}
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('target', [
    '', '.', '/', '//', '/data', '//data', '/data/config', '//data/config', '/config/../elsewhere',
])
def test_install_configs_refuses_empty_traversal_and_data_overlap(config_source, target):
    with pytest.raises(ValueError):
        install_configs(Path(target), config_source)


@pytest.mark.parametrize('location', ['same', 'parent', 'child'])
def test_install_configs_refuses_source_overlap(config_source, location):
    target = {'same': config_source, 'parent': config_source.parent,
              'child': config_source / 'config'}[location]
    with pytest.raises(ValueError, match='overlap'):
        install_configs(target, config_source)


def test_install_configs_missing_source_does_not_change_target(tmp_path, config_source):
    target = tmp_path / 'config'
    install_configs(target, config_source)
    (config_source / 'nginx.conf').write_text('new version')
    (config_source / 'mediamtx.yml').unlink()
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(FileNotFoundError):
        install_configs(target, config_source)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


@pytest.mark.parametrize('failure', ['write', 'file_fsync', 'replace', 'directory_fsync'])
@pytest.mark.parametrize('fail_on', [1, 2])
def test_install_configs_failures_are_atomic_per_file(tmp_path, config_source, monkeypatch,
                                                     failure, fail_on):
    target = tmp_path / 'config'
    target.mkdir()
    names = ('nginx.conf', 'mediamtx.yml')
    for name in names:
        (target / name).write_bytes(b'previous complete config')
    fdopen, fsync, replace = os.fdopen, os.fsync, os.replace
    calls = 0

    def maybe_fail():
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise OSError('simulated installation failure')

    def failing_fdopen(*args, **kwargs):
        output = fdopen(*args, **kwargs)
        write = output.write

        def partial_write(content):
            write(content[:3])
            maybe_fail()
            return write(content[3:])

        output.write = partial_write
        return output

    def failing_fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if directory == (failure == 'directory_fsync'):
            maybe_fail()
        return fsync(fd)

    def failing_replace(*args, **kwargs):
        maybe_fail()
        return replace(*args, **kwargs)

    if failure == 'write':
        monkeypatch.setattr(os, 'fdopen', failing_fdopen)
    elif failure == 'replace':
        monkeypatch.setattr(os, 'replace', failing_replace)
    else:
        monkeypatch.setattr(os, 'fsync', failing_fsync)
    with pytest.raises(OSError, match='simulated installation failure'):
        install_configs(target, config_source)
    assert {p.name for p in target.iterdir()} == set(names)
    for index, name in enumerate(names, start=1):
        replaced = index < fail_on or (index == fail_on and failure == 'directory_fsync')
        expected = (config_source / name).read_bytes() if replaced else b'previous complete config'
        assert (target / name).read_bytes() == expected


@pytest.mark.parametrize('args', [[], ['--config-dir', '/config']])
def test_main_config_installation_is_opt_in(monkeypatch, args):
    calls = []
    monkeypatch.setattr(init_data, 'initialize', lambda: calls.append('data'))
    monkeypatch.setattr(init_data, 'install_configs', lambda path: calls.append(path))
    monkeypatch.setattr(init_data, 'initialize_codex_auth', lambda *args, **kwargs: pytest.fail('Not opted in'))
    init_data.main(args)
    assert calls == (['data', Path('/config')] if args else ['data'])


@pytest.mark.parametrize('args', [['--config-dir'], ['--config-dir', ''], ['--config-dir', '  ']])
def test_main_rejects_missing_or_empty_config_directory(monkeypatch, args):
    def unexpected_initialization():
        pytest.fail('Invalid CLI arguments must not initialize data')

    monkeypatch.setattr(init_data, 'initialize', unexpected_initialization)
    with pytest.raises(SystemExit) as error:
        init_data.main(args)
    assert error.value.code == 2


def test_codex_auth_random_once_repairs_mount_permissions_and_touches_only_token(tmp_path, monkeypatch, capsys):
    target = tmp_path / 'codex-auth'
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    if os.geteuid() == 0:
        assert (target.stat().st_uid, target.stat().st_gid) == (0, 0)
    other = target / 'unmanaged'
    other.write_text('private unrelated content')
    other.chmod(0o600)
    outside = tmp_path / 'outside'
    outside.write_text('private outside content')
    (target / 'unmanaged-link').symlink_to(outside)
    os.link(outside, target / 'unmanaged-hardlink')
    untouched = {path: path.stat() for path in (other, outside, tmp_path)}
    chown, token_hex = os.fchown, init_data.secrets.token_hex
    owners, random_calls = [], []

    def record_chown(fd, uid, gid):
        owners.append((stat.S_ISDIR(os.fstat(fd).st_mode), uid, gid))
        chown(fd, uid, gid)

    def random_once(length):
        random_calls.append(length)
        return token_hex(length)

    monkeypatch.setattr(os, 'fchown', record_chown)
    monkeypatch.setattr(init_data.secrets, 'token_hex', random_once)
    old_umask = os.umask(0o077)
    try:
        initialize_codex_auth(target)
        token = (target / 'token').read_bytes()
        inode = (target / 'token').stat().st_ino
        assert re.fullmatch(rb'[0-9a-f]{64}', token)
        for _ in range(2):
            initialize_codex_auth(target)
            assert (target / 'token').read_bytes() == token
            assert (target / 'token').stat().st_ino == inode
        for path, mode in ((target, 0o750), (target / 'token', 0o440)):
            info = path.stat()
            assert stat.S_IMODE(info.st_mode) == mode
            if os.geteuid() == 0:
                assert (info.st_uid, info.st_gid) == (10001, 10002)
        assert (target / 'token').stat().st_nlink == 1
        assert random_calls == [16, 32]
        assert set(owners) == {(True, 10001, 10002), (False, 10001, 10002)}
        assert {p.name for p in target.iterdir()} == {'token', 'unmanaged', 'unmanaged-link', 'unmanaged-hardlink'}
        for path, before in untouched.items():
            assert path.stat() == before
    finally:
        os.umask(old_umask)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('content', [b'AB' * 16, b'aB12' * 64 + b'\n'])
def test_codex_auth_preserves_valid_existing_bytes_and_inode(tmp_path, monkeypatch, content):
    target = tmp_path / 'codex-auth'
    target.mkdir()
    path = target / 'token'
    path.write_bytes(content)
    path.chmod(0o600)
    inode = path.stat().st_ino
    monkeypatch.setattr(init_data.secrets, 'token_hex', lambda *args: pytest.fail('Existing token must not rotate'))
    monkeypatch.setattr(os, 'fdopen', lambda *args: pytest.fail('Existing token must not be rewritten'))
    initialize_codex_auth(target)
    assert path.read_bytes() == content and path.stat().st_ino == inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o440


@pytest.mark.parametrize('nested', [False, True])
def test_codex_auth_rejects_directory_and_parent_symlinks(tmp_path, nested):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o755)
    before = outside.stat()
    target = tmp_path / 'codex-auth'
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='private Codex bridge credential'):
        initialize_codex_auth(target / 'nested' if nested else target)
    assert outside.stat() == before
    assert not list(outside.iterdir())


@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'hardlink', 'directory', 'fifo', 'oversized', 'invalid'])
def test_codex_auth_rejects_unsafe_existing_token_without_mutation(tmp_path, monkeypatch, kind):
    target = tmp_path / 'codex-auth'
    target.mkdir()
    path = target / 'token'
    outside = tmp_path / 'outside'
    outside.write_bytes(b'ab' * 32)
    outside.chmod(0o600)
    if kind == 'symlink':
        path.symlink_to(outside)
    elif kind == 'dangling':
        path.symlink_to(tmp_path / 'missing')
    elif kind == 'hardlink':
        os.link(outside, path)
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    else:
        path.write_bytes(b'z' * 64 if kind == 'invalid' else b'a' * 258)
    before, outside_before = path.lstat(), outside.stat()
    if kind != 'invalid':
        monkeypatch.setattr(os, 'read', lambda *args: pytest.fail('Unsafe file must not be read'))
    with pytest.raises(RuntimeError, match='^Unable to initialize private Codex bridge credential$'):
        initialize_codex_auth(target)
    after = path.lstat()
    for field in ('st_mode', 'st_ino', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
        assert getattr(after, field) == getattr(before, field)
    assert outside.stat() == outside_before
    assert {p.name for p in target.iterdir()} == {'token'}


@pytest.mark.parametrize('target', ['', '.', 'relative', '/', '//', '/data', '//data/token', '/config',
                                        '//config/token', '/private/../codex-auth', '/' + 'a' * 4096])
def test_codex_auth_rejects_invalid_and_overlapping_paths_before_access(target, monkeypatch):
    monkeypatch.setattr(os, 'open', lambda *args, **kwargs: pytest.fail('Must validate path before opening'))
    with pytest.raises(ValueError):
        initialize_codex_auth(Path(target))


@pytest.mark.parametrize('protected', ['source', 'config', 'double_slash_config'])
@pytest.mark.parametrize('location', ['same', 'parent', 'child'])
def test_codex_auth_rejects_custom_config_and_source_overlap(tmp_path, monkeypatch, protected, location):
    source, config = tmp_path / 'source', tmp_path / 'config'
    monkeypatch.setattr(init_data, 'ROOT', source)
    root = source if protected == 'source' else config
    target = {'same': root, 'parent': root.parent, 'child': root / 'auth'}[location]
    config_arg = Path('/' + str(config)) if protected == 'double_slash_config' else config
    with pytest.raises(ValueError, match='overlap'):
        initialize_codex_auth(target, config_dir=config_arg)
    assert not source.exists() and not config.exists()


def test_codex_auth_concurrent_initializers_share_one_token(tmp_path, monkeypatch):
    target = tmp_path / 'codex-auth'
    barrier = threading.Barrier(4)
    token_hex = init_data.secrets.token_hex
    random_calls = []

    def random_once(length):
        random_calls.append(length)
        return token_hex(length)

    def install():
        barrier.wait(timeout=5)
        initialize_codex_auth(target)
        return (target / 'token').read_bytes()

    monkeypatch.setattr(init_data.secrets, 'token_hex', random_once)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: install(), range(4)))
    assert len(set(results)) == 1 and random_calls == [16, 32]
    assert {p.name for p in target.iterdir()} == {'token'}
    assert (target / 'token').stat().st_nlink == 1


@pytest.mark.parametrize('kind', ['valid', 'symlink', 'hardlink', 'invalid'])
def test_codex_auth_never_overwrites_a_concurrent_winner(tmp_path, monkeypatch, kind):
    target = tmp_path / 'codex-auth'
    outside = tmp_path / 'outside'
    winner = b'cD' * 32
    outside.write_bytes(winner)
    outside.chmod(0o600)
    link = os.link
    inode = None

    def raced_link(*args, **kwargs):
        nonlocal inode
        path = target / 'token'
        if kind == 'symlink':
            path.symlink_to(outside)
        elif kind == 'hardlink':
            link(outside, path)
        else:
            path.write_bytes(winner if kind == 'valid' else b'private invalid content')
        inode = path.lstat().st_ino
        return link(*args, **kwargs)

    monkeypatch.setattr(os, 'link', raced_link)
    if kind == 'valid':
        initialize_codex_auth(target)
        assert (target / 'token').read_bytes() == winner
    else:
        with pytest.raises(RuntimeError, match='private Codex bridge credential'):
            initialize_codex_auth(target)
    assert (target / 'token').lstat().st_ino == inode
    assert {p.name for p in target.iterdir()} == {'token'}
    assert outside.read_bytes() == winner and stat.S_IMODE(outside.stat().st_mode) == 0o600


@pytest.mark.parametrize('failure', ['write', 'short_write', 'flush', 'file_fsync', 'chown', 'chmod', 'link', 'directory_fsync'])
def test_codex_auth_failed_install_never_leaves_partial_token(tmp_path, monkeypatch, failure):
    target = tmp_path / 'codex-auth'
    fdopen, fsync, chown, chmod = os.fdopen, os.fsync, os.fchown, os.fchmod

    def fail(*args, **kwargs):
        raise OSError('private simulated failure')

    def broken_output(*args, **kwargs):
        output = fdopen(*args, **kwargs)
        write = output.write

        def partial(content):
            count = write(content[:3])
            if failure == 'short_write':
                return count
            fail()

        if failure == 'flush':
            output.flush = fail
        else:
            output.write = partial
        return output

    def broken_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) == (failure == 'directory_fsync'):
            fail()
        fsync(fd)

    def broken_metadata(fd, *args):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            fail()
        (chown if failure == 'chown' else chmod)(fd, *args)

    if failure in {'write', 'short_write', 'flush'}:
        monkeypatch.setattr(os, 'fdopen', broken_output)
    elif failure in {'chown', 'chmod'}:
        monkeypatch.setattr(os, 'f' + failure, broken_metadata)
    elif failure == 'link':
        monkeypatch.setattr(os, 'link', fail)
    else:
        monkeypatch.setattr(os, 'fsync', broken_sync)
    with pytest.raises(RuntimeError, match='^Unable to initialize private Codex bridge credential$'):
        initialize_codex_auth(target)
    if failure == 'directory_fsync':
        assert re.fullmatch(rb'[0-9a-f]{64}', (target / 'token').read_bytes())
        assert (target / 'token').stat().st_nlink == 1
        assert {p.name for p in target.iterdir()} == {'token'}
    else:
        assert not list(target.iterdir())


def test_codex_auth_failed_upgrade_keeps_existing_token(tmp_path, monkeypatch):
    target = tmp_path / 'codex-auth'
    initialize_codex_auth(target)
    path = target / 'token'
    content, inode = path.read_bytes(), path.stat().st_ino

    def fail(*args):
        raise OSError('private simulated failure')

    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(RuntimeError, match='private Codex bridge credential'):
        initialize_codex_auth(target)
    assert path.read_bytes() == content and path.stat().st_ino == inode
    assert {p.name for p in target.iterdir()} == {'token'}


@pytest.mark.parametrize('config', [None, '/config'])
def test_main_opt_in_codex_auth_directory(monkeypatch, config):
    calls = []
    monkeypatch.setattr(init_data, 'initialize', lambda: calls.append('data'))
    monkeypatch.setattr(init_data, 'install_configs', lambda path: calls.append(path))
    monkeypatch.setattr(init_data, 'initialize_codex_auth',
                        lambda path, **kwargs: calls.append((path, kwargs['config_dir'])))
    args = ['--codex-auth-dir', '/codex-auth'] + (['--config-dir', config] if config else [])
    init_data.main(args)
    assert calls == [(Path('/codex-auth'), Path('/config')), 'data'] + ([Path('/config')] if config else [])


@pytest.mark.parametrize('args', [['--codex-auth-dir'], ['--codex-auth-dir', ''], ['--codex-auth-dir', '  ']])
def test_main_rejects_empty_auth_directory_before_any_initialization(monkeypatch, args):
    monkeypatch.setattr(init_data, 'initialize', lambda: pytest.fail('Must not initialize data'))
    monkeypatch.setattr(init_data, 'initialize_codex_auth', lambda *args: pytest.fail('Must not initialize auth'))
    with pytest.raises(SystemExit) as error:
        init_data.main(args)
    assert error.value.code == 2


def test_main_rejects_config_overlap_before_changing_volumes(tmp_path, monkeypatch):
    target = tmp_path / 'private'
    monkeypatch.setattr(init_data, 'initialize', lambda: pytest.fail('Must not initialize data'))
    monkeypatch.setattr(init_data, 'install_configs', lambda *args: pytest.fail('Must not install configs'))
    with pytest.raises(ValueError, match='overlap'):
        init_data.main(['--config-dir', str(target), '--codex-auth-dir', str(target / 'auth')])
    assert not target.exists()


@pytest.mark.parametrize('module', [False, True])
def test_initializer_cli_supports_script_and_module_invocation(module):
    command = ['-m', 'backend.init_data'] if module else [str(Path(init_data.__file__))]
    result = subprocess.run([sys.executable, *command, '--help'], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and not result.stderr
    assert '--config-dir' in result.stdout and '--codex-auth-dir' in result.stdout
