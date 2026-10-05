# SPDX-FileCopyrightText: 2026 CERN.
# SPDX-License-Identifier: MIT

"""Deterministic mapping of gmxextract JSON onto the experiment metadata schema.

The gmxextract tool (``GromacsMetadataExtractor``) merges the inputs of one MD
simulation run (``.tpr``/``.gro``/``.top``/``.opt``) into a single JSON object
with top-level keys ``simulation``, ``system``, ``administrative`` and
``simulated_object``. This module maps that JSON onto the repository's
experiment record ``metadata`` schema (see
``models/experiment/metadata.yaml`` on the Invenio side):

- ``metadata.simulation_setup``
- ``metadata.thermodynamic_state``
- ``metadata.temporal_extent``
- ``metadata.system``

The mapping is deterministic and never fabricates values: an absent source
key maps to ``None``. No notes are collected along the way; at the end the
result is checked key by key against the expected fields, and every null
key is logged (with the source it was looked up under) on the ``log``
logger passed in by the caller.
"""

import logging
from typing import Any

# Relative tolerance for the a == b == c box-type comparison.
CUBIC_TOLERANCE = 1e-6


# The exact keys the experiment metadata schema expects, per sub-object.
_EXPECTED_FIELDS: dict[str, tuple[str, ...]] = {
    "simulation_setup": (
        "software",
        "software_version",
        "force_field",
        "water_model",
        "integrator",
    ),
    "thermodynamic_state": (
        "ensemble",
        "reference_temperature",
        "reference_pressure",
        "thermostat",
        "barostat",
    ),
    "temporal_extent": (
        "simulation_length",
        "timestep",
        "number_of_steps",
    ),
    "system": (
        "total_atoms",
        "box_type",
        "box_dimensions",
    ),
}

# Where each expected field is looked up under, in the gmxextract JSON.
_SOURCES = {
    "simulation_setup.software": "administrative.software_information.software",
    "simulation_setup.software_version": "administrative.software_information.version",
    "simulation_setup.integrator": "simulation.inputrec.integrator",
    "simulation_setup.force_field": "simulation.forcefield",
    "simulation_setup.water_model": "system.water_model",
    "thermodynamic_state.thermostat": "simulation.inputrec.tcoupl",
    "thermodynamic_state.barostat": "simulation.inputrec.pcoupl",
    "thermodynamic_state.reference_temperature": "simulation.inputrec.ref_t",
    "thermodynamic_state.reference_pressure": "simulation.inputrec.ref_p",
    "temporal_extent.timestep": "simulation.inputrec.dt",
    "temporal_extent.number_of_steps": "simulation.inputrec.nsteps",
    "system.total_atoms": "simulation.header.natoms",
}


def _as_dict(value: Any) -> dict[str, Any]:
    """Return *value* if it is a dict, else an empty dict (defensive)."""
    return value if isinstance(value, dict) else {}


def _get_path(source: Any, *keys: str) -> Any:
    """Walk nested dicts; return ``None`` on any missing/non-dict step."""
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _is_nullish(value: Any) -> bool:
    """Whether a source value counts as absent (None/""/"No")."""
    return value is None or value == "" or value == "No"


