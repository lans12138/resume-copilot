# Compose 基础设施与数据库启动

IMP-003/IMP-004 在仓库根目录提供以下文件：

- `compose.yaml`：固定 digest 的 PostgreSQL 17 + pgvector、Redis 7.4、基础网络和四个命名 Volume。
- `compose.override.yaml`：仅供本机开发，把 PostgreSQL 和 Redis 调试端口绑定到 `127.0.0.1`。
- `migrate`：与 API 使用同一运行时镜像的一次性 Alembic 升级服务。
- `api`：提供独立的 liveness 和 PostgreSQL、Redis、Storage readiness。

## 首次启动

先从模板创建未跟踪的本机配置，并替换所有 Secret 占位符：

~~~powershell
Copy-Item .env.example .env
docker compose config --quiet
docker compose --profile tools run --rm storage-init
docker compose up --detach --wait postgres redis
docker compose --profile tools run --rm migrate
docker compose up --detach --wait api
docker compose ps
~~~

首次创建预置账号时，通过当前 PowerShell 进程临时传入凭据；密码不会写入仓库或命令参数：

~~~powershell
$env:BOOTSTRAP_USERNAME = 'hr-demo'
$env:BOOTSTRAP_ROLE = 'HR'
$env:BOOTSTRAP_PASSWORD = Read-Host 'Initial password' -MaskInput
docker compose run --rm --no-deps -e BOOTSTRAP_USERNAME -e BOOTSTRAP_ROLE -e BOOTSTRAP_PASSWORD api python -m backend.app.auth.bootstrap
Remove-Item Env:BOOTSTRAP_USERNAME, Env:BOOTSTRAP_ROLE, Env:BOOTSTRAP_PASSWORD
~~~

角色只允许 `HR`、`HIRING_MANAGER`、`ADMIN`。用户名会经过 NFKC、去除首尾空白和大小写折叠；重复用户名会被数据库唯一约束拒绝。

默认调试端口为 PostgreSQL `127.0.0.1:5433`、Redis `127.0.0.1:6380`。可在 `.env` 中通过 `POSTGRES_HOST_PORT` 和 `REDIS_HOST_PORT` 修改；容器之间始终使用服务名 `postgres:5432` 与 `redis:6379`。

正常停止不会删除数据：

~~~powershell
docker compose down
~~~

不要对开发或演示环境随意执行 `docker compose down --volumes`。`postgres_data` 是业务事实，`resume_storage` 必须与数据库一致管理。

## 自动验证

~~~powershell
pwsh -NoProfile -File scripts/project.ps1 compose-test
pwsh -NoProfile -File scripts/project.ps1 migration-test
pwsh -NoProfile -File scripts/project.ps1 auth-test
pwsh -NoProfile -File scripts/project.ps1 job-test
~~~

探针均使用独立 Compose project。基础探针验证服务健康、固定镜像、四个 Volume 和容器重建后的持久性；迁移探针验证 Alembic 可重复升级、元数据无漂移、`vector(1024)`、三项 readiness、Redis 故障时的安全 503 以及恢复；认证/岗位探针验证 Argon2、OAuth2 form、JWT、岗位不可变版本、状态机、乐观锁及 JobAssignment 撤权。结束后只删除对应探针 project 的容器、网络和 Volume。`auth-test` 与 `job-test` 是同一纵向探针的两个入口，不需要连续重复执行。

应用 readiness 不调用外部模型，也不会在响应中返回连接串、存储路径或底层异常。liveness 只证明 API 进程可响应，流量接入应以 readiness 为准。
