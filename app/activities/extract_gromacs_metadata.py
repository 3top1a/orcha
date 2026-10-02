# SPDX-FileCopyrightText: 2026 CERN.
# SPDX-License-Identifier: MIT

"""Activity to run gmxextract over a bundle of MD simulation files.

Downloads the caller-provided file bundle (basename -> URL), selects the
files to pass to the local ``gmxextract.py`` tool (archive mode or loose
tpr/gro/top mode), runs it as a subprocess, and maps the resulting JSON onto
the experiment metadata schema. All nondeterminism (downloads, subprocess,
mapping) lives in this single activity so the workflow stays replay-safe.
"""

import asyncio
import json
import logging
import os
import tempfile
from collections import Counter
from datetime import timedelta

import httpx
from pydantic import BaseModel
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from app.activities.utils import http_verify
from app.config import get_settings
from app.schemas.gromacs_metadata import map_gromacs_metadata_to_schema

logger = logging.getLogger(__name__)

EXTRACT_GROMACS_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=1.0,
    maximum_interval=timedelta(seconds=1),
    maximum_attempts=2,
)

# File extensions treated as archives (bundle mode 1).
_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".bz2"}
# Loose-file extensions passed to gmxextract (bundle mode 2).
_LOOSE_EXTENSIONS = {".tpr", ".gro", ".top"}
# Loose flags in the order gmxextract merges the inputs (tpr first).
_LOOSE_FLAGS = ((".tpr", "--tpr"), (".gro", "--gro"), (".top", "--top"))

_STDERR_TAIL_CHARS = 2000


class ExtractGromacsMetadataRequest(BaseModel):
    """Request to extract GROMACS metadata from a file bundle."""

    files: dict[str, str]  # basename -> URL


class ExtractGromacsMetadataResponse(BaseModel):
    """Response with the mapped metadata and full provenance."""

    metadata: dict
    provenance: dict


