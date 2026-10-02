# Plan v2 — `extract_metadata_multi` (MD simulation bundle → experiment metadata)

**Status: awaiting user confirmation.**
Supersedes `multi-file-extract-metadata-plan.md` (LLM-written).
All ground-truth context: `multi-file-context.md` (read it first — it contains
verified file anchors, the gmxextract contract, the mapping table, the result
contract, and the verification plan).

## What changes vs. plan v1

| v1 (LLM plan) | v2 (user-confirmed) |
|---|---|
| Render `.tpr` → text via a `gmx dump` fork | Shell out to existing `gmxextract.py` (python3.13 + custom `gmx` fork), which already merges tpr/gro/top/opt/archive into one JSON |
| One LLM pass + resolve over combined text, flat `MetadataSuggestions` | **No LLM.** Deterministic mapping of the gmxextract JSON onto the repository's experiment `metadata.*` schema (absent → null, never fabricated). Old `extract_metadata` keeps PDFs (single file). |
| `files: {basename: url}`, PDF path for non-`.tpr` | `files: {basename: url}`; bundle = loose same-basename files (`em.tpr`/`em.gro`/`em.top`) or one archive (`sim.zip`); no PDFs |
| — | `provenance` (files, decisions, command argv, full raw JSON) attached to the result; every decision also logged in worker logs |
| Step 6 caller swap only | Caller swap + `VALID_EXTENSIONS = ["tpr","gro","top","zip"]` for allfiles mode |

## Tasks

### T1 — Orcha core (one subagent, sequential)
Repo: `/home/debian/orcha`, branch `ai/multifile` (already checked out).
Orcha dev stack is already running in tmux session `orcha` (hot reload — do
NOT restart it; code edits reload the API, and worker restarts pick up new
activities automatically via the `orcha run` supervisor).

1. `app/config.py`: add the `# GROMACS` settings section exactly as in
   `multi-file-context.md` § "Orcha-side design" item 1.
2. `app/schemas/gromacs_metadata.py` (new): pure function
   `map_gromacs_metadata_to_schema(raw: dict) -> tuple[dict, list[dict]]`
   implementing the mapping table in `multi-file-context.md`. No Temporal,
   http, or app imports. All four `metadata.*` sub-objects always present.
3. `app/activities/extract_gromacs_metadata.py` (new): per
   `multi-file-context.md` item 2 (request/response models, retry policy,
   `http_verify` per URL, download to tempdir, `_decide_files` selection
   logic, `asyncio.create_subprocess_exec` with `PYTHONPATH` env, defensive
   JSON parsing, non-retryable `ApplicationError`s: `HostNotAllowed`,
   `NoTprInBundle`, `GmxExtractFailed`; `logger.info` for every decision).
4. `app/activities/__init__.py`: register in the 3 places (import,
   `REGISTERED_ACTIVITIES`, `__all__`).
5. `app/workflows/extract_metadata_multi_workflow.py` (new): copy the
   `extract_metadata_workflow.py` bookkeeping structure;
   `ExtractMetadataMultiParams.files: dict[str, HttpUrl] = Field(min_length=1)`;
   single `extract_gromacs_metadata` activity; result =
   `response.model_dump()`; return dict.
6. `app/workflows/registry.py`: add `extract_metadata_multi` entry.
7. Tests (no LLM, deterministic):
   - `tests/test_gromacs_metadata_mapping.py`: mapping table coverage —
     full tpr JSON (use `tests/fixtures/em.gmxextract.json`, copied from
     `/tmp/gmxout.json`), gro/top additions (water_model, forcefield,
     box_size_and_shape), missing keys → null, `ensemble` always null +
     `field_unmapped`, box diagonal + cubic vs non-cubic + off-diagonal
     note, `simulation_length = dt*nsteps/1000`.
   - `tests/test_extract_metadata_multi.py`: params validation (valid dict,
     empty `{}` rejected, `{"files": {}}` rejected, unknown key rejected,
     bad URL rejected); `_decide_files` cases (3-file bundle, single zip
     wins + loose dropped, tie → alphabetical, no tpr → error, archive +
     loose → archive).
   - `tests/test_auth.py`: add `test_create_multi_file_workflow_stamps_tenant_id`
     mirroring `test_create_workflow_stamps_tenant_id` (lines 428-469) with
     `workflow_type="extract_metadata_multi"`, 2-file `files` dict → 200,
     stored params, `start_workflow` args.
8. Gate: `uv run pytest tests/ -x` green; registry shows
   `extract_metadata_multi`.

### T2 — Invenio caller (one subagent, independent of T1 at file level,
but E2E verification needs T1 done)
Repo: `/home/debian/biosimtest` (do NOT commit; working-tree edit only —
separate repo, user will handle commits there).
`oarepo-orcha/oarepo_orcha/views.py`:
1. `VALID_EXTENSIONS = ["tpr", "gro", "top", "zip"]` (+ comment: allfiles
   bundle selection; the single-file `fileKey` branch is unaffected — it uses
   an explicit key, not this filter).
2. `workflow_type` local in `get_workflow_stream_url`: `"extract_metadata"`
   (fileKey branch) / `"extract_metadata_multi"` (allfiles branch), used in
   the `payload` dict.
3. Verify gunicorn picked it up (reload log) and the function reads correctly.

### T3 — End-to-end verification (main agent, after T1+T2)
Per `multi-file-context.md` § "Verification plan" items 3-5:
1. Fixture bundle (real `/home/debian/em.tpr` + synthetic `em.gro`/`em.top`
   + `sim.zip`), `python3 -m http.server` on 127.0.0.1.
2. POST `extract_metadata_multi` to `http://localhost:8000/workflows/`, poll
   to `success`, assert `result.metadata` against a fresh `gmxextract.py`
   ground-truth run; check `result.provenance` (files/decisions/command/raw);
   check worker decision logs via `tmux capture-pane -t orcha -p`.
3. Negative cases: no-tpr bundle → workflow ERROR; `files: {}` → 422; single
   `sim.zip` → success.
4. Invenio path: try the user's curl (cookie may be stale → fall back to
   reading the reloaded code + reporting exactly what was exercised).
5. Update `CHANGELOG.md` `[Unreleased]` with a `feat:` bullet (repo
   convention).

## Explicit non-goals
- No UI/JS changes (explicitly deferred by user).
- No changes to the single-file `extract_metadata` workflow/params, router,
  migrations, LLM, `metadata.yaml`, `pyproject.toml`.
- No "fixes" to pre-existing quirks (dead allowlist code in `http_verify`,
  the caller's `finally` bug) unless a verification step actually breaks.
