# CDB Kanban — Kubernetes Deployment (`kubeadm` 3-Node Cluster on Azure)

Dự án tách kiến trúc ứng dụng **CDB Kanban** (Frontend Nginx + Backend Flask API + PostgreSQL 17) thành các container độc lập, đóng gói image gắn tag phiên bản rõ ràng (`:v1`, `:v2`, **không sử dụng `:latest`**) trên Docker Hub và triển khai chuẩn HA lên cụm **Kubernetes 3 node (`kubeadm v1.35.10`)** chạy trên Azure.

---

## 1. Kiến trúc hệ thống & Hạ tầng

### Sơ đồ kết nối trong cụm Kubernetes (`namespace: cdb-kanban`)

```text
Internet / Client Browser
   │
   ▼  HTTP :30080 (NodePort) trên worker1 (20.205.36.32) hoặc worker2 (20.24.211.55)
Service: frontend-svc (NodePort 80:30080)
   ├── Pod cdb-frontend (worker1) ──┐  Nginx 1.27-alpine:
   └── Pod cdb-frontend (worker2) ──┤  • Static HTML/CSS/JS + /frontend-info.json + /healthz
                                    │  • Reverse proxy /api/* ──► http://backend-svc:8000
                                    ▼
Service: backend-svc (ClusterIP :8000)
   ├── Pod cdb-backend (worker1) ───┐  Flask + Gunicorn (2 workers):
   └── Pod cdb-backend (worker2) ───┤  • Auth/Session (COOKIE_SECURE=false cho HTTP NodePort)
                                    │  • /livez & /api/health (trả version + hostname Pod)
                                    ▼
Service: postgres-svc (ClusterIP :5432)
   └── Pod postgres (postgres:17-alpine)
         • Khởi tạo schema từ ConfigMap postgres-initdb (db/schema.sql)
         • Thông tin đăng nhập lưu trong Secret cdb-secrets
```

### Cụm VM & Thành phần hệ thống (`rg-k8s`, `eastasia`)

| Node | Role | Public IP | Private IP | Cấu hình & Runtime |
| --- | --- | --- | --- | --- |
| `cp` | `control-plane` | `52.184.98.155` | `10.10.1.10` | Ubuntu 24.04.5 LTS · 2 vCPU / 3.8 GiB · `v1.35.10` · `containerd://2.4.2` |
| `worker1` | worker | `20.205.36.32` | `10.10.1.11` | Ubuntu 24.04.5 LTS · 2 vCPU / 3.8 GiB · `v1.35.10` · `containerd://2.4.2` |
| `worker2` | worker | `20.24.211.55` | `10.10.1.12` | Ubuntu 24.04.5 LTS · 2 vCPU / 3.8 GiB · `v1.35.10` · `containerd://2.4.2` |

- **Mạng VNet:** `10.10.0.0/16` (subnet `10.10.1.0/24`), NSG mở TCP `22`, `80`, `443`, `30000-32767`.
- **CNI:** Flannel VXLAN (`--pod-network-cidr=10.244.0.0/16`).
- **Endpoint truy cập web:**
  - `http://20.205.36.32:30080`
  - `http://20.24.211.55:30080`

---

## 2. Cấu trúc thư mục