def _decide_files(names: list[str]) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Choose which downloaded files to pass to gmxextract.

    Pure and deterministic (iterates over *names* in the given order;
    callers should pass a sorted list).

    Returns:
        ``(argv_flag_pairs, per_file_roles, dropped)`` where
        ``argv_flag_pairs`` is the flat flag/path list to append to the
        command line, ``per_file_roles`` maps each selected filename to its
        flag as ``"name (--flag)"`` strings, and ``dropped`` lists
        ``(name, reason)`` for every excluded file.

    Raises:
        ApplicationError: with type ``NoTprInBundle`` (non-retryable) when
            no archive is present and the chosen bundle has no ``.tpr``.
    """
    names = list(names)
    archives = [
        name
        for name in names
        if os.path.splitext(name)[1].lower() in _ARCHIVE_EXTENSIONS
    ]
    if archives:
        # Mode 1: first archive (sorted by name for determinism); all other
        # loose files are excluded and logged.
        archive = sorted(archives)[0]
        return (
            ["--archive", archive],
            [f"{archive} (--archive)"],
            [
                (name, "excluded while archive mode is active")
                for name in names
                if name != archive
            ],
        )

    # Mode 2: loose tpr/gro/top files grouped by basename-minus-extension.
    loose = [
        name for name in names if os.path.splitext(name)[1].lower() in _LOOSE_EXTENSIONS
    ]
    dropped: list[tuple[str, str]] = [
        (name, "unsupported extension (expected .tpr/.gro/.top)")
        for name in names
        if name not in loose
    ]
    counts = Counter(os.path.splitext(name)[0] for name in loose)
    if not counts:
        raise ApplicationError(
            "Bundle has neither an archive nor any .tpr/.gro/.top file",
            type="NoTprInBundle",
            non_retryable=True,
        )
    # Most common basename; ties break to the first alphabetically.
    max_count = max(counts.values())
    chosen = min(basename for basename, count in counts.items() if count == max_count)
    if len(counts) > 1:
        other_counts = {b: c for b, c in counts.items() if b != chosen}
        logger.info(
            "extract_gromacs_metadata: bundle basename %r wins over %s "
            "(counts: %r)",
            chosen,
            other_counts,
            dict(counts),
        )

    selected = sorted(name for name in loose if os.path.splitext(name)[0] == chosen)
    for name in loose:
        if os.path.splitext(name)[0] != chosen:
            dropped.append((name, f"basename differs from chosen bundle {chosen!r}"))

    argv_flag_pairs: list[str] = []
    per_file_roles: list[str] = []
    for ext, flag in _LOOSE_FLAGS:
        path = next((name for name in selected if name.endswith(ext)), None)
        if path is not None:
            argv_flag_pairs.extend([flag, path])
            per_file_roles.append(f"{path} ({flag})")

    if not any(name.endswith(".tpr") for name in selected):
        raise ApplicationError(
            "Bundle needs a .tpr file (or an archive); "
            f"chosen bundle {chosen!r} has no .tpr",
            type="NoTprInBundle",
            non_retryable=True,
        )
    return argv_flag_pairs, per_file_roles, dropped


def _parse_gmxextract_output(stdout: str) -> dict:
    """Parse the gmxextract JSON from stdout, defensively.

    The tool may print error text to stdout before/around the JSON object,
    so first try the whole output, then the substring from the first ``{``.

    Raises:
        ApplicationError: with type ``GmxExtractFailed`` (non-retryable) when
            no JSON object can be recovered or ``simulation`` is absent/empty.
    """
    candidates = [stdout]
    start = stdout.find("{")
    if start != -1:
        candidates.append(stdout[start:])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            simulation = parsed.get("simulation")
            if not isinstance(simulation, dict) or not simulation:
                raise ApplicationError(
                    "gmxextract output has no simulation section "
                    "(bundle without a readable .tpr is not a success)",
                    type="GmxExtractFailed",
                    non_retryable=True,
                )
            return parsed
    raise ApplicationError(
        "gmxextract produced no parseable JSON object on stdout",
        type="GmxExtractFailed",
        non_retryable=True,
    )


@activity.defn
async def extract_gromacs_metadata(
    request: ExtractGromacsMetadataRequest,
) -> ExtractGromacsMetadataResponse:
    """Run gmxextract on the file bundle and map the output to the schema."""
    settings = get_settings()
    decisions: list[dict] = []
    file_provenance: list[dict] = []

    # (a) Verify every URL, then download all files with one client.
    names = sorted(request.files)
    verify = False
    for name in names:
        try:
            file_verify = http_verify(request.files[name])
        except ValueError as e:
            raise ApplicationError(
                str(e),
                type="HostNotAllowed",
                non_retryable=True,
            ) from e
        verify = verify or file_verify
    if verify:
        logger.info(
            "extract_gromacs_metadata: at least one URL requires TLS "
            "verification; verifying all downloads"
        )

    with tempfile.TemporaryDirectory(prefix="gmxextract-") as tmpdir:
        async with httpx.AsyncClient(verify=verify) as client:
            for name in names:
                url = request.files[name]
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                path = os.path.join(tmpdir, name)
                with open(path, "wb") as handle:
                    handle.write(response.content)
                logger.info(
                    "extract_gromacs_metadata: downloaded %s (%d bytes)",
                    name,
                    len(response.content),
                )
                file_provenance.append(
                    {
                        "name": name,
                        "url": url,
                        "bytes": len(response.content),
                        "status": "ok",
                    }
                )

        # (b) Decide which files to pass to gmxextract.
        argv_flags, per_file_roles, dropped = _decide_files(names)
        mode = "archive" if argv_flags and argv_flags[0] == "--archive" else "loose"
        for role in per_file_roles:
            logger.info("extract_gromacs_metadata: selected file %s", role)
        decisions.append(
            {
                "decision": "bundle_selected",
                "value": mode,
                "detail": "selected files: " + ", ".join(per_file_roles),
            }
        )
        for name, reason in dropped:
            logger.info(
                "extract_gromacs_metadata: dropped file %s: %s", name, reason
            )
            decisions.append(
                {
                    "decision": "file_dropped",
                    "value": name,
                    "detail": reason,
                }
            )

        # Attach a role to each per-file provenance entry.
        role_by_name: dict[str, str] = {}
        for flag, path in zip(argv_flags[::2], argv_flags[1::2]):
            role_by_name[path] = flag.lstrip("-")
        for entry in file_provenance:
            entry["role"] = role_by_name.get(entry["name"], "dropped")

        # (c) Run gmxextract; argv is settings-owned + local temp paths only.
        argv = [
            settings.gmxextract_python,
            settings.gmxextract_script,
            *argv_flags,
            "--format",
            "json",
            "--gmx_bin",
            settings.gmxextract_gmx_bin,
        ]
        env = {**os.environ, "PYTHONPATH": settings.gmxextract_pythonpath}
        logger.info("extract_gromacs_metadata: running command %s", argv)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=tmpdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # (d) Check the exit code, then parse stdout defensively.
        if process.returncode != 0:
            raise ApplicationError(
                "gmxextract exited with code "
                f"{process.returncode}; stderr tail: {stderr[-_STDERR_TAIL_CHARS:]}",
                type="GmxExtractFailed",
                non_retryable=True,
            )
        raw = _parse_gmxextract_output(stdout)

        # (e) Map onto the experiment metadata schema and build provenance.
        metadata, notes = map_gromacs_metadata_to_schema(raw)
        for note in notes:
            logger.info(
                "extract_gromacs_metadata: decision %s value=%s detail=%s",
                note["decision"],
                note["value"],
                note["detail"],
            )
            decisions.append(note)

        software = metadata["simulation_setup"]["software"]
        version = metadata["simulation_setup"]["software_version"]
        if software or version:
            logger.info(
                "extract_gromacs_metadata: gromacs version %s %s",
                software,
                version,
            )
            decisions.append(
                {
                    "decision": "gromacs_version",
                    "value": version,
                    "detail": f"software={software!r} parsed by gmxextract "
                    "from gmx stderr",
                }
            )
        decisions.append(
            {
                "decision": "command",
                "value": argv,
                "detail": "exact argv passed to gmxextract",
            }
        )

        provenance = {
            "files": file_provenance,
            "decisions": decisions,
            "command": argv,
            "raw": raw,
        }
    return ExtractGromacsMetadataResponse(
        metadata=metadata, provenance=provenance
    )
