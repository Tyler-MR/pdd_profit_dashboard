#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全栈服务器：静态文件 + 订单查询 API + 下架任务队列
端口: 8080
─────────────────────────────────────
启动后替换原有的 http.server，同时支持：
  GET  /index.html          → 静态文件 (wwwroot)
  POST /api/orders          → 订单查询
  POST /api/orders/export   → CSV 导出
  GET  /api/tables          → 表列表
  GET  /api/health          → 健康检查
  POST /api/delist          → 提交下架任务
  GET  /api/delist/pending  → 影刀拉取待处理
  POST /api/delist/complete → 影刀标记完成
  GET  /api/delist/history  → 历史记录
"""
import json, re, sys, os, csv, io, traceback, time, hashlib, urllib.request, urllib.parse, uuid, threading
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import pymysql

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # The dependency is listed in requirements.txt; this fallback keeps the
    # legacy server importable before dependencies are installed.
    pass

DB = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "view"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_ORDER_DATABASE", "order_of_all_platform"),
    "charset": os.getenv("DB_CHARSET", "utf8mb4"),
    "cursorclass": pymysql.cursors.DictCursor,
}
PLATFORM_MAP = {
    "抖店": r"^order_dd", "视频号": r"^order_sph", "快手": r"^order_ks",
    "拼多多": r"pdd", "淘宝": r"tb", "京东": r"jd", "旺店通": r"wdt",
}

WWWROOT = os.path.dirname(os.path.abspath(__file__))
VUE_DIST = os.path.join(WWWROOT, "profit-dashboard-vue", "dist")
STATIC_ROOT = VUE_DIST if os.path.isdir(VUE_DIST) else WWWROOT

# ═══════════════════════════════════════════════════════
# 下架任务队列 (文件持久化，供 B电脑影刀RPA拉取)
# ═══════════════════════════════════════════════════════
DELIST_FILE = os.path.join(WWWROOT, "delist_tasks.json")
_delist_lock = threading.Lock()

def _read_delist_tasks():
    """读取下架任务文件"""
    if not os.path.exists(DELIST_FILE):
        return {"tasks": []}
    try:
        with open(DELIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"tasks": []}

def _write_delist_tasks(data):
    """写入下架任务文件"""
    with open(DELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_conn():
    return pymysql.connect(**DB)


def get_tables(months=3):
    cutoff = (datetime.now() - timedelta(days=months * 31)).strftime("%Y%m")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'order_%%' "
                "ORDER BY TABLE_NAME", (DB["database"],))
            tables = []
            for r in cur:
                tn = r["TABLE_NAME"]
                m = re.search(r"(\d{4})[_-](\d{2})", tn)
                dh = f"{m.group(1)}{m.group(2)}" if m else "000000"
                if dh >= cutoff:
                    plat = "其他"
                    for pn, pat in PLATFORM_MAP.items():
                        if re.search(pat, tn): plat = pn; break
                    tables.append((tn, plat, dh))
            tables.sort(key=lambda x: x[2], reverse=True)
            return tables
    finally:
        conn.close()


def query_orders(filters):
    """执行订单查询"""
    all_tabs = get_tables(3)
    pfs = [p for p in (filters.get("platforms") or []) if p]
    selected = [t[0] for t in all_tabs if (not pfs or t[1] in pfs)]
    if not selected:
        return {"total": 0, "rows": [], "tables_used": 0}

    # 限制表数
    if len(selected) > 60:
        selected = selected[:60]

    COLS = ["订单编号","子订单号","商品名称","商品编码","商品id",
            "规格信息","商品数量","支付金额","运费","订单优惠总金额",
            "店铺名称","下单时间","付款时间","发货时间","收货时间",
            "出单渠道","带货类型","平台","归属部门","商品标签"]

    # 取所有表列的公共交集（抽样检查以避免查几百张表）
    sample_tables = [selected[0], selected[len(selected)//2], selected[-1]] if len(selected) >= 3 else selected
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            common_cols = None
            for t in sample_tables:
                cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                            (DB["database"], t))
                t_cols = {r["COLUMN_NAME"] for r in cur.fetchall()}
                common_cols = t_cols if common_cols is None else common_cols & t_cols
    finally:
        conn.close()

    cols = [c for c in COLS if c in common_cols]
    col_s = ", ".join(f"`{c}`" for c in cols)

    # WHERE
    conds = []; prms = []
    ds = filters.get("date_start")
    de = filters.get("date_end")
    if ds: conds.append("`下单时间` >= %s"); prms.append(ds)
    if de: conds.append("`下单时间` <= %s"); prms.append(de + " 23:59:59")
    kw = filters.get("keyword","").strip()
    if kw:
        conds.append("(`订单编号` LIKE %s OR `商品名称` LIKE %s OR `店铺名称` LIKE %s)")
        lk = f"%{kw}%"; prms.extend([lk,lk,lk])
    if filters.get("amount_min") is not None:
        conds.append("`支付金额` >= %s"); prms.append(float(filters["amount_min"]))
    if filters.get("amount_max") is not None:
        conds.append("`支付金额` <= %s"); prms.append(float(filters["amount_max"]))
    sn = filters.get("shop_name","").strip()
    if sn: conds.append("`店铺名称` LIKE %s"); prms.append(f"%{sn}%")
    pc = filters.get("product_code","").strip()
    if pc: conds.append("`商品编码` LIKE %s"); prms.append(f"%{pc}%")

    wh = " AND ".join(conds) if conds else "1=1"
    n = len(selected)
    all_prms = prms * n

    subs = [f"SELECT {col_s} FROM `{t}` WHERE {wh}" for t in selected]
    u = " UNION ALL ".join(subs)

    pg = max(1, filters.get("page", 1))
    lim = min(filters.get("limit", 100), 5000)
    off = (pg - 1) * lim

    csql = f"SELECT COUNT(*) AS total FROM ({u}) AS _u"
    dsql = f"SELECT * FROM ({u}) AS _u ORDER BY `下单时间` DESC LIMIT {lim} OFFSET {off}"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(csql, all_prms)
            total = cur.fetchone()["total"]
            cur.execute(dsql, all_prms)
            rows = cur.fetchall()
            for row in rows:
                for k, v in list(row.items()):
                    if isinstance(v, datetime): row[k] = v.strftime("%Y-%m-%d %H:%M:%S")
                    elif v is None: row[k] = ""
            return {"total": total, "rows": rows, "tables_used": n,
                    "page": pg, "limit": lim,
                    "pages": max(1, (total + lim - 1) // lim)}
    finally:
        conn.close()


class Server(SimpleHTTPRequestHandler):
    """统一处理静态文件 + API"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_ROOT, **kwargs)

    def json_resp(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        cl = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(cl) if cl else b"{}"
        try: body = raw.decode("utf-8")
        except: body = raw.decode("gbk", errors="replace")
        try: data = json.loads(body) if body else {}
        except: self.json_resp({"error":"Invalid JSON"}, 400); return

        try:
            if path == "/api/orders":
                result = query_orders(data)
                self.json_resp(result)
            elif path == "/api/orders/export":
                self._export_csv(data)
            elif path == "/api/wdt/orders":
                result = wdt_query_all(data)
                self.json_resp(result)
            elif path == "/api/delist":
                self._handle_delist_submit(data)
            elif path == "/api/delist/complete":
                self._handle_delist_complete(data)
            elif path.startswith("/api/v3"):
                self._proxy_to_v3("POST", raw)
            else:
                self.json_resp({"error":"Not Found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.json_resp({"error": str(e)}, 500)

    def _export_csv(self, data):
        ids = data.get("order_ids", [])
        if not ids: self.json_resp({"error":"请提供order_ids"}, 400); return
        tables = get_tables(3)
        selected = [t[0] for t in tables]
        COLS = ["订单编号","商品名称","商品编码","商品数量","支付金额",
                "店铺名称","下单时间","平台","归属部门"]
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                            (DB["database"], selected[0]))
                avail = {r["COLUMN_NAME"] for r in cur.fetchall()}
        finally: conn.close()
        cols = [c for c in COLS if c in avail]
        ph = ",".join(["%s"]*len(ids))
        subs = [f"SELECT {', '.join(f'`{c}`' for c in cols)} FROM `{t}` WHERE `订单编号` IN ({ph})" for t in selected]
        u = " UNION ALL ".join(subs)
        prms = ids * len(selected)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(u, prms); rows = cur.fetchall()
            out = io.StringIO(); out.write('\uFEFF')
            w = csv.DictWriter(out, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for r in rows:
                for k,v in list(r.items()):
                    if isinstance(v,datetime): r[k]=v.strftime("%Y-%m-%d %H:%M:%S")
                w.writerow(r)
            csv_str = out.getvalue(); out.close()
            self.send_response(200)
            self.send_header("Content-Type","text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                f"attachment; filename=orders_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(csv_str.encode())
        finally: conn.close()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/tables":
            tabs = get_tables(3)
            result = [{"name":t[0],"platform":t[1],"date_hint":t[2]} for t in tabs]
            self.json_resp({"tables":result,"count":len(result)})
        elif path == "/api/health":
            self.json_resp({"status":"ok","time":datetime.now().isoformat()})
        elif path == "/api/stats":
            tabs = get_tables(3)
            plats = {}
            for t in tabs: plats[t[1]] = plats.get(t[1],0)+1
            self.json_resp({"total_tables":len(tabs),"platforms":plats})
        elif path == "/api/delist/pending":
            self._handle_delist_pending()
        elif path == "/api/delist/history":
            self._handle_delist_history()
        elif path.startswith("/api/v3"):
            self._proxy_to_v3("GET")
        else:
            if path == "/profit-dashboard.html":
                self.path = "/index.html"
            super().do_GET()

    # ═══════════════════════════════════════════════
    # V3 API 代理 → localhost:8090
    # ═══════════════════════════════════════════════

    def _proxy_to_v3(self, method, body=None):
        """将 /api/v3/* 请求代理转发到本地 api_server_v3.py:8090"""
        target = f"http://127.0.0.1:8090{self.path}"
        try:
            if method == "GET":
                req = urllib.request.Request(target, method="GET")
            else:
                req = urllib.request.Request(target, data=body, method="POST")
                req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
            # 透传查询参数（self.path 已包含 query string）
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.json_resp({"error": f"V3 API error: {e.code}", "detail": e.reason}, e.code)
        except Exception as e:
            self.json_resp({"error": f"V3 proxy failed: {str(e)}"}, 502)

    # ═══════════════════════════════════════════════
    # 下架任务处理方法
    # ═══════════════════════════════════════════════

    def _handle_delist_submit(self, data):
        """用户提交下架任务: {link_ids: [...], store_names: [...], operator: "..."}"""
        link_ids = data.get("link_ids", [])
        if not link_ids:
            self.json_resp({"error": "请提供link_ids"}, 400)
            return
        with _delist_lock:
            tasks = _read_delist_tasks()
            task = {
                "id": uuid.uuid4().hex[:12],
                "link_ids": link_ids,
                "count": len(link_ids),
                "store_names": data.get("store_names", []),
                "store_count": len(data.get("store_names", [])),
                "operator": data.get("operator", ""),
                "status": "pending",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "completed_at": None,
                "result": None,
                "error": None,
            }
            tasks["tasks"].append(task)
            _write_delist_tasks(tasks)
        self.json_resp({"success": True, "task": task})

    def _handle_delist_pending(self):
        """影刀RPA拉取待处理任务"""
        tasks = _read_delist_tasks()
        pending = [t for t in tasks["tasks"] if t["status"] == "pending"]
        self.json_resp({"tasks": pending, "count": len(pending)})

    def _handle_delist_complete(self, data):
        """影刀RPA标记任务完成: {task_id: "xxx", result: "ok|fail", error: "..."}"""
        task_id = data.get("task_id", "")
        with _delist_lock:
            tasks = _read_delist_tasks()
            for t in tasks["tasks"]:
                if t["id"] == task_id and t["status"] == "pending":
                    t["status"] = "completed" if data.get("result") == "ok" else "failed"
                    t["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    t["result"] = data.get("result", "")
                    t["error"] = data.get("error", "")
                    _write_delist_tasks(tasks)
                    self.json_resp({"success": True, "task": t})
                    return
        self.json_resp({"error": "任务不存在或已处理"}, 404)

    def _handle_delist_history(self):
        """查看历史任务"""
        tasks = _read_delist_tasks()
        history = tasks["tasks"][-50:]
        history.reverse()
        self.json_resp({"tasks": history, "count": len(history)})


# ═══════════════════════════════════════════════════════
# 旺店通 API 代理
# ═══════════════════════════════════════════════════════
WDT_SID = "lcwhcm05"
WDT_APPKEY = "lcwhcm05-gwb"
WDT_APPSECRET = "bc2563f914cd544f6b8d6471100be11f"
WDT_API_URL = "https://openapi.huice.com/openapi/trade_query.php"
WDT_PAGE_DELAY = 1.2

def wdt_sign(params, secret):
    keys = sorted(params.keys())
    parts = []
    for i, k in enumerate(keys):
        v = str(params[k])
        kl = len(k.encode("utf-8"))
        vl = len(v.encode("utf-8"))
        parts.append(f"{kl:02d}-{k}:{vl:04d}-{v}" + (";" if i < len(keys) - 1 else ""))
    return hashlib.md5(("".join(parts) + secret).encode("utf-8")).hexdigest()

def wdt_call(biz_params):
    ts = int(time.time())
    p = {"sid": WDT_SID, "appkey": WDT_APPKEY, "timestamp": ts, **biz_params}
    p["sign"] = wdt_sign(p, WDT_APPSECRET)
    data = urllib.parse.urlencode(p).encode("utf-8")
    req = urllib.request.Request(WDT_API_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"code": -1, "message": str(e)}

def wdt_query_all(filters):
    """全量拉取旺店通订单（分页遍历）"""
    start_time = filters.get("start_time", "")
    end_time = filters.get("end_time", "")
    platform = filters.get("platform", "39")  # 默认拼多多
    page_size = min(filters.get("page_size", 100), 100)
    max_pages = filters.get("max_pages", 20)

    all_orders = []
    page_no = 0
    while True:
        r = wdt_call({"start_time": start_time, "end_time": end_time,
                       "time_type": 1, "page_no": page_no, "page_size": page_size})
        if r.get("code") != 0:
            if r.get("code") == 1012:
                time.sleep(60); continue
            return {"error": f"WDT API error: code={r.get('code')}, {r.get('message', '')}",
                    "orders": all_orders, "total": len(all_orders)}
        trades = r.get("trades") or []
        total = r.get("total_count", "0")
        if platform:
            trades = [t for t in trades if str(t.get("platform_id", "")) == platform]
        all_orders.extend(trades)
        page_no += 1
        if len(trades) < page_size:
            break
        if max_pages > 0 and page_no >= max_pages:
            break
        time.sleep(WDT_PAGE_DELAY)
    return {"total": len(all_orders), "api_total": total, "orders": all_orders,
            "pages_fetched": page_no, "truncated": max_pages > 0 and page_no >= max_pages}


if __name__ == "__main__":
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--port" else 8080
    srv = HTTPServer(("0.0.0.0", port), Server)
    print(f"🚀 服务器已启动 http://127.0.0.1:{port}")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n⏹️ 已停止"); srv.server_close()
