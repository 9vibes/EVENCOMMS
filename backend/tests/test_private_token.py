"""The isolated backend and bridge images must enforce the same file contract."""

import os
import stat
import traceback

import pytest

from backend import private_token as backend_token
from codex_bridge import private_token as bridge_token


TOKEN = 'aB12' * 16


@pytest.fixture(params=[backend_token, bridge_token], ids=['backend', 'bridge'])
def reader(request):
    return request.param.load_token_file


@pytest.mark.parametrize('length', [32, 64, 256])
@pytest.mark.parametrize('newline', [b'', b'\n'])
@pytest.mark.parametrize('mode', [0o400, 0o440, 0o600, 0o640])
def test_token_file_contract(reader, tmp_path, length, newline, mode):
    path = tmp_path / 'token'
    token = ('aB12' * 64)[:length]
    path.write_bytes(token.encode('ascii') + newline)
    path.chmod(mode)
    before = path.stat()
    assert reader(str(path)) == token
    after = path.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode, after.st_mtime_ns) == (
        before.st_ino, before.st_uid, before.st_gid, before.st_mode, before.st_mtime_ns)


@pytest.mark.parametrize('content', [
    b'', b'a' * 31, b'a' * 257, b'a' * 257 + b'\n', b'g' * 64,
    b'a' * 64 + b'\r\n', b'a' * 64 + b'\n\n', b' ' + b'a' * 64,
    b'a' * 64 + b' ', b'a' * 32 + b'\n' + b'a' * 32, b'\xff' * 64,
    b'a' * 63 + b'\0', b'\xef\xbb\xbf' + b'a' * 64,
])
def test_token_file_rejects_invalid_content(reader, tmp_path, content, capsys, caplog):
    path = tmp_path / 'private-token'
    path.write_bytes(content)
    path.chmod(0o440)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$') as error:
        reader(str(path))
    assert str(path) not in str(error.value)
    assert capsys.readouterr() == ('', '') and not caplog.text
    assert path.read_bytes() == content


@pytest.mark.parametrize('filename', ['', 'token', './token', '/', '//', '/a/../token',
                                       '/token/', '/' + 'a' * 4096, '/private\0/token'])
def test_invalid_paths_never_touch_files(reader, filename, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid paths must fail before filesystem access')

    monkeypatch.setattr(os, 'open', forbidden)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$'):
        reader(filename)


@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'hardlink', 'directory', 'fifo', 'oversized'])
def test_unsafe_token_files_are_not_read(reader, tmp_path, kind, monkeypatch):
    outside = tmp_path / 'outside'
    outside.write_text(TOKEN)
    outside.chmod(0o600)
    path = tmp_path / 'token'
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
        with path.open('wb') as output:
            output.truncate(1024 * 1024 * 1024)
        path.chmod(0o440)
    before = outside.stat()

    def forbidden(*args):
        pytest.fail('Unsafe or oversized files must not be read')

    monkeypatch.setattr(os, 'read', forbidden)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$'):
        reader(str(path))
    assert outside.stat() == before


@pytest.mark.parametrize('nested', [False, True])
def test_token_parent_symlinks_are_rejected(reader, tmp_path, nested):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'token').write_text(TOKEN)
    (outside / 'token').chmod(0o440)
    link = tmp_path / 'link'
    link.symlink_to(outside, target_is_directory=True)
    if nested:
        (outside / 'nested').mkdir()
        (outside / 'nested/token').write_text(TOKEN)
        (outside / 'nested/token').chmod(0o440)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$'):
        reader(str(link / 'nested/token' if nested else link / 'token'))


@pytest.mark.parametrize('mode', [0o444, 0o604, 0o620, 0o602, 0o660, 0o666])
def test_insecure_permissions_rejected_before_read(reader, tmp_path, monkeypatch, mode):
    path = tmp_path / 'token'
    path.write_text(TOKEN)
    path.chmod(mode)

    def forbidden(*args):
        pytest.fail('Insecure token files must not be read')

    monkeypatch.setattr(os, 'read', forbidden)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$'):
        reader(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize('change', ['grow', 'hardlink', 'permissions', 'short_read'])
def test_bounded_read_detects_concurrent_changes(reader, tmp_path, monkeypatch, change):
    path = tmp_path / 'token'
    path.write_text(TOKEN)
    path.chmod(0o600)
    read = os.read
    calls = []

    def racing_read(descriptor, length):
        calls.append(length)
        assert length == 258
        if change == 'grow':
            with path.open('ab') as output:
                output.write(b'a' * 4096)
        elif change == 'hardlink':
            os.link(path, tmp_path / 'linked')
        elif change == 'permissions':
            path.chmod(0o666)
        return read(descriptor, 32 if change == 'short_read' else length)

    monkeypatch.setattr(os, 'read', racing_read)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$'):
        reader(str(path))
    assert calls == [258]


def test_os_read_failures_are_generic_and_close_descriptors(reader, tmp_path, monkeypatch):
    path = tmp_path / 'token'
    path.write_text(TOKEN)
    path.chmod(0o440)
    descriptors = set()
    open_file, close = os.open, os.close

    def tracked_open(*args, **kwargs):
        descriptor = open_file(*args, **kwargs)
        descriptors.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        descriptors.remove(descriptor)
        close(descriptor)

    def failing_read(*args):
        raise OSError(13, TOKEN, str(path))

    monkeypatch.setattr(os, 'open', tracked_open)
    monkeypatch.setattr(os, 'close', tracked_close)
    monkeypatch.setattr(os, 'read', failing_read)
    with pytest.raises(ValueError, match='^Invalid CODEX_BRIDGE_TOKEN_FILE$') as error:
        reader(str(path))
    rendered = ''.join(traceback.format_exception(error.value))
    assert TOKEN not in rendered and str(path) not in rendered
    assert not descriptors
