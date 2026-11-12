from __future__ import annotations

import importlib.util
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_backend_module(env_overrides: dict[str, str]):
    app_path = ROOT / "backend" / "app.py"
    if not app_path.exists():
        raise AssertionError(f"Missing backend app at {app_path}")

    spec = importlib.util.spec_from_file_location("cdb_backend_app", app_path)
    assert spec is not None and spec.loader is not None

    with mock.patch.dict(os.environ, env_overrides, clear=False):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cdb_backend_app"] = mod
        spec.loader.exec_module(mod)
        return mod


class TestLayoutAndFrontend(unittest.TestCase):
    def test_required_files_exist(self):
        required = [
            "db/schema.sql",
            "backend/Dockerfile",
            "backend/requirements.txt",
            "backend/app.py",
            "frontend/Dockerfile",
            "frontend/nginx.conf",
            "frontend/99-pod-info.sh",
            "frontend/static/index.html",
            "frontend/static/login.html",
            "frontend/static/register.html",
            "frontend/static/app.js",
            "frontend/static/app.css",
        ]
        missing = [rel for rel in required if not (ROOT / rel).is_file()]
        self.assertEqual(missing, [], f"Missing files: {missing}")

    def test_schema_has_users_and_tasks(self):
        sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE users", sql)
        self.assertIn("CREATE TABLE tasks", sql)

    def test_nginx_proxies_api_and_serves_static_routes(self):
        conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("location /api/", conf)
        self.assertIn("proxy_pass http://backend-svc:8000", conf)
        self.assertIn("location = /healthz", conf)
        self.assertIn("try_files", conf)
        self.assertIn("log_format fe_web", conf)
        self.assertIn("log_format fe_api", conf)
        self.assertIn("upstream_http_x_backend_node", conf)
        self.assertIn("upstream_http_x_backend_pod", conf)
        self.assertIn("X-Frontend-Node", conf)
        self.assertIn("X-Frontend-Pod", conf)
        self.assertIn("/etc/nginx/pod-meta.inc", conf)

    def test_frontend_dockerfile_uses_version_build_arg(self):
        df = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG APP_VERSION=v1", df)
        self.assertIn("ENV APP_VERSION=${APP_VERSION}", df)
        self.assertIn("99-pod-info.sh", df)
        self.assertIn("/etc/nginx/pod-meta.inc", df)
        self.assertNotIn("latest", df.lower().split("\n")[0])

    def test_backend_dockerfile_avoids_duplicate_gunicorn_access_log(self):
        df = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("--access-logfile", df)
        self.assertIn("--error-logfile", df)

    def test_frontend_pod_info_script_emits_version_and_pod(self):
        sh = (ROOT / "frontend" / "99-pod-info.sh").read_text(encoding="utf-8")
        self.assertIn("frontend-info.json", sh)
        self.assertIn("pod-meta.inc", sh)
        self.assertIn("APP_VERSION", sh)
        self.assertIn("NODE_NAME", sh)
        self.assertIn("HOSTNAME", sh)

    def test_frontend_html_and_js_have_version_and_pod_hooks(self):
        index_html = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
        login_html = (ROOT / "frontend" / "static" / "login.html").read_text(encoding="utf-8")
        register_html = (ROOT / "frontend" / "static" / "register.html").read_text(encoding="utf-8")
        js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="board"', index_html)
        self.assertIn('id="login-form"', login_html)
        self.assertIn('id="register-form"', register_html)

        for html in (index_html, login_html, register_html):
            self.assertIn("cluster-meta", html)

        self.assertIn("/api/health", js)
        self.assertIn("/frontend-info.json", js)


