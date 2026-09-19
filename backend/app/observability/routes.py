"""Runtime introspection endpoint (PORT-005).

One read, deliberately small: which model is configured, and whether the deployment
is running against the deterministic fake. Every page needs it — a viewer has to be
able to tell a real model's answer from a scripted one before drawing any conclusion
from what is on screen.

It requires authentication (it is a fact about the deployment, not a public one) but
no role or resource scope: it is not candidate data, and gating it behind a job
assignment would make the banner disappear on exactly the pages that need it most.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.settings import Settings
from backend.app.observability.model_mode import describe_model_mode
from backend.app.observability.schemas import ModelModeOut

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])

ActorDep = Annotated[Actor, Depends(get_current_actor)]


@router.get("/model-mode", response_model=ModelModeOut)
async def get_model_mode(request: Request, actor: ActorDep) -> ModelModeOut:
    """State which model mode this process is running in."""
    settings: Settings = request.app.state.settings
    return ModelModeOut.from_view(describe_model_mode(settings))
