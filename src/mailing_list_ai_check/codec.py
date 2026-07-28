"""Streaming compression for the export/import file format.

Exports are JSON Lines and therefore highly compressible, but they are also
unbounded: a database of 100,000 messages produces a file of several gigabytes
before compression. Both facts drive the two rules this module exists to
enforce.

- **Everything streams.** Nothing here ever holds a whole file — compressed or
  decompressed — in memory. The writer takes a text handle that flushes
  compressed frames as they fill; the reader decompresses in chunks behind a
  text handle the caller iterates line by line. Peak memory is therefore a
  function of the backend's buffer sizes, not of the file's size.
- **Reads sniff, writes declare.** Compression on read is detected from the
  first bytes of the file, never from the file name, because older exports were
  gzip and plain and may carry any suffix (and a ``.jsonl`` file may well hold
  zstd). Writes are explicit: the caller says whether to compress, and
  :func:`compressed_path` appends the conventional suffix.

zstd at level 3 is the format for new exports: it compresses this kind of text
roughly as well as gzip's default while being several times faster in both
directions, and its frame format is designed for exactly the chunked streaming
used here. Level 3 is the library default and the point where the ratio curve
flattens; higher levels cost noticeably more CPU for a percent or two of size on
JSON Lines.

**Backend.** zstd entered the standard library in Python 3.14 as
:mod:`compression.zstd`. That release is therefore the project's ``requires-python``
floor, which keeps zstd a standard-library concern and adds no third-party
dependency. Every failure mode is normalised to one exception type,
:class:`CodecError`.

(The module is called ``codec`` rather than ``compression`` deliberately: a local
module named ``compression.py`` would shadow the standard library package this
module imports from.)
"""

from __future__ import annotations

import gzip
import io
import zlib
from collections.abc import Iterator
from compression import zstd
from contextlib import contextmanager
from pathlib import Path

#: zstd compression level used for every export. The library default, and the
#: knee of the ratio/speed curve for JSON Lines; see the module docstring.
ZSTD_LEVEL = 3

#: Suffix appended to a compressed export's path by :func:`compressed_path`.
COMPRESSED_SUFFIX = ".zst"

#: Leading bytes identifying each supported container. zstd frames start with
#: the magic number 0xFD2FB528 (little-endian); gzip members start with 0x1F8B.
#: Only these prefixes are read from a file to classify it — see :func:`detect`.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"

_MAGIC_BYTES = max(len(ZSTD_MAGIC), len(GZIP_MAGIC))


class CodecError(Exception):
    """Raised when a compressed stream cannot be read.

    Every failure — a bad or truncated zstd frame, a bad gzip member, an
    unexpected end of file, or a low-level I/O error while decompressing — is
    normalised to this one type so callers need only a single ``except`` clause.
    """


#: Exceptions a stream may raise from corrupt or truncated input, mapped to
#: :class:`CodecError` by :meth:`_TranslatingTextReader._guard`. ``EOFError`` is
#: how both :mod:`compression.zstd` and :mod:`gzip` report truncation;
#: ``OSError`` covers :class:`gzip.BadGzipFile`; ``UnicodeDecodeError`` catches
#: payloads that decompress to bytes that are not the UTF-8 this module writes.
_STREAM_ERRORS: tuple[type[BaseException], ...] = (
    zstd.ZstdError,
    zlib.error,
    EOFError,
    OSError,
    UnicodeDecodeError,
)


def _zstd_open_text(path: Path, mode: str, *, level: int | None = None):
    """Open ``path`` as a UTF-8 zstd text stream (``mode`` is ``"rt"`` or ``"wt"``).

    :func:`compression.zstd.open` wraps a chunked (de)compressor in a
    :class:`io.TextIOWrapper`, so neither direction materialises the file.
    """
    return zstd.open(path, mode, level=level, encoding="utf-8")


def _gzip_open_text(path: Path, mode: str):
    """Open ``path`` as a UTF-8 gzip text stream; read-only in practice.

    Exports are no longer written as gzip, but files produced before zstd are,
    so the read path still needs it. Given the same signature as
    :func:`_zstd_open_text` so :func:`open_read_text` can pick between them.
    """
    return gzip.open(path, mode, encoding="utf-8")


# --- Paths and detection ------------------------------------------------------


