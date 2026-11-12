#!/bin/sh
set -eu

VERSION="$(printf '%s' "${APP_VERSION:-v1}" | tr -cd 'A-Za-z0-9._-')"
POD="$(printf '%s' "${POD_NAME:-${HOSTNAME:-unknown}}" | tr -cd 'A-Za-z0-9._-')"
NODE="$(printf '%s' "${NODE_NAME:-local}" | tr -cd 'A-Za-z0-9._-')"
: "${VERSION:=v1}"
: "${POD:=unknown}"
: "${NODE:=local}"

cat <<EOF > /usr/share/nginx/html/frontend-info.json
{"version":"${VERSION}","pod":"${POD}","node":"${NODE}"}
EOF
chmod 0644 /usr/share/nginx/html/frontend-info.json

cat <<EOF > /etc/nginx/pod-meta.inc
set \$fe_node "${NODE}";
set \$fe_pod "${POD}";
set \$fe_ver "${VERSION}";
EOF
chmod 0644 /etc/nginx/pod-meta.inc
