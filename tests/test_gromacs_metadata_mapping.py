# SPDX-FileCopyrightText: 2026 CERN.
# SPDX-License-Identifier: MIT

"""Tests for the deterministic gmxextract -> experiment metadata mapping.

Covers the agreed mapping table: the full ``.tpr`` fixture, the additions a
``.gro``/``.top`` bundle contributes, absent keys mapping to null (never
fabricated), the always-null ``ensemble``, the box diagonal / box type
derivation, and the ``simulation_length = dt * nsteps / 1000`` computation.
"""

import copy
import json
import logging
from pathlib import Path

import pytest

from app.schemas.gromacs_metadata import map_gromacs_metadata_to_schema

log = logging.getLogger("test.mapping")

FIXTURE = Path(__file__).parent / "fixtures" / "em.gmxextract.json"


def _fixture_raw() -> dict:
    """Fresh deep copy of the real gmxextract output for /home/debian/em.tpr."""
    return json.loads(FIXTURE.read_text())


def test_fixture_tpr_full_mapping():
    """The full tpr fixture maps every mappable field; the rest is null."""
    metadata = map_gromacs_metadata_to_schema(_fixture_raw(), log)

    # All four sub-objects always present, with the exact target keys.
    assert set(metadata) == {
        "simulation_setup",
        "thermodynamic_state",
        "temporal_extent",
        "system",
    }
    assert set(metadata["simulation_setup"]) == {
        "software",
        "software_version",
        "force_field",
        "water_model",
        "integrator",
    }
    assert set(metadata["thermodynamic_state"]) == {
        "ensemble",
        "reference_temperature",
        "reference_pressure",
        "thermostat",
        "barostat",
    }
    assert set(metadata["temporal_extent"]) == {
        "simulation_length",
        "timestep",
        "number_of_steps",
    }
    assert set(metadata["system"]) == {
        "total_atoms",
        "box_type",
        "box_dimensions",
    }

    # Mappable source keys from the fixture.
    assert metadata["simulation_setup"]["software"] == "GROMACS"
    assert metadata["simulation_setup"]["software_version"] == "2022.6-dev"
    assert metadata["simulation_setup"]["integrator"] == "steep"
    assert metadata["thermodynamic_state"]["thermostat"] is None  # tcoupl = "No"
    assert metadata["thermodynamic_state"]["barostat"] is None  # pcoupl = "No"
    assert metadata["temporal_extent"]["timestep"] == 0.002
    assert metadata["temporal_extent"]["number_of_steps"] == 5000
    assert metadata["system"]["total_atoms"] == 76979
    assert metadata["system"]["box_dimensions"] == [9.20593, 9.20593, 9.20593]
    assert metadata["system"]["box_type"] == "cubic"

    # Only a .tpr was passed: .top-sourced fields are null, not fabricated.
    assert metadata["simulation_setup"]["force_field"] is None
    assert metadata["simulation_setup"]["water_model"] is None
    # No temperature/pressure coupling in this file.
    assert metadata["thermodynamic_state"]["reference_temperature"] is None
    assert metadata["thermodynamic_state"]["reference_pressure"] is None


def test_null_fields_are_logged(caplog):
    """Every null expected field is logged once at the end, with a source."""
    with caplog.at_level(logging.INFO):
        metadata = map_gromacs_metadata_to_schema(_fixture_raw(), log)

    expected_null = [
        f"{sub}.{field}"
        for sub, obj in metadata.items()
        for field, value in obj.items()
        if value is None
    ]
    assert expected_null  # the tpr-only fixture always has nulls

    field_lines = [
        record
        for record in caplog.records
        if record.message.startswith("metadata field ")
    ]
    assert [record.message.split(" ")[2] for record in field_lines] == expected_null
    # Each field line names the source it was looked up under.
    for record in field_lines:
        assert record.message.rsplit(":", 1)[1].strip()

    diff_lines = [
        record
        for record in caplog.records
        if record.message.startswith("metadata diff:")
    ]
    assert len(diff_lines) == 1
    assert diff_lines[0].message == (
        f"metadata diff: {len(expected_null)} of 16 expected fields "
        f"are null: {', '.join(expected_null)}"
    )


def test_full_bundle_logs_only_ensemble(caplog):
    """With every mappable field present, only the ensemble is logged."""
    raw = _fixture_raw()
    raw["system"]["water_model"] = "spce"
    raw["simulation"]["forcefield"] = "amber99sb-ildn"
    raw["simulation"]["inputrec"].update(
        {"tcoupl": "V-rescale", "pcoupl": "Parrinello-Rahman", "ref_t": 300, "ref_p": 1}
    )
    with caplog.at_level(logging.INFO):
        map_gromacs_metadata_to_schema(raw, log)

    # Only the never-inferred ensemble is logged, plus its diff line.
    field_lines = [
        record
        for record in caplog.records
        if record.message.startswith("metadata field ")
    ]
    assert [record.message.split(" ")[2] for record in field_lines] == [
        "thermodynamic_state.ensemble"
    ]
    assert caplog.records[-1].message == (
        "metadata diff: 1 of 16 expected fields are null: "
        "thermodynamic_state.ensemble"
    )


