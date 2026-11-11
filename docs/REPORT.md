# BÁO CÁO TRIỂN KHAI ỨNG DỤNG WEB VỚI DOCKER VÀ KUBERNETES

**Đề tài:** Đóng gói ứng dụng CDB Kanban bằng Docker và triển khai trên cụm Kubernetes `kubeadm` 3 node tại Microsoft Azure  
**Repository mã nguồn:** `quanq026/cdb-kanban-lab_k8s`  
**Image Registry:** `docker.io/quanq026/cdb-kanban-backend` (`v1`, `v2`), `docker.io/quanq026/cdb-kanban-frontend` (`v1`, `v2`)

---

## 1. Kiến trúc tổng thể & Thiết lập hạ tầng Azure

### 1.1. Thiết kế mạng & Tài nguyên Azure (`rg-k8s` — Region `eastasia`)

Cụm hạ tầng được cấp phát hoàn toàn mới trong Resource Group `rg-k8s` tại vùng `eastasia` với các thành phần mạng và bảo mật như sau:

- **Virtual Network (`vnet-k8s`):** Dải địa chỉ `10.10.0.0/16`
  - **Subnet (`subnet-k8s`):** `10.10.1.0/24` (cấp phát IP nội bộ cố định logic cho 3 node từ `.10` đến `.12`)
- **Network Security Group (`nsg-k8s`):**
  - Priority `1000` (`Allow-SSH`): Cho phép TCP `22`
  - Priority `1010` (`Allow-HTTP`): Cho phép TCP `80`
  - Priority `1020` (`Allow-HTTPS`): Cho phép TCP `443`
  - Priority `1030` (`Allow-K8s-NodePort`): Cho phép TCP `30000-32767` phục vụ truy cập NodePort của Kubernetes
  - Giao tiếp nội bộ giữa `cp`, `worker1`, `worker2` (API Server `6443/TCP`, `kubelet` `10250/TCP`, Flannel VXLAN `8472/UDP`) đi qua luật mặc định `AllowVnetInBound` của Azure VNet.

### 1.2. Danh sách 3 máy ảo Kubernetes

Mỗi máy ảo sử dụng SKU `Standard_B2als_v2` (2 vCPU, 3.8 GiB RAM, kiến trúc `x86_64`), ổ cứng `StandardSSD_LRS` 30 GB, hệ điều hành **Ubuntu 24.04.5 LTS** (kernel `7.0.0-1017-azure`) và địa chỉ Public IPv4 Standard tĩnh:

| Hostname | Vai trò trong cụm | Public IP | Private IP | Cấu hình phần cứng |
| --- | --- | --- | --- | --- |
| `cp` | Control Plane (`etcd`, `apiserver`, `scheduler`, `controller-manager`) | `52.184.98.155` | `10.10.1.10` | 2 vCPU · 3.8 GiB RAM |
| `worker1` | Worker Node 1 (chạy Pod ứng dụng) | `20.205.36.32` | `10.10.1.11` | 2 vCPU · 3.8 GiB RAM |
| `worker2` | Worker Node 2 (chạy Pod ứng dụng + PostgreSQL) | `20.24.211.55` | `10.10.1.12` | 2 vCPU · 3.8 GiB RAM |

---

## 2. Thiết kế lại phần Web & Đóng gói Docker

### 2.1. Tách ứng dụng Web từ nguyên khối sang 3 tầng

Từ ứng dụng Kanban nguyên khối ban đầu, mã nguồn được tái cấu trúc vào thư mục độc lập nhằm đáp ứng chuẩn triển khai microservices trên Kubernetes:

1. **Tầng Frontend (`frontend/`):**
   - Sử dụng `nginx:1.27-alpine` phục vụ các trang tĩnh (`/`, `/login`, `/register`, `/app.css`, `/app.js`).
   - Cấu hình `frontend/nginx.conf` định tuyến `/api/*` tới Service nội bộ `http://backend-svc:8000`, đồng thời bổ sung endpoint `/healthz` cho Kubernetes probe.
   - Bổ sung script `frontend/99-pod-info.sh` chạy trong `/docker-entrypoint.d/` để sinh file `/frontend-info.json` chứa phiên bản build (`v1`/`v2`) và hostname của Pod Frontend; giao diện web đọc `/frontend-info.json` kết hợp `/api/health` để hiển thị trực tiếp thông tin **Version + Pod FE + Pod BE + DB user** ở góc giao diện.

2. **Tầng Backend (`backend/`):**
   - Sử dụng `python:3.12-slim`, Flask 3.1.3 + Gunicorn (`2` workers, lắng nghe cổng `8000`, chạy dưới user không đặc quyền `UID 10001`).
   - Loại bỏ phần serve HTML/CSS/JS tĩnh để Backend chỉ làm REST API thuần túy.
   - Bổ sung biến môi trường `COOKIE_SECURE` cho phép tắt cờ `Secure` trên session cookie khi người dùng truy cập qua HTTP NodePort (`COOKIE_SECURE="false"`).
   - Bổ sung `/livez` cho `livenessProbe` (không phụ thuộc kết nối DB tức thời) và mở rộng `/api/health` trả về `version`, `pod`, `db_user` phục vụ `readinessProbe` và kiểm chứng rollout.

