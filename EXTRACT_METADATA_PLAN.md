# Multi-file extract_metadata in Orcha

## Context

The Invenio caller (`/home/debian/biosimtest/oarepo-orcha/oarepo_orcha/views.py`, NOT edited — it is ground truth) already posts, for `mode == "allfiles"`, the body:

```json
{"workflow_type": "extract_metadata", "user_id": "...", "params": {"files": {"<basename>": "<signed url>", ...}}}
```

Single-file mode still posts `params: {"url": "<signed url>"}`. Same `workflow_type` for both. Today `ExtractMetadataParams` (`app/workflows/extract_metadata_workflow.py:35`) is `url: HttpUrl` with `extra="forbid"` (inherited from `WorkflowParams`, `app/workflows/specs.py:22`), so the `{files: ...}` call is rejected 422 at `app/routers/workflows.py:70`.

Files are a bundle describing one record (PDF + GROMACS `.tpr`; metadata spans both), and the caller's JS reads `workflow.result.suggestions` as one flat array. So: extract text per file, then ONE LLM pass + ONE resolve pass → single suggestions array.

Answer to "large architectural change?": **No.** The dispatch core (`routers/workflows.py` create, `workflows/registry.py`, `workflows/specs.py`, `workers.py`, DB models, SSE stream, auth) is params-shape agnostic — it validates `params` against `spec.params_model` and persists JSON. Required changes: 1 params-model change, 1 workflow-body branch, 1 new `.tpr` activity + registration. No migrations, no router/registry/worker edits.

## Approach

### 1. Accept both param shapes (validation gate)

In `app/workflows/extract_metadata_workflow.py`, rework `ExtractMetadataParams`:

```python
class ExtractMetadataParams(WorkflowParams):
    url: HttpUrl | None = None
    files: dict[str, HttpUrl] | None = None
    extractor: str = "pdfplumber"
    pages: list[int] | None = Field(default_factory=lambda: [1, 2])

    @model_validator(mode="after")
    def _one_source(self) -> "ExtractMetadataParams":
        if bool(self.url) == bool(self.files):
            raise ValueError("Provide exactly one of 'url' or 'files'.")
        return self
```

- Keep `extra="forbid"` (inherited). Import `model_validator` from `pydantic`.
- Empty dict `files: {}` fails the validator (`bool({})` is False, same as missing).
- `app/routers/workflows.py:70` already surfaces this as 422 via `ValidationError` — no router change.
- Existing tests stay green: `tests/test_auth.py:472` (`{"extractor": ...}` → 422, no source) and `:493` (extra key → 422) still hold; `:454` asserts stored `wf.params` equals `{"url": ..., "extractor": "pdfplumber", "pages": [1, 2]}` — add `files` default only if `model_dump(mode="json")` includes it. It will include `"files": null` → update that assertion at `tests/test_auth.py:454-458` to add `"files": None`. That is the only existing-test edit.

### 2. Per-file text extraction inside the workflow

In `ExtractMetadata.run` (`app/workflows/extract_metadata_workflow.py:47`), replace the single `extract_pdf_text` call (lines 67–76) with a source list + parallel fan-out, then feed the combined text into the UNCHANGED activity 2 (`extract_metadata_with_llm`, `args=[ExtractMetadataRequest(text=...), context]`) and activity 3 (`resolve_metadata_suggestions`). Pattern to copy for a second `execute_activity` arg style: `check_funding_relevance_workflow.py:54-66`.
- Build `sources: dict[str, HttpUrl]` before the helper: `sources = {"document": params.url}` when `params.url is not None`, else `sources = dict(params.files)` (caller-sent basenames; single-file mode has no basename and the key only labels the text block). Then:

```python
async def _extract_one(name: str, url: HttpUrl) -> str:
    if name.lower().endswith(".tpr"):
        res = await workflow.execute_activity(
            extract_tpr_text,
            ExtractTprContentRequest(url=str(url)),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=EXTRACT_TPR_TEXT_RETRY_POLICY,
        )
    else:
        res = await workflow.execute_activity(
            extract_pdf_text,
            ExtractPdfContentRequest(url=str(url), extractor=params.extractor, pages=params.pages),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=EXTRACT_PDF_TEXT_RETRY_POLICY,
        )
    return f"# File: {name}\n{res.text}"

texts = await asyncio.gather(*(_extract_one(n, u) for n, u in sorted(sources.items())))
content_text = "\n\n".join(texts)
```

- `import asyncio` at module top (already imported in `extract_pdf_content.py`, copy allowed).
- `sorted(sources)` keeps `asyncio.gather` order deterministic — required for workflow determinism on replay.
- Non-PDF, non-`.tpr` extensions go down the PDF path and fail there with the extractor's own error (caller filters by `VALID_EXTENSIONS = ["pdf", "tpr"]`; no extra handling).
- Failure of ANY file fails the whole workflow: the existing `except Exception:` block (`:93-106`) already marks the run ERROR. Bundle semantics: partial metadata from a half-readable record is worse than an error.
- Single-file path is behaviorally identical to today: one `extract_pdf_text` call, text passed on (the `# File: document\n` header line is inert for the LLM prompt; acceptable, keeps one code path).

### 3. New `.tpr` activity (thin, self-contained)

