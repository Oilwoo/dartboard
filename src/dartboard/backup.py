"""Backups of the bot's data, and the check that a backup really restores.

A backup is a .tar.gz with consistent SQLite snapshots (the sqlite3 backup API copies safely while the bot
writes) and a manifest of file hashes and the ledger's head. Verifying one restores it into a temporary
folder, checks every hash, re-checks the ledger's hash chain and compares its head with the manifest.

    python -m dartboard.backup create DATA_DIR OUT_DIR
    python -m dartboard.backup verify ARCHIVE
    python -m dartboard.backup restore ARCHIVE EMPTY_DATA_DIR
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ledger import Ledger

FILES = ("ledger.db", "state.db", "checkpoints.db")
MANIFEST = "manifest.json"
KEEP = 14  # local copies; older ones are removed after each new backup
PREFIX = "dartboard-backup-"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(src_path: str, dst_path: str) -> None:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create(data_dir: str, out_dir: str, now: datetime | None = None, keep: int = KEEP) -> str:
    """Writes a backup into out_dir and returns its path. The ledger must exist; the other files are optional."""
    now = now or datetime.now(timezone.utc)
    if not os.path.isfile(os.path.join(data_dir, "ledger.db")):
        raise FileNotFoundError(f"no ledger.db in {data_dir}")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        names = [n for n in FILES if os.path.isfile(os.path.join(data_dir, n))]
        for name in names:
            _snapshot(os.path.join(data_dir, name), os.path.join(tmp, name))
        count, head = _head(os.path.join(tmp, "ledger.db"))
        manifest = {"created_at": now.isoformat(timespec="seconds"),
                    "files": {n: _sha256(os.path.join(tmp, n)) for n in names},
                    "ledger_entries": count, "ledger_head": head}
        fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # judgments are private
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
            for name in names:
                tar.add(os.path.join(tmp, name), arcname=name)
        os.replace(path + ".part", path)  # a crash mid-write never leaves a half archive under the real name
    _prune(out_dir, keep)
    return path


def _head(ledger_path: str) -> tuple[int, str]:
    led = Ledger(ledger_path)
    try:
        return led.head()
    finally:
        led.db.close()


def _prune(out_dir: str, keep: int) -> None:
    backups = sorted(n for n in os.listdir(out_dir) if n.startswith(PREFIX) and n.endswith(".tar.gz"))
    for name in backups[:-keep] if keep > 0 else []:
        os.remove(os.path.join(out_dir, name))


@dataclass
class Verdict:
    ok: bool
    entries: int = 0
    head: str = ""
    problems: list[str] = field(default_factory=list)


def _extract(archive: str, into: str) -> dict:
    allowed = set(FILES) | {MANIFEST}
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        unexpected = [m.name for m in members if m.name not in allowed or not m.isfile()]
        if unexpected:
            raise ValueError(f"unexpected entries in the archive: {unexpected}")
        tar.extractall(into, members=members, filter="data")
    with open(os.path.join(into, MANIFEST), encoding="utf-8") as f:
        return json.load(f)


def verify(archive: str) -> Verdict:
    """Restores into a temporary folder and checks hashes, the hash chain and the head against the manifest."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            manifest = _extract(archive, tmp)
        except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as e:
            return Verdict(False, problems=[f"cannot read the archive: {e}"])
        problems = [f"{name}: hash does not match the manifest" for name, digest in manifest["files"].items()
                    if not os.path.isfile(os.path.join(tmp, name)) or _sha256(os.path.join(tmp, name)) != digest]
        if "ledger.db" not in manifest["files"]:
            return Verdict(False, problems=problems + ["the archive has no ledger"])
        led = Ledger(os.path.join(tmp, "ledger.db"))
        try:
            broken_at = led.verify_chain()
            count, head = led.head()
        finally:
            led.db.close()
        if broken_at is not None:
            problems.append(f"hash chain breaks at entry {broken_at}")
        if (count, head) != (manifest["ledger_entries"], manifest["ledger_head"]):
            problems.append("ledger head differs from the manifest")
        return Verdict(not problems, count, head, problems)


def restore(archive: str, data_dir: str) -> Verdict:
    """Restores into data_dir only if it holds none of the bot's files, so nothing is ever overwritten."""
    existing = [n for n in FILES if os.path.exists(os.path.join(data_dir, n))]
    if existing:
        raise FileExistsError(f"{data_dir} already has {existing}; restore into an empty folder")
    v = verify(archive)
    if not v.ok:
        return v
    os.makedirs(data_dir, exist_ok=True)
    # extract next to the target: a rename cannot cross from /tmp to another disk or mount
    with tempfile.TemporaryDirectory(dir=data_dir, prefix=".restore-") as tmp:
        manifest = _extract(archive, tmp)
        for name in manifest["files"]:
            os.replace(os.path.join(tmp, name), os.path.join(data_dir, name))
    return v


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "create":
        print(create(argv[1], argv[2]))
        return 0
    if (len(argv), argv[:1]) in ((2, ["verify"]), (3, ["restore"])):
        v = verify(argv[1]) if argv[0] == "verify" else restore(argv[1], argv[2])
        print(f"{'OK' if v.ok else 'FAILED'}: {v.entries} entries, head {v.head}")
        for p in v.problems:
            print(f"  - {p}")
        return 0 if v.ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