def compressed_path(path: str | Path) -> Path:
    """Return ``path`` with :data:`COMPRESSED_SUFFIX` appended, idempotently.

    A path that already ends ``.zst`` is returned unchanged, so a caller that
    passes ``export.jsonl.zst`` does not end up with ``export.jsonl.zst.zst``.
    The suffix is appended rather than substituted: ``export.jsonl`` becomes
    ``export.jsonl.zst``, keeping the format visible in the name.
    """
    p = Path(path)
    if p.name.endswith(COMPRESSED_SUFFIX):
        return p
    return p.with_name(p.name + COMPRESSED_SUFFIX)


def detect(path: str | Path) -> str:
    """Classify ``path`` as ``"zstd"``, ``"gzip"`` or ``"plain"`` by its content.

    Reads only the first few bytes and compares them with the container magic
    numbers; the file name is never consulted, because exports predating zstd
    carry no suffix convention that can be trusted and a compressed stream may
    be stored under any name. A file too short to hold a magic number — the
    empty file included — has no match and is reported as ``"plain"``, which is
    what an empty JSON Lines file effectively is; only the caller's parse of the
    content can judge it further.

    Propagates :class:`FileNotFoundError` (and other open-time
    :class:`OSError`\\ s) unchanged, so callers can distinguish a missing file
    from a corrupt one.
    """
    with open(path, "rb") as fh:
        prefix = fh.read(_MAGIC_BYTES)
    if prefix.startswith(ZSTD_MAGIC):
        return "zstd"
    if prefix.startswith(GZIP_MAGIC):
        return "gzip"
    return "plain"


# --- Reading and writing ------------------------------------------------------


class _TranslatingTextReader(io.TextIOBase):
    """A text handle that reports stream corruption as :class:`CodecError`.

    Decompression is lazy: a truncated frame or corrupt block surfaces while the
    caller is part-way through iterating lines, not when the file is opened. So
    the wrapping has to live on the read path rather than around the ``open``,
    and every read entry point the caller might use is funnelled through
    :meth:`_guard`. Iteration is included implicitly — :class:`io.IOBase` drives
    ``__next__`` through :meth:`readline`.
    """

    def __init__(self, handle: io.TextIOBase, path: str | Path, kind: str) -> None:
        self._handle = handle
        self._path = path
        self._kind = kind

    def _guard(self, call, *args):
        try:
            return call(*args)
        except _STREAM_ERRORS as exc:
            raise CodecError(
                f"corrupt or truncated {self._kind} stream: {self._path}: {exc}"
            ) from exc

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        return self._guard(self._handle.read, size)

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        return self._guard(self._handle.readline, size)

    def close(self) -> None:
        self._handle.close()


@contextmanager
def open_write_text(path: str | Path, *, compress: bool) -> Iterator[io.TextIOBase]:
    """Open ``path`` for writing UTF-8 text, zstd-compressed when ``compress``.

    Yields a text handle whose writes are streamed straight through the
    compressor, so the caller may emit a file of any size in bounded memory.
    ``compress=False`` yields a plain :func:`open` handle; no suffix is added or
    inspected here, so pass a path already run through :func:`compressed_path`
    when compressing.
    """
    if compress:
        handle = _zstd_open_text(Path(path), "wt", level=ZSTD_LEVEL)
    else:
        handle = open(path, "wt", encoding="utf-8")
    try:
        yield handle
    finally:
        handle.close()


@contextmanager
def open_read_text(path: str | Path) -> Iterator[io.TextIOBase]:
    """Open ``path`` for reading UTF-8 text, decompressing whatever it holds.

    The container is identified by :func:`detect`, so zstd, gzip (the format of
    older exports) and uncompressed files are all read transparently and the
    file name is irrelevant. Decompression is chunked: iterating the yielded
    handle line by line reads a file of any size in bounded memory.

    Failures below the text layer of a compressed stream — a corrupt or
    truncated frame, an unexpected end of file — are raised as
    :class:`CodecError`, whether they occur on open or part-way through
    iteration. A plain file is not wrapped: there is no stream to corrupt, and
    an :class:`OSError` from it is a genuine I/O error the caller should see as
    itself.
    """
    kind = detect(path)
    if kind == "plain":
        with open(path, "rt", encoding="utf-8") as handle:
            yield handle
        return

    opener = _zstd_open_text if kind == "zstd" else _gzip_open_text
    try:
        raw = opener(Path(path), "rt")
    except _STREAM_ERRORS as exc:
        raise CodecError(f"cannot open {kind} stream: {path}: {exc}") from exc
    reader = _TranslatingTextReader(raw, path, kind)
    try:
        yield reader
    finally:
        reader.close()