```text
cdb-kanban-lab_k8s/
├── backend/
│   ├── Dockerfile          # python:3.12-slim, ARG APP_VERSION=v1, healthcheck /livez
│   ├── app.py              # Flask API-only, COOKIE_SECURE env, /livez, /api/health trả version+pod
│   └── requirements.txt
├── frontend/
│   ├── Dockerfile          # nginx:1.27-alpine, ARG APP_VERSION=v1, thay badge __APP_VERSION__
│   ├── nginx.conf          # Proxy /api/ -> http://backend-svc:8000, /healthz, SPA/HTML routes
│   ├── 99-pod-info.sh      # Sinh /frontend-info.json chứa version + hostname Pod khi container chạy
│   └── static/             # index.html, login.html, register.html, app.js, app.css
├── db/
│   └── schema.sql          # DDL bảng users, tasks, index và trigger updated_at
├── k8s/
│   ├── 00-namespace.yaml   # Namespace cdb-kanban
│   ├── 01-postgres.yaml    # Secret cdb-secrets, ConfigMap postgres-initdb, Deployment & Service postgres-svc
│   ├── 02-backend.yaml     # ConfigMap backend-config, Deployment cdb-backend (2 replicas), ClusterIP backend-svc
│   └── 03-frontend.yaml    # Deployment cdb-frontend (2 replicas), NodePort frontend-svc (:30080)
└── tests/
    ├── test_app_and_config.py
    └── test_k8s_manifests.py
```

---

## 3. Quy trình cài đặt cụm `kubeadm` (Tóm tắt kỹ thuật)

1. **Chuẩn bị cả 3 node (`cp`, `worker1`, `worker2`):**
   - Tắt swap (`swapoff -a`, cấu hình `/etc/fstab`), khai báo `/etc/hosts` nội bộ (`10.10.1.10 cp`, `10.10.1.11 worker1`, `10.10.1.12 worker2`).
   - Nạp module kernel `overlay`, `br_netfilter` và bật sysctl `net.bridge.bridge-nf-call-iptables=1`, `net.ipv4.ip_forward=1`.
   - Cấu hình `/etc/containerd/config.toml` bằng `containerd config default` với **`SystemdCgroup = true`** và **`disabled_plugins = []`** (mở plugin CRI).
   - Cài đặt và hold `kubelet kubeadm kubectl` (`v1.35.10`).
2. **Khởi tạo Control Plane (`cp`):**
   ```bash
   sudo kubeadm init \
     --apiserver-advertise-address=10.10.1.10 \
     --pod-network-cidr=10.244.0.0/16 \
     --node-name=cp
   kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
   ```
3. **Join 2 Worker Node:**
   - Sinh lệnh `kubeadm token create --print-join-command` và chạy trên `worker1`, `worker2`.

---

## 4. Build, Push Docker Hub & Triển khai YAML

Tất cả image đều gắn tag phiên bản `:v1` / `:v2`, tuyệt đối không dùng `:latest`:

```bash
docker build --build-arg APP_VERSION=v1 -t quanq026/cdb-kanban-backend:v1 ./backend
docker build --build-arg APP_VERSION=v2 -t quanq026/cdb-kanban-backend:v2 ./backend
docker build --build-arg APP_VERSION=v1 -t quanq026/cdb-kanban-frontend:v1 ./frontend
docker build --build-arg APP_VERSION=v2 -t quanq026/cdb-kanban-frontend:v2 ./frontend

docker push quanq026/cdb-kanban-backend:v1
docker push quanq026/cdb-kanban-backend:v2
docker push quanq026/cdb-kanban-frontend:v1
docker push quanq026/cdb-kanban-frontend:v2

kubectl apply -f ./k8s/
kubectl get all -n cdb-kanban -o wide
```

---

## 5. Kết quả kiểm thử & Diễn tập CKA thực tế

### 5.1. Trạng thái ban đầu (`v1`) & Smoke Test End-to-End

- Các Pod được rải đều qua `topologySpreadConstraints` (`worker1` và `worker2`):
  - `cdb-backend-784bfd57c5-fs8h5` (`worker1`), `cdb-backend-784bfd57c5-djngf` (`worker2`)
  - `cdb-frontend-7c4fb64f7c-lzq8k` (`worker1`), `cdb-frontend-7c4fb64f7c-xxmb6` (`worker2`)
  - `postgres-b457ffcd6-9srxc` (`worker2`)
