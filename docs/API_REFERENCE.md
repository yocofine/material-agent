# material-agent 接口说明文档

## 1. 基本信息

- 协议：HTTP/HTTPS
- 数据格式：除文件上传外均为 `application/json`
- 生产地址示例：`https://upload.example.com`
- 本地地址：`http://127.0.0.1:8000`
- 时间字段：Unix 时间戳，单位为秒
- 在线调试：`GET /docs`
- OpenAPI：`GET /openapi.json`

所有分类任务默认跳过规则匹配，直接调用配置的大模型。只有请求显式传入 `llm_only=false` 时才启用“规则优先、LLM 兜底”。

## 2. 身份与权限

系统存在三类访问身份：

| 身份 | 认证方式 | 权限 |
|---|---|---|
| 超级管理员 | 登录后获得会话 Cookie | 管理教辅账号、查看和分类全部批次 |
| 教辅 | 登录后获得会话 Cookie | 查看自己的客户、生成链接、修改密码、处理自己的批次 |
| 客户 | 上传链接中的 `token` | 打开上传页、上传或立即分类自己的文件 |

登录成功后服务端设置 HttpOnly Cookie：

```text
material_agent_session=<random-token>
```

前端与后端同源时浏览器会自动携带。跨域前端必须设置：

```javascript
fetch(url, { credentials: "include" })
```

当前推荐通过反向代理让前端与后端保持同源。

## 3. 页面入口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 管理员和教辅统一登录页 |
| GET | `/admin` | 超级管理员页面；未登录跳转 `/login` |
| GET | `/staff` | 教辅工作台；未登录跳转 `/login` |
| GET | `/?token=...` | 客户专属上传页 |

## 4. 登录与会话

### 4.1 登录

```http
POST /api/auth/login
Content-Type: application/json
```

请求：

```json
{
  "username": "jiaofu01",
  "password": "staff-password"
}
```

成功 `200`：

```json
{
  "ok": true,
  "redirect": "/staff"
}
```

超级管理员的 `redirect` 为 `/admin`。响应同时设置会话 Cookie，默认有效期由 `STAFF_SESSION_HOURS` 控制。

失败：

- `401`：账号或密码错误。

### 4.2 查询当前登录身份

```http
GET /api/auth/me
```

成功 `200`：

```json
{
  "staff_id": "4b82...",
  "username": "jiaofu01",
  "display_name": "教辅一",
  "is_admin": false
}
```

### 4.3 退出登录

```http
POST /api/auth/logout
```

成功后删除当前服务端会话和浏览器 Cookie。

### 4.4 教辅修改自己的密码

```http
PATCH /api/staff/me/password
Content-Type: application/json
```

请求：

```json
{
  "current_password": "old-password",
  "new_password": "new-password-at-least-10"
}
```

规则：

- 仅普通教辅可以调用；
- 新密码长度为 10～256 个字符；
- 新密码不能与当前密码相同；
- 成功后该教辅的所有旧会话立即失效，需要重新登录。

## 5. 教辅客户接口

### 5.1 查询“我的客户”

```http
GET /api/staff/customers
```

成功 `200`：

```json
{
  "ok": true,
  "official_available": true,
  "official_error": "",
  "items": [
    {
      "customer_id": "C001",
      "customer_name": "客户甲",
      "customer_source": "mysql",
      "pending_batch_count": 2,
      "last_upload_at": 1788912000
    },
    {
      "customer_id": "TEMP001",
      "customer_name": "临时客户",
      "customer_source": "temporary",
      "pending_batch_count": 0,
      "last_upload_at": 0
    }
  ]
}
```

说明：

- `mysql`：来自外部只读 MySQL 关系表；
- `temporary`：教辅在本系统创建，保存在 SQLite；
- MySQL 不可用时仍返回 `200`，但 `official_available=false`，列表中只包含本地临时客户。

### 5.2 创建临时客户

```http
POST /api/staff/customers/temporary
Content-Type: application/json
```

请求：

```json
{
  "customer_id": "TEMP001",
  "customer_name": "临时客户"
}
```

限制：

- 客户编号全局唯一；
- 不得与 MySQL 正式客户编号重复；
- MySQL 不可用时禁止创建，因为无法完成正式客户编号冲突检查。

常见失败：

