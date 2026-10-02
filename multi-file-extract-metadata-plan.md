# Separate extract_metadata_multi workflow in Orcha

## Context

The Invenio caller (`/home/debian/biosimtest/oarepo-orcha/oarepo_orcha/views.py`, edited only in Step 6 — otherwise ground truth) posts, for `mode == "allfiles"`:

```json
{"workflow_type": "extract_metadata", "user_id": "...", "params": {"files": {"<basename>": "<signed url>", ...}}}
```

and for single-file mode `params: {"url": "<signed url>"}`. The `{files: ...}` body currently 422s at `app/routers/workflows.py:69-72` because `ExtractMetadataParams` (`app/workflows/extract_metadata_workflow.py:35`) is `url: HttpUrl` with inherited `extra="forbid"` (`app/workflows/specs.py:22-25`).

The files are one bundle describing one MD-simulation record (PDF + GROMACS `.tpr`; the `.tpr` holds the simulation metadata and is rendered to text by a custom fork of `gmx dump` that Orcha shells out to inside the worker). The caller's JS consumes `workflow?.result?.suggestions` as one flat array (`oarepo_orcha/theme/assets/semantic-ui/js/oarepo_orcha/deposit/Provider.js:247`) → the new workflow extracts text per file, then runs ONE LLM pass + ONE resolve pass over the combined text, producing the same flat `MetadataSuggestions`.

Chosen shape: a **separate workflow type `extract_metadata_multi`**, leaving `extract_metadata` untouched. No large architectural change is needed: dispatch (`app/routers/workflows.py`, `app/workflows/registry.py`, `app/workflows/specs.py`, `app/workers.py`), DB models, SSE stream, and auth are all params-shape agnostic — they validate `params` against `spec.params_model` and persist/forward JSON. "New workflow" = one new module + one registry entry, copying the existing file patterns. No migrations.

## Approach

All steps in `/home/debian/orcha` except Step 6 (caller repo). Dependencies: Step 2 imports Step 1's activity; Step 3 imports Step 2; Step 5 tests Steps 2–3; Step 4 is a guardrail (no edit); Step 6 is independent. Existing suite green after each step.

### 1. New `.tpr` text activity (thin)

New file `app/activities/extract_tpr_content.py`, mirroring the structure of `app/activities/extract_pdf_content.py` (httpx download with `http_verify` from `app/activities/utils.py`; same non-retryable `ApplicationError(type="HostNotAllowed")` on disallowed host):

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

Body: GET `request.url` (`follow_redirects=True`, `raise_for_status()`) → write bytes to `tempfile.NamedTemporaryFile(suffix=".tpr")` → shell out:

```python
proc = await asyncio.create_subprocess_exec(
    *get_settings().gmx_dump_command,
    tmp.name,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
```

- Non-zero exit → `ApplicationError(f"gmx dump failed: {stderr.decode(errors='replace')[:2000]}", type="TprDumpFailed", non_retryable=True)`.
- Empty stdout → `ExtractTprContentResponse(text="")`; the downstream `MIN_TEXT_CHARS` gate in `extract_metadata_with_llm` (`app/activities/extract_metadata.py:102-104`) already handles near-empty text without fabrication.
- Never pass the URL or caller basenames to argv: argv is settings-owned only; file bytes reach the fork through the temp path. No injection surface.

`app/config.py` `Settings`: new `# GROMACS` section after `# Security` (after `http_allowlist`, line ~61):

```python
# argv of the gmx-dump fork used to render .tpr files as text; env override
# GMX_DUMP_COMMAND is a JSON list.
gmx_dump_command: list[str] = ["gmx-dump-fork", "-s"]
```

Confirm the fork's real binary name/flags (`command -v` in the worker image) before finalizing the default literal; the code is argv-agnostic.

Register in `app/activities/__init__.py` at the same 3 places as `extract_pdf_text`: import line, `REGISTERED_ACTIVITIES`, `__all__`. `app/workers.py` consumes `REGISTERED_ACTIVITIES` directly — no worker edit.

### 2. New workflow module `extract_metadata_multi`

New file `app/workflows/extract_metadata_multi_workflow.py`. Copy `app/workflows/extract_metadata_workflow.py`; the status bookkeeping (start/ERROR/SUCCESS `update_workflow` blocks, lines ~54-64, 93-120) and the downstream activities stay byte-identical. Changes:

```python
class ExtractMetadataMultiParams(WorkflowParams):
    """User-provided params for the extract_metadata_multi workflow."""

    files: dict[str, HttpUrl] = Field(min_length=1)
    extractor: str = "pdfplumber"
    pages: list[int] | None = Field(default_factory=lambda: [1, 2])


@workflow.defn
class ExtractMetadataMulti(PydanticAIWorkflow):
    """Workflow that extracts metadata from a bundle of related files."""

    @workflow.run
    async def run(
        self,
        context: WorkflowContext,
        params: ExtractMetadataMultiParams,
    ) -> MetadataSuggestions: ...
```