- Kiểm thử HTTP qua cả `20.205.36.32:30080` và `20.24.211.55:30080`: xác nhận load balancing đi qua đủ cả 2 Pod frontend và 2 Pod backend, đăng ký tài khoản, lưu cookie `cdb_session` qua HTTP (`COOKIE_SECURE=false`), tạo task `"CKA Persistent Kanban Task"` và chuyển trạng thái sang `doing`.

### 5.2. Scale Deployment lên 4 replicas và thu gọn về 2

```bash
kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=4
kubectl rollout status deployment/cdb-frontend -n cdb-kanban --timeout=120s
kubectl get pods -n cdb-kanban -l app=cdb-frontend -o wide
```

Kết quả quan sát khi scale lên 4 (cân đều 2 Pod trên `worker1`, 2 Pod trên `worker2`):

```text
cdb-frontend-7c4fb64f7c-d4vll   1/1     Running   0   8s    10.244.1.4   worker1
cdb-frontend-7c4fb64f7c-lzq8k   1/1     Running   0   72s   10.244.1.3   worker1
cdb-frontend-7c4fb64f7c-s4xhv   1/1     Running   0   8s    10.244.2.5   worker2
cdb-frontend-7c4fb64f7c-xxmb6   1/1     Running   0   72s   10.244.2.4   worker2
```

Sau khi kiểm tra, thu gọn về `2` replicas (`kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=2`).

### 5.3. Rolling Update `:v1` -> `:v2` và Rollback `:v2` -> `:v1`

```bash
kubectl set image deployment/cdb-backend backend=quanq026/cdb-kanban-backend:v2 -n cdb-kanban
kubectl set image deployment/cdb-frontend frontend=quanq026/cdb-kanban-frontend:v2 -n cdb-kanban
kubectl rollout status deployment/cdb-backend -n cdb-kanban
kubectl rollout status deployment/cdb-frontend -n cdb-kanban
```

- Với cấu hình `maxSurge: 1, maxUnavailable: 0`, cụm tạo Pod `v2` sẵn sàng (`Running`, đạt `readinessProbe`) trước khi chấm dứt Pod `v1`.
- Giao diện `/login`, `/frontend-info.json` và `/api/health` trên cả 2 NodePort đều trả về `version: "v2"`, dữ liệu user và task `"CKA Persistent Kanban Task"` trong PostgreSQL vẫn giữ nguyên.
- Thực hiện Rollback về `v1`:
  ```bash
  kubectl rollout undo deployment/cdb-backend -n cdb-kanban
  kubectl rollout undo deployment/cdb-frontend -n cdb-kanban
  ```
  Kết quả `/api/health` quay lại `version: "v1"` (`revision 3` trong `rollout history`).

### 5.4. Mô phỏng sự cố tắt 1 Worker Node (`worker1`) & Khôi phục

- Trong phiên diễn tập, `postgres` chạy trên `worker2`. Thực hiện tắt node `worker1` (`az vm deallocate -g rg-k8s -n worker1`):
  - Node `worker1` chuyển trạng thái `NotReady`.
  - Với `tolerations` `tolerationSeconds: 30` và `whenUnsatisfiable: ScheduleAnyway`, Kubernetes tự động tạo Pod thay thế trên node sống (`worker2`), đưa `cdb-backend` và `cdb-frontend` đạt lại `2/2 Running` trên `worker2`.
  - Truy cập `http://20.24.211.55:30080` khi `worker1` đang tắt vẫn đăng nhập và tải dữ liệu Kanban bình thường (`/api/health` trả `ok: true`).
- Khởi động lại `worker1` (`az vm start -g rg-k8s -n worker1`): node tự động quay lại cụm ở trạng thái `Ready`, sau đó `kubectl rollout restart deployment/cdb-backend deployment/cdb-frontend -n cdb-kanban` để cân bằng lại tải về hai worker.

---

## 6. Chạy kiểm thử tự động trong repo

```bash
python -m venv .venv
.\.venv\Scripts\pip.exe install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
ruff check backend tests
```
