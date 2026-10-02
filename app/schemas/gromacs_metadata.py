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
key maps to ``None``. Every mappable-but-absent source key, and every derived
value (e.g. the box type), is reported back as a decision note
(``{"decision", "value", "detail"}``) so the activity can attach it to the
workflow's provenance.
"""

from typing import Any

# Relative tolerance for the a == b == c box-type comparison.
CUBIC_TOLERANCE = 1e-6

# Off-diagonal box entries (row 2, cols 0-1) considered "zero".
_OFFDIAGONAL_ZERO_TOLERANCE = 0.0


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


def map_gromacs_metadata_to_schema(
    raw: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Map a gmxextract JSON object onto the experiment metadata schema.

    Args:
        raw: The parsed gmxextract output. Any shape is tolerated; malformed
            or missing nested keys map to ``None`` rather than raising.

    Returns:
        A ``(metadata, notes)`` tuple where ``metadata`` always contains all
        four sub-objects (``simulation_setup``, ``thermodynamic_state``,
        ``temporal_extent``, ``system``; keys present, values may be null) and
        ``notes`` is a list of ``{"decision", "value", "detail"}`` dicts.
    """
    notes: list[dict[str, Any]] = []
    simulation = _as_dict(_as_dict(raw).get("simulation"))
    inputrec = _as_dict(simulation.get("inputrec"))
    header = _as_dict(simulation.get("header"))
    system = _as_dict(raw.get("system"))
    administrative = _as_dict(raw.get("administrative"))

    # --- metadata.simulation_setup -------------------------------------
    software = _get_path(administrative, "software_information", "software")
    if software is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "simulation_setup.software",
                "detail": "administrative.software_information.software absent",
            }
        )
    software_version = _get_path(
        administrative, "software_information", "version"
    )
    if software_version is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "simulation_setup.software_version",
                "detail": "administrative.software_information.version absent",
            }
        )
    integrator = inputrec.get("integrator")
    if integrator is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "simulation_setup.integrator",
                "detail": "simulation.inputrec.integrator absent",
            }
        )
    force_field = simulation.get("forcefield")
    if force_field is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "simulation_setup.force_field",
                "detail": "simulation.forcefield absent (.top input with an "
                "amber force field not provided)",
            }
        )
    water_model = system.get("water_model")
    if water_model is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "simulation_setup.water_model",
                "detail": "system.water_model absent (.top input not provided)",
            }
        )

    # --- metadata.thermodynamic_state ----------------------------------
    thermostat = inputrec.get("tcoupl")
    if _is_nullish(thermostat):
        thermostat = None
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "thermodynamic_state.thermostat",
                "detail": "simulation.inputrec.tcoupl is \"No\", empty or absent",
            }
        )
    barostat = inputrec.get("pcoupl")
    if _is_nullish(barostat):
        barostat = None
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "thermodynamic_state.barostat",
                "detail": "simulation.inputrec.pcoupl is \"No\", empty or absent",
            }
        )
    reference_temperature = _as_number(inputrec.get("ref_t"))
    if reference_temperature is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "thermodynamic_state.reference_temperature",
                "detail": "simulation.inputrec.ref_t absent",
            }
        )
    reference_pressure = _as_number(inputrec.get("ref_p"))
    if reference_pressure is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "thermodynamic_state.reference_pressure",
                "detail": "simulation.inputrec.ref_p absent",
            }
        )
    # The ensemble is not derivable from gmxextract output. Never inferred.
    notes.append(
        {
            "decision": "field_unmapped",
            "value": "thermodynamic_state.ensemble",
            "detail": "ensemble is not derivable from gmxextract output",
        }
    )

    # --- metadata.temporal_extent ---------------------------------------
    timestep = _as_number(inputrec.get("dt"))
    if timestep is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "temporal_extent.timestep",
                "detail": "simulation.inputrec.dt absent",
            }
        )
    number_of_steps = _as_number(inputrec.get("nsteps"))
    if number_of_steps is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "temporal_extent.number_of_steps",
                "detail": "simulation.inputrec.nsteps absent",
            }
        )
    simulation_length: float | None
    if timestep is None or number_of_steps is None:
        simulation_length = None
        notes.append(
            {
                "decision": "computation",
                "value": "temporal_extent.simulation_length",
                "detail": "simulation_length = dt * nsteps / 1000 (ns) skipped: "
                "dt and/or nsteps absent",
            }
        )
    else:
        simulation_length = timestep * number_of_steps / 1000
        notes.append(
            {
                "decision": "computation",
                "value": simulation_length,
                "detail": "simulation_length = dt * nsteps / 1000 "
                f"({timestep} fs * {number_of_steps} steps / 1000)",
            }
        )

    # --- metadata.system -------------------------------------------------
    total_atoms = _as_number(header.get("natoms"))
    if total_atoms is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "system.total_atoms",
                "detail": "simulation.header.natoms absent",
            }
        )

    # Box: diagonal of simulation["box (3x3)"].
    box_dimensions: list[float] | None = None
    box_type: str | None = None
    matrix = _box_matrix(simulation)
    if matrix is None:
        notes.append(
            {
                "decision": "field_unmapped",
                "value": "system.box_dimensions",
                "detail": "simulation.box (3x3) absent or malformed",
            }
        )
    else:
        box_dimensions = [matrix[0], matrix[4], matrix[8]]
        a, b, c = box_dimensions
        off_diagonals = [
            (name, value)
            for name, value in (
                ("box[2][0]", matrix[6]),
                ("box[2][1]", matrix[7]),
            )
            if abs(value) > _OFFDIAGONAL_ZERO_TOLERANCE
        ]
        if off_diagonals:
            notes.append(
                {
                    "decision": "box_offdiagonal",
                    "value": [name for name, _ in off_diagonals],
                    "detail": "non-zero off-diagonal box entries reported as "
                    f"{dict(off_diagonals)}; box_dimensions holds the 3 "
                    "diagonals only, angles are not inferred",
                }
            )
        if a == b == c or (
            abs(a - b) <= CUBIC_TOLERANCE * abs(a)
            and abs(a - c) <= CUBIC_TOLERANCE * abs(a)
        ):
            box_type = "cubic"
        else:
            notes.append(
                {
                    "decision": "field_unmapped",
                    "value": "system.box_type",
                    "detail": f"box diagonals {a}, {b}, {c} are not equal "
                    "(relative tolerance "
                    f"{CUBIC_TOLERANCE}); no box type inferred",
                }
            )

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
    return metadata, notes
