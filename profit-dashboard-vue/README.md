# 利润率看板前端

Vue 3 + ECharts 前端。页面不内置业务数据，所有看板数据通过 FastAPI API 获取。

## 开发

在仓库根目录启动 FastAPI 后端（默认 `8090`）：

```powershell
python api_server_v3.py
```

另开终端启动 Vue 开发服务器：

```powershell
cd profit-dashboard-vue
npm.cmd ci
npm.cmd run dev
```

访问 `http://localhost:5173`。Vite 会将 `/api` 请求代理到 `VITE_DEV_API_TARGET`。

## 构建

```powershell
npm.cmd run build
```

构建产物位于 `dist/`，由根目录的 `server.py` 或其他静态服务器提供。
