"""Small crash-safe filesystem helpers shared by the store and the built-in defaults."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def fsync_dir(directory: Path) -> None:
    """Persist directory entries (new, renamed or removed files) on POSIX.

    ``os.replace`` makes a rename atomic, but only an fsync of the containing
    directory makes it durable across a power loss. Windows has no directory
    handles to fsync; NTFS journals metadata itself, so this is a no-op there.
    """
    if os.name == "nt":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # some filesystems (e.g. certain network mounts) refuse directory fsync
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so readers see either the old or the new file.

    Writes a temporary file in the same directory, flushes and fsyncs it,
    promotes it with ``os.replace`` and fsyncs the directory. The temporary
    file is removed on failure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str) -> None:
    """Text variant of :func:`atomic_write_bytes` (UTF-8, ``\\n`` line endings)."""
    atomic_write_bytes(Path(path), text.encode("utf-8"))


def append_line_durable(path: Path, line: str) -> None:
    """Append one line, flush and fsync it; fsync the directory when the file is new."""
    path = Path(path)
    created = not path.exists()
    if created:
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        fsync_dir(path.parent)