def _as_number(value: Any) -> float | None:
    """Return *value* as a number if it is one (bools excluded), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _box_matrix(simulation: dict[str, Any]) -> list[float] | None:
    """Extract the box matrix (3x3 list of lists of numbers) if well-formed.

    Returns the 9 values flattened in row-major order, or ``None`` when the
    box key is absent or malformed.
    """
    box = simulation.get("box (3x3)")
    if not isinstance(box, list) or len(box) != 3:
        return None
    flattened: list[float] = []
    for row in box:
        if not isinstance(row, list) or len(row) != 3:
            return None
        for entry in row:
            number = _as_number(entry)
            if number is None:
                return None
            flattened.append(number)
    return flattened


def _log_unmapped(
    metadata: dict[str, dict[str, Any]],
    log: logging.Logger,
) -> None:
    """Check every expected field; log each one that is null and why."""

    def reason(path: str) -> str:
        if path == "thermodynamic_state.ensemble":
            return "ensemble is not derivable from gmxextract output"
        if path == "temporal_extent.simulation_length":
            return (
                "simulation_length = dt * nsteps / 1000 (ns) not computed: "
                "dt and/or nsteps absent"
            )
        if path == "system.box_type":
            return "box diagonals are not equal; no box type inferred"
        if path == "system.box_dimensions":
            return "simulation.box (3x3) absent or malformed"
        return f"{_SOURCES[path]} absent"

    missing = [
        f"{sub}.{field}"
        for sub, fields in _EXPECTED_FIELDS.items()
        for field in fields
        if metadata[sub][field] is None
    ]
    if not missing:
        return
    for path in missing:
        log.info("metadata field %s is null: %s", path, reason(path))
    log.info(
        "metadata diff: %d of %d expected fields are null: %s",
        len(missing),
        sum(len(fields) for fields in _EXPECTED_FIELDS.values()),
        ", ".join(missing),
    )


def map_gromacs_metadata_to_schema(
    raw: dict[str, Any],
    log: logging.Logger,
) -> dict[str, dict[str, Any]]:
    """Map a gmxextract JSON object onto the experiment metadata schema.

    Args:
        raw: The parsed gmxextract output. Any shape is tolerated; malformed
            or missing nested keys map to ``None`` rather than raising.
        log: Logger for the end-of-run diff (one line per null field plus a
            summary); typically the activity's per-run logger.

    Returns:
        The ``metadata`` dict, always containing all four sub-objects
        (``simulation_setup``, ``thermodynamic_state``, ``temporal_extent``,
        ``system``; keys present, values may be null).
    """
    simulation = _as_dict(_as_dict(raw).get("simulation"))
    inputrec = _as_dict(simulation.get("inputrec"))
    header = _as_dict(simulation.get("header"))
    system = _as_dict(raw.get("system"))
    administrative = _as_dict(raw.get("administrative"))

    # --- metadata.simulation_setup -------------------------------------
    software = _get_path(administrative, "software_information", "software")
    software_version = _get_path(
        administrative, "software_information", "version"
    )
    integrator = inputrec.get("integrator")
    force_field = simulation.get("forcefield")
    water_model = system.get("water_model")

    # --- metadata.thermodynamic_state ----------------------------------
    thermostat = inputrec.get("tcoupl")
    if _is_nullish(thermostat):
        thermostat = None
    barostat = inputrec.get("pcoupl")
    if _is_nullish(barostat):
        barostat = None
    reference_temperature = _as_number(inputrec.get("ref_t"))
    reference_pressure = _as_number(inputrec.get("ref_p"))

    # --- metadata.temporal_extent ---------------------------------------
    timestep = _as_number(inputrec.get("dt"))
    number_of_steps = _as_number(inputrec.get("nsteps"))
    if timestep is None or number_of_steps is None:
        simulation_length: float | None = None
    else:
        simulation_length = timestep * number_of_steps / 1000

    # --- metadata.system -------------------------------------------------
    total_atoms = _as_number(header.get("natoms"))

    # Box: diagonal of simulation["box (3x3)"].
    box_dimensions: list[float] | None = None
    box_type: str | None = None
    matrix = _box_matrix(simulation)
    if matrix is not None:
        box_dimensions = [matrix[0], matrix[4], matrix[8]]
        a, b, c = box_dimensions
        if a == b == c or (
            abs(a - b) <= CUBIC_TOLERANCE * abs(a)
            and abs(a - c) <= CUBIC_TOLERANCE * abs(a)
        ):
            box_type = "cubic"

    metadata = {
        "simulation_setup": {
            "software": software,
            "software_version": software_version,
            "force_field": force_field,
            "water_model": water_model,
            "integrator": integrator,
        },
        "thermodynamic_state": {
            "ensemble": None,
            "reference_temperature": reference_temperature,
            "reference_pressure": reference_pressure,
            "thermostat": thermostat,
            "barostat": barostat,
        },
        "temporal_extent": {
            "simulation_length": simulation_length,
            "timestep": timestep,
            "number_of_steps": number_of_steps,
        },
        "system": {
            "total_atoms": total_atoms,
            "box_type": box_type,
            "box_dimensions": box_dimensions,
        },
    }

    # --- end of run: diff the result against the expected fields ---------
    _log_unmapped(metadata, log)
    return metadata
