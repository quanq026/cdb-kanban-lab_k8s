# CDB Kanban on Kubernetes

Triển khai ứng dụng quản lý công việc **CDB Kanban** theo kiến trúc tách tầng (**Nginx Frontend + Flask Backend API + PostgreSQL 17**) trên cụm **Kubernetes 3 node (`kubeadm`)** chạy trên hạ tầng Microsoft Azure.

---

## Mục lục

1. [Kiến trúc hệ thống](#1-kiến-trúc-hệ-thống)
2. [Cấu trúc mã nguồn](#2-cấu-trúc-mã-nguồn)
3. [Yêu cầu tiên quyết](#3-yêu-cầu-tiên-quyết)
4. [Hướng dẫn triển khai](#4-hướng-dẫn-triển-khai)
5. [Cấu hình môi trường & Bảo mật](#5-cấu-hình-môi-trường--bảo-mật)
6. [Vận hành Kubernetes (Scaling, Update, Rollback)](#6-vận-hành-kubernetes-scaling-update-rollback)
7. [Kiểm thử tự động](#7-kiểm-thử-tự-động)
8. [Xử lý sự cố thường gặp](#8-xử-lý-sự-cố-thường-gặp)

---

## 1. Kiến trúc hệ thống

Ứng dụng được đóng gói độc lập thành các container và triển khai trong namespace `cdb-kanban`:

```text
Client Browser (HTTP :30080)
       │
       ▼
Service: frontend-svc (NodePort 80:30080)
       ├── Pod cdb-frontend (worker1)   ── Nginx 1.27 Alpine
       └── Pod cdb-frontend (worker2)      • Static UI (/, /login, /register)
                                           • GET /healthz & /frontend-info.json
                                           • Reverse proxy /api/* ──► backend-svc:8000
                                                 │
                                                 ▼
Service: backend-svc (ClusterIP :8000)
       ├── Pod cdb-backend (worker1)    ── Python 3.12 + Flask + Gunicorn
       └── Pod cdb-backend (worker2)       • Auth cookie session & Kanban REST API
                                           • GET /livez & /api/health (version, pod)
                                                 │
                                                 ▼
Service: postgres-svc (ClusterIP :5432)
       └── Pod postgres                 ── PostgreSQL 17 Alpine
                                           • Schema khởi tạo từ ConfigMap postgres-initdb
```

### Đặc điểm thiết kế chính

- **Tách biệt Frontend / Backend:** Nginx phục vụ toàn bộ tài nguyên tĩnh và định tuyến `/api/` nội bộ qua CoreDNS (`http://backend-svc:8000`) giúp trình duyệt giao tiếp cùng nguồn (same-origin), không cần cấu hình CORS.
- **High Availability (HA) cho lớp ứng dụng:** Cả `cdb-frontend` và `cdb-backend` đều chạy `replicas: 2`, cấu hình `topologySpreadConstraints` (`topologyKey: kubernetes.io/hostname`, `whenUnsatisfiable: ScheduleAnyway`) để phân bố đều trên hai worker node và vẫn có thể dồn về node sống khi một worker gặp sự cố.
- **Zero-downtime Rollout:** Chiến lược `RollingUpdate` với `maxSurge: 1, maxUnavailable: 0` kết hợp `readinessProbe` và `livenessProbe` tách biệt (`/api/health` kiểm tra DB cho readiness, `/livez` kiểm tra tiến trình cho liveness).
- **Quản lý phiên bản image chặt chẽ:** Mọi `Dockerfile` và Kubernetes manifest đều gắn tag phiên bản tường minh (`:v1`, `:v2`, `17-alpine`), không sử dụng `:latest`.

---

## 2. Cấu trúc mã nguồn

```text
cdb-kanban-lab_k8s/
├── backend/
│   ├── Dockerfile             # Image python:3.12-slim, ARG APP_VERSION=v1, chạy user UID 10001
│   ├── app.py                 # Flask API server (/api/*, /livez), cấu hình COOKIE_SECURE
│   └── requirements.txt       # Flask, Werkzeug, gunicorn, psycopg, psycopg-pool
├── frontend/
│   ├── Dockerfile             # Image nginx:1.27-alpine, ARG APP_VERSION=v1
│   ├── nginx.conf             # Cấu hình static routes, /healthz và proxy_pass /api/
│   ├── 99-pod-info.sh         # Entrypoint script ghi /frontend-info.json khi Pod khởi động
│   └── static/                # Giao diện Kanban (index.html, login.html, register.html, app.js, app.css)
├── db/
│   └── schema.sql             # DDL bảng users, tasks, index và trigger updated_at
├── k8s/
│   ├── 00-namespace.yaml      # Namespace cdb-kanban
│   ├── 01-postgres.yaml       # Secret cdb-secrets, ConfigMap postgres-initdb, Deployment & Service postgres
│   ├── 02-backend.yaml        # ConfigMap backend-config, Deployment cdb-backend, Service backend-svc
│   └── 03-frontend.yaml       # Deployment cdb-frontend, NodePort Service frontend-svc (:30080)
├── docs/
│   └── REPORT.md              # Báo cáo chi tiết quá trình triển khai Azure, Web, Docker & K8s
├── tests/
│   ├── test_app_and_config.py # Kiểm thử cấu hình backend API, nginx và static hooks
│   └── test_k8s_manifests.py  # Kiểm thử ràng buộc YAML K8s (không dùng :latest, HA, probes)
└── ruff.toml                  # Cấu hình linter Python
```

---

## 3. Yêu cầu tiên quyết

- **Cụm máy ảo (3 node):** `cp` (control-plane), `worker1`, `worker2` chạy Ubuntu 24.04 LTS, cấu hình tối thiểu 2 vCPU / 4 GiB RAM mỗi node, giao tiếp nội bộ qua mạng VNet (`10.10.1.0/24`).
- **Tường lửa / NSG:** Mở inbound TCP `22` (SSH), `30000-32767` (NodePort), tùy chọn `80`/`443`; cho phép lưu lượng nội bộ trong VNet (API server `6443`, VXLAN `8472/UDP`, `kubelet` `10250`).
- **Công cụ trên node:** `containerd` (đã bật plugin `cri` và `SystemdCgroup = true`), `kubelet`, `kubeadm`, `kubectl`, Docker Engine trên máy build, và tài khoản Docker Hub (`quanq026`).

---

## 4. Hướng dẫn triển khai

### Bước 1: Chuẩn bị 3 node & Dựng cụm Kubernetes (`kubeadm`)

Thực hiện trên **cả 3 node (`cp`, `worker1`, `worker2`)**:

```bash
# 1. Tắt swap và khai báo phân giải tên node nội bộ
sudo swapoff -a
sudo sed -i '/\sswap\s/ s/^/#/' /etc/fstab

cat <<'EOF' | sudo tee -a /etc/hosts
10.10.1.10 cp
10.10.1.11 worker1
10.10.1.12 worker2
EOF

# 2. Nạp module kernel & cấu hình sysctl cho mạng Kubernetes
cat <<'EOF' | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
sudo modprobe overlay
sudo modprobe br_netfilter

cat <<'EOF' | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system

# 3. Cấu hình containerd với SystemdCgroup = true (đảm bảo không bị tắt CRI)
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd && sudo systemctl enable containerd
```

Khởi tạo Control Plane và cài đặt CNI **Flannel VXLAN** trên `cp`:

```bash
sudo kubeadm init \
  --apiserver-advertise-address=10.10.1.10 \
  --pod-network-cidr=10.244.0.0/16 \
  --node-name=cp

mkdir -p "$HOME/.kube"
sudo cp -f /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"

kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
```

Sinh lệnh join trên `cp` và chạy trên `worker1`, `worker2`:

```bash
# Trên cp:
kubeadm token create --print-join-command

# Chạy lệnh kết quả với sudo trên worker1 và worker2, sau đó kiểm tra trên cp:
kubectl get nodes -o wide
```

### Bước 2: Build và Push Docker Image (`v1`, `v2`)

Thực hiện tại thư mục gốc của repository trên node có Docker đã đăng nhập (`docker login`):

```bash
docker build --build-arg APP_VERSION=v1 -t quanq026/cdb-kanban-backend:v1 ./backend
docker build --build-arg APP_VERSION=v2 -t quanq026/cdb-kanban-backend:v2 ./backend
docker build --build-arg APP_VERSION=v1 -t quanq026/cdb-kanban-frontend:v1 ./frontend
docker build --build-arg APP_VERSION=v2 -t quanq026/cdb-kanban-frontend:v2 ./frontend

docker push quanq026/cdb-kanban-backend:v1
docker push quanq026/cdb-kanban-backend:v2
docker push quanq026/cdb-kanban-frontend:v1
docker push quanq026/cdb-kanban-frontend:v2
```

### Bước 3: Triển khai lên Kubernetes

```bash
kubectl apply -f ./k8s/
kubectl rollout status deployment/postgres -n cdb-kanban
kubectl rollout status deployment/cdb-backend -n cdb-kanban
kubectl rollout status deployment/cdb-frontend -n cdb-kanban
kubectl get all -n cdb-kanban -o wide
```

Truy cập ứng dụng trên trình duyệt tại:
- `http://<IP-worker1>:30080`
- `http://<IP-worker2>:30080`

---

## 5. Cấu hình môi trường & Bảo mật

| Thành phần | Tài nguyên K8s | Biến / Khóa chính | Mục đích |
| --- | --- | --- | --- |
| Backend | `ConfigMap/backend-config` | `DB_HOST=postgres-svc`, `DB_PORT=5432`, `DB_NAME=studydb`, `DB_SSLMODE=disable` | Kết nối nội bộ tới PostgreSQL Service |
| Backend | `ConfigMap/backend-config` | `COOKIE_SECURE="false"` | Cho phép cookie phiên `cdb_session` hoạt động qua HTTP NodePort |
| Database & App | `Secret/cdb-secrets` | `POSTGRES_*`, `DB_USER`, `DB_PASSWORD`, `APP_SECRET_KEY` | Thông tin xác thực DB và khóa ký session Flask |
| Schema Init | `ConfigMap/postgres-initdb` | `01-schema.sql` | Tự động tạo bảng `users`, `tasks`, index và trigger lúc khởi tạo DB |

> **Lưu ý bảo mật:** Giá trị `Secret` trong `k8s/01-postgres.yaml` chỉ dùng cho môi trường lab/học tập. Khi triển khai môi trường thực tế, cần tạo Secret từ CI/CD hoặc External Secrets và bật HTTPS kèm `COOKIE_SECURE="true"`.

---

## 6. Vận hành Kubernetes (Scaling, Update, Rollback)

### Scale số lượng bản sao (Replicas)

```bash
kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=4
kubectl get pods -n cdb-kanban -l app=cdb-frontend -o wide
kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=2
```

### Rolling Update lên `v2` & Rollback về `v1`

```bash
# Cập nhật image lên phiên bản v2
kubectl set image deployment/cdb-backend backend=quanq026/cdb-kanban-backend:v2 -n cdb-kanban
kubectl set image deployment/cdb-frontend frontend=quanq026/cdb-kanban-frontend:v2 -n cdb-kanban
kubectl rollout status deployment/cdb-backend -n cdb-kanban
kubectl rollout status deployment/cdb-frontend -n cdb-kanban

# Kiểm tra lịch sử và khôi phục về v1
kubectl rollout history deployment/cdb-backend -n cdb-kanban
kubectl rollout undo deployment/cdb-backend -n cdb-kanban
kubectl rollout undo deployment/cdb-frontend -n cdb-kanban
```

### Kiểm tra Health & Thông tin Pod qua HTTP

```bash
curl -s http://<worker-ip>:30080/frontend-info.json
curl -s http://<worker-ip>:30080/api/health
```

---

## 7. Kiểm thử tự động

Chạy kiểm thử đơn vị, kiểm thử cấu trúc manifest và kiểm tra lint mã nguồn:

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\pip.exe install -r backend\requirements.txt
# Linux/macOS: ./.venv/bin/pip install -r backend/requirements.txt

.\.venv\Scripts\python.exe -m unittest discover -s tests -v
ruff check backend tests
kubectl apply --dry-run=client -f k8s/
```

---

## 8. Xử lý sự cố thường gặp

- **Trình duyệt không tải được khi chỉ gõ IP:** Bắt buộc kèm cổng NodePort `:30080` và giao thức `http://` (ví dụ `http://20.205.36.32:30080`), tránh để trình duyệt tự nâng cấp sang `https://`.
- **Đăng nhập xong vẫn quay lại `/login`:** Kiểm tra biến `COOKIE_SECURE` trong `k8s/02-backend.yaml` phải là `"false"` nếu truy cập bằng HTTP thuần không có TLS.
- **Node stuck ở `NotReady` sau khi cài `kubeadm`:** Kiểm tra `/etc/containerd/config.toml` đảm bảo không có `disabled_plugins = ["cri"]` và đã bật `SystemdCgroup = true`.
- **Pod không giao tiếp qua lại giữa các worker trên Azure:** Kiểm tra CNI đang dùng là VXLAN (Flannel) thay vì IP-in-IP (bị Azure SDN chặn) và NSG cho phép lưu lượng nội bộ trong cùng VNet.
