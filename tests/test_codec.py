"""Tests for the export container codec (:mod:`mailing_list_ai_check.codec`).

Written against the module's two rules. Reads sniff: the container is classified
from the file's leading bytes and never from its name, so zstd, the gzip of
older exports and plain text are all read transparently under any suffix. Writes
declare: the caller says whether to compress, and :func:`codec.compressed_path`
appends the conventional suffix. Every failure below the text layer of a
compressed stream is reported as :class:`codec.CodecError`, whether it surfaces
on open or part-way through iteration.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from mailing_list_ai_check import codec

TEXT = "first line\nsecond line\nthird line\n"

#: Non-ASCII across several scripts plus an astral-plane character, to prove the
#: UTF-8 round trip is byte-exact and not silently narrowed.
UNICODE_TEXT = "héllo wörld — naïve café\nθεός ✓ 🎉\nمرحبا بالعالم\n"

#: A payload long enough that a zstd frame spans more than a trivial number of
#: bytes, so truncating it really removes frame content.
LONG_TEXT = "".join(f"line {i} {'padding ' * 12}\n" for i in range(400))


# --- file builders ------------------------------------------------------------


def _zstd_file(path: Path, text: str = TEXT) -> Path:
    with codec.open_write_text(path, compress=True) as fh:
        fh.write(text)
    return path


def _gzip_file(path: Path, text: str = TEXT) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _plain_file(path: Path, text: str = TEXT) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _read(path: Path) -> str:
    with codec.open_read_text(path) as fh:
        return fh.read()


# ==============================================================================
# detect
# ==============================================================================


def test_detect_zstd(tmp_path):
    assert codec.detect(_zstd_file(tmp_path / "a.zst")) == "zstd"


def test_detect_gzip(tmp_path):
    assert codec.detect(_gzip_file(tmp_path / "a.gz")) == "gzip"


def test_detect_plain(tmp_path):
    assert codec.detect(_plain_file(tmp_path / "a.jsonl")) == "plain"


def test_detect_empty_file_is_plain(tmp_path):
    """An empty file has no magic number; it is what an empty JSON Lines file is."""
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert codec.detect(empty) == "plain"


@pytest.mark.parametrize("prefix", [b"\x28", b"\x28\xb5", b"\x1f", b"{"])
def test_detect_file_shorter_than_the_magic_is_plain(tmp_path, prefix):
    """A file too short to hold a magic number matches nothing and reads as plain."""
    short = tmp_path / "short.bin"
    short.write_bytes(prefix)
    assert codec.detect(short) == "plain"


def test_detect_ignores_the_file_name(tmp_path):
    """Classification is by content: the suffix carries no weight either way."""
    assert codec.detect(_zstd_file(tmp_path / "misnamed.jsonl")) == "zstd"
    assert codec.detect(_gzip_file(tmp_path / "misnamed.zst")) == "gzip"
    assert codec.detect(_plain_file(tmp_path / "misnamed.zst")) == "plain"


def test_detect_missing_file_raises_file_not_found(tmp_path):
    """A missing file is distinguishable from a corrupt one."""
    with pytest.raises(FileNotFoundError):
        codec.detect(tmp_path / "nope.jsonl")


# ==============================================================================
# compressed_path
# ==============================================================================


def test_compressed_path_appends_the_suffix(tmp_path):
    """The suffix is appended, not substituted, so the format stays visible."""
    assert codec.compressed_path(tmp_path / "export.jsonl") == tmp_path / "export.jsonl.zst"


def test_compressed_path_is_idempotent(tmp_path):
    """A path already ending .zst is returned unchanged -- no .zst.zst."""
    once = codec.compressed_path(tmp_path / "export.jsonl")
    assert codec.compressed_path(once) == once
    assert codec.compressed_path(codec.compressed_path(once)) == once


def test_compressed_path_accepts_a_string_and_returns_a_path(tmp_path):
    result = codec.compressed_path(str(tmp_path / "export.jsonl"))
    assert isinstance(result, Path)
    assert result.name == "export.jsonl" + codec.COMPRESSED_SUFFIX


def test_compressed_path_suffixless_name(tmp_path):
    assert codec.compressed_path(tmp_path / "export") == tmp_path / "export.zst"


# ==============================================================================
# write -> read round trips
# ==============================================================================


def test_roundtrip_compressed(tmp_path):
    """compress=True writes a real zstd frame that reads back verbatim."""
    path = tmp_path / "out.jsonl.zst"
    with codec.open_write_text(path, compress=True) as fh:
        fh.write(TEXT)
    with open(path, "rb") as fh:
        assert fh.read(4) == codec.ZSTD_MAGIC
    assert _read(path) == TEXT


def test_roundtrip_plain(tmp_path):
    """compress=False writes the bytes as given, and they read back verbatim."""
    path = tmp_path / "out.jsonl"
    with codec.open_write_text(path, compress=False) as fh:
        fh.write(TEXT)
    assert path.read_bytes() == TEXT.encode("utf-8")
    assert _read(path) == TEXT


@pytest.mark.parametrize("compress", [True, False])
def test_roundtrip_preserves_non_ascii(tmp_path, compress):
    """UTF-8 content -- accents, other scripts, an emoji -- survives both modes."""
    path = tmp_path / "unicode.jsonl"
    with codec.open_write_text(path, compress=compress) as fh:
        fh.write(UNICODE_TEXT)
    assert _read(path) == UNICODE_TEXT


@pytest.mark.parametrize("compress", [True, False])
def test_roundtrip_iterates_line_by_line(tmp_path, compress):
    """The reader is a text handle: iterating it yields the original lines."""
    path = tmp_path / "lines.jsonl"
    with codec.open_write_text(path, compress=compress) as fh:
        fh.write(LONG_TEXT)
    with codec.open_read_text(path) as fh:
        lines = list(fh)
    assert lines == LONG_TEXT.splitlines(keepends=True)


def test_read_is_transparent_across_containers(tmp_path):
    """The same text reads back identically from zstd, gzip and plain files."""
    assert _read(_zstd_file(tmp_path / "a.zst")) == TEXT
    assert _read(_gzip_file(tmp_path / "a.gz")) == TEXT
    assert _read(_plain_file(tmp_path / "a.jsonl")) == TEXT


def test_read_ignores_the_file_name(tmp_path):
    """A misnamed file still reads: the container comes from the content."""
    assert _read(_zstd_file(tmp_path / "zstd-called-jsonl.jsonl")) == TEXT
    assert _read(_gzip_file(tmp_path / "gzip-called-zst.zst")) == TEXT
    assert _read(_plain_file(tmp_path / "plain-called-zst.zst")) == TEXT


def test_open_read_text_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        with codec.open_read_text(tmp_path / "nope.jsonl"):
            pass


# ==============================================================================
# corruption -> CodecError
# ==============================================================================


def test_truncated_zstd_raises_codec_error(tmp_path):
    """A frame cut short fails while it is being read, not when it is opened."""
    good = _zstd_file(tmp_path / "good.zst", LONG_TEXT)
    truncated = tmp_path / "truncated.zst"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    assert codec.detect(truncated) == "zstd"
    with pytest.raises(codec.CodecError):
        with codec.open_read_text(truncated) as fh:
            for _line in fh:
                pass


def test_zstd_magic_followed_by_garbage_raises_codec_error(tmp_path):
    """Content that only looks like zstd is a corrupt stream, not a plain file."""
    path = tmp_path / "fake.zst"
    path.write_bytes(codec.ZSTD_MAGIC + b"this is not a zstd frame" * 8)

    assert codec.detect(path) == "zstd"
    with pytest.raises(codec.CodecError):
        _read(path)


def test_corrupt_gzip_raises_codec_error(tmp_path):
    """The gzip read path reports corruption as the same one exception type."""
    good = _gzip_file(tmp_path / "good.gz", LONG_TEXT)
    truncated = tmp_path / "truncated.gz"
    truncated.write_bytes(good.read_bytes()[:40])

    assert codec.detect(truncated) == "gzip"
    with pytest.raises(codec.CodecError):
        with codec.open_read_text(truncated) as fh:
            for _line in fh:
                pass


def test_codec_error_names_the_container_and_the_path(tmp_path):
    """The message identifies which file failed and how it was classified."""
    path = tmp_path / "fake.zst"
    path.write_bytes(codec.ZSTD_MAGIC + b"not a frame" * 8)
    with pytest.raises(codec.CodecError) as exc:
        _read(path)
    assert "zstd" in str(exc.value)
    assert str(path) in str(exc.value)
