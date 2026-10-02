# Multi-file MD metadata extraction — full context

Ground-truth context gathered 2026-10-02 for implementing `extract_metadata_multi`.
This file is self-contained; the subagents executing the plan read it, not the chat.

## Goal (user-confirmed)

Add a new Orcha workflow type `extract_metadata_multi` that accepts a **bundle of
files** (the files of one MD-simulation experiment record), runs the local
`gmxextract.py` tool on them, and returns the GROMACS metadata **mapped
deterministically** onto the repository's experiment metadata schema.

User clarifications (these supersede the LLM-written
`multi-file-extract-metadata-plan.md`):

1. **TPR → JSON, not text.** `gmxextract.py` returns JSON, not text. No "render
   .tpr to text + one LLM pass" as in the original plan.
2. **No LLM at all.** "This new workflow will only be ran on MD data. The old
   ExtractMetadata workflow will still be used, but on a single file basis only,
   and will take care of PDFs." The mapped values are facts, not guesses: absent
   key → `null`, never fabricated.
3. **Result shape** (user chose "metadata + provenance + raw"):
   `result = { "metadata": {...mapped schema object...},
               "provenance": { "files": [...], "decisions": [...],
                               "command": [...argv...], "raw": {...full gmxextract JSON...} } }`
4. **File selection rule** (user chose "As proposed"):
   - (1) If any file is an archive (`.zip`/`.tar`/`.gz`/`.bz2`) → pass the first
     archive to `gmxextract --archive`; other loose files excluded + logged.
     Use case: one `sim.zip` passed to gmxextract.
   - (2) Otherwise group loose files by basename-minus-extension, pick the most
     common basename (tie → first alphabetically, logged); pass matching files by
     extension (`--tpr`/`--gro`/`--top`). Use case: `em.top`, `em.gro`, `em.tpr`.
     A bundle needs a `.tpr` or it fails.
   - Files not matching (different basename / unsupported extension) are
     excluded, each exclusion logged as a decision.
   - There is **no paper.pdf** in these bundles: the paper/study is a different
     record type. It is the user's responsibility to have normal filenames.
5. **Provenance**: "the whole workflow's captured output to be in the response
   (WANT, not MUST). So something akin to Per-file log in result." → every
   decision (bundle choice, dropped files, command argv, unmappable fields,
   software version found) is (a) logged in worker logs and (b) attached to the
   API result in `provenance.decisions`.
