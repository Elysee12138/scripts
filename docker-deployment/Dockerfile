# Dockerfile - 通用应用镜像（示例占位）
FROM alpine:3.20

LABEL maintainer="ops@example.com"

RUN apk add --no-cache bash curl tzdata \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

WORKDIR /app
COPY . /app

EXPOSE 8080
CMD ["/bin/bash", "-c", "echo 'placeholder service'; sleep infinity"]