New file `app/activities/extract_tpr_content.py`, mirroring the structure of `app/activities/extract_pdf_content.py` (download with httpx + `http_verify` from `app/activities/utils.py`, same non-retryable `ApplicationError(type="HostNotAllowed")` on bad host):

```python
EXTRACT_TPR_TEXT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=1.0,
    maximum_interval=timedelta(seconds=1),
    maximum_attempts=2,
)

class ExtractTprContentRequest(BaseModel):
    url: str

class ExtractTprContentResponse(BaseModel):
    text: str

@activity.defn
async def extract_tpr_text(request: ExtractTprContentRequest) -> ExtractTprContentResponse: ...
```

Body: GET `request.url` (follow_redirects=True, raise_for_status) → write bytes to `tempfile.NamedTemporaryFile(suffix=".tpr")` → shell out:

```python
proc = await asyncio.create_subprocess_exec(
    *get_settings().gmx_dump_command, tmp.name,
    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
```

- Non-zero exit → `ApplicationError(f"gmx dump failed: {stderr.decode(errors='replace')[:2000]}", type="TprDumpFailed", non_retryable=True)`.
- Empty stdout → return `ExtractTprContentResponse(text="")`; the downstream `MIN_TEXT_CHARS` gate in `extract_metadata_with_llm` already handles near-empty text (no fabrication).
- Do NOT pass the URL to the subprocess; the fork reads a local path. `gmx_dump_command` is a settings-owned argv list, so no shell injection surface; basename from the caller never reaches argv.

`app/config.py`: add to `Settings`, under a `# GROMACS` comment:

```python
gmx_dump_command: list[str] = ["gmx-dump-fork", "-s"]
```

(env override `GMX_DUMP_COMMAND` as JSON list, works via existing `SettingsConfigDict`; adjust the default binary name/flags to the actual fork when known — implementer checks `which`/`PATH` in the worker image before finalizing the literal).

Register in `app/activities/__init__.py`: import, append to `REGISTERED_ACTIVITIES`, add to `__all__` (same 3 spots as `extract_pdf_text`). Workers pick it up automatically (`app/workers.py` uses `REGISTERED_ACTIVITIES`).

Deployment one-liner: the worker image (`Dockerfile` / `charts/orcha`) must ship the gmx fork binary; add an install step for it.

### 4. Order / dependencies

1 → 2 → 3 are sequential (2 imports the activity from 3). Step 3's module is independent of 1–2 and can be written first. Existing suite must pass after each step.

## Critical files & anchors

- `app/workflows/extract_metadata_workflow.py:35-51,67-84` — params model + the activity-1 call site being fanned out; the only nontrivial logic change.
- `app/activities/extract_pdf_content.py` — structure to mirror for the new tpr activity (retry policy, `http_verify`, ApplicationError types).
- `app/activities/__init__.py` — 3-place activity registration.
- `app/config.py:59-61` — Settings field placement.
- `tests/test_auth.py:454-458` — the one stored-params assertion that gains `"files": None`.

## Verification

1. `cd /home/debian/orcha && uv run pytest tests/ -x` — green after edits.
2. Params gate (new behavior, no LLM needed):
   ```bash
   cd /home/debian/orcha && uv run python - <<'EOF'
   from app.workflows.extract_metadata_workflow import ExtractMetadataParams as P
   P.model_validate({"files": {"run.x.tpr": "http://h/a.tpr", "paper.pdf": "http://h/b.pdf"}})
   P.model_validate({"url": "http://h/a.pdf"})
   for bad in ({}, {"url": "http://h/a.pdf", "files": {"a": "http://h/b"}}, {"files": {}}):
       try: P.model_validate(bad); raise SystemExit(f"accepted {bad}")
       except Exception: pass
   print("ok")
   EOF
   ```
   Expected `ok`.
3. End-to-end single workflow, mocked LLM/vocab: throwaway pytest-style script (delete after) or:
   - `uv run orcha run` (dev stack, auth off), then POST
     `{"workflow_type":"extract_metadata","params":{"files":{"paper.pdf":"<url of a local pdf>","run.tpr":"<url of a fixture .tpr>"}}}` to `http://localhost:8000/workflows/` with a dev token (`uv run orcha token` per README).
   - Expected: 200; `GET /workflows/{public_id}` eventually `status: "success"` and `result.suggestions` a flat array (JS contract `workflow?.result?.suggestions` unchanged). With `GMX_DUMP_COMMAND` pointed at a stub script that prints JSON to stdout, the tpr branch is provable without the real fork.

## Assumptions & contingencies

- Caller keeps `workflow_type: "extract_metadata"` for `{files}` (verified in views.py:123-142). If a dedicated type is ever preferred, that is a views.py edit — out of scope here; nothing in this plan blocks it.
- `.tpr` → tpr activity, everything else → PDF path, keyed on basename suffix (caller's `VALID_EXTENSIONS` is `["pdf","tpr"]`). If the bundle later gains `.gro`/`.mdp`, add suffix branches in `_extract_one` only.
- One merged LLM call over concatenated per-file text (bundle = one record), not N parallel LLM calls + suggestion merging — dedupe/conflict policy across files would be invented complexity; single pass matches "single array" requirement.
- `gmx_dump_command` default argv is a placeholder until the fork's real binary name/flags are confirmed at implementation time; everything else is argv-agnostic.