- `409`：编号已存在或与正式客户冲突；
- `503`：正式客户数据库不可用。

### 5.3 为客户生成上传链接

```http
POST /api/staff/access-links
Content-Type: application/json
```

请求：

```json
{
  "customer_id": "C001",
  "customer_source": "mysql",
  "expires_in_hours": 24
}
```

`customer_source` 只能为：

- `mysql`
- `temporary`

有效期范围为 1 分钟到 365 天。

成功 `200`：

```json
{
  "ok": true,
  "token": "eyJ...",
  "path": "/?token=eyJ...",
  "url": "https://upload.example.com/?token=eyJ...",
  "user_id": "C001",
  "customer_id": "C001",
  "customer_name": "客户甲",
  "customer_source": "mysql",
  "staff_id": "4b82...",
  "staff_name": "教辅一",
  "issued_at": 1788912000,
  "expires_at": 1788998400
}
```

服务端会校验客户确实属于当前教辅。正式客户关系改变或教辅账号被停用后，旧链接立即失效。

## 6. 客户上传接口

### 6.1 暂存上传

```http
POST /api/upload?token=<upload-token>
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `files` | File，可重复 | 否 | 支持同时上传多个文件 |
| `text` | String | 否 | 服务端保存为 Markdown 文件 |

`files` 和 `text` 至少提供一个。

成功 `200`：

```json
{
  "ok": true,
  "upload_id": "a1b2c3d4e5f6",
  "count": 2,
  "saved_files": ["materials.zip"],
  "text_file": "input_123456789abc.md",
  "user_id": "C001",
  "message": "上传成功，共接收 2 个文件/文字"
}
```

上传限制：

- 一次请求最多 100 个上传文件；
- 普通文件单文件上限由 `API_MAX_FILE_MB` 控制，默认 1024 MB；
- 单次请求中压缩包原始体积合计不超过 300 MB；
- 解压后全部文件体积合计不超过 600 MB；
- 解压后的文件数量最多 199 个；
- ZIP、嵌套 ZIP、TAR、TAR.GZ、TGZ、RAR 均进行安全路径和资源限制检查。

超过大小或数量限制返回 `413`。

### 6.2 上传并立即分类

```http
POST /api/classify?token=<upload-token>&llm_only=true&concurrency=20
Content-Type: multipart/form-data
```

表单字段与 `/api/upload` 相同。

查询参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `llm_only` | `true` | `true` 全部走大模型；`false` 规则优先 |
| `concurrency` | `20` | 并发数，范围 1～50 |

成功响应包含分类结果、归档相对路径、token 使用量和耗时。

## 7. 待分类批次

### 7.1 查询批次

```http
GET /api/pending
```

- 超级管理员返回全部待分类批次；
- 教辅只返回自己的批次。

响应：

```json
{
  "ok": true,
  "count": 1,
  "items": [
    {
      "upload_id": "a1b2c3d4e5f6",
      "created": 1788912000,
      "files": 3,
      "user_id": "C001",
      "customer_name": "客户甲",
      "customer_source": "mysql",
      "staff_id": "4b82...",
      "staff_name": "教辅一"
    }
  ]
}
```

### 7.2 分类一个或全部批次

```http
POST /api/classify_pending?upload_id=a1b2c3d4e5f6&llm_only=true&concurrency=20
```

- 传 `upload_id`：分类指定批次；
- 不传 `upload_id`：分类当前身份可见的全部批次；
- `llm_only` 默认 `true`；
- 教辅访问其他教辅批次时返回 `404`，避免泄露批次是否存在。

## 8. 超级管理员接口

### 8.1 查询教辅账号

```http
GET /api/admin/staff
```

### 8.2 创建教辅账号

```http
POST /api/admin/staff
Content-Type: application/json
```

```json
{
  "username": "jiaofu01",
  "display_name": "教辅一",
  "password": "initial-password"
}
```

约束：

- 登录名长度 3～64；
- 只允许文字、数字、`.`、`_`、`-`、`@`；
- 密码至少 10 位；
- 登录名不能与超级管理员相同。

### 8.3 启用、停用或重置密码

```http
PATCH /api/admin/staff/{staff_id}
Content-Type: application/json
```

请求示例：

```json
{
  "active": false
}
```

或：

```json
{
  "password": "reset-password-at-least-10"
}
```

停用账号或重置密码会注销该教辅的所有旧会话。

### 8.4 查询服务端路径设置

```http
GET /api/admin/settings
```

### 8.5 修改归档路径

```http
POST /api/admin/settings
Content-Type: application/json
```

```json
{
  "organized_dir": "/app/data/organized"
}
```

## 9. Classbro CRM 第三方 AI 分类接口

这两个接口与《第三方 AI 分类对接说明》V1.1 一致。CRM 生成并传入 `taskId`，本服务只接收任务和供 CRM 主动查询，不向 CRM 回调。

### 9.1 发起分类

```http
POST /material-ai/api/classify
Content-Type: application/json; charset=utf-8
```

请求：

```json
{
  "taskId": "a1b2c3d4e5f6",
  "customerKey": "123456",
  "courses": [
    {
      "courseId": "1001",
      "courseName": "BUSI60441 Customer Analytics",
      "files": [
        {
          "fileId": "9001",
          "fileName": "BUSI60441_Module_Handbook.pdf",
          "fileUrl": "https://xxx.oss-cn-xxx.aliyuncs.com/material/xxx.pdf"
        }
      ]
    }
  ]
}
```

成功接收后立即返回 HTTP 200：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "accepted": true,
    "taskId": "a1b2c3d4e5f6"
  }
}
```

