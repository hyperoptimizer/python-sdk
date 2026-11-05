from __future__ import annotations

import os
from dataclasses import dataclass

from ._transport import Transport


@dataclass(frozen=True)
class Project:
    id: str
    name: str


def create_project(
    *,
    name: str,
    description: str | None = None,
    workspace: str | None = None,
    api_url: str | None = None,
    api_token: str | None = None,
) -> Project:
    """Create in the token's organization; requires projects:write, no read scope."""
    if os.environ.get("HYPEROPTIMIZER_TRACKING_URL"):
        raise ValueError("Managed trial credentials cannot create projects")
    if not name.strip() or len(name) > 255:
        raise ValueError("Project name must have 1–255 characters")
    transport = Transport(api_url, api_token)
    if workspace is not None:
        identity = transport.request("SDKIdentity", {})
        if identity.get("organizationId") != workspace:
            raise ValueError("Workspace must match the token's organization")
    body: dict[str, object] = {"name": name, "description": description}
    result = transport.request("SDKCreateProject", body, retry=False)
    return Project(str(result["id"]), str(result["name"]))
