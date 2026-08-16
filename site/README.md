# 市场证据台

这是 `investment-research` 的 GitHub Pages 纯静态公开站，与需要登录、数据库和服务端能力的私有 `web/` 分离。

站点只展示四类公开信息：采集与就绪状态、市场／ETF 数据、公司公开资料、研究成果与血缘。缺失数据按 `missing`、`empty` 或 `partial` 原样展示，不使用演示数量或投资判断填充。

## 本地构建

从仓库根目录运行：

```bash
python3 scripts/export_public_site.py --output-dir site/public/data
pnpm --dir site install --frozen-lockfile
pnpm --dir site build
python3 scripts/validate_public_site.py site/dist
```

本地预览：

```bash
pnpm --dir site dev
```

项目路径前缀为 `/investment-research/`。`public/data/`、`dist/` 和 `node_modules/` 均不提交；GitHub Actions 会从通过仓库验证的 `main` revision 重新生成、构建和部署。

公开数据边界与失败语义见仓库根目录的 `ARCHITECTURE.md` 和 `PRIVACY.md`。
