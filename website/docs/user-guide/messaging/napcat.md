# NapCat（QQ via OneBot 11）

通过 [**NapCatQQ**](https://github.com/NapNeko/NapCatQQ) 把 Hermes 接到 QQ —— NapCat 是一个实现了 **OneBot 11** 协议的第三方 QQ 客户端。借助它你可以让 Hermes 以**反向 WebSocket**的方式驱动一个普通的个人 QQ 账号（不是 QQ 机器人开放平台 APPID），这也是家庭/内网场景里最常见的部署形态。

> 如果你**只想自动化一个已登录的个人 QQ**，并且没有官方机器人资质，就用 NapCat。如果你已经申请到官方 QQ Bot 应用，请看 [QQ Bot](./qqbot.md)。

## 工作原理

```
┌──────────────┐   反向 WS     ┌───────────────┐
│   NapCatQQ   │  ───────────▶ │    Hermes     │
│ （QQ 客户端） │               │    gateway    │
└──────────────┘   OneBot 11   └───────────────┘
        ▲                              │
        │ 登录                          │ send_private_msg /
        ▼                              │ send_group_msg
     QQ 账号                            ▼
                                     Agent 回复
```

- NapCat 与 QQ 客户端一起运行（通常在登录了 QQ 的 Windows 机器上）。
- 它主动向 Hermes 发起一条出站 WebSocket 连接，并用一个共享 token 做鉴权。**QQ 侧不需要公网 IP**。
- Hermes 在 `NAPCAT_HOST:NAPCAT_PORT` 上监听，路径默认 `/napcat/ws`；token 匹配即升级为 WebSocket。
- OneBot 11 的消息事件会被翻译成 Hermes 的 `MessageEvent`，走正常的 agent 回路，回复通过同一条 WebSocket 以 `send_private_msg` / `send_group_msg` 发回。

### 支持的能力

| 能力                       | 状态 |
|----------------------------|------|
| 好友私聊                   | ✅ 文字进、文字出 |
| 群聊                       | ✅ 需要 `@机器人`，分发前会剥掉 `@` 段 |
| 回复串联                   | ✅ `reply_to` 会映射为 OneBot 的 `reply` 段 |
| 引用即 @                    | ✅ 回复机器人之前发的消息也算被 @ |
| 多段长消息                 | ✅ 超长回复会被切片发送 |
| 出站 echo 关联             | ✅ 使用 `echo` 做请求/响应匹配 |
| 语音 / 图片 / 文件         | 🛈 入站暂时只解析文字；富媒体转发后续再补 |

## 先决条件

1. 一个已登录 QQ 的 **NapCatQQ** 实例。按 [官方安装指南](https://napcat.napneko.icu/) 部署，Windows 壳启动器最简单。
2. NapCat → Hermes 的网络可达：同机、同局域网、或 VPN/Tailscale 都可以，NapCat 只要能连到 Hermes 的 WebSocket 端口即可。
3. Hermes 依赖：`aiohttp`（已包含在 `messaging` extra 里）。

## 配置

### 交互式引导

```bash
hermes setup gateway
```

在清单里勾选 **NapCat (QQ via OneBot 11)**，向导会：

- 生成强随机的 `NAPCAT_TOKEN`（或让你粘贴已有的）
- 询问监听 host / port / path
- 询问用户白名单和 home channel
- 把配置写入 `~/.hermes/.env`

### 手工配置

在 `~/.hermes/.env` 里添加：

```bash
NAPCAT_ENABLED=true
NAPCAT_TOKEN=pick-a-long-random-string
NAPCAT_HOST=0.0.0.0            # 同机就用 127.0.0.1
NAPCAT_PORT=8646
NAPCAT_PATH=/napcat/ws
NAPCAT_ALLOWED_USERS=10001,10002   # 允许私聊的 QQ 号
NAPCAT_HOME_CHANNEL=10001          # 或 group:987654 代表群聊
```

只要设置了 `NAPCAT_TOKEN`，适配器就会自动启用。`NAPCAT_ENABLED=true` 用于**没 token 也强制启用**的场景（比如 token 写在 `config.yaml` 里）。

### 高级配置（`~/.hermes/config.yaml`）

```yaml
platforms:
  napcat:
    enabled: true
    extra:
      token: "pick-a-long-random-string"
      host: "0.0.0.0"
      port: 8646
      path: "/napcat/ws"
      allow_from: ["10001", "10002"]        # 私聊白名单
      group_allow_from: ["10001"]           # 群聊白名单（可选）
```

## 环境变量

| 变量                         | 说明                                                                | 默认值        |
|------------------------------|---------------------------------------------------------------------|---------------|
| `NAPCAT_TOKEN`               | 共享密钥，用于 `Authorization: Bearer` / `?access_token=`          | —（必填）     |
| `NAPCAT_ENABLED`             | 即使 `.env` 里没 token 也强制启用适配器                             | `false`       |
| `NAPCAT_HOST`                | 反向 WS 服务监听地址                                                | `0.0.0.0`     |
| `NAPCAT_PORT`                | 反向 WS 服务监听端口                                                | `8646`        |
| `NAPCAT_PATH`                | NapCat 连接的 WebSocket 路径                                        | `/napcat/ws`  |
| `NAPCAT_ALLOWED_USERS`       | 允许私聊机器人的 QQ 号，逗号分隔                                    | 全部放行      |
| `NAPCAT_GROUP_ALLOWED_USERS` | 允许在群里对话的 QQ 号，逗号分隔                                    | 全部放行      |
| `NAPCAT_ALLOW_ALL_USERS`     | `true` 表示完全跳过白名单                                           | `false`       |
| `NAPCAT_HOME_CHANNEL`        | cron 任务默认投递的目标：`10001` 或 `group:987654`                  | —             |
| `NAPCAT_HOME_CHANNEL_NAME`   | home channel 的展示名                                               | `Home`        |

## 配置 NapCat 客户端

1. 启动 NapCat，登录你要让 agent 驱动的 QQ。
2. 打开 NapCat WebUI，进入 **网络配置 ➜ 新建 ➜ WebSocket 客户端**（反向 WebSocket）。
3. 填写：
   - **URL**：`ws://<hermes 主机>:8646/napcat/ws`（如果 Hermes 前面挂了 TLS，就写 `wss://`）
   - **access_token**：与 Hermes 的 `NAPCAT_TOKEN` 完全一致
   - **messagePostFormat**：推荐选 `array`；适配器也能容忍 CQ 码，但会直接剥掉
   - **心跳**保持默认（30 秒）即可，服务端也会发 ping
4. 启用该连接，NapCat 会与 Hermes 建立一条常驻 WebSocket。
5. 启动 Hermes：`hermes gateway run`，日志里应该出现：

   ```
   [NapCat] Reverse WebSocket listening on ws://0.0.0.0:8646/napcat/ws
   [NapCat] NapCat connected (self_id=..., remote=...)
   ```

## 部署教程

### 方案 A — NapCat 与 Hermes 同机部署

最简单的方式，适合 Windows 重度用户。

1. 在同一台 Windows（或 WSL）上安装 Hermes：`pip install hermes-agent[messaging]`。
2. 跑交互式引导：`hermes setup gateway` → 选 NapCat。
3. 把 `NAPCAT_HOST` 设成 `127.0.0.1`，端口就不会暴露到局域网。
4. NapCat 里 URL 填 `ws://127.0.0.1:8646/napcat/ws`。
5. 一个终端跑 `hermes gateway run`，另一个跑 NapCat。

### 方案 B — NapCat 在 Windows，Hermes 在 Linux VPS

更接近生产的拆分。

1. 在 VPS 上装好 Hermes，开放 gateway 端口（最好只对自己家的出口 IP 放行）：

   ```bash
   sudo ufw allow from <你家出口 IP> to any port 8646 proto tcp
   ```
2. VPS 的 `~/.hermes/.env`：`NAPCAT_HOST=0.0.0.0`，设一个至少 32 位的强随机 `NAPCAT_TOKEN`。
3. 推荐在 Hermes 前面加 nginx / caddy / cloudflared 做 TLS：

   ```nginx
   location /napcat/ws {
       proxy_pass http://127.0.0.1:8646;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_set_header Host $host;
       proxy_read_timeout 3600s;
   }
   ```

   然后 NapCat 的 URL 就填 `wss://napcat.example.com/napcat/ws`。
4. 把 Hermes 托管成 systemd 服务：

   ```bash
   hermes gateway install-service  # 生成 user 级 systemd 单元
   systemctl --user start hermes-gateway
   ```
5. 在 Windows 上配置 NapCat 的 WebSocket 客户端，URL 指向公网地址，access_token 与 VPS 一致。**务必勾选自动重连**，这样 Windows 重启后也能恢复。

### 方案 C — Docker Compose 一把起

适合想把 NapCat 也跑在 Docker 里的玩家。

```yaml
# docker-compose.yaml
services:
  hermes:
    image: ghcr.io/nousresearch/hermes-agent:latest
    environment:
      NAPCAT_ENABLED: "true"
      NAPCAT_TOKEN: "${NAPCAT_TOKEN}"
      NAPCAT_HOST: "0.0.0.0"
      NAPCAT_PORT: "8646"
    ports:
      - "8646:8646"
    volumes:
      - ./hermes-home:/root/.hermes

  napcat:
    image: mlikiowa/napcat-docker:latest
    environment:
      ACCOUNT: "${QQ_ACCOUNT}"
      # NapCat 从配置文件读取 websocketClients，挂载进去即可：
    volumes:
      - ./napcat-config:/app/.config/QQ
    restart: unless-stopped
    depends_on:
      - hermes
```

把下面这段写进挂载的 NapCat 配置里：

```json
websocketClients: [
  {
    "enable": true,
    "url": "ws://hermes:8646/napcat/ws",
    "messagePostFormat": "array",
    "reconnectInterval": 5000,
    "accessToken": "${NAPCAT_TOKEN}"
  }
]
```

## 使用小贴士

- **群里必须 @ 机器人。** 只有被显式 `@` 的群消息才会触发回路；回复机器人之前的消息也算被 @，所以跟帖追问不用每次都 @。
- **私聊永远触发。** 任何发给机器人 QQ 的 1:1 文字都会转进去，不想被任意人打扰就配 `NAPCAT_ALLOWED_USERS`。
- **定时任务发群消息。** 在 `NAPCAT_HOME_CHANNEL` 或 `send_message(deliver="napcat", chat_id="group:987654")` 里用 `group:<群号>`；私聊直接写 QQ 号。
- **同一时刻只保留一条 NapCat 连接。** 新的反向 WS 会顶掉旧的 —— NapCat 本身就是一号对一端。要跑多账号就起多个 Hermes 实例，换不同端口和 token。

## 常见问题排查

### 始终看不到 `[NapCat] Reverse WebSocket listening on …`

- 确认 `NAPCAT_TOKEN` 已设置，否则适配器会直接给出致命错误短路。
- 在日志里搜 `napcat_bind_error`，通常是端口被占用；换一个 `NAPCAT_PORT`。

### NapCat 显示 `connection refused` 或 `401`

- URL（`ws://` vs `wss://`）和路径必须和 `NAPCAT_PATH` **完全一致**。
- NapCat 的 `accessToken` 要和 `NAPCAT_TOKEN` **一字不差**（注意不要有多余空格）。
- 如果把 Hermes 放在 Cloudflare / nginx 后面，记得开启 WebSocket upgrade（`proxy_set_header Upgrade …`）。

### 群里不回消息

- 检查消息里是否包含 `@<机器人>` —— NapCat 会发出一个 `at` 段，`qq` 等于机器人的 `self_id`。如果日志里能看到入站事件但没回复，核对 `[NapCat] lifecycle=connect self_id=...` 打印的 `self_id` 是否与 @ 的对象一致。
- 如果是发送失败，确认该 QQ 在这个群里有发言权限（NapCat 会完整继承 QQ 客户端本身的所有限制）。

### 消息被重复触发

- 适配器会按 `message_id` 去重 5 分钟。如果还是看到重复，检查是不是有**两个 NapCat 实例**同时往同一个 `NAPCAT_PATH` 推。

### cron 或 `send_message` 直接报错

- NapCat 的跨平台发送（cron 投递、`send_message(deliver="napcat", …)`）**必须走正在运行的 gateway 适配器**。只有 `hermes gateway run` 在线**且** NapCat 已连接时才能用；否则工具会直接返回一条明确的错误提示，先把 gateway 跑起来即可。