3. **Tầng Cơ sở dữ liệu (`db/schema.sql`):**
   - Chạy PostgreSQL `17-alpine` trong cụm với bảng `users`, `tasks`, index `idx_tasks_user_status_position` và trigger tự cập nhật `updated_at`.

### 2.2. Đóng gói Docker Image theo phiên bản (`v1`, `v2`)

Sử dụng `ARG APP_VERSION` ngay trong hai `Dockerfile` để đóng gói và đẩy lên Docker Hub (`quanq026`) bốn image tách biệt, **tuyệt đối không sử dụng tag `:latest`**:

| Repository Docker Hub | Tag | Kích thước | Điểm khác biệt phiên bản |
| --- | --- | --- | --- |
| `quanq026/cdb-kanban-backend` | `:v1`, `:v2` | ~217 MB | Trả `"version": "v1"` hoặc `"v2"` ở `/api/health` và `/livez` |
| `quanq026/cdb-kanban-frontend` | `:v1`, `:v2` | ~73.7 MB | Thay placeholder `__APP_VERSION__` trên HTML, đổi kiểu pill badge `v2` trên CSS và `/frontend-info.json` |

---

## 3. Dựng cụm Kubernetes (`kubeadm`) & Triển khai tài nguyên

### 3.1. Cài đặt Kubernetes `v1.35.10` + `containerd v2.4.2` + Flannel CNI

1. **Chuẩn bị OS trên cả 3 node:** Tắt swap hoàn toàn (`swapoff -a`, `/etc/fstab`), ghi `/etc/hosts` nội bộ (`cp`, `worker1`, `worker2`), nạp module `overlay`, `br_netfilter` và bật `net.ipv4.ip_forward = 1`, `net.bridge.bridge-nf-call-iptables = 1`.
2. **Container Runtime:** Trên `cp` (nơi đã cài Docker Engine `29.9.3`) và 2 worker node (cài `containerd.io`), tái tạo `/etc/containerd/config.toml` bằng `containerd config default` để mở lại plugin CRI (`disabled_plugins = []`) và chuyển `SystemdCgroup = true`.
3. **Khởi tạo & Join cụm:**
   - Cài đặt và giữ nguyên phiên bản (`apt-mark hold`) cho `kubelet`, `kubeadm`, `kubectl` bản `v1.35.10`.
   - Khởi tạo trên `cp`: `kubeadm init --apiserver-advertise-address=10.10.1.10 --pod-network-cidr=10.244.0.0/16 --node-name=cp`.
   - Cài CNI **Flannel VXLAN** (`kube-flannel.yml`) vì hạ tầng Azure SDN không cho phép đóng gói gói tin IP-in-IP giữa các VM.
   - Join `worker1` và `worker2`; kết quả `kubectl get nodes -o wide` cho thấy cả 3 node ở trạng thái `Ready`.

### 3.2. Thiết kế Kubernetes YAML Manifests (`k8s/`)

Toàn bộ tài nguyên được khai báo dưới dạng mã trong thư mục `k8s/` và áp dụng qua `kubectl apply -f k8s/`:

1. `00-namespace.yaml`: Tạo Namespace riêng `cdb-kanban`.
2. `01-postgres.yaml`: Khai báo `Secret` `cdb-secrets`, `ConfigMap` `postgres-initdb` chứa `schema.sql`, `Deployment` `postgres` (`postgres:17-alpine`, chiến lược `Recreate`, readiness/liveness dùng `pg_isready`) và `ClusterIP Service` `postgres-svc:5432`.
3. `02-backend.yaml`: Khai báo `ConfigMap` `backend-config`, `Deployment` `cdb-backend` (`replicas: 2`, `RollingUpdate` `maxSurge: 1, maxUnavailable: 0`, `topologySpreadConstraints` theo `kubernetes.io/hostname`, `tolerations` 30s cho `not-ready`/`unreachable`) và `ClusterIP Service` `backend-svc:8000`.
4. `03-frontend.yaml`: Khai báo `Deployment` `cdb-frontend` (`replicas: 2`, `topologySpreadConstraints` + `RollingUpdate` + `tolerations` tương tự Backend) và `NodePort Service` `frontend-svc` ánh xạ cổng `80 -> 30080`.

---

## 4. Kết quả kiểm thử & Diễn tập vận hành thực tế

