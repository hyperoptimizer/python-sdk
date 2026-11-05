import os
import unittest
from unittest.mock import patch

from hyperoptimizer.projects import create_project


class ProjectTests(unittest.TestCase):
    def test_wrong_workspace_is_rejected_before_creation(self):
        with (
            patch.dict(os.environ, {"HYPEROPTIMIZER_API_TOKEN": "ho_test"}, clear=True),
            patch(
                "hyperoptimizer.projects.Transport.request",
                return_value={"organizationId": "org_actual"},
            ) as request,
        ):
            with self.assertRaises(ValueError):
                create_project(name="Must not create", workspace="org_other")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], "SDKIdentity")

    def test_project_write_does_not_require_read_scope(self):
        with (
            patch.dict(os.environ, {"HYPEROPTIMIZER_API_TOKEN": "ho_test"}, clear=True),
            patch(
                "hyperoptimizer.projects.Transport.request",
                return_value={"id": "project", "name": "New"},
            ) as request,
        ):
            project = create_project(name="New")
            self.assertEqual(project.name, "New")
            self.assertEqual(request.call_args.args[0], "SDKCreateProject")
