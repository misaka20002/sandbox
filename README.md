# Private Robot Sandbox

面向私人机器人和小群使用的开放式联网沙盒。项目现在以 Vercel Container Images 为主要部署目标，使用 `Dockerfile.vercel` 在 Fluid Compute 上运行 FastAPI、Shell、Python、Node.js、FFmpeg、ImageMagick 和常用图片处理库。

## 一键部署

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsyfantasy%2Fsandbox&env=SANDBOX_TOKEN&envDescription=%E8%AE%BF%E9%97%AE%E6%B2%99%E7%9B%92%E7%9A%84%20Bearer%20%E6%98%8E%E6%96%87%20Token%EF%BC%9B%E5%AE%A2%E6%88%B7%E7%AB%AF%E7%94%A8%20Authorization%3A%20Bearer%20%3Ctoken%3E%20%E6%90%BA%E5%B8%A6&envLink=https%3A%2F%2Fgithub.com%2Fsyfantasy%2Fsandbox%23%E9%83%A8%E7%BD%B2%E5%88%B0-vercel&project-name=sandbox&repository-name=sandbox)

点击后 Vercel 会克隆本仓库并进入导入流程，**提示你填写 `SANDBOX_TOKEN`**（明文，自定义一个强口令即可）。服务端启动时会自动对它做 SHA-256，客户端用 `Authorization: Bearer <你的口令>` 调用。Vercel 自动识别根目录的 `Dockerfile.vercel` 完成容器构建。