约束：

- `taskId`、`customerKey`、`courses` 以及每个课程的 `courseId`、`courseName`、`files` 均必填；
- 每个文件的 `fileId`、`fileName`、`fileUrl` 均必填，`fileUrl` 只接受 HTTP/HTTPS；
- 同一任务内 `fileId` 不得重复；
- `taskId` 由 CRM 生成并原样保存；同一 `taskId` 重复提交返回受理成功，但不会重复执行或覆盖已存在的稳定结果；
- OSS URL 的查询参数可用于下载，但不会写入 SQLite。

### 9.2 查询分类结果

```http
GET /material-ai/api/result?taskId=a1b2c3d4e5f6
```

处理中：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "taskId": "a1b2c3d4e5f6",
    "status": "PROCESSING",
    "results": null
  }
}
```

已完成：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "taskId": "a1b2c3d4e5f6",
    "status": "DONE",
    "results": [
      {
        "fileId": "9001",
        "courseId": "1001",
        "fileType": "Unit Guide (Syllabus)",
        "success": true,
        "failReason": null
      }
    ]
  }
}
```

文件失败不会阻断其他文件，仍返回 `DONE` 和本次任务的全量 `fileId`：

```json
{
  "fileId": "9002",
  "courseId": "1001",
  "success": false,
  "failReason": "文件下载失败：HTTP 404"
}
```

不存在的任务返回 HTTP 200，`data.status` 为 `NOT_FOUND`。结果可重复查询，`DONE` 后保持稳定。

当前最小版不返回结构化 `summary`；该字段在对方契约中为可选字段。分类强制使用大模型，未配置 `QWEN_API_KEY` 时，每个文件会以 `success=false` 返回。

同一任务中的 OSS 文件最多并行下载 3 个；该限制对当前进程中的全部任务全局生效。下载完成后的压缩包仍依次解压，避免低配服务器同时解压多个大文件。并行数可通过 `TASK_DOWNLOAD_CONCURRENCY` 调整，允许范围为 1～10。

默认不要求鉴权头，符合受控内网联调契约。如果双方后续另行约定 Bearer Token，可设置 `TASK_API_TOKEN`，两个接口会共同启用校验。

正式对外路径使用 `/material-ai/` 独立前缀，便于共享服务器按路径路由。旧路径 `/api/ai/classify`、`/api/ai/result` 继续作为兼容别名，但不应提供给新的调用方。

### 9.3 部署后需要回告 CRM 的信息

- 发起分类完整内网 URL，例如 `https://server.internal/material-ai/api/classify`；
- 查询结果完整内网 URL，例如 `https://server.internal/material-ai/api/result`；
- 单任务建议最长处理时长；
- 已支持相同 `taskId` 幂等重试；
- 服务器内网访问方式、端口、VPN 或 IP 白名单要求。

## 10. 兼容版单文件异步分类任务

该接口用于已有业务系统通过 OSS/HTTP 文件地址提交分类任务，不依赖网页登录会话。

