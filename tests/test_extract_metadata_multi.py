# SPDX-FileCopyrightText: 2026 CERN.
# SPDX-License-Identifier: MIT

"""Tests for the extract_metadata_multi workflow params and file selection.

Deterministic, no LLM: the params model (bundle of basename -> URL) and the
pure ``_decide_files`` bundle-selection rule (archive vs loose tpr/gro/top).
"""

import asyncio

import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from app.activities.extract_gromacs_metadata import _decide_files, _download_file
from app.workflows.extract_metadata_multi_workflow import ExtractMetadataMultiParams


# ---------- Params validation ----------


def test_params_valid_bundle():
    """A basename -> URL bundle validates."""
    params = ExtractMetadataMultiParams.model_validate(
        {
            "files": {
                "em.tpr": "https://example.com/em.tpr",
                "em.gro": "https://example.com/em.gro",
            }
        }
    )
    assert len(params.files) == 2
    assert str(params.files["em.tpr"]) == "https://example.com/em.tpr"


def test_params_rejects_no_files_key():
    """Params without the required ``files`` key are rejected."""
    with pytest.raises(ValidationError):
        ExtractMetadataMultiParams.model_validate({})


def test_params_rejects_empty_files():
    """An empty bundle is rejected (min_length=1)."""
    with pytest.raises(ValidationError):
        ExtractMetadataMultiParams.model_validate({"files": {}})


def test_params_rejects_unknown_keys():
    """Unknown top-level params are rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        ExtractMetadataMultiParams.model_validate(
            {
                "files": {"em.tpr": "https://example.com/em.tpr"},
                "extractor": "pdfplumber",
            }
        )


def test_params_rejects_bad_url():
    """Non-URL values in the bundle are rejected."""
    with pytest.raises(ValidationError):
        ExtractMetadataMultiParams.model_validate(
            {"files": {"em.tpr": "not a url"}}
        )


# ---------- _decide_files ----------


def test_decide_files_three_file_bundle():
    """em.top + em.gro + em.tpr -> one flag each, tpr first, nothing dropped."""
    argv, roles, dropped = _decide_files(
        ["em.gro", "em.tpr", "em.top"]
    )
    assert argv == ["--tpr", "em.tpr", "--gro", "em.gro", "--top", "em.top"]
    assert roles == ["em.tpr (--tpr)", "em.gro (--gro)", "em.top (--top)"]
    assert dropped == []


def test_decide_files_single_zip_wins_loose_dropped():
    """One archive + loose files -> archive mode; the loose files are dropped."""
    argv, roles, dropped = _decide_files(
        ["em.gro", "sim.zip", "em.tpr"]
    )
    assert argv == ["--archive", "sim.zip"]
    assert roles == ["sim.zip (--archive)"]
    assert dict(dropped) == {
        "em.gro": "excluded while archive mode is active",
        "em.tpr": "excluded while archive mode is active",
    }


def test_decide_files_first_archive_sorted():
    """Multiple archives -> the first one alphabetically wins."""
    argv, _, dropped = _decide_files(["b.zip", "a.zip"])
    assert argv == ["--archive", "a.zip"]
    assert [name for name, _ in dropped] == ["b.zip"]


def test_decide_files_tie_breaks_alphabetically():
    """Two basenames with equal counts -> the first alphabetically is chosen;
    the other bundle's files are dropped."""
    argv, roles, dropped = _decide_files(
        ["beta.gro", "alpha.tpr", "beta.tpr", "alpha.gro"]
    )
    assert argv == ["--tpr", "alpha.tpr", "--gro", "alpha.gro"]
    assert roles == ["alpha.tpr (--tpr)", "alpha.gro (--gro)"]
    assert dict(dropped) == {
        "beta.gro": "basename differs from chosen bundle 'alpha'",
        "beta.tpr": "basename differs from chosen bundle 'alpha'",
    }


def test_decide_files_most_common_wins():
    """The strictly most common basename beats a rarer one with more files
    overall... i.e. counts are per basename."""
    argv, _, dropped = _decide_files(
        ["a.tpr", "b.gro", "a.gro", "a.top", "b.tpr"]
    )
    # b has 2 files (gro+tpr), a has 3 -> a wins.
    assert argv == ["--tpr", "a.tpr", "--gro", "a.gro", "--top", "a.top"]
    assert dict(dropped) == {
        "b.gro": "basename differs from chosen bundle 'a'",
        "b.tpr": "basename differs from chosen bundle 'a'",
    }


def test_decide_files_unsupported_extension_dropped():
    """Non-tpr/gro/top/loose files are dropped with a reason."""
    argv, roles, dropped = _decide_files(["em.tpr", "em.log", "em.xtc"])
    assert argv == ["--tpr", "em.tpr"]
    assert roles == ["em.tpr (--tpr)"]
    assert dict(dropped) == {
        "em.log": "unsupported extension (expected .tpr/.gro/.top)",
        "em.xtc": "unsupported extension (expected .tpr/.gro/.top)",
    }


def test_decide_files_no_tpr_raises():
    """A loose bundle without a .tpr (and no archive) fails non-retryably."""
    with pytest.raises(ApplicationError) as excinfo:
        _decide_files(["em.gro", "em.top"])
    assert excinfo.value.type == "NoTprInBundle"
    assert excinfo.value.non_retryable is True


def test_decide_files_no_usable_files_raises():
    """A bundle with only unsupported files fails non-retryably."""
    with pytest.raises(ApplicationError) as excinfo:
        _decide_files(["em.log", "em.xtc"])
    assert excinfo.value.type == "NoTprInBundle"
    assert excinfo.value.non_retryable is True


def test_decide_files_empty_bundle_raises():
    """An empty bundle fails non-retryably."""
    with pytest.raises(ApplicationError) as excinfo:
        _decide_files([])
    assert excinfo.value.type == "NoTprInBundle"


# ---------- _download_file size cap ----------


class _FakeStreamResponse:
    """Minimal async stream context manager yielding fixed-size chunks."""

    def __init__(self, body: bytes):
        self._body = body

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size: int):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """httpx.AsyncClient double that streams a fixed body."""

    def __init__(self, body: bytes):
        self._body = body

    def stream(self, method: str, url: str, follow_redirects: bool = True):
        return _FakeStreamResponse(self._body)


def test_download_file_writes_content(tmp_path):
    """A file under the cap is written fully and its size is returned."""
    path = tmp_path / "em.tpr"
    size = asyncio.run(
        _download_file(_FakeClient(b"hello"), "http://h/em.tpr", str(path), 100)
    )
    assert size == 5
    assert path.read_bytes() == b"hello"


def test_download_file_rejects_oversized(tmp_path):
    """A file above the cap raises FileTooLarge and writes no more than the cap."""
    path = tmp_path / "big.tpr"
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(
            _download_file(
                _FakeClient(b"x" * 101), "http://h/big.tpr", str(path), 100
            )
        )
    assert excinfo.value.type == "FileTooLarge"
    assert excinfo.value.non_retryable is True
    assert path.stat().st_size <= 100


def test_download_file_rejects_at_limit(tmp_path):
    """A file at exactly the cap is rejected (only smaller files pass)."""
    path = tmp_path / "exact.tpr"
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(
            _download_file(
                _FakeClient(b"x" * 100), "http://h/exact.tpr", str(path), 100
            )
        )
    assert excinfo.value.type == "FileTooLarge"


def test_default_download_cap_is_50mb():
    """The default per-file cap is 50 MB."""
    from app.config import Settings

    assert Settings(_env_file=None).gmxextract_max_download_bytes == 50 * 1024 * 1024