> 一键部署要求本仓库为 Public，且账号已开启 Container Images（Beta）。手动部署或轮换 Token 见[部署到 Vercel](#部署到-vercel)。

## 功能

- 执行任意 Shell 命令或 argv 命令
- 默认允许出站网络请求
- 使用单个 Bearer Token 鉴权
- 使用 `uv` 自动创建会话级 Python 环境
- 自动安装 Node.js 包
- 支持通过 URL 下载消息图片、音频和视频，避免占用 Vercel 请求体额度
- 普通 `/v1/exec` 接口继续以内联 Base64 返回小文件
- `/v1/exec-stream` 以 NDJSON 分块返回较大的图片、音频和视频，机器人端边接收边落盘
- 输出目录中的任意类型文件（txt/docx/pdf/xlsx/zip 等）都会带正确 MIME 流式返回，机器人端可作为文件发给用户
- 预装文档库（`python-docx`、`openpyxl`、`reportlab`、`pypdf`），可生成 Word/Excel/PDF 并作为文件下发
- 自动将 `/tmp/inputs`、`/tmp/outputs` 映射到当前会话，兼容模型习惯性的 `cd /tmp`
- 超时后终止整个进程组
- 限制并发、命令输出和返回文件体积
- 进程以 UID 1000 运行，不是 root

## Vercel 运行特性

Vercel 容器是无状态 Functions：生产实例连续 5 分钟没有流量后会缩容。实例仍然热着时，同一个 `session_id` 可以复用文件和动态依赖；实例缩容或迁移后这些内容会消失。

常用依赖已经写进镜像，不受缩容影响。动态 `pip`/`npm` 依赖只应作为临时补充。

Hobby 计划的重要限制：

- 2 GB RAM / 1 vCPU
- 单次请求最长 300 秒
- 请求或响应体最大 4.5 MB
- 大型 Functions Beta 最高支持 5 GB 未压缩内容

`Dockerfile.vercel` 因此默认限制：

```text
MAX_OUTPUT_BYTES=131072
MAX_FILE_OUTPUT_BYTES=2400000
MAX_STREAM_FILE_OUTPUT_BYTES=64000000
MAX_OUTPUT_FILES=4
MAX_CONCURRENT_JOBS=1
MAX_TIMEOUT_SECONDS=300
```

普通 `/v1/exec` 的全部返回文件应控制在 2.4 MB 内。配套 Chaite 工具使用
`/v1/exec-stream`，默认可流式返回合计 64 MB 的输出媒体；该上限可通过
`MAX_STREAM_FILE_OUTPUT_BYTES` 调整。

## 部署到 Vercel

1. 安装并登录 Vercel CLI：

   ```bash
   npm install -g vercel
   vercel login
   ```

2. 在项目设置中添加环境变量：

   ```text
   SANDBOX_TOKEN=<你的明文 Bearer Token>
   ```

   服务端启动时会自动对 `SANDBOX_TOKEN` 做 SHA-256，因此 Vercel 里只需填明文，不用手动算哈希。轮换 Token 只需改这个值并重新部署。

   `Dockerfile.vercel` 内置了一个默认 `SANDBOX_TOKEN_SHA256`，仅在未设置 `SANDBOX_TOKEN` 时作为回退。若你更想直接管理哈希，也可以改设 `SANDBOX_TOKEN_SHA256`（预计算摘要）覆盖镜像默认值；两者同时存在时以明文 `SANDBOX_TOKEN` 为准。

   如果部署时遇到镜像体积相关报错，可再补一个 `VERCEL_SUPPORT_LARGE_FUNCTIONS=1`。

3. 从项目根目录部署：

   ```bash
   vercel
   ```

   Vercel 会自动检测根目录的 `Dockerfile.vercel`。`.vercelignore` 会排除 Chaite 工具、工具示例、本地 Token 配置和测试文件，避免它们进入部署源码。

4. 获得 `https://YOUR-PROJECT.vercel.app` 地址后，写入：

   ```text
   chaite工具/PrivateSandbox.js → SANDBOX_API_URL
   ```

本地 Chaite 工具已经写入生成好的单 Token。该工具文件已加入 `.gitignore` 和 `.vercelignore`。

## Chaite 工具

工具文件：

```text
chaite工具/PrivateSandbox.js
```

新版工具会从当前消息和引用消息中提取图片、视频和音频。图片仍兼容
`geminidraw.js` 的引用解析方式：

1. 当前消息图片
2. `e.img`
3. 引用或回复消息
4. 没有图片时，被 @ 用户的头像

工具把远程媒体 URL 发给沙盒。沙盒分别下载到：

```text
inputs/reference_1.img
inputs/reference_2.img
inputs/media_1.mp4
inputs/media_2.mp3
```

路径列表同时存在环境变量：

```text
SANDBOX_INPUT_IMAGES
SANDBOX_INPUT_MEDIA
SANDBOX_INPUT_FILES
```

命令应把结果保存到 `outputs/`，并使用正确扩展名，便于服务端识别 MIME 类型。
新版工具通过流式接口把媒体写入机器人本机临时文件，然后执行：

```js
await e.reply(segment.image(localPath))
await e.reply(segment.video(localPath))
await e.reply(segment.record(localPath))
```

工具执行命令前会创建以下映射：

```text
/tmp/inputs  -> 当前会话/inputs
/tmp/outputs -> 当前会话/outputs
```

因此模型即使先执行 `cd /tmp`，再保存 `outputs/result.mp4`，服务端也能正常收集并发送媒体。Vercel 单实例并发设为 1，避免不同任务同时修改这两个兼容映射。

建议输出格式：图片使用 JPEG/WebP，音频使用 MP3，视频使用 H.264/AAC MP4。
`Dockerfile.vercel` 已预装 FFmpeg，可直接进行转码。

## API 调用

```bash
curl -X POST 'https://YOUR-PROJECT.vercel.app/v1/exec' \
  -H 'Authorization: Bearer YOUR_SANDBOX_TOKEN' \
  -H 'Content-Type: application/json' \
  --data '{
    "session_id": "robot-task-001",
    "command": "curl -sS https://httpbin.org/get | jq .headers",
    "timeout_seconds": 60
  }'
```

### 图片处理

URL 输入不会占用请求体的大量空间：

```json
{
  "session_id": "robot-image-001",
  "command": "mkdir -p outputs && python -c 'from PIL import Image; im=Image.open(\"inputs/source.img\"); im.thumbnail((1024,1024)); im.convert(\"RGB\").save(\"outputs/result.webp\", quality=82)'",
  "input_urls": [
    {
      "path": "inputs/source.img",
      "url": "https://example.com/source.png"
    }
  ],
  "reset_paths": ["inputs", "outputs"],
  "output_files": ["outputs/*"]
}
```

仍然支持 `input_files[].content_base64`，但不建议在 Vercel 上用它传大图片。

### 流式返回音频/视频

`POST /v1/exec-stream` 接收与 `/v1/exec` 相同的请求体，响应类型为
`application/x-ndjson`：

1. `result` 事件包含执行结果和输出文件元数据，不包含文件 Base64
2. `file_chunk` 事件按文件索引分块传输 Base64 内容
3. `end` 事件表示所有文件传输完成

配套的 `sandbox-新.js` 会持续读取这些事件，把媒体写入临时文件，再通过
`segment.image`、`segment.video` 或 `segment.record` 回复用户。整个媒体传输发生在
同一次 Vercel 请求内，不依赖后续请求命中同一个临时容器实例。

### 动态 Python 依赖

```json
{
  "python_packages": ["cloudscraper", "selectolax"],
  "command": "python -c 'import cloudscraper; print(cloudscraper.create_scraper().get(\"https://example.com\").status_code)'"
}
```

动态依赖仅在当前热实例中复用。经常使用的包应加入 `requirements.txt` 后重新部署。

## 预装工具

- Python 3.12、Node.js、npm、`uv`
- curl、wget、git、jq、OpenSSL、DNS 工具
- GCC/G++、make、pkg-config
- FFmpeg、ImageMagick
- Chromium、Puppeteer Core、轻量中文字体和彩色 Emoji 字体
- Pillow、OpenCV Headless、scikit-image、imageio、matplotlib
- NumPy、Pandas、httpx、requests、aiohttp

## 网页截图

镜像内置 Chromium、Puppeteer Extra、常用字体和网页截图脚本。通过通用执行
接口运行：

```bash
node /app/tools/web_capture.mjs 'https://example.com' \
  --output outputs/page.png --full-page --wait-ms 2000
```

默认使用当前会话下的 `.browser-profile`，因此同一热实例、同一 `session_id`
可以复用 cookies 和站点状态。也可以通过 `--cookies inputs/cookies.json` 导入
已获授权的 Puppeteer Cookie 数组。

Cloudflare 等验证依赖网站策略、出口 IP 和浏览器环境，服务端无可靠且合规的
通用绕过方式。推荐让用户在正常浏览器中完成验证后导出获授权的 cookies，或
改用站点官方 API；不要依赖修改指纹或自动解验证码。

图片和 GIF 可使用预装的 FFmpeg、ImageMagick、Pillow 或内置脚本处理：

```bash
python /app/tools/media_edit.py flip-horizontal inputs/source.gif outputs/result.gif
python /app/tools/media_edit.py flip-vertical inputs/source.png outputs/result.png
python /app/tools/media_edit.py reverse inputs/source.gif outputs/reversed.gif
```

脚本处理 GIF 时会先读取合成后的完整帧，再重新编码并设置 disposal，避免差分帧
翻转或倒放后出现残影。

## 本地运行与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
set -a
. ./.env.vercel.local
set +a
uvicorn app.main:app --host 127.0.0.1 --port 7860
```

```bash
python3 -m unittest discover -s tests -v
node --check 'chaite工具/PrivateSandbox.js'
```

## 接口

- `GET /healthz`：健康状态
- `GET /docs`：FastAPI 接口文档
- `POST /v1/exec`：执行命令，需要 Bearer Token
- `POST /v1/exec-stream`：执行命令并分块返回较大的输出文件，需要 Bearer Token
- `DELETE /v1/sessions/{session_id}`：清理当前实例中的会话目录

## 运行边界

这是一个有意开放的远程命令执行器。应用不分析或过滤具体命令，只保留 Token 鉴权、非 root 用户、超时、并发和响应体限制。不要把数据库密码、机器人 Token、云服务写入凭据等秘密放进执行容器的环境变量。
