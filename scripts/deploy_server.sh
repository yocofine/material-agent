#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env.production}"

info() {
  printf '[material-agent] %s\n' "$*"
}

fail() {
  printf '[material-agent] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：bash scripts/deploy_server.sh [--env-file PATH] [--check]

  --env-file PATH  使用指定的生产环境变量文件
  --check          只检查配置，不启动或更新服务
  -h, --help       显示帮助
EOF
}

CHECK_ONLY=false
while (($#)); do
  case "$1" in
    --env-file)
      (($# >= 2)) || fail "--env-file 缺少路径"
      ENV_FILE="$2"
      shift 2
      ;;
    --check)
      CHECK_ONLY=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "未知参数：$1"
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || fail "未找到 Docker，请先安装 Docker Engine"
docker compose version >/dev/null 2>&1 || fail "未找到 docker compose 插件"
docker info >/dev/null 2>&1 || fail "无法连接 Docker daemon，请启动 Docker 或检查当前用户权限"

for required_file in Dockerfile compose.yaml Caddyfile .env.production.example; do
  [[ -f "${PROJECT_DIR}/${required_file}" ]] || fail "缺少 ${required_file}"
done

env_quote() {
  local value="$1"
  [[ "$value" != *"'"* ]] || fail "配置值不能包含单引号"
  printf "'%s'" "$value"
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -d '\r\n'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
  else
    head -c 48 /dev/urandom | base64 | tr -d '\r\n'
  fi
}

create_env_file() {
  [[ -t 0 ]] || fail "首次部署需要交互输入配置，请在终端中运行脚本"

  local domain admin_username admin_password password_confirm qwen_key link_secret
  read -r -p "公网域名（例如 upload.example.com）：" domain
  [[ "$domain" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] \
    || fail "域名格式不正确"
  [[ "$domain" == *.* ]] || fail "请输入完整公网域名"

  read -r -p "超级管理员用户名 [admin]：" admin_username
  admin_username="${admin_username:-admin}"
  [[ "$admin_username" =~ ^[A-Za-z0-9._@-]{3,64}$ ]] \
    || fail "管理员用户名只能包含字母、数字及 . _ - @，长度 3-64"

  read -r -s -p "请设置超级管理员密码（至少 12 位）：" admin_password
  printf '\n'
  ((${#admin_password} >= 12)) || fail "管理员密码至少需要 12 位"
  read -r -s -p "请再次输入管理员密码：" password_confirm
  printf '\n'
  [[ "$admin_password" == "$password_confirm" ]] || fail "两次输入的管理员密码不一致"

  read -r -s -p "Qwen API Key（可留空，留空时仅使用规则分类）：" qwen_key
  printf '\n'
  link_secret="$(generate_secret)"
  [[ ${#link_secret} -ge 32 ]] || fail "无法生成链接签名密钥"

  umask 077
  {
    printf 'DOMAIN=%s\n' "$domain"
    printf 'PUBLIC_BASE_URL=https://%s\n' "$domain"
    printf 'ADMIN_USERNAME=%s\n' "$admin_username"
    printf 'ADMIN_PASSWORD=%s\n' "$(env_quote "$admin_password")"
    printf 'LINK_SIGNING_SECRET=%s\n' "$(env_quote "$link_secret")"
    printf '%s\n' 'STAFF_SESSION_HOURS=12'
    printf '%s\n' 'TASK_API_TOKEN='
    printf '%s\n' 'TASK_DOWNLOAD_TIMEOUT=120'
    printf '%s\n' 'TASK_DOWNLOAD_CONCURRENCY=3'
    printf '%s\n' 'TASK_WORKER_CONCURRENCY=3'
    printf '%s\n' 'TASK_CLASSIFICATION_CONCURRENCY=5'
    printf '%s\n' 'TASK_ALLOW_PRIVATE_URLS=false'
    printf 'QWEN_API_KEY=%s\n' "$(env_quote "$qwen_key")"
    printf '%s\n' 'QWEN_MODEL=qwen3.7-plus'
    printf '%s\n' 'QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1'
    printf '%s\n' 'QWEN_TIMEOUT=60'
    printf '%s\n' 'EXTRACT_TIMEOUT=60'
    printf '%s\n' 'CLASSIFICATION_SNIPPET_CHARS=4000'
    printf '%s\n' 'API_MAX_FILE_MB=1024'
    printf '%s\n' 'API_MAX_ARCHIVE_MB=300'
    printf '%s\n' 'API_MAX_EXTRACTED_MB=600'
    printf '%s\n' 'API_MAX_EXTRACTED_FILES=199'
    printf '%s\n' 'DATABASE_URL=sqlite:////app/data/material_agent.db'
    printf '%s\n' 'WORKSPACE_DIR=/app/data/workspace'
    printf '%s\n' 'INBOX_DIR=/app/data/inbox'
    printf '%s\n' 'ORGANIZED_DIR=/app/data/organized'
    printf '%s\n' 'CUSTOMER_DB_HOST='
    printf '%s\n' 'CUSTOMER_DB_PORT=3306'
    printf '%s\n' 'CUSTOMER_DB_NAME='
    printf '%s\n' 'CUSTOMER_DB_USER='
    printf '%s\n' 'CUSTOMER_DB_PASSWORD='
    printf '%s\n' 'CUSTOMER_DB_TABLE='
    printf '%s\n' 'CUSTOMER_ID_COLUMN='
    printf '%s\n' 'CUSTOMER_NAME_COLUMN='
    printf '%s\n' 'STAFF_USERNAME_COLUMN='
    printf '%s\n' 'ORDER_API_BASE_URL='
    printf '%s\n' 'ORDER_API_TOKEN='
  } >"$ENV_FILE"
  chmod 600 "$ENV_FILE"
  info "已创建生产配置：${ENV_FILE}（权限 600）"
}

read_env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^'//" -e "s/'$//" -e 's/^"//' -e 's/"$//'
}

validate_env_file() {
  local domain public_base admin_username admin_password link_secret
  domain="$(read_env_value DOMAIN)"
  public_base="$(read_env_value PUBLIC_BASE_URL)"
  admin_username="$(read_env_value ADMIN_USERNAME)"
  admin_password="$(read_env_value ADMIN_PASSWORD)"
  link_secret="$(read_env_value LINK_SIGNING_SECRET)"

  [[ -n "$domain" && "$domain" != *example.com* ]] || fail "请在 ${ENV_FILE} 中配置真实 DOMAIN"
  [[ "$public_base" == "https://${domain}" ]] \
    || fail "PUBLIC_BASE_URL 必须为 https://${domain}"
  [[ -n "$admin_username" ]] || fail "ADMIN_USERNAME 不能为空"
  ((${#admin_password} >= 12)) || fail "ADMIN_PASSWORD 至少需要 12 位"
  ((${#link_secret} >= 32)) || fail "LINK_SIGNING_SECRET 至少需要 32 字节"
}

cd "$PROJECT_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  create_env_file
else
  info "复用现有生产配置：${ENV_FILE}"
fi
validate_env_file

export ENV_FILE
COMPOSE=(docker compose --env-file "$ENV_FILE")
"${COMPOSE[@]}" config -q
info "Docker Compose 配置检查通过"

if [[ "$CHECK_ONLY" == true ]]; then
  exit 0
fi

info "开始构建并重建服务，重新加载 ${ENV_FILE}，现有数据库和上传文件会保留"
"${COMPOSE[@]}" pull caddy
"${COMPOSE[@]}" up -d --build --force-recreate --remove-orphans

container_id="$("${COMPOSE[@]}" ps -q app)"
[[ -n "$container_id" ]] || fail "应用容器未创建"

health=""
for _ in $(seq 1 30); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  [[ "$health" == "unhealthy" || "$health" == "exited" || "$health" == "dead" ]] && break
  sleep 2
done

if [[ "$health" != "healthy" ]]; then
  "${COMPOSE[@]}" logs --tail=100 app >&2 || true
  fail "应用健康检查未通过，当前状态：${health:-unknown}"
fi

caddy_id="$("${COMPOSE[@]}" ps -q caddy)"
[[ -n "$caddy_id" ]] || fail "Caddy 容器未创建"
caddy_running="$(docker inspect --format '{{.State.Running}}' "$caddy_id" 2>/dev/null || true)"
if [[ "$caddy_running" != "true" ]]; then
  "${COMPOSE[@]}" logs --tail=100 caddy >&2 || true
  fail "Caddy HTTPS 服务未正常运行"
fi

domain="$(read_env_value DOMAIN)"
info "部署完成"
printf '\n发起分类：https://%s/material-ai/api/classify\n' "$domain"
printf '查询结果：https://%s/material-ai/api/result?taskId=...\n' "$domain"
printf '健康检查：https://%s/material-ai/health\n' "$domain"
printf '请确认域名已解析到本机，并已开放 TCP 80、TCP/UDP 443。\n'
