# 投资工作台

个人使用的持仓与投资决策网站。首版包含：

- GitHub OAuth 单用户白名单登录；
- 从固定 GitHub 分支的 `portfolio/public` 读取持仓；
- 使用仓库内已有行情估算可覆盖标的的市值；
- Neon Postgres 私有存储结构化决策日志；
- 展示数据来源、日期、覆盖率和缺失项。

首版不展示收益曲线，不推算缺失行情，也不把费用未知的现金数据描述为精确余额。

## 固定数据源

- Repository: `WenxuanZhang120/investment-research`
- Branch: `codex/github-connector-small-files`
- Holdings: `portfolio/public/holdings.csv`
- Execution status: `portfolio/public/execution_status.json`

生产构建如果不是来自上述固定分支，`data:sync` 会主动失败，避免误用其他分支的数据。

## 环境变量

复制 `.env.example` 为 `.env.local`，配置：

- `AUTH_GITHUB_ID`：GitHub OAuth App Client ID；
- `AUTH_GITHUB_SECRET`：GitHub OAuth App Client Secret；
- `AUTH_SECRET`：Auth.js 会话签名密钥；
- `AUTH_TRUST_HOST`：本地开发设为 `true`；Vercel 会自动识别可信 Host；
- `ALLOWED_GITHUB_LOGIN`：保持为 `WenxuanZhang120`；
- `DATABASE_URL`：Neon Postgres 连接串。

GitHub OAuth App 的回调地址：

```text
https://YOUR_DOMAIN/api/auth/callback/github
```

本地调试时使用：

```text
http://localhost:3000/api/auth/callback/github
```

## 数据库与构建

```bash
pnpm db:migrate
pnpm data:sync
pnpm build
```

首个迁移文件位于 `drizzle/0000_married_zuras.sql`。所有决策日志读写都会在服务端重新校验 GitHub 登录名，不能只依赖页面隐藏。

## 部署约束

Vercel 项目的 Root Directory 应设为 `web`，Production Branch 应设为 `codex/github-connector-small-files`。环境变量和数据库迁移完成后再发布生产版本。
