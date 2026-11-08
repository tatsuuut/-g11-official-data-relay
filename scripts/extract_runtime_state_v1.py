"""Restore the complete authenticated state in one gzip pass, then expose it.

The caller must finish GPG authentication first. No state is selected or omitted;
the existing authority, source and LOCK hash checks still run after restoration.
"""
from __future__ import annotations

import gzip
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import time


def extract_complete_state(archive: Path, destination: Path) -> None:
    archive, destination = Path(archive), Path(destination).absolute()
    if destination.is_symlink() or (destination.exists() and
            (not destination.is_dir() or any(destination.iterdir()))):
        raise ValueError('G11_STATE_DESTINATION_NOT_EMPTY')
    if any(p.is_symlink() for p in destination.parents):
        raise ValueError('G11_STATE_DESTINATION_SYMLINK')
    started = time.monotonic()
    staging = Path(tempfile.mkdtemp(prefix='.g11-state-extract-', dir=destination.parent))
    seen: set[str] = set()
    count = 0

    def checked_members(handle):
        nonlocal count
        for member in handle:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())
                    or (str(path) == '.' and not member.isdir())):
                raise ValueError('G11_RELAY_UNSAFE_STATE_ARCHIVE')
            name = str(path)
            if name in seen:
                raise ValueError('G11_STATE_DUPLICATE_ARCHIVE_PATH')
            seen.add(name)
            count += 1
            yield member

    try:
        with gzip.open(archive, 'rb') as compressed:
            with tarfile.open(fileobj=compressed, mode='r|') as handle:
                handle.extractall(staging, members=checked_members(handle), filter='data')
            # tar stops at its end marker; also consume the gzip footer/CRC.
            while compressed.read(1024 * 1024):
                pass
        if not count:
            raise ValueError('G11_RELAY_EMPTY_STATE_ARCHIVE')
        # A failed validation never exposes a partial state. rmdir also rejects
        # a destination that became nonempty while extraction was in progress.
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(f'G11_COMPLETE_STATE_EXTRACT=PASS MEMBERS={count} '
          f'SECONDS={time.monotonic() - started:.3f} PASSES=1')
