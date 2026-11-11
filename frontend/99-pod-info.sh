#!/bin/sh
set -eu

VERSION="${APP_VERSION:-v1}"
POD="${HOSTNAME:-unknown}"

cat <<EOF > /usr/share/nginx/html/frontend-info.json
{"version":"${VERSION}","pod":"${POD}"}
EOF

chmod 0644 /usr/share/nginx/html/frontend-info.json
