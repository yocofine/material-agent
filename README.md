# material-agent —— 教辅「资料归集与核对」AI Agent

把「资料归集与核对 - 标准化流程图」落地为 **Python + LangGraph** 的确定性工作流，
只在「文件分类」一处引入 LLM（qwen3.7-plus），其余环节走确定性代码，可复现、可审计、可测试。

对应业务：教辅日常工作项目表单中的 **004 / 005 / 006「资料归集与核对」**。

## 流程五阶段

```
① 接收预处理   ② 解析分类      ③ 命名上传      ④ 核对反馈         ⑤ 异常处理
(ingest)      (classify)      (rename_upload)  (verify)          (exception)
  下载/解压      规则+LLM分类     模板重命名        核对清单           损坏/缺项/版本冲突
  建订单目录     落 Other待确认    调订单API上传     学员二次确认        状态记录
  完整性校验                                      human-in-the-loop
```

## 技术选型

| 层 | 选型 |
|----|------|
| 编排 | LangGraph（状态机 + interrupt 人机协作） |
| LLM | qwen3.7-plus（DashScope OpenAI 兼容接口） |
| 文档抽取 | PyMuPDF / python-docx / python-pptx / tesseract OCR |
| 存储 | SQLite（默认，可切 Postgres） |
| 订单系统 | REST 上传接口（`order_api.py`） |
| Agent 工具层 | FastMCP（`mcp_server.py`） |

## 目录结构

```
material-agent/
├── requirements.txt
├── .env.example
├── src/material_agent/
│   ├── constants.py      # 分类规则/命名模板/核对清单/异常类型（业务核心）
│   ├── models.py         # 数据模型
│   ├── state.py          # LangGraph 状态
│   ├── config.py         # 配置
│   ├── llm.py            # Qwen 客户端
│   ├── extraction.py     # 文档文本抽取
│   ├── classifier.py     # 规则 + LLM 分类
│   ├── order_api.py      # 订单系统客户端（需按真实接口对齐）
│   ├── store.py          # SQLite 持久化
│   ├── graph.py          # 状态机装配 + 运行/恢复
│   ├── nodes/            # 五个阶段节点
│   ├── mcp_server.py     # MCP tools
│   └── cli.py            # 命令行入口
└── examples/sample_order/  # 可跑的示例
```

## 快速开始

```bash
# 1. 安装（editable 安装后可直接用 material-agent 命令）
pip install -r requirements.txt
pip install -e .

# 2. 配置环境变量（复制后填写）
copy .env.example .env

# 3. 验证（两个离线测试，不依赖 LLM/订单系统）
python scripts/smoke_test.py   # 规则分类 + 完整状态机
python scripts/test_hitl.py    # human-in-the-loop 挂起/恢复

# 4. 跑通离线主干（分类走规则、上传走模拟）
material-agent run \
  --order-id O001 \
  --source examples/sample_order \
  --course-code ACCT1001 \
  --assignment-type Essay
```

## 关键设计说明

### 1. 分类分级（第②阶段）

1. 先按**文件名关键词**命中（syllabus / brief / rubric / sample / past paper …）
2. 文件名模糊时扫**正文首段**
3. 仍未命中 → **LLM 语义分类**（qwen3.7-plus）
4. LLM 低置信度或不可用 → 落 `Other` + `needs_confirmation=True`，交给第④阶段人工确认

规则表见 `constants.py` 的 `CATEGORY_RULES`，增删类别只需改这张表。

### 2. human-in-the-loop（第④阶段）

`verify` 节点通过 LangGraph 的 `interrupt()` 挂起，等学员确认后 `resume`。
缺必传项或存在模糊分类文件时触发；全部齐全则自动通过。

### 3. 异常分级（第⑤阶段）

- **阻塞型**（文件损坏/加密、缺必传项）→ 状态置 `exception`，记录日志，暂停等学生重发
- **非阻塞**（版本冲突、无法归类）→ 就地处理（归 Additional / 标待确认），流程继续

## 需要你补齐的部分

### ⚠️ 订单系统上传接口（唯一强依赖）

`order_api.py` 已按契约写好，但**接口路径/字段名需按你们真实系统替换**，集中在两个方法：

- `upload_attachment()` —— 上传附件（示例假设 `POST /orders/{order_id}/attachments`）
- `get_required_categories()` —— 拉取必传项配置（无此接口则返回 None，用本地默认）

未配置 `ORDER_API_BASE_URL` 时，上传走「模拟成功」，方便先离线验证其余流程。

### Qwen key