class TestBackendApp(unittest.TestCase):
    def test_cookie_secure_false_for_http_nodeport(self):
        mod = load_backend_module(
            {
                "APP_SECRET_KEY": "unit-test-secret",
                "DB_HOST": "127.0.0.1",
                "DB_NAME": "studydb",
                "DB_USER": "app_user",
                "DB_PASSWORD": "secret",
                "DB_SSLMODE": "disable",
                "COOKIE_SECURE": "false",
                "APP_VERSION": "v1",
            }
        )
        self.addCleanup(lambda: mod.pool.close(timeout=0.5))
        self.assertFalse(mod.app.config["SESSION_COOKIE_SECURE"])

    def test_cookie_secure_true_by_flag_and_livez_health(self):
        mod = load_backend_module(
            {
                "APP_SECRET_KEY": "unit-test-secret",
                "DB_HOST": "127.0.0.1",
                "DB_NAME": "studydb",
                "DB_USER": "app_user",
                "DB_PASSWORD": "secret",
                "DB_SSLMODE": "disable",
                "COOKIE_SECURE": "true",
                "APP_VERSION": "v2",
            }
        )
        self.addCleanup(lambda: mod.pool.close(timeout=0.5))
        self.assertTrue(mod.app.config["SESSION_COOKIE_SECURE"])

        client = mod.app.test_client()
        live = client.get("/livez")
        self.assertEqual(live.status_code, 200)
        live_data = live.get_json()
        self.assertEqual(live_data["status"], "alive")
        self.assertEqual(live_data["version"], "v2")
        self.assertEqual(live_data["pod"], socket.gethostname())

        with mock.patch.object(
            mod,
            "q_all",
            return_value=[{"db_user": "app_user", "database": "studydb", "backend_addr": "10.0.0.5"}],
        ):
            ok_resp = client.get("/api/health")
        self.assertEqual(ok_resp.status_code, 200)
        ok_data = ok_resp.get_json()
        self.assertTrue(ok_data["ok"])
        self.assertEqual(ok_data["status"], "ok")
        self.assertEqual(ok_data["db_user"], "app_user")
        self.assertEqual(ok_data["version"], "v2")
        self.assertEqual(ok_data["pod"], socket.gethostname())

        with mock.patch.object(mod, "q_all", side_effect=mod.OperationalError("down")):
            err_resp = client.get("/api/health")
        self.assertEqual(err_resp.status_code, 503)
        err_data = err_resp.get_json()
        self.assertFalse(err_data["ok"])
        self.assertEqual(err_data["code"], "db_unavailable")
        self.assertEqual(err_data["version"], "v2")
        self.assertEqual(err_data["pod"], socket.gethostname())
        self.assertEqual(err_data["node"], "local")

    def test_custom_logging_filters_kube_probe_and_traces_frontend_hop(self):
        import io
        import logging

        mod = load_backend_module(
            {
                "APP_SECRET_KEY": "unit-test-secret",
                "DB_HOST": "127.0.0.1",
                "DB_NAME": "studydb",
                "DB_USER": "app_user",
                "DB_PASSWORD": "secret",
                "DB_SSLMODE": "disable",
                "COOKIE_SECURE": "false",
                "APP_VERSION": "v1",
                "NODE_NAME": "worker1",
                "POD_NAME": "cdb-backend-test",
            }
        )
        self.addCleanup(lambda: mod.pool.close(timeout=0.5))

        buf = io.StringIO()
        capture = logging.StreamHandler(buf)
        base_handler = mod.app.logger.handlers[0]
        capture.setFormatter(base_handler.formatter)
        for flt in base_handler.filters:
            capture.addFilter(flt)
        mod.app.logger.addHandler(capture)
        self.addCleanup(lambda: mod.app.logger.removeHandler(capture))

        client = mod.app.test_client()
        with mock.patch.object(
            mod,
            "q_all",
            return_value=[{"db_user": "app_user", "database": "studydb", "backend_addr": "10.0.0.5"}],
        ):
            r_probe = client.get("/api/health", headers={"User-Agent": "kube-probe/1.35"})
            self.assertEqual(r_probe.status_code, 200)
            self.assertEqual(buf.getvalue(), "")

            r_curl = client.get(
                "/api/health",
                headers={
                    "User-Agent": "curl/8.5.0",
                    "X-Frontend-Node": "worker2; bad=1",
                    "X-Frontend-Pod": "cdb-frontend-abc",
                },
            )
            self.assertEqual(r_curl.status_code, 200)
            self.assertEqual(r_curl.headers.get("X-Backend-Node"), "worker1")
            self.assertEqual(r_curl.headers.get("X-Backend-Pod"), "cdb-backend-test")
            self.assertEqual(r_curl.get_json()["node"], "worker1")
            self.assertEqual(mod.clean_meta("worker2\nFAKE"), "worker2FAKE")

        with mock.patch.object(mod, "q_all", side_effect=mod.OperationalError("down")):
            r_fail = client.get("/api/health", headers={"User-Agent": "kube-probe/1.35"})
            self.assertEqual(r_fail.status_code, 503)

        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("[BE node=worker1 pod=cdb-backend-test ver=v1]", lines[0])
        self.assertIn("GET /api/health -> 200", lines[0])
        self.assertIn("via=FE:worker2bad1/cdb-frontend-abc", lines[0])
        self.assertIn("GET /api/health -> 503", lines[1])


if __name__ == "__main__":
    unittest.main()