- `extra="forbid"` is inherited; empty `files: {}` is rejected by `min_length=1`. The router already turns any `ValidationError` into 422 (`app/routers/workflows.py:69-72`) — no router change.
- Replace the single `extract_pdf_text` call (sibling file lines 67–76) with per-file fan-out:

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
            ExtractPdfContentRequest(
                url=str(url), extractor=params.extractor, pages=params.pages
            ),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=EXTRACT_PDF_TEXT_RETRY_POLICY,
        )
    return f"# File: {name}\n{res.text}"

texts = await asyncio.gather(
    *(_extract_one(n, u) for n, u in sorted(params.files.items(), key=lambda kv: kv[0]))
)
content_text = "\n\n".join(texts)
```

Then feed the unchanged downstream chain: `extract_metadata_with_llm` with `args=[ExtractMetadataRequest(text=content_text), context]`, then `resolve_metadata_suggestions` with `ResolveMetadataRequest(metadata=metadata)` → `MetadataSuggestions`. `import asyncio` at module top. `sorted(...)` gives deterministic `asyncio.gather` order — required for Temporal replay determinism.
- Any file failing fails the whole run through the copied `except Exception:` ERROR block: bundle semantics — metadata from a half-readable record must not silently succeed.
- Non-`.tpr` extensions go down the PDF path and fail there with the extractor's own error; the caller already filters to `VALID_EXTENSIONS = ["pdf", "tpr"]` (views.py:29).

### 3. Register the workflow type

`app/workflows/registry.py`: import `ExtractMetadataMulti`, `ExtractMetadataMultiParams`; add to `WORKFLOW_REGISTRY`, copying the existing `extract_metadata` entry pattern (the registry is explicit by design — do NOT add discovery magic):

```python
"extract_metadata_multi": WorkflowSpec(
    workflow_cls=ExtractMetadataMulti,
    params_model=ExtractMetadataMultiParams,
    task_queue=DEFAULT_TASK_QUEUE,
    id_prefix="extract-metadata-multi",
),
```

`get_registered_types()`, task-queue helpers, and the worker's `get_specs_for_task_queue` pick it up automatically.

### 4. Existing single-file workflow: do NOT touch

`app/workflows/extract_metadata_workflow.py` and `ExtractMetadataParams` stay exactly as-is (`url: HttpUrl`, `extra="forbid"`). Do NOT relax it to accept `files`: the new type would be dead weight and `tests/test_auth.py:472` (`test_create_workflow_rejects_invalid_params`) pins a no-`url` POST as 422.

### 5. Tests

New `tests/test_extract_metadata_multi_params.py` (plain `model_validate`, sync, following `tests/test_extract_metadata.py`'s no-LLM style):
- `{"files": {"paper.pdf": "http://h/a.pdf", "run.tpr": "http://h/b.tpr"}}` validates; defaults `extractor == "pdfplumber"`, `pages == [1, 2]`, `len(files) == 2`.
- Rejected: `{}`, `{"files": {}}`, `{"url": "http://h/a.pdf"}` (unknown key), `{"files": {"a": "not-a-url"}}`.

New test in `tests/test_auth.py`, copying `test_create_workflow_stamps_tenant_id` (lines 428–469, mocker.AsyncMock temporal client): POST `workflow_type="extract_metadata_multi"` with a two-entry `files` dict → 200; stored `wf.params == {"files": {...both urls...}, "extractor": "pdfplumber", "pages": [1, 2]}`; `start_workflow` awaited with `ExtractMetadataMultiParams` whose `.files` holds both URLs.

### 6. Caller repo (one behavior swap, `/home/debian/biosimtest/oarepo-orcha/oarepo_orcha/views.py`)

In `get_workflow_stream_url`, the payload (lines 139–143) hardcodes `"workflow_type": "extract_metadata"`. Introduce a `workflow_type` local alongside `payload_params` in each branch: `"extract_metadata"` in the `fileKey` branch, `"extract_metadata_multi"` in the `mode == "allfiles"` branch; use it in the payload dict. Nothing else in views.py changes (`client.trigger_workflow` payload shape is unchanged). Outside Orcha's test suite — verify by reading the edited function.

## Critical files & anchors

- `app/workflows/extract_metadata_workflow.py:35-121` — template to copy; blocks that stay byte-identical (update_workflow bookkeeping, activities 2–3) vs. the one replaced (lines 67–76).
- `app/activities/extract_pdf_content.py` — structure to mirror for `extract_tpr_content.py` (retry policy, `http_verify`, ApplicationError types).
- `app/workflows/registry.py:19-32` — registry entry to copy.
- `app/activities/__init__.py` — 3-place activity registration.
- `tests/test_auth.py:428-469` — POST-asserting test pattern for Step 5.

## Verification

1. `cd /home/debian/orcha && uv run pytest tests/ -x` — full suite green; single-file tests unchanged.
2. Params gate (new behavior, no LLM):
   ```bash
   cd /home/debian/orcha && uv run python - <<'EOF'
   from app.workflows.extract_metadata_multi_workflow import ExtractMetadataMultiParams as P
   p = P.model_validate({"files": {"run.x.tpr": "http://h/a.tpr", "paper.pdf": "http://h/b.pdf"}})
   assert p.extractor == "pdfplumber" and p.pages == [1, 2] and len(p.files) == 2
   for bad in ({}, {"files": {}}, {"url": "http://h/a.pdf"}, {"files": {"a": "not-a-url"}}):
       try: P.model_validate(bad); raise SystemExit(f"accepted {bad}")
       except Exception: pass
   print("ok")
   EOF
   ```
   Expected `ok`.
3. Registry visible: `uv run python -c "from app.workflows.registry import get_registered_types; print(get_registered_types())"` → includes `'extract_metadata_multi'`.
4. End-to-end, real dispatch, stubbed external deps: `uv run orcha run` (dev stack, auth off; needs `temporal` CLI). Point `GMX_DUMP_COMMAND` at a stub script that echoes fixture text to stdout; serve a small real PDF and a fake `.tpr` via `python -m http.server`. POST `{"workflow_type":"extract_metadata_multi","params":{"files":{"paper.pdf":"http://127.0.0.1:PORT/paper.pdf","run.tpr":"http://127.0.0.1:PORT/run.tpr"}}}` to `http://localhost:8000/workflows/` (dev-mode token: `uv run orcha token`, README). Expected: 200; worker log shows both `extract_pdf_text` and `extract_tpr_text` executing; `GET /workflows/{public_id}` reaches `status: "success"` with `result.suggestions` a flat array (with a stubbed/unreachable LLM, reaching ERROR after both extraction activities ran still proves the fan-out; success requires a reachable LLM per `.env`). Then POST the same `{"files": ...}` body with `"workflow_type": "extract_metadata"` → 422 (single-file type still strict).

