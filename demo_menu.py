from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
K8S_DIR = ROOT_DIR / "k8s"

DEFAULT_CP_HOST = os.environ.get("K8S_SSH_HOST", "52.184.98.155")
DEFAULT_SSH_USER = os.environ.get("K8S_SSH_USER", "azureuser")
DEFAULT_NAMESPACE = os.environ.get("K8S_NAMESPACE", "cdb-kanban")
DEFAULT_RG = os.environ.get("AZURE_RG", "rg-k8s")
DEFAULT_NODEPORT = int(os.environ.get("K8S_NODEPORT", "30080"))
DEFAULT_IMAGE_PREFIX = os.environ.get("DOCKER_IMAGE_PREFIX", "quanq026")
WORKER_NODES = [
    ("worker1", os.environ.get("WORKER1_IP", "20.205.36.32")),
    ("worker2", os.environ.get("WORKER2_IP", "20.24.211.55")),
]


@dataclass(frozen=True)
class RuntimeConfig:
    cp_host: str = DEFAULT_CP_HOST
    ssh_user: str = DEFAULT_SSH_USER
    ssh_key: Path | None = None
    namespace: str = DEFAULT_NAMESPACE
    resource_group: str = DEFAULT_RG
    nodeport: int = DEFAULT_NODEPORT
    image_prefix: str = DEFAULT_IMAGE_PREFIX
    use_local_kubectl: bool = False


def resolve_ssh_key() -> Path | None:
    env_key = os.environ.get("K8S_SSH_KEY")
    if env_key:
        candidate = Path(env_key).expanduser()
        return candidate if candidate.is_file() else None
    default_key = Path.home() / ".ssh" / "id_rsa_azure"
    return default_key if default_key.is_file() else None


def detect_runtime_config() -> RuntimeConfig:
    force_ssh = os.environ.get("K8S_FORCE_SSH", "").strip().lower() in ("1", "true", "yes")
    has_local_kubectl = shutil.which("kubectl") is not None and not force_ssh
    return RuntimeConfig(
        ssh_key=resolve_ssh_key(),
        use_local_kubectl=has_local_kubectl,
    )