DashScope 控制台创建 API Key，填 `QWEN_API_KEY`。不填时分类降级为纯规则 + Other 待确认。

## MCP 使用

```bash
python -m material_agent.cli serve-mcp
```

暴露的 tools：`list_categories` / `classify_document` / `run_order` /
`get_order_status` / `confirm_order` / `get_verify_checklist`。
可被 Claude / Cursor / 任何 MCP 客户端挂载，作为「资料归集」子代理调用。

## 文件上传分类 API

不需要执行分类命令时，可以直接启动网页上传服务：

```bat
set PYTHONPATH=src
python -m material_agent.api_server
```

启动桌面端后，填写用户名称和有效时长生成专属链接；本机预览可点击桌面端的“打开上传页”。上传页左侧选择文件、右侧输入文字，服务端接收后暂存到 `INBOX_DIR`。

默认只监听本机 `127.0.0.1`。如果想让外网/局域网用户直接访问，启动前设置：

```bat
set API_HOST=0.0.0.0
```

公网访问由你自己的域名、服务器或反向代理提供。外部用户不能直接访问裸地址，必须使用管理员生成的带签名 token 的专属链接。

服务端需要分类时，打开本机管理页 `http://127.0.0.1:8000/admin`，点击「一键分类全部」，系统会处理 `INBOX_DIR` 中所有待分类上传，并把分类结果保存到 `ORGANIZED_DIR`。也可以在管理页修改 `ORGANIZED_DIR` 指定新的保存路径。

支持 PDF、DOCX、PPT、PPTX、TXT、MD、HTML、CSV、XLSX 和常见图片。普通文件单文件上限默认 1024 MB；单次上传压缩包原始体积合计不超过 300 MB，解压后文件合计不超过 600 MB 且最多 199 个。所有分类入口默认跳过规则、直接使用大模型；只有显式传入 `llm_only=false` 或 CLI 使用 `--use-rules` 才启用规则优先。外部接口 `POST /api/upload` 和兼容接口 `POST /api/classify` 都必须携带专属链接中的 `token`；登录教辅通过 `POST /api/staff/access-links` 生成 token，通过 `POST /api/classify_pending` 分类自己名下的批次。接口调试页面仍可在 `/docs` 查看。

### CRM 第三方 AI 分类接口（最小版）

Classbro CRM 可以使用自己生成的 `taskId`，一次提交多个课程和文件，然后主动轮询结果：

```http
POST /material-ai/api/classify
{"taskId":"a1b2c3d4e5f6","customerKey":"123456","courses":[...]}

GET /material-ai/api/result?taskId=a1b2c3d4e5f6
```

提交接口立即返回 `accepted=true`；查询接口返回 `PROCESSING`、`DONE` 或 `NOT_FOUND`，完成后按原 `fileId` 返回全量结果。相同 `taskId` 重复提交是幂等的。默认受控内网不鉴权，也可通过 `TASK_API_TOKEN` 另行启用 Bearer 鉴权；旧的 `/api/tasks` 单文件接口继续保留兼容。完整契约见 `docs/API_REFERENCE.md`。

## Linux 服务器部署（Docker Compose）

部署包包含 `Dockerfile`、`compose.yaml` 和 `Caddyfile`。Caddy 负责 HTTPS，应用及上传数据保存在 Docker 持久化卷中。推荐在项目根目录运行部署脚本：

```bash
chmod +x scripts/deploy_server.sh
bash scripts/deploy_server.sh
```

首次运行会依次询问域名、管理员用户名、管理员密码及 Qwen API Key。管理员密码由你静默输入并二次确认，脚本不会生成或打印密码；链接签名密钥由脚本单独随机生成。生产配置写入 `.env.production` 并设置为 `600` 权限。

更新代码后重复执行同一命令即可重建应用，Docker 数据卷不会被删除。只检查现有配置时使用：

```bash
bash scripts/deploy_server.sh --check
```

部署前需将域名 A/AAAA 记录指向服务器，并开放 TCP 80、TCP/UDP 443。部署完成后访问 `https://你的域名/login`，使用正式登录页进入系统。

权限模型：

- `.env.production` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 是超级管理员，可创建、启用或停用教辅账号并查看全部批次；
- 每位教辅登录后进入 `/staff`，可以修改自己的密码、查看正式客户与临时客户、生成链接，并且只能查看和分类自己的批次；
- 正式客户归属实时读取只读 MySQL 关系表，用教辅登录名匹配；临时客户由教辅在本系统创建并保存在 SQLite；
- 正式客户关系发生变化后，原教辅生成的链接会立即失效；
- 普通用户不需要账号或登录，直接使用带有效期的专属链接；
- 归档路径为 `ORGANIZED_DIR/<staff_id>/<customer_id>/<压缩包名>/<分类>`；
- 停用教辅账号后，该教辅无法登录，其此前生成且尚未过期的用户链接也会立即失效。

