
# 小雅同步元数据

高速异步爬虫从 https://emby.xiaoya.pro/ 同步小雅元数据
##### [FAQ](爬虫FAQ.md)

---


## Prerequisite

Python 3.12+

Linux kernal "Max open files" need to be extended

Save it into /etc/profile.d/ to make it global avaiable after reboot

```bash
ulimit -n 4096
```
---

For Network mount, please add 'nolock' option. See [Issue#47](https://github.com/Rik-F5/xiaoya_db/issues/47#issuecomment-2162641424)

## Docker Run

**docker-cli**

```shell
docker run -d \
    --name=xiaoya-emd \
    --restart=always \
    --net=host \
    -v 媒体库目录:/media \
    -e CYCLE=86400 \
    -e RESTART_AUTO_UPDATE=false \
    ddsderek/xiaoya-emd:latest \
    --media /media
```

**docker-compose**

```yaml
version: "3"
services:
    xiaoya-emd:
        container_name: xiaoya-emd
        restart: always
        network_mode: host
        volumes:
            - 媒体库目录:/media
        environment:
            - CYCLE=86400
            - RESTART_AUTO_UPDATE=false
        image: ddsderek/xiaoya-emd:latest
        command: --media /media
```

## 容器代理配置（HTTP / mixed）

支持在部署容器时通过标准环境变量设置 HTTP 代理，无需将地址写入代码：

```yaml
environment:
    CYCLE: "43200"
    RESTART_AUTO_UPDATE: "false"
    TZ: Asia/Shanghai
    HTTP_PROXY: "http://192.168.1.10:7890"
    HTTPS_PROXY: "http://192.168.1.10:7890"
    NO_PROXY: "localhost,127.0.0.1,::1"
```

将此配置合并到自己的镜像服务中，地址替换为容器能够访问的代理 IP 和 HTTP / mixed 端口。
`HTTPS_PROXY` 表示访问 HTTPS 网站时使用的代理，其值仍然可以是 `http://` 地址，
HTTPS 请求通过 HTTP CONNECT 隧道转发。此配置不支持仅提供 SOCKS5 的端口。

使用 `docker run` 时，在镜像名称之前添加：

```bash
-e HTTP_PROXY=http://192.168.1.10:7890 \
-e HTTPS_PROXY=http://192.168.1.10:7890 \
-e NO_PROXY=localhost,127.0.0.1,::1 \
```

服务器列表获取、镜像探测、远端文件清单、目录扫描和文件下载均遵循代理设置。
未配置代理时保持原有连接方式；`NO_PROXY` 中列出的主机和域名直接连接，
例如 `localhost,127.0.0.1,.example.com`。不要将需要代理的小雅域名加入 `NO_PROXY`。
使用 `host` 网络且代理运行在 NAS 本机时，可使用 `127.0.0.1`；
代理运行在其他设备时，应填写该设备可访问的局域网 IP。

如代理需要认证，可使用 `http://用户名:密码@代理地址:端口`，用户名和密码中的特殊字符需要 URL 编码。
代理地址和凭据应在部署时配置，不要提交到仓库或写入镜像。
这些环境变量配置的是容器内程序的代理；NAS 拉取镜像的代理需在 Docker 服务侧另行配置。
新功能需要构建包含本次代码的自用镜像后生效。

## 同步完成后刷新 Emby

爬虫可在本轮同步、数据库处理和过期文件清理全部结束后，向 Emby 提交媒体库刷新请求。
仅当本轮至少成功下载一个文件，且目录扫描和文件下载没有报错时触发；
零下载、仅删除过期文件、同步出错或提前退出时不触发。
刷新失败会单独记录日志，不撤销已经下载的文件，也不会在本轮自动重试。
API 接受请求后由 Emby 异步扫描，爬虫不会等待媒体库扫描完成。

仓库提供的 `compose.yaml` 默认启用此功能，在同目录的 `.env` 中配置：

```dotenv
EMBY_REFRESH_ENABLED=true
EMBY_URL=http://127.0.0.1:2345/emby
XIAOYA_DATA_DIR=/vol1/1000/xiaoya
```

`XIAOYA_DATA_DIR` 填写 `xiaoya` 容器 `/data` 对应的 NAS 目录，可以通过以下只读命令确认：

```bash
docker inspect xiaoya --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

Compose 将该目录只读挂载到爬虫的 `/xiaoya-data`，并设置
`EMBY_API_KEY_FILE=/xiaoya-data/infuse_api_key.txt`。
爬虫每次准备刷新时重新打开文件读取密钥，不需要在 `.env` 中填写或 URL 编码 API Key。
挂载整个目录可在密钥文件被替换后继续读取新文件；爬虫不会写入这个目录。
如果文件缺失、为空或不可读，本轮刷新会记录失败，已经下载的元数据仍然保留。
`.env` 已加入 Git 忽略规则，不要把代理凭据或真实密钥提交到仓库。
单独运行 Python 或 `docker run` 时，需显式设置 `EMBY_REFRESH_ENABLED=true`，
并配置密钥文件挂载和 `EMBY_API_KEY_FILE`。

默认按名称精确匹配以下媒体库，与 `refresh_media.sh` 的列表一致：

- 电视剧/每日更新
- 电影/每日更新
- 动漫/每日更新
- 纪录片
- 动漫/每日更新 剧场版
- 综艺

依次调用 `Library/VirtualFolders` 和 `Items/{ItemId}/Refresh`，
参数为 `Recursive=true`、`MetadataRefreshMode=Default`、`ReplaceAllMetadata=false`。
密钥通过 `X-Emby-Token` 请求头传递，日志不输出密钥。
Emby 请求使用单独的直连会话，不读取爬虫的代理环境变量。

需要自定义媒体库时，在容器环境变量中配置 JSON 数组：

```yaml
EMBY_LIBRARY_NAMES: '["电影/每日更新", "电视剧/每日更新"]'
```

源码也支持用 `EMBY_API_KEY` 直接传入密钥；如果同时设置，优先使用它。
默认 Compose 只传入 `EMBY_API_KEY_FILE`，确保每轮从文件读取当前密钥。
设置 `EMBY_REFRESH_ENABLED=false` 可关闭自动刷新。

容器构建会从当前构建目录复制 `solid.py`、`emby_refresh.py`、`sync_logging.py` 和 `requirements.txt`，
使本地修改进入镜像。自动更新的默认仓库为 `Clowrain/xiaoya_db`，
可以通过构建参数 `REPO_URL`、`BRANCH` 调整；自用版本建议保持 `RESTART_AUTO_UPDATE=false`。

## 查看版本、进度与代理路由

启动日志会显示程序版本、构建提交、构建时间和隐藏账号密码后的代理地址。
每个目标主机首次请求时，会显示是否连接 HTTP 代理，或因 `NO_PROXY` 等原因选择 `DIRECT`。
连接到 Mihomo 后，最终出站节点仍由 Mihomo 的规则决定，需要从其连接记录核对。

扫描期间默认每 15 秒输出一次目录数、已发现文件数、下载数、无需下载数、错误数、
进行中的请求数量，以及当前最久请求的地址和耗时，便于区分等待网络与正常扫描。
同步结束后单独报告过期清理状态：远端清单与扫描数量不一致而跳过清理时，
会明确显示差值，不再只显示“0 errors”。

可通过容器环境变量调整日志间隔和异步请求超时（单位为秒，必须大于零）：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `PROGRESS_INTERVAL` | `15` | 扫描进度日志间隔 |
| `CONNECT_TIMEOUT` | `15` | 单次请求连接超时 |
| `READ_TIMEOUT` | `60` | 单次请求连续没有收到数据的超时 |
| `REQUEST_TIMEOUT` | `300` | 单次请求总超时，包含响应读取 |

镜像探测设置 15 秒超时，远端清单读取设置 30 秒的 socket 超时。
这些设置限制单次请求的等待时间，不限制整个同步任务的总时长。

## 发布自用镜像到 GitHub Packages

推送到 `main` 分支后，GitHub Actions 会先运行测试，再构建并发布：

```text
ghcr.io/clowrain/xiaoya-emd:latest
ghcr.io/clowrain/xiaoya-emd:sha-<提交短哈希>
```

镜像包含 `linux/amd64`、`linux/arm64/v8` 和 `linux/arm/v7` 三种架构。
工作流通过自带的 `GITHUB_TOKEN` 获取 `packages: write` 权限，无需 Docker Hub 凭据。
也可手动运行 `release` 工作流；推送 `v*` 标签会额外发布对应镜像标签及二进制 Release。
手动运行 `beta` 工作流会从所选分支构建并发布 `beta` 标签，不覆盖 `latest`。

NAS 部署使用仓库提供的 `compose.yaml` 和 `.env.example`。
如果 GitHub Package 为私有，需要先用有 `read:packages` 权限的凭据登录 `ghcr.io`；
公开 Package 可以匿名拉取。

## Installation

#### Clone the project

```bash
  git clone https://github.com/Rik-F5/xiaoya_db
```

#### Go to the project directory

```bash
  cd xiaoya_db
```

#### Switch to venv

```bash
  python3 -m venv .venv
  source .venv/bin/activate
```

#### Install modules

```bash
  python -m pip install -r requirements.txt
```
---
## Run Locally with venv activated

```bash
python solid.py --media <Xiaoya Folder>

```
---
## Usage/Examples

Download the metadata without replace the local NFOs
```bash
python solid.py --media <folder>
```

Download the metadata and replace the local NFOs if they are out of date or different

```bash
python solid.py --media <folder> --nfo true
```

Download the metadata and save the file list to a local DB (Python3.12+)

```bash
python solid.py --media <folder> --db true
```

Do not download any files. For testing or benchmark only.

```bash
python solid.py
```
---
## Parameters

```bash
  -h, --help           show this help message and exit

  --media <folder>     Path to store downloaded media files [Default: None]

  --count [number]     Max concurrent HTTP Requests [Default: 100]

  --debug, --no-debug  Verbose debug [Default: False]

  --db, --no-db        <Python3.12+ required> Save into DB [Default: False]

  --nfo, --no-nfo      Download NFO [Default: False]

  --url [url]          Download path [Default: None]

  --purge, --no-purge  Purge removed files [Default: True]

  --all, --no-all      Download all folders [Default: False]

  --location <folder>  Path to store database files [Default: None]

  --paths <file>       Bitmap of paths or a file containing paths to be selected (See paths.example)
···
