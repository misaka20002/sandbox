# Private Robot Sandbox

面向私人机器人和小群的远程联网命令沙箱。服务通过 Docker Compose
长驻运行，使用持久卷保存会话，支持 Shell、Python、Node.js、Chromium、
FFmpeg、ImageMagick、附件输入和文件输出。

## 运行模型

- Docker 容器是宿主机隔离边界，命令以容器内 UID 1000 执行。
- 会话目录保存在 `sandbox_sessions` 持久卷中，容器重启后仍可恢复。
- 同一会话串行执行，不同会话可在全局并发限制内并行。
- `inputs/`、`outputs/`、`.tmp/` 每次调用前清空。
- 其他文件，包括 `.venv`、`node_modules`、`.home` 和
  `.browser-profile`，保留到会话闲置过期。
- 服务端每分钟自动清理闲置会话，不依赖机器人在线或外部 Cron。

这是一个有意开放的远程命令执行器。它不会分析或过滤具体命令，只保留
Bearer Token、容器边界、非 root 用户、超时、并发、输出和磁盘限制。
默认只应向机器人的主人开放。

## Docker Compose 部署

1. 生成强随机 Bearer Token，并计算 SHA-256：

   ```bash
   TOKEN="$(openssl rand -hex 32)"
   printf '%s' "$TOKEN" | sha256sum
   ```

   保存第一行明文 Token，机器人调用时需要它。只把摘要写入服务端 `.env`：

   ```bash
   cp .env.example .env
   ```

   ```dotenv
   SANDBOX_TOKEN_SHA256=<64位SHA-256摘要>
   SESSION_RETENTION_MINUTES=60
   MAX_SESSION_BYTES=268435456
   SANDBOX_PORT=7860
   ```

2. 构建并启动：

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

3. 检查健康状态：

   ```bash
   curl http://<沙箱服务器IP>:7860/healthz
   ```

Compose 默认监听宿主机所有网络接口，其他服务器可以通过
`http://<沙箱服务器IP>:<SANDBOX_PORT>` 访问。生产环境建议使用防火墙限制来源，
或通过 Caddy、Nginx、VPN、受控内网提供 HTTPS，不要把任意命令执行接口直接暴露到公网。

更新服务不会删除会话：

```bash
docker compose up -d --build
```

只有显式执行 `docker compose down -v` 才会删除持久会话卷。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `SANDBOX_TOKEN_SHA256` | 无 | Bearer Token 的 SHA-256，Compose 必填 |
| `SANDBOX_SESSION_ROOT` | `/tmp/sandbox-sessions` | 会话根目录；Compose 固定为 `/data/sessions` |
| `SESSION_RETENTION_MINUTES` | `60` | 闲置保留分钟数，范围 1–1440 |
| `MAX_SESSION_BYTES` | `268435456` | 单会话最大磁盘占用 |
| `MAX_CONCURRENT_JOBS` | `2` | 不同会话的全局并发数 |
| `DEFAULT_TIMEOUT_SECONDS` | `120` | 默认命令超时 |
| `MAX_TIMEOUT_SECONDS` | `300` | 最大命令超时 |
| `MAX_OUTPUT_BYTES` | `2000000` | stdout 与 stderr 合计上限 |
| `MAX_INPUT_BYTES` | `20000000` | 单次输入附件合计上限 |
| `MAX_FILE_OUTPUT_BYTES` | `20000000` | 普通接口返回文件合计上限 |
| `MAX_STREAM_FILE_OUTPUT_BYTES` | `64000000` | 流式接口返回文件合计上限 |
| `MAX_OUTPUT_FILES` | `8` | 单次最多返回文件数 |

## 会话接口

`POST /v1/exec` 与 `POST /v1/exec-stream` 使用相同请求结构。

创建新会话：

```json
{
  "command": "printf 'hello' > project.txt",
  "new_session": true,
  "owner_key": "<群/私聊范围与用户的SHA-256>"
}
```

继续会话：

```json
{
  "command": "cat project.txt",
  "session_id": "服务器返回的UUID",
  "owner_key": "<同一个owner_key>"
}
```

替换当前会话：

```json
{
  "command": "printf 'new task' > project.txt",
  "new_session": true,
  "replace_session_id": "需要永久删除的当前会话UUID",
  "owner_key": "<同一个owner_key>"
}
```

`new_session=true` 不得与 `session_id` 同时使用。替换只删除
`replace_session_id` 指定的当前会话，不影响同一用户的其他历史会话。

响应包含：

- `session_id`
- `session_created`
- `replaced_session_id`
- `replaced_session_removed`
- `expires_at`（Unix 秒）
- 命令退出码、stdout、stderr、输出文件及截断信息

旧客户端仍可只传 `session_id`；这种请求继续使用旧的单 Token 会话模式。

## 工作目录与附件

命令默认在会话根目录执行。以下环境变量始终存在：

```text
SANDBOX_SESSION_ID
SANDBOX_SESSION_DIR
SANDBOX_INPUT_DIR
SANDBOX_OUTPUT_DIR
SANDBOX_INPUT_IMAGES
SANDBOX_INPUT_MEDIA
SANDBOX_INPUT_FILES
SANDBOX_CHROMIUM
TMPDIR
```

可继续编辑的源文件必须保存在会话根目录或子目录中。需要发送给用户的文件复制到
`outputs/`；因为 `outputs/` 会在下次调用前清空，不能把它当作项目源目录。

网页截图示例：

```bash
cp page.html outputs/page.html
"$SANDBOX_CHROMIUM" --headless=new --no-sandbox \
  --disable-dev-shm-usage --window-size=1440,1000 \
  --screenshot=outputs/page.png "file://$PWD/page.html"
```

也可以使用镜像内置脚本：

```bash
node /app/tools/web_capture.mjs 'https://example.com' \
  --output outputs/page.png --full-page --wait-ms 2000
```

## 接口

- `GET /healthz`：健康状态、会话保留时间和下一次过期时间
- `GET /docs`：FastAPI OpenAPI 文档
- `POST /v1/exec`：执行命令并以内联 Base64 返回小文件
- `POST /v1/exec-stream`：以 NDJSON 分块返回较大文件
- `DELETE /v1/sessions/{session_id}`：兼容旧客户端的显式会话删除

所有执行和删除接口都需要：

```http
Authorization: Bearer <明文Token>
```

## 本地测试

测试需要 Linux，因为执行器固定使用 `/bin/bash` 和 POSIX 进程组：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