## Assumptions & contingencies

- `workflow_type` literal is `extract_metadata_multi` (user-chosen); `id_prefix` `extract-metadata-multi`. If views.py (Step 6) can't ship at the same time, Orcha-side steps still land; old allfiles calls keep 422 until that swap ships.
- `.tpr` routing keys on basename suffix (caller filters `["pdf","tpr"]`). New bundle formats later → add suffix branches in `_extract_one` only.
- One merged LLM pass over concatenated per-file text (one bundle = one record = one flat suggestions array), not N LLM calls + merge: cross-file dedupe/conflict policy would be invented complexity and the JS consumer expects a single array.
- `gmx_dump_command` default `["gmx-dump-fork", "-s"]` is a placeholder until the fork's real binary/flags are confirmed; the worker image (`Dockerfile`, `charts/orcha`) must ship the binary — add its install step once the package/path is known. If the fork takes a flag instead of a positional path, put the path after the argv list as shown (argv-first convention) and adjust only the setting literal.


## Human clarifications

### Testing

End to end test:
```
curl 'https://biosimcz-test.biodata.ceitec.cz/uploads/zxk6n-xhf96/orcha' \
  -X POST \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0' \
  -H 'Accept: */*' \
  -H 'Accept-Language: en-US,en;q=0.9' \
  -H 'Accept-Encoding: gzip, deflate, br, zstd' \
  -H 'Referer: https://biosimcz-test.biodata.ceitec.cz/experiment/uploads/zxk6n-xhf96?tab=files' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://biosimcz-test.biodata.ceitec.cz' \
  -H 'Connection: keep-alive' \
  -H 'Cookie: session=713ba0574c45b276_6aafc103.s1a8pa40LX5AZaq8NfEdagkfJ5E' \
  -H 'Pragma: no-cache' \
  -H 'Cache-Control: no-cache' \
  --data-raw '{"mode":"allfiles"}'
```

Make sure Orcha is running - see README.

## Format

As for step #1, there is a program that reads tpr et al files, and returns JSON.
Try `python3.13 ~/gromacs-metadump/cli/gmxextract.py  --tpr ~/em.tpr --gmx_bin /home/debian/gromacs/build/bin/gmx --verbose`.

```
$ python3.13 ~/gromacs-metadump/cli/gmxextract.py  --tpr ~/em.tpr --gmx_bin /home/debian/gromacs/build/bin/gmx --verbose --help
usage: gmxextract.py [-h] [--tpr TPR] [--gro GRO] [--top TOP] [--opt OPT] [--format {json,yaml}] [--gmx_bin GMX_BIN] [--archive ARCHIVE] [--verbose]

options:
  -h, --help            show this help message and exit
  --tpr TPR             Tpr file from GROMACS with metadata. (Obligatory argument)
  --gro GRO             Gro file from GROMACS with metadata.
  --top TOP             Cpt file from GROMACS with metadata.
  --opt OPT             Optional metadata for extending metadata.
  --format {json,yaml}  Print extracted metadata as json or yaml formats.
  --gmx_bin GMX_BIN     Path to GROMACS binary used to parse the metadata. Default: gmx
  --archive ARCHIVE     Path to archive with files.
  --verbose             If true, verbose information is printed as script runs.
```

As for file selection, extract the basenames, and the choose most common basename.
Make sure every decision such as this is logged.
If possible, attach workflow logging to API result for provenance.
