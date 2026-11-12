FROM nginx:1.27-alpine

ARG APP_VERSION=v1
ENV APP_VERSION=${APP_VERSION}

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY static/ /usr/share/nginx/html/
COPY 99-pod-info.sh /docker-entrypoint.d/99-pod-info.sh

RUN sed -i 's/\r$//' /docker-entrypoint.d/99-pod-info.sh \
    && chmod +x /docker-entrypoint.d/99-pod-info.sh \
    && printf 'set $fe_node "local";\nset $fe_pod "unknown";\nset $fe_ver "%s";\n' "${APP_VERSION}" > /etc/nginx/pod-meta.inc \
    && chmod 0644 /etc/nginx/pod-meta.inc \
    && sed -i "s/__APP_VERSION__/${APP_VERSION}/g" \
        /usr/share/nginx/html/index.html \
        /usr/share/nginx/html/login.html \
        /usr/share/nginx/html/register.html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "-qO-", "http://127.0.0.1/healthz"]
