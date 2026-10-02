# SPDX-FileCopyrightText: 2026 CERN.
# SPDX-License-Identifier: MIT

from datetime import timedelta

from pydantic import Field, HttpUrl
from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
from temporalio import workflow

from app.activities.extract_gromacs_metadata import (
    EXTRACT_GROMACS_RETRY_POLICY,
    ExtractGromacsMetadataRequest,
    extract_gromacs_metadata,
)
from app.activities.update_workflow import (
    UPDATE_WORKFLOW_RETRY_POLICY,
    WorkflowUpdateRequest,
    update_workflow,
)
from app.database.models import WorkflowStatus
from app.workflows.specs import WorkflowContext, WorkflowParams


class ExtractMetadataMultiParams(WorkflowParams):
    """User-provided params for the extract_metadata_multi workflow."""

    files: dict[str, HttpUrl] = Field(min_length=1)


@workflow.defn
class ExtractMetadataMulti(PydanticAIWorkflow):
    """Workflow that runs gmxextract over a bundle of MD simulation files."""

    @workflow.run
    async def run(
        self,
        context: WorkflowContext,
        params: ExtractMetadataMultiParams,
    ) -> dict:
        """Execute the gmxextract + mapping workflow (no LLM)."""
        try:
            await workflow.execute_activity(
                update_workflow,
                WorkflowUpdateRequest(
                    public_id=context.workflow_id,
                    tenant_id=context.tenant_id,
                    start_time=workflow.now(),
                ),
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=UPDATE_WORKFLOW_RETRY_POLICY,
            )

            # Activity 1: Download the bundle, run gmxextract, map to schema
            response = await workflow.execute_activity(
                extract_gromacs_metadata,
                ExtractGromacsMetadataRequest(
                    files={
                        name: str(url) for name, url in params.files.items()
                    },
                ),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=EXTRACT_GROMACS_RETRY_POLICY,
            )
        except Exception:
            await workflow.execute_activity(
                update_workflow,
                WorkflowUpdateRequest(
                    public_id=context.workflow_id,
                    tenant_id=context.tenant_id,
                    status=WorkflowStatus.ERROR,
                    result=None,
                    end_time=workflow.now(),
                ),
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=UPDATE_WORKFLOW_RETRY_POLICY,
            )
            raise

        result = response.model_dump()
        await workflow.execute_activity(
            update_workflow,
            WorkflowUpdateRequest(
                public_id=context.workflow_id,
                tenant_id=context.tenant_id,
                status=WorkflowStatus.SUCCESS,
                result=result,
                end_time=workflow.now(),
            ),
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=UPDATE_WORKFLOW_RETRY_POLICY,
        )

        return result
