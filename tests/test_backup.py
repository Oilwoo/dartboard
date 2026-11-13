import os
import sqlite3
import tarfile
from datetime import datetime, timezone

import pytest

from dartboard import backup
from dartboard.ledger import Ledger


def data_dir(tmp_path, entries=3):
    d = tmp_path / "data"
    d.mkdir()
    led = Ledger(str(d / "ledger.db"))
    for i in range(entries):
        led._append("judgment", f"judgment:{i}", {"n": i})
    led.db.close()
    s = sqlite3.connect(str(d / "state.db"))
    s.execute("CREATE TABLE seen (rcept_no TEXT)")
    s.commit()
    s.close()
    return d


def at(minute):
    return datetime(2026, 11, 13, 18, minute, tzinfo=timezone.utc)


def test_a_backup_restores_with_an_intact_chain_and_the_same_head(tmp_path):
    d = data_dir(tmp_path)
    path = backup.create(str(d), str(tmp_path / "out"), at(30))
    assert os.path.basename(path) == "dartboard-backup-20261113-183000.tar.gz"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    v = backup.verify(path)
    assert v.ok and v.entries == 3 and v.head == Ledger(str(d / "ledger.db")).head()[1]
    restored = tmp_path / "restored"
    assert backup.restore(path, str(restored)).ok
    assert Ledger(str(restored / "ledger.db")).verify_chain() is None
    assert sorted(os.listdir(restored)) == ["ledger.db", "state.db"]  # no checkpoints.db existed


def test_a_changed_file_inside_the_archive_is_caught(tmp_path):
    path = backup.create(str(data_dir(tmp_path)), str(tmp_path / "out"), at(30))
    work = tmp_path / "x"
    work.mkdir()
    with tarfile.open(path) as tar:
        tar.extractall(work, filter="data")
    raw = sqlite3.connect(str(work / "ledger.db"))
    raw.execute("DROP TRIGGER entries_no_update")
    raw.execute("UPDATE entries SET body='{\"n\":9}' WHERE seq=2")
    raw.commit()
    raw.close()
    forged = tmp_path / "forged.tar.gz"
    with tarfile.open(forged, "w:gz") as tar:
        for name in ("manifest.json", "ledger.db", "state.db"):
            tar.add(work / name, arcname=name)
    v = backup.verify(str(forged))
    assert not v.ok
    assert any("hash does not match" in p for p in v.problems) and any("chain breaks at entry 2" in p for p in v.problems)


def test_restore_never_overwrites_existing_data(tmp_path):
    d = data_dir(tmp_path)
    path = backup.create(str(d), str(tmp_path / "out"), at(30))
    with pytest.raises(FileExistsError):
        backup.restore(path, str(d))


def test_unexpected_files_in_an_archive_are_refused(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    (tmp_path / "x").write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(tmp_path / "x", arcname="../outside.txt")
    v = backup.verify(str(evil))
    assert not v.ok and "unexpected entries" in v.problems[0]
    assert not (tmp_path.parent / "outside.txt").exists()


def test_only_the_newest_backups_are_kept(tmp_path):
    d = data_dir(tmp_path)
    for minute in range(5):
        backup.create(str(d), str(tmp_path / "out"), at(minute), keep=3)
    assert sorted(os.listdir(tmp_path / "out")) == [f"dartboard-backup-20261113-180{m}00.tar.gz" for m in (2, 3, 4)]


def test_no_ledger_no_backup(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        backup.create(str(tmp_path / "empty"), str(tmp_path / "out"))


def test_command_line(tmp_path, capsys):
    d = data_dir(tmp_path)
    assert backup.main(["create", str(d), str(tmp_path / "out")]) == 0
    path = capsys.readouterr().out.strip()
    assert backup.main(["verify", path]) == 0 and "OK: 3 entries" in capsys.readouterr().out
    assert backup.main(["restore", path]) == 2  # restore needs a target folder


def test_restore_works_when_the_target_is_on_another_disk(tmp_path, monkeypatch):
    # found in the restore drill: a rename from /tmp into a mounted folder fails with EXDEV
    path = backup.create(str(data_dir(tmp_path)), str(tmp_path / "out"), at(30))
    target = tmp_path / "mounted" / "data"
    real_replace = os.replace

    def replace_on_one_disk_only(src, dst):
        if not str(src).startswith(str(target)):
            raise OSError(18, "Invalid cross-device link")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_on_one_disk_only)
    assert backup.restore(path, str(target)).ok
    assert sorted(os.listdir(target)) == ["ledger.db", "state.db"]  # the temporary folder is gone too