正式客户关系需要在 `.env.production` 配置只读 MySQL 变量：

```env
CUSTOMER_DB_HOST=mysql.example.internal
CUSTOMER_DB_PORT=3306
CUSTOMER_DB_NAME=业务数据库名
CUSTOMER_DB_USER=只读账号
CUSTOMER_DB_PASSWORD=只读账号密码
CUSTOMER_DB_TABLE=客户教辅关系表
CUSTOMER_ID_COLUMN=客户编号列
CUSTOMER_NAME_COLUMN=客户名称列
STAFF_USERNAME_COLUMN=教辅登录名列
```

表名和列名仅允许字母、数字和下划线；数据库账号只需授予该关系表的 `SELECT` 权限。

## 打包为 Windows 桌面应用（方案 A）

可以把服务端封装成单个桌面 `exe`，在自己的电脑上运行：

```bat
cd /d C:\Users\daobi\Desktop\11\material-agent
scripts\build_exe.bat
```

打包完成后会生成：

```text
dist\material-agent-desktop.exe
```

如果还需要生成安装包（setup），先安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)，然后运行：

```bat
scripts\build_setup.bat
```

会额外生成：

```text
dist\material-agent-setup.exe
```

安装包默认安装到当前用户目录，不需要管理员权限。

双击运行后，桌面窗口提供：

- 启动/停止本地服务；
- 打开外网上传页 `/`；
- 打开本机管理页 `/admin`；
- 一键分类全部；
- “全部使用大模型”选项；
- 手动选择 Tesseract OCR 路径；
- 手动选择解压软件目录（unrar / 7-Zip）；
- 设置分类保存路径；
- 设置管理页密码；
- 选择 LLM 供应商并填写 Token；
- 为不同用户生成带有效期的专属上传链接。

### 用户专属上传链接

公网连通由自己的 HTTPS 域名或反向代理负责，不再依赖 ngrok。桌面端负责生成访问凭证：

1. 填写公网服务地址，例如 `https://upload.example.com`；
2. 填写用户名称或编号；
3. 设置链接有效时长（最短 1 分钟，最长 365 天）；
4. 点击「生成用户专属链接」并发送给对应用户。

用户不需要注册或登录。服务端会验证链接里的签名和到期时间，并将上传批次记录到对应用户名下。不同用户、不同签发批次的 token 均不相同。

### 注意事项

- 打包后 `.env`、`desktop_config.json` 放在 exe 同目录。
- Tesseract OCR、unrar/7-Zip 是外部程序，不会被打进 exe，需在桌面端设置中手动选择路径。
- `ORGANIZED_DIR` / `INBOX_DIR` / `WORKSPACE_DIR` 也可以在桌面端设置。
- 管理页 `/admin` 已支持密码保护；外部用户只应收到带 token 的上传链接。
- `LINK_SIGNING_SECRET` 必须妥善保存；更换后，之前生成的所有链接都会立即失效。
- OCR 依赖的 Tesseract、RAR 解压用的 unrar 等外部程序不会被塞进 exe，需要另外安装。
- 如果只是内网/本机使用，也可以不打包，直接 `start_api.bat` 启动。

## 生产化建议

1. **持久化 checkpoint**：运行时默认使用 `SqliteSaver`，人工确认可以跨 CLI 进程恢复。
   多实例部署时建议改为共享的 `PostgresSaver`，并确保订单数据库与 checkpoint 数据库都使用
   同一套备份和访问控制策略。
2. **数据库**：当前订单与企微记录实现使用 SQLite；切换 PostgreSQL 需要实现并接入对应的
   store/checkpoint 适配器，不能只修改 `.env`。
3. **任务队列**：长下载/解压用 Celery/RQ 或 Temporal，Agent 层只触发与查询。
4. **可观测**：为每个订单记录 `current_stage` 与节点耗时，接入日志/监控。

## 原则提醒

> 这是**确定性工作流 + 局部 LLM**，不是自由 Agent。分类是唯一引入 LLM 的点，
> 且带「可拒绝」设计（低置信度落 Other 待人工）。第④阶段的学员确认是质量门禁，
> 不要自动替学员确认，否则责任归属会错。
