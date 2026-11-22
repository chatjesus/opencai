# opencai.date 私测部署

网页版和 Telegram Mini App 使用同一个 Next.js 入口和 FastAPI 后端。Cloudflare 负责域名和 HTTPS，Docker Compose 在有持久磁盘的主机上运行应用。数据库和图片保存在独立的 Docker 卷中。

## 主机配置

从待发布的 Git 版本部署。在仓库根目录建立 `backend/.env.production` 和 `frontend/.env.production`，文件权限设为 `0600`。

`backend/.env.production`

```dotenv
LLM_PROVIDER=xai
XAI_API_KEY=
XAI_MODEL=grok-4.3
XAI_MAX_TOKENS=600
IMAGE_PROVIDER=xai
XAI_IMAGE_MODEL=grok-imagine-image-2.0
CLERK_JWT_ISSUER=
CLERK_AUTHORIZED_PARTIES=https://opencai.date
OPENCAI_ALLOWED_CLERK_USER_IDS=
TELEGRAM_WEBAPP_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_WEBAPP_URL=https://opencai.date
WEBAPP_SESSION_SECRET=
OPS_ADMIN_TOKEN=
```

填写 Clerk 实例的 Frontend API URL 和受邀用户的 `user_…` ID。填写 Telegram 测试员的数字 ID，用逗号分隔。若同一个人的 Web 用户 ID 和 Telegram ID 已确定，可用 `TELEGRAM_USER_LINKS=12345678:user_…` 预先关联。否则通过应用的绑定码关联。

`frontend/.env.production`

```dotenv
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
CLERK_SECRET_KEY=
NEXT_PUBLIC_API_URL=
```

使用 Clerk 的生产实例和为该域名签发的前端密钥。Grok 和 Telegram 凭据只放在后端环境文件中。Compose 仅将前端映射到主机的 `127.0.0.1:3300`。

```sh
docker compose --env-file frontend/.env.production -f deploy/compose.private.yml up -d --build
curl -fsS http://127.0.0.1:3300/api/health
```

## 域名和 Telegram

在 Cloudflare 中为该主机创建命名隧道，将 `opencai.date` 的 public hostname 指向主机上的 `http://127.0.0.1:3300`。让隧道随主机启动。若主机已有 HTTPS 反向代理，也可以在该代理中添加新站点，并使用 Cloudflare 的严格证书验证。

在 BotFather 用 `/setmenubutton` 将 Telegram 机器人的菜单地址设为 `https://opencai.date`。私测员由机器人菜单启动小程序。首次绑定后，两端显示相同的会话列表。

## 上线验证

- 确认 `https://opencai.date/api/health` 返回 `llm_healthy` 和 `image_healthy` 为 true，且模型名称与环境配置一致。此检查只核对配置，不调用付费 API。
- 用未登录状态访问 `/api/characters`，预期返回 `401`。未受邀的 Clerk 用户预期返回 `403`。
- 用同一受邀身份在 Web 和 Telegram 打开一个会话，核对上下文和已保存消息。撤掉测试员邀请后，两端的业务 API 应拒绝访问。
- 检查同一角色的不同存档都能从桌面侧栏和移动端「存档」入口单独打开。
- 用有额度的 xAI 密钥实际发一轮对话，再主动点一次「剧情生图」。刷新并核对同一回合的图片。检查图片请求对未授权用户返回 `401` 或 `404`。
- 保留上一个可运行的 Git 版本和编排配置。备份数据库时通过 SQLite 的备份接口创建一致快照，并同时备份图片卷。