def build_kubectl_invocation(cfg: RuntimeConfig, k8s_args: list[str]) -> list[str]:
    if cfg.use_local_kubectl:
        kubectl_bin = shutil.which("kubectl") or "kubectl"
        return [kubectl_bin, *k8s_args]

    ssh_bin = shutil.which("ssh") or "ssh"
    remote_cmd = "kubectl " + " ".join(shlex.quote(arg) for arg in k8s_args)
    cmd = [ssh_bin, "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]
    if cfg.ssh_key is not None:
        cmd.extend(["-i", str(cfg.ssh_key)])
    cmd.append(f"{cfg.ssh_user}@{cfg.cp_host}")
    cmd.append(remote_cmd)
    return cmd


def run_kubectl(cfg: RuntimeConfig, k8s_args: list[str], stdin_text: str | None = None) -> int:
    display_cmd = "kubectl " + " ".join(k8s_args)
    target = "local" if cfg.use_local_kubectl else f"{cfg.ssh_user}@{cfg.cp_host}"
    print(f"\n[{target}]$ {display_cmd}", flush=True)
    cmd = build_kubectl_invocation(cfg, k8s_args)
    try:
        if stdin_text is not None:
            res = subprocess.run(cmd, input=stdin_text, text=True)
        else:
            stdin_mode = subprocess.DEVNULL if cfg.ssh_key is not None else None
            res = subprocess.run(cmd, stdin=stdin_mode, text=True)
        return res.returncode
    except FileNotFoundError as exc:
        print(f"Khong tim thay cong cu thuc thi: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nDa dung lenh.")
        return 130


def run_az(args: list[str]) -> int:
    az_bin = shutil.which("az")
    if not az_bin:
        print("Khong tim thay Azure CLI ('az') trong PATH.")
        return 1
    print(f"\n[local]$ az {' '.join(args)}", flush=True)
    try:
        res = subprocess.run([az_bin, *args], text=True)
        return res.returncode
    except KeyboardInterrupt:
        print("\nDa dung lenh.")
        return 130


def http_get_text(url: str, timeout: float = 5.0) -> tuple[int | None, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            return resp.status, body
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace").strip()
        return err.code, body
    except Exception as exc:
        return None, str(exc)


def ask_choice(prompt: str, default: str | None = None) -> str:
    raw = input(prompt).strip()
    if not raw and default is not None:
        return default
    return raw


def pause_after_action() -> None:
    try:
        input("\nNhan Enter de quay lai menu...")
    except EOFError:
        pass


def action_show_nodes(cfg: RuntimeConfig) -> None:
    run_kubectl(cfg, ["get", "nodes", "-o", "wide"])


def action_show_workloads(cfg: RuntimeConfig) -> None:
    run_kubectl(cfg, ["get", "pods,svc,deployments", "-n", cfg.namespace, "-o", "wide"])


def action_check_http(cfg: RuntimeConfig) -> None:
    print("\n--- Kiem tra Endpoint HTTP & Version qua NodePort ---")
    for name, ip in WORKER_NODES:
        base = f"http://{ip}:{cfg.nodeport}"
        print(f"\n[{name}] {base}")
        for path in ("/healthz", "/frontend-info.json", "/api/health"):
            status, body = http_get_text(base + path, timeout=5.0)
            if status is None:
                print(f"  GET {path:<20} -> LOI ({body})")
            else:
                if path != "/healthz":
                    try:
                        parsed = json.loads(body)
                        body = json.dumps(parsed, ensure_ascii=False)
                    except json.JSONDecodeError:
                        pass
                print(f"  GET {path:<20} -> {status} {body}")


def action_scale(cfg: RuntimeConfig) -> None:
    print("\nChon Deployment can scale:")
    print("  1. cdb-frontend (mac dinh)")
    print("  2. cdb-backend")
    choice = ask_choice("Chon [1/2]: ", default="1")
    dep = "cdb-backend" if choice == "2" else "cdb-frontend"

    raw_rep = ask_choice("Nhap so replicas (mac dinh 4): ", default="4")
    if not raw_rep.isdigit() or not (1 <= int(raw_rep) <= 12):
        print("So replicas khong hop le (can nam trong khoang 1..12).")
        return

    rc = run_kubectl(cfg, ["scale", "deployment", dep, "-n", cfg.namespace, f"--replicas={raw_rep}"])
    if rc == 0:
        run_kubectl(cfg, ["rollout", "status", f"deployment/{dep}", "-n", cfg.namespace, "--timeout=90s"])
        run_kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-l", f"app={dep}", "-o", "wide"])


def select_deployments_scope() -> list[str]:
    print("\nChon thanh phan ap dung:")
    print("  1. Ca Frontend va Backend (mac dinh)")
    print("  2. Chi cdb-backend")
    print("  3. Chi cdb-frontend")
    scope = ask_choice("Chon [1/2/3]: ", default="1")
    if scope == "2":
        return ["cdb-backend"]
    if scope == "3":
        return ["cdb-frontend"]
    return ["cdb-backend", "cdb-frontend"]


def action_update_version(cfg: RuntimeConfig) -> None:
    ver = ask_choice("\nNhap tag phien ban (v1 hoac v2, mac dinh v2): ", default="v2")
    if ver not in ("v1", "v2"):
        print("Chi ho tro tag phien ban 'v1' hoac 'v2'.")
        return

    targets = select_deployments_scope()
    for dep in targets:
        if dep == "cdb-backend":
            container_img = f"backend={cfg.image_prefix}/cdb-kanban-backend:{ver}"
        else:
            container_img = f"frontend={cfg.image_prefix}/cdb-kanban-frontend:{ver}"
        rc = run_kubectl(cfg, ["set", "image", f"deployment/{dep}", container_img, "-n", cfg.namespace])
        if rc != 0:
            return

    for dep in targets:
        run_kubectl(cfg, ["rollout", "status", f"deployment/{dep}", "-n", cfg.namespace, "--timeout=120s"])

    action_check_http(cfg)


def action_rollback(cfg: RuntimeConfig) -> None:
    targets = select_deployments_scope()
    for dep in targets:
        rc = run_kubectl(cfg, ["rollout", "undo", f"deployment/{dep}", "-n", cfg.namespace])
        if rc != 0:
            return

    for dep in targets:
        run_kubectl(cfg, ["rollout", "status", f"deployment/{dep}", "-n", cfg.namespace, "--timeout=120s"])

    action_check_http(cfg)


def action_history_and_spread(cfg: RuntimeConfig) -> None:
    run_kubectl(
        cfg,
        ["rollout", "history", "deployment/cdb-backend", "deployment/cdb-frontend", "-n", cfg.namespace],
    )
    run_kubectl(
        cfg,
        ["get", "pods", "-n", cfg.namespace, "-o", "wide", "--sort-by=.spec.nodeName"],
    )


def action_internal_connectivity(cfg: RuntimeConfig) -> None:
    run_kubectl(cfg, ["get", "endpointslices", "-n", cfg.namespace])
    run_kubectl(
        cfg,
        [
            "exec",
            "-n",
            cfg.namespace,
            "deployment/cdb-frontend",
            "--",
            "wget",
            "-qO-",
            "http://backend-svc:8000/api/health",
        ],
    )
    print(flush=True)
    run_kubectl(
        cfg,
        [
            "exec",
            "-n",
            cfg.namespace,
            "deployment/postgres",
            "--",
            "pg_isready",
            "-U",
            "app_user",
            "-d",
            "studydb",
        ],
    )


def action_azure_vm(cfg: RuntimeConfig) -> None:
    print("\n--- Quan ly Worker VM tren Azure ---")
    print("  1. Xem trang thai VM trong Resource Group")
    print("  2. Tat worker1 (az vm deallocate)")
    print("  3. Bat lai worker1 (az vm start)")
    print("  4. Tat / Bat VM theo ten tuy chon")
    sub = ask_choice("Chon [1-4]: ", default="1")

    if sub == "1":
        run_az(["vm", "list", "-d", "-g", cfg.resource_group, "-o", "table"])
        return

    if sub == "2":
        vm_name, op = "worker1", "deallocate"
    elif sub == "3":
        vm_name, op = "worker1", "start"
    elif sub == "4":
        vm_name = ask_choice("Ten VM (vd: worker1, worker2): ")
        op = ask_choice("Thao tac [deallocate/start]: ")
        if vm_name not in ("cp", "worker1", "worker2") or op not in ("deallocate", "start"):
            print("Ten VM hoac thao tac khong hop le.")
            return
    else:
        print("Lua chon khong hop le.")
        return

    confirm = ask_choice(f"Xac nhan chay 'az vm {op} -g {cfg.resource_group} -n {vm_name}'? [y/N]: ", default="n")
    if confirm.lower() != "y":
        print("Da huy thao tac.")
        return

    rc = run_az(["vm", op, "-g", cfg.resource_group, "-n", vm_name])
    if rc == 0:
        while True:
            run_kubectl(cfg, ["get", "nodes", "-o", "wide"])
            run_kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-o", "wide"])
            again = ask_choice("\nNhan Enter de cap nhat lai trang thai Node/Pod, hoac nhap 'q' de ve menu: ", "q")
            if again.lower() == "q":
                break


def action_apply_yaml(cfg: RuntimeConfig) -> None:
    yaml_files = sorted(K8S_DIR.glob("*.yaml"))
    if not yaml_files:
        print(f"Khong tim thay tep YAML trong {K8S_DIR}")
        return

    if cfg.use_local_kubectl:
        run_kubectl(cfg, ["apply", "-f", str(K8S_DIR)])
        return

    combined = "\n---\n".join(path.read_text(encoding="utf-8") for path in yaml_files)
    run_kubectl(cfg, ["apply", "-f", "-"], stdin_text=combined)


def action_local_tests() -> None:
    py_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    python_bin = str(py_exe) if py_exe.is_file() else sys.executable
    print(f"\n[local]$ {python_bin} -m unittest discover -s tests -v", flush=True)
    subprocess.run([python_bin, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT_DIR, text=True)

    ruff_bin = shutil.which("ruff")
    if ruff_bin:
        print(f"\n[local]$ ruff check backend tests {Path(__file__).name}", flush=True)
        subprocess.run([ruff_bin, "check", "backend", "tests", Path(__file__).name], cwd=ROOT_DIR, text=True)


def print_menu(cfg: RuntimeConfig) -> None:
    mode = "local kubectl" if cfg.use_local_kubectl else f"SSH -> {cfg.ssh_user}@{cfg.cp_host}"
    print("\n" + "=" * 58)
    print("  CDB Kanban - Menu kiem thu & van hanh Kubernetes")
    print(f"  Ket noi: {mode} | Namespace: {cfg.namespace}")
    print("=" * 58)
    print("   1. Xem trang thai Nodes (kubectl get nodes -o wide)")
    print("   2. Xem Pods, Services & Deployments")
    print("   3. Kiem tra HTTP/API & Version (/healthz, /api/health)")
    print("   4. Scale Deployment (cdb-frontend / cdb-backend)")
    print("   5. Cap nhat image phien ban (v1 <-> v2)")
    print("   6. Rollback Deployment (kubectl rollout undo)")
    print("   7. Xem lich su Rollout & phan bo Pod theo Node")
    print("   8. Kiem tra ket noi noi bo (Frontend -> Backend -> Postgres)")
    print("   9. Quan ly Worker VM tren Azure (list / deallocate / start)")
    print("  10. Ap dung lai YAML manifests (k8s/)")
    print("  11. Chay Unit Test & Ruff Lint (local)")
    print("   0. Thoat")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cfg = detect_runtime_config()
    actions = {
        "1": lambda: action_show_nodes(cfg),
        "2": lambda: action_show_workloads(cfg),
        "3": lambda: action_check_http(cfg),
        "4": lambda: action_scale(cfg),
        "5": lambda: action_update_version(cfg),
        "6": lambda: action_rollback(cfg),
        "7": lambda: action_history_and_spread(cfg),
        "8": lambda: action_internal_connectivity(cfg),
        "9": lambda: action_azure_vm(cfg),
        "10": lambda: action_apply_yaml(cfg),
        "11": action_local_tests,
    }

    while True:
        print_menu(cfg)
        try:
            choice = input("Chon chuc nang [0-11]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nThoat.")
            return 0

        if choice == "0":
            print("Thoat.")
            return 0

        handler = actions.get(choice)
        if handler is None:
            print("Lua chon khong hop le, vui long nhap tu 0 den 11.")
            continue

        handler()
        pause_after_action()


if __name__ == "__main__":
    sys.exit(main())
