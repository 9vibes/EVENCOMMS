import os
import stat

import pytest

from backend.init_data import initialize


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
