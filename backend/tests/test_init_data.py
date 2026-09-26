import os
from pathlib import Path
import stat

import pytest

from backend import init_data
from backend.init_data import initialize, install_configs


@pytest.fixture(autouse=True)
def no_ownership_change(monkeypatch):
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
