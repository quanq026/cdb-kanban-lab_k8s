from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import demo_menu


class TestDemoMenu(unittest.TestCase):
    def test_build_kubectl_invocation_local(self):
        cfg = demo_menu.RuntimeConfig(use_local_kubectl=True)
        with mock.patch("shutil.which", return_value="/usr/bin/kubectl"):
            cmd = demo_menu.build_kubectl_invocation(cfg, ["get", "nodes", "-o", "wide"])
        self.assertEqual(cmd, ["/usr/bin/kubectl", "get", "nodes", "-o", "wide"])

    def test_build_kubectl_invocation_ssh_with_key(self):
        key = Path("/tmp/test_key")
        cfg = demo_menu.RuntimeConfig(
            cp_host="52.184.98.155",
            ssh_user="azureuser",
            ssh_key=key,
            use_local_kubectl=False,
        )
        with mock.patch("shutil.which", return_value="ssh"):
            cmd = demo_menu.build_kubectl_invocation(cfg, ["get", "pods", "-n", "cdb-kanban"])
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("-i", cmd)
        self.assertIn(str(key), cmd)
        self.assertIn("azureuser@52.184.98.155", cmd)
        self.assertEqual(cmd[-1], "kubectl get pods -n cdb-kanban")

    def test_scale_rejects_out_of_range_and_runs_valid(self):
        cfg = demo_menu.RuntimeConfig(namespace="cdb-kanban", use_local_kubectl=True)

        with (
            mock.patch("builtins.input", side_effect=["1", "99"]),
            mock.patch.object(demo_menu, "run_kubectl") as run_mock,
            redirect_stdout(io.StringIO()),
        ):
            demo_menu.action_scale(cfg)
        run_mock.assert_not_called()

        with (
            mock.patch("builtins.input", side_effect=["1", "4"]),
            mock.patch.object(demo_menu, "run_kubectl", return_value=0) as run_mock,
            redirect_stdout(io.StringIO()),
        ):
            demo_menu.action_scale(cfg)

        calls = [c.args[1] for c in run_mock.call_args_list]
        self.assertEqual(
            calls,
            [
                ["scale", "deployment", "cdb-frontend", "-n", "cdb-kanban", "--replicas=4"],
                ["rollout", "status", "deployment/cdb-frontend", "-n", "cdb-kanban", "--timeout=90s"],
                ["get", "pods", "-n", "cdb-kanban", "-l", "app=cdb-frontend", "-o", "wide"],
            ],
        )

    def test_update_version_and_rollback(self):
        cfg = demo_menu.RuntimeConfig(namespace="cdb-kanban", image_prefix="quanq026", use_local_kubectl=True)

        with (
            mock.patch("builtins.input", side_effect=["latest"]),
            mock.patch.object(demo_menu, "run_kubectl") as run_mock,
            redirect_stdout(io.StringIO()),
        ):
            demo_menu.action_update_version(cfg)
        run_mock.assert_not_called()

        with (
            mock.patch("builtins.input", side_effect=["v2", "1"]),
            mock.patch.object(demo_menu, "run_kubectl", return_value=0) as run_mock,
            mock.patch.object(demo_menu, "action_check_http") as http_mock,
            redirect_stdout(io.StringIO()),
        ):
            demo_menu.action_update_version(cfg)

        calls = [c.args[1] for c in run_mock.call_args_list]
        self.assertEqual(
            calls[0],
            ["set", "image", "deployment/cdb-backend", "backend=quanq026/cdb-kanban-backend:v2", "-n", "cdb-kanban"],
        )
        self.assertEqual(
            calls[1],
            ["set", "image", "deployment/cdb-frontend", "frontend=quanq026/cdb-kanban-frontend:v2", "-n", "cdb-kanban"],
        )
        http_mock.assert_called_once_with(cfg)

        with (
            mock.patch("builtins.input", side_effect=["2"]),
            mock.patch.object(demo_menu, "run_kubectl", return_value=0) as undo_mock,
            mock.patch.object(demo_menu, "action_check_http"),
            redirect_stdout(io.StringIO()),
        ):
            demo_menu.action_rollback(cfg)

        undo_calls = [c.args[1] for c in undo_mock.call_args_list]
        self.assertEqual(
            undo_calls[0],
            ["rollout", "undo", "deployment/cdb-backend", "-n", "cdb-kanban"],
        )

    def test_apply_yaml_streams_stdin_in_ssh_mode(self):
        cfg = demo_menu.RuntimeConfig(use_local_kubectl=False)
        with (
            mock.patch.object(demo_menu, "run_kubectl", return_value=0) as run_mock,
        ):
            demo_menu.action_apply_yaml(cfg)

        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[1], ["apply", "-f", "-"])
        stdin_payload = run_mock.call_args.kwargs["stdin_text"]
        self.assertIn("kind: Namespace", stdin_payload)
        self.assertIn("nodePort: 30080", stdin_payload)

    def test_check_http_prints_worker_status(self):
        cfg = demo_menu.RuntimeConfig(nodeport=30080)
        buf = io.StringIO()
        with (
            mock.patch.object(demo_menu, "http_get_text", return_value=(200, '{"version":"v1","ok":true}')),
            redirect_stdout(buf),
        ):
            demo_menu.action_check_http(cfg)
        out = buf.getvalue()
        self.assertIn("worker1", out)
        self.assertIn("worker2", out)
        self.assertIn("200", out)


if __name__ == "__main__":
    unittest.main()
