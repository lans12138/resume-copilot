# Nginx：统一公开入口

FIN-012 把 Nginx 落地为前端和 API 的唯一入口。浏览器只访问 `http://localhost:8080`，
`/api/v1/*` 由这里反代到 `api:8000`，静态资源直接由本容器提供。

## 文件

| 文件 | 作用 |
| --- | --- |
| `nginx.conf` | 主配置：日志、压缩、`client_max_body_size`、`include conf.d/*.conf`。 |
| `templates/default.conf.template` | 唯一 server 块。由镜像入口的 `envsubst` 展开 `${API_UPSTREAM}` / `${NGINX_PORT}` 后写入 `/etc/nginx/conf.d/default.conf`。 |
| `security-headers.conf` | 共享响应头（nosniff、DENY、CSP 等），由 server 块 `include`。 |

`templates/` 之外的任何 `.conf` 都不会被镜像 `COPY` 到 `conf.d`，因此不会出现「模板和展开结果同时生效」的第二份 server 块。

## 两条硬约束

1. **SSE 必须逐帧到达**。Nginx 默认缓冲反代响应，会把实时流攒成一次突发。API 已在
   `backend/app/sse/routes.py` 返回 `X-Accel-Buffering: no`，但那依赖上游记得发头；
   代理侧另有独立的 `location ~ ^/api/v1/(application-runs|match-runs)/[^/]+/events$`
   显式设置 `proxy_buffering off` / `proxy_cache off` / `gzip off` /
   `chunked_transfer_encoding on` / `proxy_read_timeout 1h`。
2. **Bearer 流必须保留**。API 用 `Authorization` 头鉴权而非 Cookie。Nginx 默认透传
   请求头，配置里不回写、不剥离该头；带令牌的流经代理可以成功，不带令牌的仍由 API
   返回 401——即授权判定仍在上游，代理没有替它回答。

这两条都由 `tests/validate_nginx_proxy.ps1` 在真实容器上断言，而不是靠读配置。

## 本机手工验证

~~~powershell
pwsh -NoProfile -File tests/validate_nginx_proxy.ps1
~~~

探针使用独立 Compose project，起完整栈（含 web/nginx），验证健康检查、静态资源、SPA
回退、安全头、经代理的 API 读写，以及 SSE 的「打字机」式逐帧到达，最后整栈清理。

请求从 `web-probe` 边车容器发往 `web:8080`，**不走发布到主机的端口**：这样断言的是
代理本身，而不是 Docker Desktop 的端口转发。边车是一次性容器（`compose run`），
所以 `web` 运行时镜像里既没有 Python 也没有 curl——探测能力不进入交付物。

判定「逐帧到达」的判据是终态事件之后仍能读到心跳帧：缓冲中的代理只会把终态帧
随关闭时的一次突发交出来，之后没有任何东西可读；逐帧转发则保持连接，于是心跳
会被量到（实测终态后 0.027s 收到心跳，共 45 帧）。

撤权一节断言的是同一个令牌在 `DELETE /jobs/{id}/assignments/{user_id}` 之后拿不到
该 Run（403）——即授权判定在上游。（SSE 流被中途撤权后关闭由 FIN-011 的浏览器矩阵
覆盖，不在本探针内。）

## 环境变量

`API_UPSTREAM` 默认 `http://api:8000`；`NGINX_PORT` 默认 `8080`。两者都在
`compose.yaml` 的 `web` 服务里给定，无需重新构建镜像即可改指。