### 10.1 提交任务

```http
POST /api/tasks
Content-Type: application/json
Authorization: Bearer <TASK_API_TOKEN>
```

请求：

```json
{
  "fileUrl": "https://oss.example.com/material.pdf?signature=temporary-signature"
}
```

成功返回 `202`：

```json
{
  "taskId": "78a4e42dc88d42e2a710852764b2419e",
  "status": "queued"
}
```

`TASK_API_TOKEN` 为空时不检查 Authorization，适用于受控内网联调；生产建议配置随机 Bearer Token。

### 10.2 查询任务

```http
GET /api/tasks/{taskId}
Authorization: Bearer <TASK_API_TOKEN>
```

处理中：

```json
{
  "taskId": "78a4e42dc88d42e2a710852764b2419e",
  "status": "running",
  "type": null,
  "summary": null,
  "items": [],
  "error": null,
  "createdAt": 1788912000,
  "updatedAt": 1788912001
}
```

完成：

```json
{
  "taskId": "78a4e42dc88d42e2a710852764b2419e",
  "status": "completed",
  "type": "Requirement",
  "summary": "文档包含作业要求和提交说明",
  "items": [
    {
      "filename": "material.pdf",
      "type": "Requirement",
      "summary": "文档包含作业要求和提交说明",
      "confidence": 0.95,
      "promptTokens": 1200,
      "completionTokens": 80,
      "totalTokens": 1280,
      "elapsedMs": 2500
    }
  ],
  "error": null,
  "createdAt": 1788912000,
  "updatedAt": 1788912004
}
```

任务状态：`queued`、`running`、`completed`、`failed`。失败时查看 `error`。

行为说明：

- `fileUrl` 必须是 HTTP/HTTPS 地址，可使用 OSS 预签名 URL；
- URL 查询参数用于下载，但不会保存进数据库；
- 默认禁止访问内网、环回和保留 IP；内网 OSS 必须设置 `TASK_ALLOW_PRIVATE_URLS=true`；
- 文件和压缩包使用与上传接口相同的 300/600 MB 和 199 文件限制；
- 分类强制使用大模型，未配置 `QWEN_API_KEY` 时任务失败；
- 单文件的 `summary` 是模型分类理由；压缩包含多个文件时，顶层 `type` 可能为 `Multiple`，详细结果在 `items`；
- 最小版本不自动重试，服务重启时未完成任务不会自动续跑。

相关配置：

```env
TASK_API_TOKEN=
TASK_DOWNLOAD_TIMEOUT=120
TASK_DOWNLOAD_CONCURRENCY=3
TASK_WORKER_CONCURRENCY=3
TASK_CLASSIFICATION_CONCURRENCY=5
TASK_ALLOW_PRIVATE_URLS=false
```

## 11. 健康检查

```http
GET /material-ai/health
```

```json
{
  "status": "ok"
}
```

该接口不需要登录。

旧路径 `GET /health` 继续保留兼容，但共享服务器应使用带独立前缀的新路径。

## 12. 通用错误码

| HTTP 状态码 | 含义 |
|---:|---|
| 400 | 参数、密码或文件格式无效 |
| 401 | 未登录、会话过期或上传 token 缺失 |
| 403 | 无权访问该资源、token 被篡改或客户归属不匹配 |
| 404 | 批次或资源不存在，跨教辅访问也返回此状态 |
| 409 | 客户编号重复或与正式客户冲突 |
| 410 | 上传链接已过期 |
| 413 | 文件、压缩包、解压体积或文件数超过限制 |
| 500 | 服务端内部错误 |
| 503 | MySQL、签名密钥或必要外部服务不可用 |

错误响应格式：

```json
{
  "detail": "错误原因"
}
```

## 13. 推荐前端调用流程

### 教辅工作台

```text
POST /api/auth/login
→ GET /api/auth/me
→ GET /api/staff/customers
→ POST /api/staff/access-links
→ GET /api/pending
→ POST /api/classify_pending
```

### 客户上传

```text
打开 /?token=...
→ POST /api/upload?token=...
→ 教辅登录后处理待分类批次
```

前端不得接收或保存 `QWEN_API_KEY`、`CUSTOMER_DB_PASSWORD`、`LINK_SIGNING_SECRET`、`ADMIN_PASSWORD` 等服务器凭据。
