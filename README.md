# CDB Kanban on Kubernetes

Triển khai ứng dụng quản lý công việc **CDB Kanban** theo kiến trúc tách tầng (**Nginx Frontend + Flask Backend API + PostgreSQL 17**) trên cụm **Kubernetes 3 node (`kubeadm`)** chạy trên hạ tầng Microsoft Azure.

---

## Mục lục

1. [Kiến trúc hệ thống](#1-kiến-trúc-hệ-thống)
2. [Cấu trúc mã nguồn](#2-cấu-trúc-mã-nguồn)
3. [Hạ tầng cụm & Truy cập SSH](#3-hạ-tầng-cụm--truy-cập-ssh)
4. [Hướng dẫn triển khai](#4-hướng-dẫn-triển-khai)
5. [Cấu hình môi trường & Bảo mật](#5-cấu-hình-môi-trường--bảo-mật)
6. [Kiểm thử vận hành thực tế](#6-kiểm-thử-vận-hành-thực-tế)
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
- **High Availability (HA) cho lớp ứng dụng:** Cả `cdb-frontend` và `cdb-backend` đều chạy `replicas: 2`, cấu hình `topologySpreadConstraints` (`topologyKey: kubernetes.io/hostname`, `whenUnsatisfiable: ScheduleAnyway`, `matchLabelKeys: [pod-template-hash]`) để luôn phân bố đều trên hai worker node kể cả trong lúc rollout, đồng thời cho phép dồn Pod về node sống chỉ với `tolerationSeconds: 10` khi một worker gặp sự cố.
- **Zero-downtime Rollout:** Chiến lược `RollingUpdate` với `maxSurge: 1, maxUnavailable: 0`, `preStop: ["sleep", "2"]` cùng `readinessProbe` và `livenessProbe` tách biệt (`/api/health` kiểm tra DB cho readiness, `/livez` kiểm tra tiến trình cho liveness).
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
│   ├── REPORT.md              # Báo cáo chi tiết quá trình triển khai Azure, Web, Docker & K8s
│   └── Bao-cao-CKA-2026-CDB-Kanban.pdf
├── tests/
│   ├── test_app_and_config.py # Kiểm thử cấu hình backend API, nginx và static hooks
│   └── test_k8s_manifests.py  # Kiểm thử ràng buộc YAML K8s (không dùng :latest, HA, probes, tolerations)
└── ruff.toml                  # Cấu hình linter Python
```

---

## 3. Hạ tầng cụm & Truy cập SSH

### Thông tin 3 Node Azure (`rg-k8s` — Region `eastasia`)

| Node | Vai trò | Private IP | Public IP | Truy cập ứng dụng (NodePort) |
| --- | --- | --- | --- | --- |
| `cp` | Control Plane (`apiserver`, `etcd`, `scheduler`, `controller-manager`) | `10.10.1.10` | `52.184.98.155` | — |
| `worker1` | Worker Node 1 | `10.10.1.11` | `20.205.36.32` | `http://20.205.36.32:30080` |
| `worker2` | Worker Node 2 | `10.10.1.12` | `20.24.211.55` | `http://20.24.211.55:30080` |

### Lệnh SSH vào 3 Node

```bash
# Đăng nhập bằng SSH key (hoặc bỏ cờ '-i ~/.ssh/id_rsa_azure' nếu dùng mật khẩu)
ssh -i ~/.ssh/id_rsa_azure azureuser@52.184.98.155   # cp (Control Plane)
ssh -i ~/.ssh/id_rsa_azure azureuser@20.205.36.32    # worker1
ssh -i ~/.ssh/id_rsa_azure azureuser@20.24.211.55    # worker2
```

### Yêu cầu môi trường & Tường lửa

- **Cấu hình VM:** Ubuntu 24.04.5 LTS, tối thiểu 2 vCPU / 3.8 GiB RAM mỗi node (`Standard_B2als_v2`), giao tiếp nội bộ qua subnet `10.10.1.0/24`.
- **Tường lửa / NSG (`nsg-k8s`):** Mở inbound TCP `22` (SSH), `30000-32767` (NodePort), tùy chọn `80`/`443`; cho phép lưu lượng nội bộ trong VNet (`AllowVnetInBound` cho API server `6443/TCP`, Flannel VXLAN `8472/UDP`, `kubelet` `10250/TCP`).
- **Công cụ trên node:** `containerd` (đã bật plugin `cri` và `SystemdCgroup = true`), `kubelet`, `kubeadm`, `kubectl` (`v1.35.10`), Docker Engine trên máy build và tài khoản Docker Hub (`quanq026`).

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

## 6. Kiểm thử vận hành thực tế

Các bước kiểm tra dưới đây thực hiện trực tiếp trên `cp` (`ssh -i ~/.ssh/id_rsa_azure azureuser@52.184.98.155`).

### 6.1. Xác minh Cluster, phân bố Pod & liên kết Service nội bộ

```bash
# 1. Kiểm tra trạng thái 3 Node và CNI Flannel
kubectl get nodes -o wide
kubectl get pods -n kube-flannel

# 2. Kiểm tra toàn bộ Deployment, Pod, Service và EndpointSlice
kubectl get deployments,pods,svc,endpointslices -n cdb-kanban -o wide

# 3. Kiểm tra luồng gọi nội bộ Frontend -> Backend Service -> PostgreSQL Service
kubectl exec -n cdb-kanban deployment/cdb-frontend -- wget -qO- http://backend-svc:8000/api/health
kubectl exec -n cdb-kanban deployment/postgres -- pg_isready -U app_user -d studydb

# 4. Kiểm tra truy cập từ ngoài qua NodePort của cả 2 Worker
curl -s http://20.205.36.32:30080/frontend-info.json
curl -s http://20.205.36.32:30080/api/health
curl -s http://20.24.211.55:30080/api/health
```

*Kỳ vọng:* Cả 3 node ở trạng thái `Ready`; `cdb-frontend` (`2/2`) và `cdb-backend` (`2/2`) chia đều mỗi worker 1 Pod; các lệnh HTTP đều trả `200 OK` kèm `"version": "v1"`.

### 6.2. Xem log ứng dụng & kiểm tra cân bằng tải giữa 2 Worker

**1. Xem log real-time trên `cp` (`52.184.98.155`):** `kubectl logs` tự động gom log của toàn bộ Pod trên cả `worker1` và `worker2` về một màn hình.

```bash
# Log Frontend Nginx (hiện FE node/pod và Backend node/pod đích ở trường be=...)
kubectl logs -f -n cdb-kanban -l app=cdb-frontend --since=1m

# Log Backend Flask (đã ẩn nhiễu kube-probe, hiện via=FE:<node>/<pod> -> [BE node=<node>])
kubectl logs -f -n cdb-kanban -l app=cdb-backend --since=1m

# Log PostgreSQL (khi cần theo dõi trạng thái khởi tạo hoặc truy vấn DB)
kubectl logs -f -n cdb-kanban -l app=postgres --tail=50
```

**2. Gửi request kiểm tra cân bằng tải qua NodePort:** Nhờ `externalTrafficPolicy: Cluster`, gọi vào Public IP của `worker1` (`20.205.36.32:30080`) hay `worker2` (`20.24.211.55:30080`) đều tự phân phối kết nối mới sang cả 2 worker:

```bash
for i in $(seq 1 6); do
  curl -s http://20.24.211.55:30080/frontend-info.json; echo
  curl -s http://20.24.211.55:30080/api/health; echo
done
```

*(Ghi chú: Trình duyệt giữ kết nối TCP qua HTTP Keep-Alive nên trong một phiên mở trang có thể đi chung 1 Pod Frontend, nhưng các kết nối TCP mới và luồng proxy `/api/*` sang `backend-svc` vẫn chia tải đều giữa hai node.)*

**3. Xem bộ đếm gói `iptables` ở tầng kernel (chạy trực tiếp trên `worker1` hoặc `worker2`):**

```bash
# Liệt kê tên chain KUBE-SVC-* của các Service trong namespace cdb-kanban
sudo iptables -t nat -L KUBE-SERVICES -n -v | grep cdb-kanban

# Xem chi tiết rule xác suất 50% và cột pkts/bytes chuyển tới từng Pod endpoint
sudo iptables -t nat -L -n -v | grep -B 3 -A 1 cdb-kanban
```

### 6.3. Mở rộng số lượng bản sao (Scale Replicas)

```bash
kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=4
kubectl rollout status deployment/cdb-frontend -n cdb-kanban
kubectl get pods -n cdb-kanban -l app=cdb-frontend -o wide

# Thu gọn lại trạng thái ban đầu sau khi kiểm tra
kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=2
```

*Kỳ vọng:* 4/4 Pod đạt `Running` và tự phân bố 2 Pod trên `worker1`, 2 Pod trên `worker2`.

### 6.4. Cập nhật không gián đoạn (`v1 -> v2`) & Rollback (`v2 -> v1`)

```bash
# Cập nhật image lên phiên bản v2
kubectl set image deployment/cdb-backend backend=quanq026/cdb-kanban-backend:v2 -n cdb-kanban
kubectl set image deployment/cdb-frontend frontend=quanq026/cdb-kanban-frontend:v2 -n cdb-kanban
kubectl rollout status deployment/cdb-backend -n cdb-kanban
kubectl rollout status deployment/cdb-frontend -n cdb-kanban

# Xác nhận version mới trên endpoint (UI đổi badge sang v2, dữ liệu DB giữ nguyên)
curl -s http://20.205.36.32:30080/frontend-info.json
curl -s http://20.205.36.32:30080/api/health

# Kiểm tra lịch sử và khôi phục về v1
kubectl rollout history deployment/cdb-backend -n cdb-kanban
kubectl rollout undo deployment/cdb-backend -n cdb-kanban
kubectl rollout undo deployment/cdb-frontend -n cdb-kanban
kubectl rollout status deployment/cdb-backend -n cdb-kanban
kubectl rollout status deployment/cdb-frontend -n cdb-kanban
curl -s http://20.205.36.32:30080/api/health
```

*Kỳ vọng:* Nhờ `maxUnavailable: 0`, `preStop` và `readinessProbe`, quá trình nâng cấp/rollback giữ `200 OK` liên tục và toàn bộ tài khoản/task đã tạo trước đó vẫn hiển thị bình thường.

### 6.5. Mô phỏng sự cố tắt 1 Worker Node & Tự phục hồi

Trước khi tắt node, kiểm tra vị trí Pod `postgres` hiện tại (`kubectl get pods -n cdb-kanban -o wide`). Vì `01-postgres.yaml` dùng `emptyDir`, chọn tắt worker **không chứa** `postgres` để giữ nguyên dữ liệu (ví dụ nếu `postgres` đang ở `worker1` thì tắt `worker2`, hoặc ngược lại).

```bash
# Terminal 1 (trên cp): theo dõi Node và Pod thay đổi theo thời gian thực
kubectl get nodes,pods -n cdb-kanban -o wide -w

# Terminal 2 (trên máy local có Azure CLI): tắt nóng 1 worker (thay worker2/worker1 tương ứng)
az vm deallocate -g rg-k8s -n worker2
```

*Kết quả quan sát:*
1. Với `--node-monitor-grace-period=20s` trên control plane và `tolerationSeconds: 10` trong các manifest, worker bị tắt chuyển `NotReady` sau ~`16–20s` và toàn bộ Pod trên node đó được tạo mới sang worker còn lại sau tổng thời gian ~`28–30s`.
2. Mở trình duyệt hoặc `curl` vào Public IP của worker đang sống (`http://20.205.36.32:30080` hoặc `http://20.24.211.55:30080`) vẫn đăng nhập và thao tác Kanban bình thường.
3. *(Lưu ý: Nếu tắt đúng worker đang giữ `postgres`, Pod DB mới khởi tạo lại trên worker sống sau ~30s nhưng database được tạo mới trống từ `ConfigMap/postgres-initdb`).*

Khôi phục lại node sau kiểm thử:

```bash
az vm start -g rg-k8s -n worker2
kubectl get nodes -o wide
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
