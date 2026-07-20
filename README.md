# PDD Profit Dashboard

拼多多利润率看板：Vue 3 + ECharts 前端、FastAPI 数据 API 和 MySQL 数据源。

## 项目结构

```text
profit-dashboard-vue/   Vue 3 + ECharts 前端
api_server_v3.py        FastAPI 数据聚合、目标管理和明细 API
server.py               生产静态文件服务器与 /api/v3 反向代理
deploy/                 Cloudflare Tunnel 配置示例
requirements.txt        Python 依赖
```

看板不依赖前端嵌入数据，主要 API 包括：

- `GET /api/v3/data`
- `GET /api/v3/status`
- `GET /api/v3/admin/targets`
- `GET /api/v3/links`
- `GET /api/v3/link-dashboard`
- `GET /api/v3/link-fields`

## 本地配置

复制环境变量模板并填写实际配置：

```powershell
Copy-Item .env.example .env
```

至少需要配置数据库连接：`DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_PROFIT_DATABASE`。`.env` 不得提交到 Git。

如果只使用数据库中已有的数据，可以不配置网络文件目录；ETL 自动同步功能需要配置 `PROFIT_NETWORK_BASE` 和 `PROFIT_PROMOTION_BASE`。

## 启动开发环境

安装 Python 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

启动 FastAPI：

```powershell
python api_server_v3.py
```

另开终端启动前端：

```powershell
cd profit-dashboard-vue
npm.cmd ci
npm.cmd run dev
```

访问 `http://localhost:5173`。

## 公共演示模式

仓库内提供了一个脱敏周期，其他协作者无需访问私有 MySQL 数据库即可
克隆并查看看板。当前演示周期为 `2026-07-01` 至 `2026-07-14`。

使用演示数据启动 Vue：

```powershell
cd profit-dashboard-vue
$env:VITE_DEMO_MODE="true"
npm.cmd ci
npm.cmd run dev
```

fixture 位于 `profit-dashboard-vue/public/demo-data/`，包含看板聚合数据、
链接趋势数据，以及所选周期的一行一日链接明细。负责人、店铺、商品和链接
标识均已匿名化；金额和指标保留用于界面与图表测试。演示模式下目标修改和
下架操作只保留在当前浏览器会话中。

从本机 ETL 缓存重新导出其他周期：

```powershell
python tools/export_demo_data.py --start 2026-07-01 --end 2026-07-14
```

原始 Excel、数据库凭据和数据库备份不会进入公共仓库。

## 生产构建

```powershell
cd profit-dashboard-vue
npm.cmd run build
cd ..
python server.py --port 8080
```

生产服务器会从 `profit-dashboard-vue/dist` 提供静态文件，并将 `/api/v3/*` 代理到本机 `8090`。

## Cloudflare Tunnel

Tunnel 只负责把公网域名转发到本机 `8080`，不会携带数据库或 Tunnel 凭证。请参考 `deploy/cloudflared-config.example.yml` 创建本机配置，并将凭证文件保存在仓库之外。

运行 Tunnel：

```powershell
cloudflared tunnel --protocol http2 run YOUR_TUNNEL_NAME
```

## 安全注意事项

- 不要提交 `.env`、数据库备份、ETL 缓存、`node_modules`、`dist` 或 Cloudflare 凭证。
- 数据库密码只通过环境变量提供。
- 公共仓库只包含源代码、配置模板和脱敏演示 fixture，不包含真实业务标识或凭据。
- 生产环境建议为数据库账号配置最小权限，并通过 Cloudflare Access 或其他认证方式保护管理接口。
