# Generated from operations.graphql. Run pnpm sdk:generate in the GraphQL package.
from typing import Literal, TypeAlias

TrackingOperation: TypeAlias = Literal[
    "SDKIdentity",
    "SDKCreateProject",
    "SDKEnsureExperiment",
    "SDKCreateRun",
    "SDKResume",
    "SDKBind",
    "SDKHeartbeat",
    "SDKIngest",
    "SDKComplete",
]

OPERATIONS: dict[TrackingOperation, tuple[str, str, bool]] = {
    "SDKIdentity": (
        "cliIdentity",
        "query SDKIdentity{cliIdentity{organizationId}}",
        False,
    ),
    "SDKCreateProject": (
        "createProject",
        "mutation SDKCreateProject($input:CreateProjectInput!){createProject(input:$input){id name organizationId}}",
        True,
    ),
    "SDKEnsureExperiment": (
        "ensureTrackingExperiment",
        "mutation SDKEnsureExperiment($input:CreateTrackingExperimentInput!){ensureTrackingExperiment(input:$input){id}}",
        True,
    ),
    "SDKCreateRun": (
        "createTrackingRun",
        "mutation SDKCreateRun($input:TrackingRunCreateInput!){createTrackingRun(input:$input){id projectId experimentId url status finalizationState}}",
        True,
    ),
    "SDKResume": (
        "resumeTrackingRun",
        "mutation SDKResume($input:TrackingWriterInput!){resumeTrackingRun(input:$input){id projectId experimentId url status finalizationState}}",
        True,
    ),
    "SDKBind": (
        "bindManagedTrackingRun",
        "mutation SDKBind{bindManagedTrackingRun{id projectId experimentId url status finalizationState}}",
        False,
    ),
    "SDKHeartbeat": (
        "heartbeatTrackingRun",
        "mutation SDKHeartbeat($input:TrackingWriterInput!){heartbeatTrackingRun(input:$input){id projectId experimentId url status finalizationState}}",
        True,
    ),
    "SDKIngest": (
        "ingestRunEvents",
        "mutation SDKIngest($input:IngestRunEventsInput!){ingestRunEvents(input:$input){requestId accepted duplicates}}",
        True,
    ),
    "SDKComplete": (
        "completeTrackingRun",
        "mutation SDKComplete($input:TrackingCompleteInput!){completeTrackingRun(input:$input){id projectId experimentId url status finalizationState}}",
        True,
    ),
}
