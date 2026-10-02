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
from pathlib import Path

import pytest

from app.schemas.gromacs_metadata import map_gromacs_metadata_to_schema

FIXTURE = Path(__file__).parent / "fixtures" / "em.gmxextract.json"


def _fixture_raw() -> dict:
    """Fresh deep copy of the real gmxextract output for /home/debian/em.tpr."""
    return json.loads(FIXTURE.read_text())


def _decisions(notes: list[dict]) -> dict:
    """Index decision notes by their target field (value) for assertions."""
    return {note["value"]: note for note in notes if note["decision"] == "field_unmapped"}


def test_fixture_tpr_full_mapping():
    """The full tpr fixture maps every mappable field; the rest is null."""
    metadata, notes = map_gromacs_metadata_to_schema(_fixture_raw())

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

    unmapped = _decisions(notes)
    for field in (
        "simulation_setup.force_field",
        "simulation_setup.water_model",
        "thermodynamic_state.reference_temperature",
        "thermodynamic_state.reference_pressure",
        "thermodynamic_state.thermostat",
        "thermodynamic_state.barostat",
    ):
        assert field in unmapped, f"expected a field_unmapped note for {field}"


def test_ensemble_always_null_with_field_unmapped():
    """Ensemble is never inferred; the note explains why, for any input."""
    raw = _fixture_raw()
    raw["simulation"]["inputrec"].update(
        {"tcoupl": "V-rescale", "pcoupl": "Parrinello-Rahman"}
    )
    metadata, notes = map_gromacs_metadata_to_schema(raw)
    assert metadata["thermodynamic_state"]["ensemble"] is None
    assert metadata["thermodynamic_state"]["thermostat"] == "V-rescale"
    assert metadata["thermodynamic_state"]["barostat"] == "Parrinello-Rahman"
    assert any(
        note["decision"] == "field_unmapped"
        and note["value"] == "thermodynamic_state.ensemble"
        for note in notes
    )


def test_gro_top_additions():
    """A .gro contributes box_size_and_shape context; a .top contributes
    forcefield and water_model to the same merged JSON."""
    raw = _fixture_raw()
    raw["system"] = {
        "box_size_and_shape": [9.20593, 9.20593, 9.20593],
        "water_model": "spce",
    }
    raw["simulation"]["forcefield"] = "amber99sb-ildn"

    metadata, notes = map_gromacs_metadata_to_schema(raw)
    assert metadata["simulation_setup"]["force_field"] == "amber99sb-ildn"
    assert metadata["simulation_setup"]["water_model"] == "spce"
    # Box still comes from the tpr's box (3x3), not the gro line.
    assert metadata["system"]["box_dimensions"] == [9.20593, 9.20593, 9.20593]
    assert metadata["system"]["box_type"] == "cubic"
    unmapped = _decisions(notes)
    assert "simulation_setup.force_field" not in unmapped
    assert "simulation_setup.water_model" not in unmapped


def test_missing_keys_map_to_none():
    """An empty gmxextract-shaped input maps everything to null without raising."""
    metadata, notes = map_gromacs_metadata_to_schema({})

    for sub in ("simulation_setup", "thermodynamic_state", "temporal_extent", "system"):
        assert all(value is None for value in metadata[sub].values()), sub
    # Every mappable field plus the always-null ensemble is explained.
    unmapped = _decisions(notes)
    assert len(unmapped) == 14  # 13 mappable-but-absent + ensemble


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
        metadata, _ = map_gromacs_metadata_to_schema(raw)
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
    metadata, notes = map_gromacs_metadata_to_schema(raw)
    assert metadata["system"]["box_dimensions"] == [10.0, 5.0, 5.0]
    assert metadata["system"]["box_type"] is None
    assert any(
        note["decision"] == "field_unmapped"
        and note["value"] == "system.box_type"
        for note in notes
    )


def test_box_offdiagonal_note():
    """Non-zero off-diagonals (row 2 cols 0-1) keep the 3 diagonals and add a note."""
    raw = _fixture_raw()
    raw["simulation"]["box (3x3)"] = [[5.0, 0, 0], [0, 5.0, 0], [0.25, 0.1, 5.0]]
    metadata, notes = map_gromacs_metadata_to_schema(raw)
    assert metadata["system"]["box_dimensions"] == [5.0, 5.0, 5.0]
    assert metadata["system"]["box_type"] == "cubic"
    off = [note for note in notes if note["decision"] == "box_offdiagonal"]
    assert len(off) == 1
    assert off[0]["value"] == ["box[2][0]", "box[2][1]"]


def test_simulation_length_computation():
    """simulation_length = dt * nsteps / 1000 (ns), computed and logged."""
    raw = _fixture_raw()
    metadata, notes = map_gromacs_metadata_to_schema(raw)
    # 0.002 fs * 5000 / 1000 = 0.01 ns
    assert metadata["temporal_extent"]["simulation_length"] == pytest.approx(0.01)
    computations = [
        note
        for note in notes
        if note["decision"] == "computation"
        and note["value"] == pytest.approx(0.01)
    ]
    assert len(computations) == 1
    assert "dt * nsteps / 1000" in computations[0]["detail"]

    # Missing nsteps: length is null and the formula note says so.
    raw2 = copy.deepcopy(raw)
    del raw2["simulation"]["inputrec"]["nsteps"]
    metadata2, notes2 = map_gromacs_metadata_to_schema(raw2)
    assert metadata2["temporal_extent"]["simulation_length"] is None
    assert metadata2["temporal_extent"]["timestep"] == 0.002
    assert any(
        note["decision"] == "computation"
        and note["value"] == "temporal_extent.simulation_length"
        for note in notes2
    )