def test_ensemble_always_null():
    """Ensemble is never inferred, for any input."""
    raw = _fixture_raw()
    raw["simulation"]["inputrec"].update(
        {"tcoupl": "V-rescale", "pcoupl": "Parrinello-Rahman"}
    )
    metadata = map_gromacs_metadata_to_schema(raw, log)
    assert metadata["thermodynamic_state"]["ensemble"] is None
    assert metadata["thermodynamic_state"]["thermostat"] == "V-rescale"
    assert metadata["thermodynamic_state"]["barostat"] == "Parrinello-Rahman"


def test_gro_top_additions():
    """A .gro contributes box_size_and_shape context; a .top contributes
    forcefield and water_model to the same merged JSON."""
    raw = _fixture_raw()
    raw["system"] = {
        "box_size_and_shape": [9.20593, 9.20593, 9.20593],
        "water_model": "spce",
    }
    raw["simulation"]["forcefield"] = "amber99sb-ildn"

    metadata = map_gromacs_metadata_to_schema(raw, log)
    assert metadata["simulation_setup"]["force_field"] == "amber99sb-ildn"
    assert metadata["simulation_setup"]["water_model"] == "spce"
    # Box still comes from the tpr's box (3x3), not the gro line.
    assert metadata["system"]["box_dimensions"] == [9.20593, 9.20593, 9.20593]
    assert metadata["system"]["box_type"] == "cubic"


def test_missing_keys_map_to_none(caplog):
    """An empty gmxextract-shaped input maps everything to null without raising."""
    with caplog.at_level(logging.INFO):
        metadata = map_gromacs_metadata_to_schema({}, log)

    for sub in ("simulation_setup", "thermodynamic_state", "temporal_extent", "system"):
        assert all(value is None for value in metadata[sub].values()), sub
    # The end-of-run diff reports all 16 fields as null.
    diff = [
        record
        for record in caplog.records
        if record.message.startswith("metadata diff:")
    ]
    assert len(diff) == 1
    assert diff[0].message.startswith("metadata diff: 16 of 16 expected fields")


def test_malformed_input_does_not_raise():
    """Non-dict/malformed nested values are tolerated and map to null."""
    for raw in (
        {"simulation": None},
        {"simulation": {}},
        {"simulation": {"inputrec": "steep"}},  # wrong type, no crash
        {"simulation": {"inputrec": {"dt": "nan-ish"}}},
        {"simulation": {"box (3x3)": [[1, 2]]}},  # malformed matrix
        {"simulation": {"box (3x3)": [[1, 0, 0], [0, "x", 0], [0, 0, 3]]}},
        {"administrative": "GROMACS"},
    ):
        metadata = map_gromacs_metadata_to_schema(raw, log)
        assert set(metadata) == {
            "simulation_setup",
            "thermodynamic_state",
            "temporal_extent",
            "system",
        }


def test_box_diagonal_cubic_vs_non_cubic():
    """Diagonals are reported for any box; box_type is only 'cubic' if a==b==c."""
    raw = _fixture_raw()
    raw["simulation"]["box (3x3)"] = [[10.0, 0, 0], [0, 5.0, 0], [0, 0, 5.0]]
    metadata = map_gromacs_metadata_to_schema(raw, log)
    assert metadata["system"]["box_dimensions"] == [10.0, 5.0, 5.0]
    assert metadata["system"]["box_type"] is None


def test_box_offdiagonal_keeps_diagonals():
    """Non-zero off-diagonals (row 2 cols 0-1) keep the 3 diagonals."""
    raw = _fixture_raw()
    raw["simulation"]["box (3x3)"] = [[5.0, 0, 0], [0, 5.0, 0], [0.25, 0.1, 5.0]]
    metadata = map_gromacs_metadata_to_schema(raw, log)
    assert metadata["system"]["box_dimensions"] == [5.0, 5.0, 5.0]
    assert metadata["system"]["box_type"] == "cubic"


def test_simulation_length_computation():
    """simulation_length = dt * nsteps / 1000 (ns), computed deterministically."""
    raw = _fixture_raw()
    metadata = map_gromacs_metadata_to_schema(raw, log)
    # 0.002 fs * 5000 / 1000 = 0.01 ns
    assert metadata["temporal_extent"]["simulation_length"] == pytest.approx(0.01)

    # Missing nsteps: length is null.
    raw2 = copy.deepcopy(raw)
    del raw2["simulation"]["inputrec"]["nsteps"]
    metadata2 = map_gromacs_metadata_to_schema(raw2, log)
    assert metadata2["temporal_extent"]["simulation_length"] is None
    assert metadata2["temporal_extent"]["timestep"] == 0.002
