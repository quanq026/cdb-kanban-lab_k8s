from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
K8S_DIR = ROOT / "k8s"


class TestK8sManifests(unittest.TestCase):
    def test_manifest_files_exist(self):
        expected = [
            "00-namespace.yaml",
            "01-postgres.yaml",
            "02-backend.yaml",
            "03-frontend.yaml",
        ]
        missing = [name for name in expected if not (K8S_DIR / name).is_file()]
        self.assertEqual(missing, [], f"Missing manifests: {missing}")

    def test_no_latest_image_tags_anywhere(self):
        files = sorted(K8S_DIR.glob("*.yaml"))
        self.assertTrue(files, "Expected k8s/*.yaml files")
        for path in files:
            text = path.read_text(encoding="utf-8")
            images = re.findall(r"^\s*image:\s*(\S+)", text, flags=re.MULTILINE)
            for img in images:
                self.assertNotIn(":latest", img, f"{path.name} uses forbidden :latest tag in {img}")
                self.assertIn(":", img, f"{path.name} image {img} must pin an explicit tag")

    def test_namespace_and_postgres_manifest(self):
        ns = (K8S_DIR / "00-namespace.yaml").read_text(encoding="utf-8")
        self.assertIn("kind: Namespace", ns)
        self.assertIn("name: cdb-kanban", ns)

        pg = (K8S_DIR / "01-postgres.yaml").read_text(encoding="utf-8")
        self.assertIn("namespace: cdb-kanban", pg)
        self.assertIn("kind: Secret", pg)
        self.assertIn("kind: ConfigMap", pg)
        self.assertIn("postgres:17-alpine", pg)
        self.assertIn("CREATE TABLE users", pg)
        self.assertIn("CREATE TABLE tasks", pg)
        self.assertIn("name: postgres-svc", pg)
        self.assertIn("port: 5432", pg)

    def test_backend_manifest_ha_and_cookie_config(self):
        be = (K8S_DIR / "02-backend.yaml").read_text(encoding="utf-8")
        self.assertIn("namespace: cdb-kanban", be)
        self.assertIn("replicas: 2", be)
        self.assertIn("topologySpreadConstraints:", be)
        self.assertIn("kubernetes.io/hostname", be)
        self.assertIn("quanq026/cdb-kanban-backend:v1", be)
        self.assertIn('COOKIE_SECURE: "false"', be)
        self.assertIn('DB_HOST: "postgres-svc"', be)
        self.assertIn("readinessProbe:", be)
        self.assertIn("livenessProbe:", be)
        self.assertIn("path: /api/health", be)
        self.assertIn("path: /livez", be)
        self.assertIn("name: backend-svc", be)
        self.assertIn("port: 8000", be)

    def test_frontend_manifest_ha_and_nodeport(self):
        fe = (K8S_DIR / "03-frontend.yaml").read_text(encoding="utf-8")
        self.assertIn("namespace: cdb-kanban", fe)
        self.assertIn("replicas: 2", fe)
        self.assertIn("topologySpreadConstraints:", fe)
        self.assertIn("kubernetes.io/hostname", fe)
        self.assertIn("quanq026/cdb-kanban-frontend:v1", fe)
        self.assertIn("type: NodePort", fe)
        self.assertIn("nodePort: 30080", fe)
        self.assertIn("readinessProbe:", fe)
        self.assertIn("livenessProbe:", fe)


if __name__ == "__main__":
    unittest.main()