| Bài kiểm thử | Lệnh / Thao tác thực hiện | Kết quả quan sát thực tế |
| --- | --- | --- |
| **Triển khai ban đầu (`v1`)** | `kubectl apply -f ./k8s/` & Smoke test HTTP qua `20.205.36.32:30080` và `20.24.211.55:30080` | 2 Pod Backend (`worker1`, `worker2`), 2 Pod Frontend (`worker1`, `worker2`), 1 Pod Postgres (`worker2`) đều `1/1 Running`. Đăng ký user, tạo và cập nhật task thành công. |
| **Scale lên 4 replicas** | `kubectl scale deployment cdb-frontend -n cdb-kanban --replicas=4` | 4/4 Pod `Running`: đúng 2 Pod trên `worker1` và 2 Pod trên `worker2`; sau đó thu gọn sạch về `2`. |
| **Rolling Update `v1 -> v2`** | `kubectl set image` lên `:v2` cho Backend và Frontend | Pod `:v2` đạt `Ready` trước khi chấm dứt Pod `:v1` (`maxUnavailable: 0`); UI & `/api/health` trả `version: "v2"`, task Kanban trong PostgreSQL được giữ nguyên. |
| **Rollback `v2 -> v1`** | `kubectl rollout undo` cho Backend và Frontend | Lịch sử lên revision 3, `/api/health` xác nhận trả về lại `"version": "v1"` không mất dữ liệu. |
| **Tắt 1 Worker Node (`worker1`)** | `az vm deallocate -g rg-k8s -n worker1` (trong khi `postgres` nằm trên `worker2`) | `worker1` chuyển `NotReady`; sau ~30s Pod displaced được tạo mới ngay trên `worker2` đưa `cdb-backend` & `cdb-frontend` về `2/2 Running`. Truy cập `http://20.24.211.55:30080` đăng nhập và đọc task bình thường. Bật lại `worker1` bằng `az vm start`, node quay lại `Ready`. |

---

## 5. Các khó khăn gặp phải & Giải pháp xử lý

1. **Giới hạn vùng & Quota trên Azure:**
   - *Vấn đề:* Một số region như `japaneast` chặn SKU máy ảo B-series `x86_64` hoặc giới hạn IPv4 Standard ở mức tối đa `3` IP mỗi region.
   - *Khắc phục:* Rà soát quota khả dụng theo từng region, chuyển sang `eastasia` dùng dòng `Standard_B2als_v2` (2 vCPU) vừa khớp mức trần 6 vCPU / 3 IPv4 Standard.

2. **`containerd` của Docker package mặc định tắt CRI:**
   - *Vấn đề:* Khi cài `docker-ce` / `containerd.io`, file `/etc/containerd/config.toml` mặc định chứa `disabled_plugins = ["cri"]` khiến `kubeadm init` không giao tiếp được container runtime, và driver cgroup mặc định là `cgroupfs` gây xung đột với `systemd` của `kubelet`.
   - *Khắc phục:* Tạo mới file cấu hình chuẩn qua `containerd config default` (chứa `disabled_plugins = []`) và đổi `SystemdCgroup = true` trên cả 3 node trước khi chạy `kubeadm`.

3. **Mạng Pod giữa các Worker trên Azure SDN:**
   - *Vấn đề:* Hạ tầng mạng ảo đám mây Azure không định tuyến gói tin đóng gói IP-in-IP giữa các máy ảo.
   - *Khắc phục:* Sử dụng Flannel ở chế độ **VXLAN** (`10.244.0.0/16`) đóng gói qua UDP nội bộ VNet, giúp giao tiếp `frontend` ↔ `backend` ↔ `postgres` xuyên node hoạt động ổn định.

4. **Lỗi mất phiên đăng nhập khi chuyển từ HTTPS sang HTTP NodePort:**
   - *Vấn đề:* Bản gốc gắn cứng `SESSION_COOKIE_SECURE=True`, khiến trình duyệt từ chối lưu cookie `cdb_session` khi truy cập qua `http://<Node-IP>:30080`.
   - *Khắc phục:* Đưa cấu hình này ra biến môi trường `COOKIE_SECURE`, đặt `"false"` trong `ConfigMap` cho bài toán NodePort HTTP trong khi vẫn giữ mặc định an toàn `"true"` khi chạy sau TLS.

5. **Phân bổ Pod mặc định & Thời gian phục hồi khi mất Node:**
   - *Vấn đề:* Scheduler mặc định có thể đặt cả 2 replica lên cùng một worker, và thời gian chờ mặc định trước khi Pod trên node chết bị loại bỏ lên tới 300 giây (`tolerationSeconds: 300`).
   - *Khắc phục:* Khai báo `topologySpreadConstraints` (`whenUnsatisfiable: ScheduleAnyway`) để rải đều Pod khi đủ node nhưng vẫn cho phép dồn Pod khi một node tắt, kết hợp giảm `tolerationSeconds: 30` để hệ thống tự phục hồi nhanh trong demo.

6. **Cấu hình SSH bằng mật khẩu trên Ubuntu 24.04 Cloud Image:**
   - *Vấn đề:* File cấu hình ưu tiên thấp hơn không thể ghi đè `PasswordAuthentication no` trong `50-cloud-init.conf` và `60-cloudimg-settings.conf` vì OpenSSH áp dụng nguyên tắc *first-match-wins*.
   - *Khắc phục:* Đặt file `00-password-auth.conf` đứng đầu thứ tự bảng chữ cái và cập nhật đồng bộ các file cloud-init trong `/etc/ssh/sshd_config.d/`.