6. **Scope**: Orcha **and** the Invenio caller (`/home/debian/biosimtest/oarepo-orcha`).
7. **UI/JS are out of scope** ("Don't care about the UI and JS now, that's for
   later, just having the API request return the correct MD data is the goal.").

## Environment (all verified 2026-10-02)

- Orcha repo: `/home/debian/orcha`, git branch `ai/multifile` (clean, in sync
  with `origin/ai/multifile`). Stay on this branch.
- Orcha dev stack is RUNNING in tmux session `orcha` (`uv run orcha run`):
  SQLite `orcha.db`, Temporal dev server (`temporal.db`, :7233), FastAPI
  :8000 with **hot reload** (WatchFiles on `/home/debian/orcha`), Temporal
  worker on the default queue. Auth OFF (DEV_MODE → every request = `dev`
  tenant). Logs: `tmux capture-pane -t orcha -p`. Health:
  `curl -s http://localhost:8000/` →
  `{"message":"This is the backend service for Orcha!"}`.
- `temporal` CLI 1.8.2 installed. `uv` at `/home/debian/.local/bin/uv`.
- `.env` (auto-loaded by pydantic-settings): `LLM="litellm/mini"` etc. The new
  workflow does NOT use the LLM; no LLM env needed.
- GROMACS toolchain:
  - Script: `/home/debian/gromacs-metadump/cli/gmxextract.py` (CLI wrapper),
    module `GromacsMetadataExtractor.py` in the same dir.
  - Must run with **python3.13** and `PYTHONPATH=/home/debian/gromacs-metadump/cli`
    (the module is NOT installed system-wide; only dep is `pyyaml`, present).
  - gmx binary: `/home/debian/gromacs/build/bin/gmx` (custom fork with
    `gmx dump -format json`).
  - Verified working:
    `PYTHONPATH=.../cli python3.13 .../cli/gmxextract.py --tpr /home/debian/em.tpr --gmx_bin /home/debian/gromacs/build/bin/gmx`
    → exit 0, ~10.7 KB JSON on stdout (see "gmxextract contract" below).
    Test TPR available: `/home/debian/em.tpr` (1.9 MB); captured real output:
    `/tmp/gmxout.json` (may be gone; fixture copied to `tests/fixtures/`).
- Invenio caller: `/home/debian/biosimtest` (BioSIM-CZ dev, running:
  gunicorn `:5000` with `--reload --reload-extra-file oarepo-orcha/`,
  celery workers). `oarepo_orcha` is **editable-installed** from
  `/home/debian/biosimtest/oarepo-orcha` → editing `views.py` there hot-reloads
  Invenio. Config `/home/debian/biosimtest/invenio.cfg`:
  `RDM_DEPOSIT_ORCHA_ENABLED = True`, `RDM_ORCHA_DEV_MODE = True`,
  `RDM_ORCHA_URL = "http://localhost:8000"`, `SITE_UI_URL =
  "https://biosimcz-test.biodata.ceitec.cz"`.
- Orcha downloads files with `http_verify` (`app/activities/utils.py`):
  returns `False` (skip TLS verify) for localhost/127.0.0.1/::1, `True`
  otherwise. NOTE: the `HTTP_ALLOWLIST` enforcement below the `return True` is
  **dead code** (pre-existing; do not "fix" as part of this task unless
  needed for verification).

## Orcha codebase anchors (read before editing)

- `app/workflows/extract_metadata_workflow.py` — template to copy.
  `ExtractMetadataParams(url: HttpUrl, extractor="pdfplumber", pages=[1,2])`
  (line 35). `@workflow.defn class ExtractMetadata(PydanticAIWorkflow)` with
  `@workflow.run async def run(self, context: WorkflowContext, params) ->
  MetadataSuggestions`. Structure: (1) `update_workflow` with `start_time=
  workflow.now()`; (2) try: activities; except Exception → `update_workflow`
  status=ERROR + `raise`; (3) `update_workflow` status=SUCCESS,
  `result=result.model_dump()`. **Do NOT touch this file or its params**
  (guardrail; `tests/test_auth.py:472` pins a no-`url` POST as 422).
- `app/workflows/specs.py` — `WorkflowContext(workflow_id, tenant_id,
  user_id)`, `WorkflowParams` (BaseModel, `extra="forbid"`), `WorkflowSpec`
  frozen dataclass `(workflow_cls, params_model, task_queue, id_prefix)`.
- `app/workflows/registry.py` — explicit `WORKFLOW_REGISTRY` dict. Existing
  entry to copy (lines 20-25): `"extract_metadata": WorkflowSpec(
  workflow_cls=..., params_model=..., task_queue=DEFAULT_TASK_QUEUE,
  id_prefix="extract-metadata")`. Explicit by design — no discovery magic.
- `app/activities/__init__.py` — register activities in exactly 3 places:
  import line, `REGISTERED_ACTIVITIES` list, `__all__`. `app/workers.py`
  consumes `REGISTERED_ACTIVITIES` directly (no worker edit needed).
- `app/activities/extract_pdf_content.py` — structure to mirror:
  `http_verify` → `ApplicationError(type="HostNotAllowed",
  non_retryable=True)`; httpx AsyncClient `follow_redirects=True`,
  `raise_for_status()`; `RetryPolicy(initial 1s, backoff 1.0, max 1s,
  maximum_attempts=2)`.
- `app/activities/update_workflow.py` — `WorkflowUpdateRequest(public_id,
  tenant_id, status=None, result: dict|None, start_time=None, end_time=None,
  ...)`; writes JSON-serializable fields onto the `Workflow` row.
- `app/database/models.py` — `Workflow.result: dict | None =
  Field(sa_column=Column(JSON))` → **any JSON-serializable dict is accepted;
  no migration needed.**
- `app/routers/workflows.py` — POST `/workflows/` (line 52): validates
  `params` against `spec.params_model` (`ValidationError` → 422), persists
  `params=model_dump(mode="json")`, `client.start_workflow(spec.workflow_cls.
  run, args=[WorkflowContext, params], id=f"{spec.id_prefix}-{public_id}",
  task_queue=spec.task_queue, retry_policy max_attempts=1)`. GET
  `/workflows/{id}` returns the `Workflow` row (incl. `result`). SSE stream
  polls status only. No router changes needed.
- `app/config.py` — `Settings(BaseSettings)`, `env_file=".env"`,
  `extra="ignore"`. Add a `# GROMACS` section (after `# Security`, ~line 61).
- `app/schemas/metadata_suggestions.py` — `MetadataSuggestions` flat
  suggestions container; used ONLY by the single-file workflow. The new
  workflow returns a plain dict, so no changes here.
- Existing test style: `tests/test_extract_metadata.py` (no-LLM, plain
  `model_validate`), `tests/test_auth.py:428-469`
  (`test_create_workflow_stamps_tenant_id`, mocker.AsyncMock temporal client,
  asserts stored `wf.params` and `start_workflow` args). `tests/conftest.py`
  provides the fixtures.

## gmxextract contract (verified)

CLI: `gmxextract.py [-h] [--tpr TPR] [--gro GRO] [--top TOP] [--opt OPT]
--format {json,yaml} [--gmx_bin GMX_BIN] [--archive ARCHIVE] [--verbose]`

- `--tpr` obligatory **unless** `--archive` is given.
- `--archive`: `.zip`/`.tar`/`.gz`/`.bz2`; walks the extracted tree and
  auto-processes `*.tpr`/`*.gro`/`*.top`/`*.json`/`*.yaml`/`*.yml` inside
  (plus `*.custom-metadata` per `ALLOWED_EXTENSIONS`).
- One process merges all inputs into ONE JSON and prints it to stdout:
  top-level keys `simulation`, `system`, `administrative`, `simulated_object`.
  Merge order tpr → gro → top → opt; later inputs only fill keys not already
  present.
- Per-file behavior:
  - tpr: `gmx dump -s <file> -format json -section ...` (fork); software
    version parsed from stderr regex `Reading file .*, VERSION (.*) \(.*\)` →
    `administrative.software_information = {"software": "GROMACS", "version": ...}`.
  - gro: last line parsed as floats → `system.box_size_and_shape` (list).
  - top: regex `"(amber\d+)\.ff/forcefield\.itp"` → `simulation.forcefield`
    (**amber-only** — CHARMM36 etc. will NOT be captured; that's the tool's
    behavior, leave it and log absence); water-topology regex →
    `system.water_model`.
  - opt: json/yaml with top-level `administrative`/`simulated_object` keys.
- **Error handling is weak**: on failure a `process_*` prints `Error: ...` to
  **stdout** (polluting it) and the CLI still exits 0 with a possibly-degraded
  JSON. The activity must therefore: parse stdout defensively (find the JSON
  object in the output), and fail with a non-retryable `ApplicationError`
  when (a) exit code != 0, (b) no JSON object found, or (c) `simulation` is
  empty/absent (a bundle without a readable .tpr is not a success).

Verified real output shape (from `/home/debian/em.tpr`):

```
administrative.software_information = {"software": "GROMACS", "version": "2022.6-dev"}
simulation.inputrec: integrator="steep", dt=0.002, nsteps=5000, tcoupl="No",
    pcoupl="No", emstep=0.01, emtol=10, ...   (ref_t/ref_p/tau_t/tau_p ABSENT
    in this file because no temperature coupling — NVT/NPT runs have them)
simulation.header: natoms=76979, bBox="present", bTop="present", ...
"simulation."box (3x3)": [[9.20593,0,0],[0,9.20593,0],[0,0,9.20593]]   # NOTE the key contains " (3x3)"
system = {}          # filled only by .gro/.top inputs
simulated_object = {}
```

Other `inputrec` keys seen: `comm-mode`, `pbc`, `tinit`, `init-step`,
`ld-seed`, `cutoff-scheme`, `nstxout`, ... For a full NPT MD run you'd also
see `ref_t`, `ref_p`, `tau_t`, `tau_p`, `tc-grps`, `pc-couple`, `nsttcouple`,
`nstpcouple`, `nstlog`, etc. The mapping must be tolerant: read keys if
present, null otherwise.

## Target schema (the experiment record model)

`/home/debian/biosimtest/models/experiment/metadata.yaml` (read it; do not
modify). The `metadata` object the API result must fill:

```
metadata.simulation_setup:    software (vocab software), software_version (keyword),
                              force_field (vocab force_field), water_model (vocab water_model),
                              integrator (vocab integrator)
metadata.thermodynamic_state: ensemble (vocab), reference_temperature (double ≥0),
                              reference_pressure (double ≥0), thermostat (vocab), barostat (vocab)
metadata.temporal_extent:     simulation_length (double >0, ns), timestep (double >0, fs),
                              number_of_steps (long >0)
metadata.system:              total_atoms (long >0), box_type (vocab box_type),
                              box_dimensions (array of 3-6 doubles)
```

## Agreed mapping (deterministic; absent → null, never fabricated)

| target field | source in gmxextract JSON | rule / notes |
|---|---|---|
| `simulation_setup.software` | `administrative.software_information.software` | e.g. "GROMACS" |
| `simulation_setup.software_version` | `administrative.software_information.version` | e.g. "2022.6-dev"; parsed from gmx stderr by the tool |
| `simulation_setup.integrator` | `simulation.inputrec.integrator` | e.g. "steep" (GROMACS "steep" = SD for minimization) |
| `simulation_setup.force_field` | `simulation.forcefield` | only from `.top`, amber FFs only (tool limitation) |
| `simulation_setup.water_model` | `system.water_model` | only from `.top` |
| `thermodynamic_state.thermostat` | `simulation.inputrec.tcoupl` | null if "No"/""/missing |
| `thermodynamic_state.barostat` | `simulation.inputrec.pcoupl` | null if "No"/""/missing |
| `thermodynamic_state.reference_temperature` | `simulation.inputrec.ref_t` | null if missing |
| `thermodynamic_state.reference_pressure` | `simulation.inputrec.ref_p` | null if missing |
| `thermodynamic_state.ensemble` | — | **not derivable** from gmxextract output → always null + `field_unmapped` decision log. Do NOT infer from tcoupl/pcoupl. |
| `temporal_extent.timestep` | `simulation.inputrec.dt` | fs |
| `temporal_extent.number_of_steps` | `simulation.inputrec.nsteps` | |
| `temporal_extent.simulation_length` | `dt * nsteps / 1000` | computed, ns; log the formula as a decision; null if either absent |
| `system.total_atoms` | `simulation.header.natoms` | |
| `system.box_dimensions` | diagonal of `simulation["box (3x3)"]` | 3 values `[a,b,c]`; if off-diagonals (row 2 cols 0-1) are non-zero, still report the 3 diagonals AND add a decision log noting the off-diagonals. Do not invent angles. |
| `system.box_type` | derived from diagonals | `"cubic"` only if a==b==c (relative tol 1e-6); otherwise null + decision log. No other inference. |

Every null that corresponds to a mappable-but-absent source key, and every
excluded file, is recorded in `provenance.decisions` as
`{"decision": "<kind>", "value": ..., "detail": ...}` (kinds:
`bundle_selected`, `file_dropped`, `command`, `gromacs_version`,
`field_unmapped`, `box_offdiagonal`, `computation` for the length formula, ...).

## Result shape (contract)

```json
{
  "metadata": {
    "simulation_setup":     {"software": "...", "software_version": null, "force_field": null, "water_model": null, "integrator": null},
    "thermodynamic_state":  {"ensemble": null, "reference_temperature": null, "reference_pressure": null, "thermostat": null, "barostat": null},
    "temporal_extent":      {"simulation_length": null, "timestep": null, "number_of_steps": null},
    "system":               {"total_atoms": null, "box_type": null, "box_dimensions": null}
  },
  "provenance": {
    "files":    [{"name": "em.tpr", "url": "...", "role": "tpr", "bytes": 1953444, "status": "ok"}],
    "decisions": [{"decision": "...", "value": "...", "detail": "..."}],
    "command":  ["python3.13", "/home/debian/gromacs-metadump/cli/gmxextract.py", "--tpr", "em.tpr", "--gmx_bin", "/home/debian/gromacs/build/bin/gmx", "--format", "json"],
    "raw":      { "<full unmodified gmxextract JSON>" }
  }
}
```

All four `metadata` sub-objects are ALWAYS present (keys present, values
possibly null) so the consumer can rely on the shape.

## Orcha-side design (files to create/change)

1. `app/config.py` — new `# GROMACS` section after `# Security`:
   ```python
   # GROMACS metadata extraction (gmxextract CLI).
   gmxextract_python: str = "python3.13"
   gmxextract_pythonpath: str = "/home/debian/gromacs-metadump/cli"
   gmxextract_script: str = "/home/debian/gromacs-metadump/cli/gmxextract.py"
   gmxextract_gmx_bin: str = "/home/debian/gromacs/build/bin/gmx"
   ```
   (env overrides: `GMXEXTRACT_PYTHON`, `GMXEXTRACT_PYTHONPATH`,
   `GMXEXTRACT_SCRIPT`, `GMXEXTRACT_GMX_BIN`).
2. `app/activities/extract_gromacs_metadata.py` (new):
   - `ExtractGromacsMetadataRequest(BaseModel)`: `files: dict[str, str]`
     (basename → URL string).
   - `ExtractGromacsMetadataResponse(BaseModel)`: `metadata: dict`,
     `provenance: dict`.
   - `EXTRACT_GROMACS_RETRY_POLICY` = same shape as
     `EXTRACT_PDF_TEXT_RETRY_POLICY`.
   - `@activity.defn async def extract_gromacs_metadata(request) ->
     ExtractGromacsMetadataResponse`:
     (a) `http_verify` each URL (HostNotAllowed non-retryable), download all
     files with one httpx AsyncClient (`follow_redirects=True`,
     `raise_for_status()`), bytes → files in a `tempfile.TemporaryDirectory`
     (keep the caller's basenames; never put URLs in argv).
     (b) `_decide_files(names)` (pure, unit-tested): apply the file-selection
     rule → returns (argv flags list, list of per-file roles, list of dropped
     (name, reason)). Archive mode: first archive (sorted by name for
     determinism) → `--archive`; loose mode: most-common basename
     (count, tie → first alphabetically), extensions tpr/gro/top →
     `--tpr`/`--gro`/`--top`; error if no `.tpr` (and no archive) →
     `ApplicationError(type="NoTprInBundle", non_retryable=True)`.
     (c) run gmxextract via `asyncio.create_subprocess_exec(*[python, script,
     *flags, "--format", "json", "--gmx_bin", gmx_bin], cwd=None,
     env={**os.environ, "PYTHONPATH": gmxextract_pythonpath},
     stdout=PIPE, stderr=PIPE) — argv is settings-owned + local temp paths only.
     (d) parse stdout: exit != 0 → `ApplicationError(type="GmxExtractFailed",
     non_retryable=True)` with stderr tail; JSON parse: try whole stdout,
     else substring from first `{\n` / first `{`; still fails or `simulation`
     empty → same error type. (Tool prints errors to stdout; be defensive.)
     (e) `metadata, notes = map_gromacs_metadata_to_schema(raw)`; assemble
     `provenance` (files with bytes + status, all decisions incl. notes,
     command argv, raw). Log every decision via `logger.info`
     ("Make sure every decision such as this is logged.").
3. `app/schemas/gromacs_metadata.py` (new, pure, no Temporal/http imports):
   `map_gromacs_metadata_to_schema(raw: dict) -> tuple[dict, list[dict]]`
   implementing the mapping table above; returns (metadata with all 4
   sub-objects, decision notes). Unit-tested directly.
4. `app/workflows/extract_metadata_multi_workflow.py` (new; copy
   `extract_metadata_workflow.py` bookkeeping byte-for-byte):
   ```python
   class ExtractMetadataMultiParams(WorkflowParams):
       """User-provided params for the extract_metadata_multi workflow."""
       files: dict[str, HttpUrl] = Field(min_length=1)

   @workflow.defn
   class ExtractMetadataMulti(PydanticAIWorkflow):
       @workflow.run
       async def run(self, context: WorkflowContext,
                     params: ExtractMetadataMultiParams) -> dict:
   ```
   Body: (1) `update_workflow` start_time; (2) `extract_gromacs_metadata`
   with `files={name: str(url) for name, url in params.files.items()}`,
   `start_to_close_timeout=timedelta(minutes=5)`,
   retry policy; (3) except → `update_workflow` ERROR + raise; (4)
   `update_workflow` SUCCESS with `result=response.model_dump()`; return the
   result dict. **No LLM, no resolve activity.** No `extractor`/`pages`
   params (those are PDF-specific).
5. `app/activities/__init__.py` — add `extract_gromacs_metadata` in the 3 places.
6. `app/workflows/registry.py` — add entry:
   ```python
   "extract_metadata_multi": WorkflowSpec(
       workflow_cls=ExtractMetadataMulti,
       params_model=ExtractMetadataMultiParams,
       task_queue=DEFAULT_TASK_QUEUE,
       id_prefix="extract-metadata-multi",
   ),
   ```
7. NO router changes, NO migrations, NO changes to the single-file
   `extract_metadata` workflow/params/activities.

## Invenio caller change (`/home/debian/biosimtest/oarepo-orcha/oarepo_orcha/views.py`)

`get_workflow_stream_url` (lines 105-175):
- Module constant `VALID_EXTENSIONS = ["pdf", "tpr"]` (line 29) is used ONLY
  by the `allfiles` branch filter (line 132). Change to
  `["tpr", "gro", "top", "zip"]` with a comment that it applies to allfiles
  bundle selection (keep the `file.endswith` style; pre-existing quirk).
- Introduce a `workflow_type` local: `"extract_metadata"` in the `fileKey`
  branch, `"extract_metadata_multi"` in the `mode == "allfiles"` branch; use
  it in the `payload` dict (line 140). Nothing else changes
  (`trigger_workflow` payload shape stays `{"workflow_type", "user_id",
  "params"}`).
- The gunicorn instance reloads on change (`--reload --reload-extra-file
  oarepo-orcha/`); confirm via gunicorn logs (its terminal is tmux session `0`,
  window with pts/3 — or just verify behavior).
- Note the caller's existing `finally:` bug (line 129-133: file collection in
  `finally` instead of `try` body) — pre-existing, out of scope, do not fix.

## Verification plan

1. `cd /home/debian/orcha && uv run pytest tests/ -x` — full suite green
   (single-file tests unchanged).
2. Registry: `uv run python -c "from app.workflows.registry import
   get_registered_types; print(get_registered_types())"` → includes
   `extract_metadata_multi`.
3. Direct Orcha E2E (the authoritative check, works without Invenio):
   - fixture dir: `em.tpr` (copy of `/home/debian/em.tpr`) + a small synthetic
     `em.gro` (header line + one last-line "N  x  y  z  a  b  c" box line) + a
     synthetic `em.top` (amber-style include lines) + a `sim.zip` variant;
     serve with `python3 -m http.server` on 127.0.0.1 (localhost → TLS skip).
   - `POST http://localhost:8000/workflows/` with
     `{"workflow_type":"extract_metadata_multi","params":{"files":{...}}}`
     → 200. Poll `GET /workflows/{public_id}` until `status == "success"`.
   - Assert `result.metadata` matches expected values (cross-check against a
     fresh `gmxextract.py` run on the same files) and `result.provenance`
     (command, files, decisions) is present and sane. Also test: loose
     3-file bundle, single .zip bundle, no-tpr rejection (4xx/ERROR),
     empty-files 422.
   - Worker logs: `tmux capture-pane -t orcha -p` must show the decision logs.
4. Invenio-path check (best effort): the plan's curl
   (`POST .../uploads/zxk6n-xhf96/orcha` with the session cookie from the
   user's message) — the cookie may be stale; Invenio is local on :5000 and
   reachable via the tail4eac46 ts.net hostname (check `/etc/hosts`). If the
   cookie is dead, fall back to verifying: (a) Invenio reloaded the new
   `views.py`, (b) the allfiles branch code path by reading the edited
   function + a Flask test-request if feasible, and report exactly what was
   and wasn't exercised. Orcha-side E2E in (3) uses the identical
   `{"files": {...}}` params the caller would produce.
5. TLS contingency for (4): if Orcha's download of
   `https://biosimcz-test.biodata.ceitec.cz/...` signed URLs fails
   certificate verification (self-signed / SAN mismatch), report it; do NOT
   silently disable TLS verification or "fix" the dead allowlist code without
   user input.

## Constraints & guardrails

- Branch: `ai/multifile` only. Commit work there; do not push tags.
- Do not touch `app/workflows/extract_metadata_workflow.py`,
  `ExtractMetadataParams`, `app/routers/workflows.py`, the single-file
  activities, UI/JS, `metadata.yaml`, or Invenio beyond the two
  `views.py` changes.
- No new dependencies (no `pyproject.toml` changes): python3.13 + pyyaml +
  httpx + asyncio stdlib only.
- Keep Temporal replay determinism: workflow code only calls activities and
  `workflow.now()`; all nondeterminism (downloads, subprocess, mapping) lives
  inside the one activity. File iteration order: `sorted(params.files)`.
- Every user-facing decision must be both logged (worker) and in
  `provenance.decisions` (API result).
- Test style: no LLM, deterministic, follow `tests/test_extract_metadata.py`
  and `tests/test_auth.py:428-469` patterns; `tests/conftest.py` fixtures.
