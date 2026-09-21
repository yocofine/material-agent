# material-agent 服务器部署交接说明

## 1. 部署内容

部署包提供 FastAPI 后端、SQLite 持久化、只读 MySQL 客户关系查询、Docker Compose 与 Caddy HTTPS。

访问入口：

- `/login`：超级管理员和教辅统一登录；
- `/admin`：超级管理员管理教辅账号和全部待处理批次；
- `/staff`：教辅查看自己的客户、修改密码、生成上传链接和处理批次；
- `/material-ai/health`：健康检查。

## 2. 前置条件

- Linux x86_64/arm64 服务器；
- Docker Engine 与 Docker Compose 插件；
- 已解析到服务器的域名；
- 安全组/防火墙开放 TCP 80、TCP/UDP 443；
- 服务器可以访问 Qwen API 和客户关系 MySQL；
- MySQL 提供仅有关系表 `SELECT` 权限的专用账号。

## 3. 首次部署

```bash
unzip material-agent-server-deploy.zip -d material-agent
cd material-agent
cp .env.production.example .env.production
chmod 600 .env.production
openssl rand -base64 48
```

编辑 `.env.production`：

- `DOMAIN` 与 `PUBLIC_BASE_URL`；
- `ADMIN_USERNAME` 与 `ADMIN_PASSWORD`；
- 将随机字符串填入 `LINK_SIGNING_SECRET`；
- `QWEN_API_KEY`；
- `CUSTOMER_DB_*` 与关系表列名。
- CRM 对接默认不鉴权，`TASK_API_TOKEN` 留空；双方另行约定鉴权后再填写。如果 OSS 使用内网地址，设置 `TASK_ALLOW_PRIVATE_URLS=true`。

分类与压缩包默认策略：

```env
API_MAX_ARCHIVE_MB=300
API_MAX_EXTRACTED_MB=600
API_MAX_EXTRACTED_FILES=199
```

所有分类任务默认直接调用大模型，因此生产环境应配置有效的 `QWEN_API_KEY`。压缩包限制按单次上传/批次累计计算。

CRM任务默认最多同时从OSS下载3个文件：

```env
TASK_DOWNLOAD_CONCURRENCY=3
TASK_CLASSIFICATION_CONCURRENCY=5
TASK_WORKER_CONCURRENCY=3
EXTRACT_TIMEOUT=60
QWEN_TIMEOUT=60
```

OSS下载并发限制为进程级全局限制；文件解压仍保持串行。文本提取不设置独立并发限制，系统最多同时运行3个整批任务，单批最多调用5个模型分类请求。

关系表配置示例：

```env
CUSTOMER_DB_HOST=mysql.internal
CUSTOMER_DB_PORT=3306
CUSTOMER_DB_NAME=db01
CUSTOMER_DB_USER=material_agent_reader
CUSTOMER_DB_PASSWORD=replace-me
CUSTOMER_DB_TABLE=customer_staff_relation
CUSTOMER_ID_COLUMN=customer_id
CUSTOMER_NAME_COLUMN=customer_name
STAFF_USERNAME_COLUMN=staff_username
```

执行：

```bash
chmod +x scripts/deploy_server.sh
bash scripts/deploy_server.sh --check
bash scripts/deploy_server.sh
```

验证：

```bash
docker compose ps
docker compose logs --tail=100 app
curl -fsS https://你的域名/material-ai/health
```

如果服务器由多个服务共用，请让网关或反向代理将 `/material-ai/` 前缀转发到本应用，并保留完整请求路径。对外接口为：

```text
POST /material-ai/api/classify
GET  /material-ai/api/result?taskId=...
GET  /material-ai/health
```

随部署包提供的 Caddy 配置已经只匹配 `/material-ai/*`，并把匹配请求的完整路径转发到 Python 应用的8000端口。其他路径不会转发到本应用。

健康监控建议使用以下策略，避免瞬时网络抖动导致频繁重启：

- 检查间隔：60 秒；
- 单次超时：5 秒；
- 启动宽限期：30 秒；
- 连续失败阈值：3 次；
- 连续失败达到阈值后由部署平台重启容器。

Compose 已配置相同的检查间隔和失败阈值。Docker Compose 本身只会将容器标记为 `unhealthy`，共享服务器上的部署平台仍需启用“连续失败后重启”策略。

旧路径 `/api/ai/classify` 和 `/api/ai/result` 仅保留兼容，不建议新系统继续使用。

当前共享服务器代理只开放 `/material-ai/*`，因此对外提供分类、结果查询和健康检查接口，不开放 `/login`、`/admin`、`/staff` 等管理页面。如生产环境仍需管理页面，必须由运维另行配置受限内网路由，不能直接暴露公网。

## 4. 更新与维护

更新代码后保留原 `.env.production`，重新执行：

```bash
bash scripts/deploy_server.sh
```

发布脚本的所有 Compose 命令都会显式使用 `--env-file .env.production`（或 `--env-file` 参数指定的文件），同时应用服务通过 Compose 的 `env_file` 将其中变量注入容器。配置文件不得打入镜像或提交到代码仓库。

发布时会使用 `--force-recreate` 重新创建容器，确保当前 `.env.production` 中的变量全部重新注入，不复用旧容器环境。SQLite、上传文件和工作区存放在持久化卷中，重建容器不会删除这些数据。

常用命令：

```bash
docker compose logs -f app
docker compose logs -f caddy
docker compose restart app
docker compose stop
docker compose up -d
```

禁止执行 `docker compose down -v`，该命令会删除保存数据库和客户文件的持久化卷。

## 5. 数据与安全

- SQLite、上传文件和分类文件位于 Compose 的 `material_data` 持久化卷；
- `.env.production` 包含管理员密码、API Key 和数据库密码，不得发送给前端或提交版本库；
- 教辅密码以随机盐 `scrypt` 哈希保存，不保存明文；
- 登录会话仅保存 token 哈希，修改密码或停用账号会使旧会话失效；
- 正式客户关系从 MySQL 实时只读查询；MySQL 不可用时正式客户功能会暂停；
- 更换 `LINK_SIGNING_SECRET` 会使全部已生成的客户链接失效。
