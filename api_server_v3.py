"""
利润率看板 V3 — 完整数据管道
1. ETL: \\192.168.16.31\...\*.xlsx → 清洗 → MySQL bi.pdd_web_profit_data
2. API: MySQL → 聚合 → JSON (FastAPI :8090)
3. 调度: 每小时自动 ETL + 手动 POST /api/v3/etl/run
"""
import os, sys, json, re, shutil, subprocess, tempfile, threading, time, uuid
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import pandas as pd
import numpy as np
import pymysql
from sqlalchemy import create_engine
from fastapi import FastAPI, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # The dependency is listed in requirements.txt; this fallback keeps the
    # module importable in a minimal diagnostic shell.
    pass

# ============ 配置 ============
NETWORK_BASE = Path(os.getenv("PROFIT_NETWORK_BASE", r"\\192.168.16.31\拼多多链接利润率\日利润率"))
PROMOTION_BASE = Path(os.getenv("PROFIT_PROMOTION_BASE", r"\\192.168.16.26\Users\Financial\Desktop\A.影刀\拼多多\平台推广"))
LOCAL_CACHE = Path(os.getenv("PROFIT_LOCAL_CACHE", str(Path(__file__).with_name("cache_v3_etl"))))
MYSQL_CFG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "view"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_PROFIT_DATABASE", "bi"),
    "charset": os.getenv("DB_CHARSET", "utf8mb4"),
}
TABLE_NAME = "pdd_web_profit_data"
API_PORT = int(os.getenv("PROFIT_API_PORT", "8090"))
ETL_INTERVAL = 3600  # 每小时

# 推广明细会以“商品ID + 日期”关联到主表的“链接id + 数据日期”。
# 这些字段只补充到主表，不覆盖主表已有的收入、利润和推广费字段。
PROMOTION_STRING_COLUMNS = ["出价方式", "商品名称", "store", "推广来源文件"]
PROMOTION_NUMERIC_COLUMNS = [
    "总花费(元)", "交易额(元)", "净交易额(元)", "净成交笔数", "成交笔数",
    "直接交易额(元)", "间接交易额(元)", "直接成交笔数", "间接成交笔数",
    "曝光量", "点击量", "询单花费(元)", "询单量", "收藏花费(元)", "收藏量",
    "关注花费(元)", "关注量",
    "平均收藏成本(元)", "平均关注成本(元)", "平均询单成本(元)",
    "全站推广费比", "净交易额占比", "实际投产比", "净实际投产比",
    "每笔净成交花费(元)", "每笔成交花费(元)", "每笔成交金额(元)",
    "每笔直接成交金额(元)", "每笔间接成交金额(元)",
]
PROMOTION_RAW_NUMERIC_COLUMNS = [
    "总花费(元)", "交易额(元)", "净交易额(元)", "净成交笔数", "成交笔数",
    "直接交易额(元)", "间接交易额(元)", "直接成交笔数", "间接成交笔数",
    "曝光量", "点击量", "询单花费(元)", "询单量", "收藏花费(元)", "收藏量",
    "关注花费(元)", "关注量",
]
PROMOTION_DATA_COLUMNS = PROMOTION_STRING_COLUMNS + PROMOTION_NUMERIC_COLUMNS + ["推广数据匹配"]

app = FastAPI(title="利润率看板 V3 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ============ 工具函数 ============
def get_mysql():
    return pymysql.connect(**MYSQL_CFG)

def safe_copy(remote, local_dir):
    """跨平台复制源文件到本地缓存目录。"""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / remote.name
    shutil.copy2(remote, local_path)
    return local_path

def list_xlsx_files():
    """递归列出利润率工作簿，排除明确标记为无需读取的内部文件夹。"""
    base = Path(NETWORK_BASE)
    if not base.is_dir():
        print(f"  ⚠ 利润率目录不存在或不可访问: {base}")
        return []

    files = []
    for path in base.rglob("*.xlsx"):
        if path.name.startswith("~$") or "拼多多链接利润率" not in path.name:
            continue
        relative_parts = path.relative_to(base).parts[:-1]
        if any("不需要读取" in part for part in relative_parts):
            continue
        match = re.search(r"(\d{1,2})月", "/".join(relative_parts)) or re.search(r"拼多多链接利润率(\d{4})-(\d{2})-", path.name)
        month = int(match.group(1) if match and len(match.groups()) == 1 else match.group(2)) if match else 0
        files.append((month, path))
    return sorted(files, key=lambda item: (item[0], item[1].name))

# ============ ETL: xlsx → MySQL ============
def clean_percentage(val):
    """清洗百分比：'45.87%'→0.4587, 0.3864→0.3864, nan→0, -inf→0"""
    if val is None or (isinstance(val, float) and (pd.isna(val) or np.isinf(val))):
        return 0.0
    if isinstance(val, str):
        val = val.strip()
        if val.endswith("%"):
            try:
                return float(val[:-1]) / 100.0
            except ValueError:
                return 0.0
        # "int%" 这类异常值
        if "%" in val:
            return 0.0
    try:
        f = float(val)
        if np.isinf(f) or pd.isna(f):
            return 0.0
        # 如果已经是小数形式 (>1 可能是百分比整数形式，如 45 表示 45%)
        # 这里保持原值，不做假设
        return f
    except (ValueError, TypeError):
        return 0.0

def clean_product_code(code):
    """商品编码：按'-'切分取前段，为空填'暂无编码'"""
    if code is None or (isinstance(code, float) and pd.isna(code)):
        return "暂无编码"
    code = str(code).strip()
    if not code:
        return "暂无编码"
    return code.split("-")[0].strip() or "暂无编码"

def extract_date_from_filename(filename):
    """从文件名提取日期：拼多多链接利润率2026-06-01.xlsx → 2026-06-01"""
    name = Path(filename).stem
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else None

def process_single_xlsx(month, filepath, cache_dir):
    """处理单个 xlsx 文件，返回清洗后的 DataFrame"""
    try:
        local = safe_copy(filepath, cache_dir)
    except Exception as e:
        print(f"  ⚠ 复制失败 {filepath.name}: {e}")
        return None

    try:
        # 找到包含"链接"的 sheet
        xl = pd.ExcelFile(local)
        target_sheet = None
        for sn in xl.sheet_names:
            if "链接" in sn:
                target_sheet = sn
                break
        if not target_sheet:
            print(f"  ⚠ {filepath.name}: 未找到包含'链接'的sheet, sheets={xl.sheet_names}")
            xl.close()
            return None

        df = pd.read_excel(local, sheet_name=target_sheet)
        xl.close()
    except Exception as e:
        print(f"  ⚠ 读取失败 {filepath.name}: {e}")
        return None

    if df.empty or len(df) < 2:
        return None

    # 日期从文件名取
    file_date = extract_date_from_filename(filepath.name)
    if not file_date:
        print(f"  ⚠ 无法提取日期: {filepath.name}")
        return None

    # 识别并跳过汇总行（第一数据行"共计："）
    # 找到真正的列名行（通常是第0行）
    # 重新读取，使用第一行作为列名
    try:
        df = pd.read_excel(local, sheet_name=target_sheet, dtype=str)
        xl2 = pd.ExcelFile(local)
        df = pd.read_excel(local, sheet_name=target_sheet)
        xl2.close()
    except:
        return None

    # 标准化列名（去除空格）
    df.columns = [str(c).strip() for c in df.columns]

    # 过滤：链接id 不为空
    if "链接id" not in df.columns:
        print(f"  ⚠ {filepath.name}: 缺少'链接id'列, cols={list(df.columns)[:10]}")
        return None

    df = df[df["链接id"].notna() & (df["链接id"] != "")]
    if len(df) == 0:
        return None

    # 百分比列处理
    pct_cols = ["成本占比", "快递占比", "货品快递总和占比", "毛利率", "推广费占比", "利润率", "退货率"]
    for col in pct_cols:
        if col in df.columns:
            df[col] = df[col].apply(clean_percentage)

    # 数值列处理
    num_cols = ["单量", "收入", "成本", "快递", "成本+快递", "毛利",
                "技术服务费(1%)", "预估售后", "推广费", "运费险", "税费", "平台利润",
                "30天销量", "收藏"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # 商品编码清洗
    if "商品编码" in df.columns:
        df["商品编码"] = df["商品编码"].apply(clean_product_code)

    # 添加数据日期
    df["数据日期"] = file_date
    df["来源文件"] = filepath.name

    # 重命名含特殊字符的列
    rename_map = {}
    for c in df.columns:
        if '(' in str(c) or ')' in str(c) or '%' in str(c):
            new_name = str(c).replace('(1%)', '').replace('(', '').replace(')', '').replace('%', 'pct').strip()
            rename_map[c] = new_name
    if rename_map:
        df = df.rename(columns=rename_map)

    # 保留需要的列
    keep_cols = ["店铺名称", "负责人", "商品标题", "链接id", "商品编码",
                 "单量", "收入", "成本", "成本占比", "快递", "快递占比",
                 "成本+快递", "货品快递总和占比", "毛利", "毛利率",
                 "技术服务费", "预估售后", "推广费", "推广费占比",
                 "运费险", "税费", "平台利润", "利润率", "数据日期", "来源文件"]
    available = [c for c in keep_cols if c in df.columns]
    df = df[available].copy()

    return df


def list_promotion_xlsx_files(folder_path=PROMOTION_BASE):
    """列出推广目录下的 xlsx，忽略 Excel 临时文件。"""
    folder = Path(folder_path)
    try:
        if not folder.is_dir():
            print(f"  ⚠ 推广目录不存在或不可访问: {folder}")
            return []
        return sorted(
            [
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$")
            ],
            key=lambda p: p.name,
        )
    except OSError as e:
        print(f"  ⚠ 读取推广目录失败 {folder}: {e}")
        return []


def normalize_join_id(values):
    """统一商品 ID/链接 ID，避免 Excel 数值被读成带 .0 的字符串。"""
    result = values.astype("string").str.strip()
    result = result.str.replace(r"\.0$", "", regex=True)
    return result.mask(result.isin(["", "-", "nan", "None", "<NA>"]))


def safe_divide_series(numerator, denominator):
    """按行安全相除，分母为空或 0 时返回 0，避免产生 inf/NaN。"""
    left = pd.to_numeric(numerator, errors="coerce").fillna(0.0)
    right = pd.to_numeric(denominator, errors="coerce").fillna(0.0)
    return left.div(right.where(right.ne(0), np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def calculate_promotion_metrics(df):
    """补充推广报表中的衍生指标。"""
    df["平均收藏成本(元)"] = safe_divide_series(df["收藏花费(元)"], df["收藏量"])
    df["平均关注成本(元)"] = safe_divide_series(df["关注花费(元)"], df["关注量"])
    df["平均询单成本(元)"] = safe_divide_series(df["询单花费(元)"], df["询单量"])
    df["全站推广费比"] = safe_divide_series(df["直接交易额(元)"], df["总花费(元)"])
    df["净交易额占比"] = safe_divide_series(df["净交易额(元)"], df["交易额(元)"])
    df["实际投产比"] = safe_divide_series(df["交易额(元)"], df["总花费(元)"])
    df["净实际投产比"] = safe_divide_series(df["净交易额(元)"], df["总花费(元)"])
    df["每笔净成交花费(元)"] = safe_divide_series(df["总花费(元)"], df["净成交笔数"])
    df["每笔成交花费(元)"] = safe_divide_series(df["总花费(元)"], df["成交笔数"])
    df["每笔成交金额(元)"] = safe_divide_series(df["交易额(元)"], df["成交笔数"])
    df["每笔直接成交金额(元)"] = safe_divide_series(df["直接交易额(元)"], df["直接成交笔数"])
    df["每笔间接成交金额(元)"] = safe_divide_series(df["间接交易额(元)"], df["间接成交笔数"])
    return df


def process_single_promotion_xlsx(filepath):
    """读取一个推广工作簿中的所有商品明细 sheet。"""
    frames = []
    try:
        xls = pd.ExcelFile(filepath)
        sheet_names = [s for s in xls.sheet_names if "汇总" not in s and "商品" in s]
        for sheet_name in sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet_name, dtype={"商品ID": str})
            if df is None or df.empty:
                continue

            df.columns = [str(c).strip() for c in df.columns]
            if "总营销花费(元)" in df.columns and "总花费(元)" not in df.columns:
                df = df.rename(columns={"总营销花费(元)": "总花费(元)"})

            required = {"日期", "商品ID", "总花费(元)"}
            missing = required.difference(df.columns)
            if missing:
                print(f"  ⚠ {filepath.name}/{sheet_name}: 缺少字段 {sorted(missing)}")
                continue

            df["商品ID"] = normalize_join_id(df["商品ID"])
            df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
            df["总花费(元)"] = pd.to_numeric(df["总花费(元)"], errors="coerce")
            df = df[
                df["商品ID"].notna()
                & df["日期"].notna()
                & df["总花费(元)"].notna()
                & df["总花费(元)"].ne(0)
            ].copy()
            if df.empty:
                continue

            for col in PROMOTION_STRING_COLUMNS:
                if col in ("store", "推广来源文件"):
                    df[col] = filepath.name
                elif col not in df.columns:
                    df[col] = ""
                else:
                    df[col] = df[col].fillna("").astype(str).str.strip()

            for col in PROMOTION_RAW_NUMERIC_COLUMNS:
                if col not in df.columns:
                    df[col] = 0.0
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

            df = calculate_promotion_metrics(df)
            frames.append(df[["商品ID", "日期"] + PROMOTION_STRING_COLUMNS + PROMOTION_NUMERIC_COLUMNS])
        xls.close()
    except Exception as e:
        print(f"  ⚠ 读取推广文件失败 {filepath.name}: {e}")
        return None

    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def load_promotion_data():
    """扫描推广目录并按商品 ID + 日期去重，保留最后一条。"""
    files = list_promotion_xlsx_files()
    stats = {
        "files_found": len(files),
        "files_processed": 0,
        "files_error": 0,
        "rows_before_dedup": 0,
        "rows_after_dedup": 0,
    }
    frames = []
    for filepath in files:
        result = process_single_promotion_xlsx(filepath)
        if result is None or result.empty:
            stats["files_error"] += 1
            continue
        frames.append(result)
        stats["files_processed"] += 1

    if not frames:
        return pd.DataFrame(columns=["商品ID", "日期"] + PROMOTION_STRING_COLUMNS + PROMOTION_NUMERIC_COLUMNS), stats

    promotion = pd.concat(frames, ignore_index=True)
    stats["rows_before_dedup"] = len(promotion)
    promotion = promotion.drop_duplicates(["日期", "商品ID"], keep="last").reset_index(drop=True)
    stats["rows_after_dedup"] = len(promotion)
    return promotion, stats


def merge_promotion_data(main_df, promotion_df):
    """以主表为左表，将推广明细按链接 ID + 日期补齐。"""
    main = main_df.copy()

    if promotion_df is None or promotion_df.empty:
        for col in PROMOTION_DATA_COLUMNS:
            if col not in main.columns:
                main[col] = 0 if col == "推广数据匹配" else np.nan
        return main, {"matched_rows": 0, "promotion_rows": 0}

    promotion = promotion_df.copy()
    promotion["__link_id"] = normalize_join_id(promotion["商品ID"])
    promotion["__date"] = pd.to_datetime(promotion["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    promotion = promotion[promotion["__link_id"].notna() & promotion["__date"].notna()].copy()
    promotion = promotion.drop_duplicates(["__link_id", "__date"], keep="last")

    main["__link_id"] = normalize_join_id(main["链接id"])
    main["__date"] = pd.to_datetime(main["数据日期"], errors="coerce").dt.strftime("%Y-%m-%d")

    fields = [c for c in PROMOTION_STRING_COLUMNS + PROMOTION_NUMERIC_COLUMNS if c in promotion.columns]
    lookup = promotion[["__link_id", "__date"] + fields].copy()
    merged = main.merge(lookup, how="left", on=["__link_id", "__date"], validate="m:1", suffixes=("", "_推广"))
    matched = merged["推广来源文件"].notna() if "推广来源文件" in merged.columns else pd.Series(False, index=merged.index)
    merged["推广数据匹配"] = matched.astype(int)
    merged = merged.drop(columns=["__link_id", "__date"])
    return merged, {"matched_rows": int(matched.sum()), "promotion_rows": len(promotion)}


def normalize_join_id_value(value):
    """统一单个数据库键值，用于生成主表业务键集合。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    text = re.sub(r"\.0$", "", text)
    return text or None


def ensure_promotion_columns(conn):
    """为现有主表补充推广字段；已存在字段不会被改写。"""
    cur = conn.cursor()
    cur.execute(f"SHOW COLUMNS FROM {TABLE_NAME}")
    existing = {row[0] for row in cur.fetchall()}
    added = []
    for col in PROMOTION_STRING_COLUMNS:
        if col in existing:
            continue
        sql_type = "VARCHAR(200)" if col == "出价方式" else "TEXT"
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN `{col}` {sql_type} NULL")
        added.append(col)
    for col in PROMOTION_NUMERIC_COLUMNS:
        if col in existing:
            continue
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN `{col}` DOUBLE NULL")
        added.append(col)
    if "推广数据匹配" not in existing:
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN `推广数据匹配` TINYINT NOT NULL DEFAULT 0")
        added.append("推广数据匹配")
    conn.commit()
    return added


def run_promotion_backfill():
    """只回填推广字段，不新增主表行，也不覆盖主表已有字段。"""
    promotion, promotion_stats = load_promotion_data()
    if promotion.empty:
        return {
            "status": "error",
            "message": "推广目录没有读取到有效商品明细",
            "promotion": promotion_stats,
        }

    conn = get_mysql()
    try:
        added_columns = ensure_promotion_columns(conn)
        cur = conn.cursor()
        cur.execute(f"SELECT `链接id`, `数据日期` FROM {TABLE_NAME}")
        main_keys = {
            (normalize_join_id_value(link_id), str(data_date))
            for link_id, data_date in cur.fetchall()
            if normalize_join_id_value(link_id) and data_date is not None
        }

        promotion = promotion.copy()
        promotion["__link_id"] = normalize_join_id(promotion["商品ID"])
        promotion["__date"] = pd.to_datetime(promotion["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        promotion["__key"] = list(zip(promotion["__link_id"], promotion["__date"]))
        matched = promotion[promotion["__key"].isin(main_keys)].copy()

        update_fields = PROMOTION_STRING_COLUMNS + PROMOTION_NUMERIC_COLUMNS
        set_clause = ", ".join([f"`{col}` = %s" for col in update_fields] + ["`推广数据匹配` = 1"])
        update_sql = (
            f"UPDATE {TABLE_NAME} SET {set_clause} "
            "WHERE `链接id` = %s AND `数据日期` = %s"
        )

        def db_value(value):
            if value is None or (isinstance(value, float) and (pd.isna(value) or np.isinf(value))):
                return None
            return value.item() if isinstance(value, np.generic) else value

        update_rows = []
        for _, row in matched.iterrows():
            values = [db_value(row[col]) for col in update_fields]
            values.extend([row["__link_id"], row["__date"]])
            update_rows.append(tuple(values))

        updated = 0
        for i in range(0, len(update_rows), 500):
            batch = update_rows[i:i + 500]
            cur.executemany(update_sql, batch)
            conn.commit()
            updated += len(batch)

        return {
            "status": "ok",
            "message": "推广字段回填完成",
            "promotion": {
                **promotion_stats,
                "main_keys": len(main_keys),
                "matched_keys": len(matched),
                "updated_rows": updated,
                "added_columns": added_columns,
            },
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def run_etl():
    """完整 ETL 流程：扫描→清洗→入库"""
    print(f"\n{'='*60}")
    print(f"🔄 ETL 开始 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"{'='*60}")

    xlsx_files = list_xlsx_files()
    print(f"📁 找到 {len(xlsx_files)} 个 xlsx 文件")

    if not xlsx_files:
        print("❌ 未找到任何文件")
        return {"status": "error", "message": "未找到文件", "files": 0}

    cache_dir = LOCAL_CACHE / "etl"
    all_data = []
    processed = 0
    errors = 0

    for month, filepath in xlsx_files:
        df = process_single_xlsx(month, filepath, cache_dir)
        if df is not None and len(df) > 0:
            all_data.append(df)
            processed += 1
            if processed % 20 == 0:
                print(f"  已处理 {processed}/{len(xlsx_files)} ...")
        else:
            errors += 1

    if not all_data:
        print("❌ 没有有效数据")
        return {"status": "error", "message": "没有有效数据", "files": processed, "errors": errors}

    merged = pd.concat(all_data, ignore_index=True)
    
    # 过滤：删除负责人=淘宝的行（李世豪）
    if "负责人" in merged.columns:
        before = len(merged)
        merged = merged[~merged["负责人"].str.contains("淘宝", na=False)]
        print(f"🗑 已过滤负责人=淘宝: {before - len(merged)} 行")

    # 读取推广目录，并以主表为左表补充商品推广明细。
    promotion, promotion_stats = load_promotion_data()
    if not promotion_stats["files_found"] or not promotion_stats["files_processed"]:
        print("❌ 推广目录没有有效数据，停止本次主表重建，避免覆盖已有完整数据")
        return {
            "status": "error",
            "message": "推广目录没有有效数据，未执行主表重建",
            "files_processed": processed,
            "files_error": errors,
            "promotion": promotion_stats,
        }
    merged, merge_stats = merge_promotion_data(merged, promotion)
    print(
        "📣 推广数据: "
        f"{promotion_stats['files_processed']}/{promotion_stats['files_found']} 个文件, "
        f"去重后 {promotion_stats['rows_after_dedup']} 行, "
        f"匹配主表 {merge_stats['matched_rows']} 行"
    )
    
    print(f"✅ 清洗完成: {processed} 文件, {len(merged)} 行, {errors} 错误")

    # 入库 MySQL（全量替换）
    conn = get_mysql()
    cur = conn.cursor()

    # 删旧表重建
    cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    
    # 动态生成建表语句（基于实际数据列）
    col_defs = ["id INT AUTO_INCREMENT PRIMARY KEY"]
    for c in merged.columns:
        col_name = f"`{c}`"
        if c in ("数据日期",):
            col_defs.append(f"{col_name} DATE")
        elif c in ("商品标题", "商品名称", "来源文件", "store", "推广来源文件"):
            col_defs.append(f"{col_name} TEXT")
        elif c in ("链接id", "商品编码", "店铺名称", "负责人", "出价方式"):
            col_defs.append(f"{col_name} VARCHAR(200)")
        else:
            col_defs.append(f"{col_name} DOUBLE")
    
    # 添加索引
    if "链接id" in merged.columns:
        col_defs.append("INDEX idx_link (`链接id`(32))")
    if "数据日期" in merged.columns:
        col_defs.append("INDEX idx_date (`数据日期`)")
    if "链接id" in merged.columns and "数据日期" in merged.columns:
        col_defs.append("INDEX idx_link_date (`链接id`(32), `数据日期`)")
    if "商品编码" in merged.columns:
        col_defs.append("INDEX idx_code (`商品编码`(32))")
    
    create_sql = f"CREATE TABLE {TABLE_NAME} (\n        " + ",\n        ".join(col_defs) + "\n    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    cur.execute(create_sql)
    conn.commit()

    # 分批 INSERT（每批1000行，避免 SQL 过大）
    # 替换 NaN 和 Inf 为 None
    merged = merged.replace([np.inf, -np.inf], np.nan)
    merged = merged.where(pd.notna(merged), None)
    cols = [f"`{c}`" for c in merged.columns]
    col_names = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = f"INSERT INTO {TABLE_NAME} ({col_names}) VALUES ({placeholders})"

    batch_size = 100
    total_inserted = 0
    for i in range(0, len(merged), batch_size):
        batch = merged.iloc[i:i+batch_size]
        rows = [tuple(None if pd.isna(v) else v for v in row) for row in batch.values]
        cur.executemany(insert_sql, rows)
        conn.commit()
        total_inserted += len(rows)
        if total_inserted % 20000 == 0:
            print(f"  已入库 {total_inserted}/{len(merged)} ...")
    conn.close()

    print(f"✅ 入库完成: {total_inserted} 行 → {TABLE_NAME}")

    return {
        "status": "ok",
        "files_processed": processed,
        "files_error": errors,
        "rows_inserted": total_inserted,
        "promotion": {
            **promotion_stats,
            **merge_stats,
        },
        "dates": sorted(merged["数据日期"].unique().tolist()),
        "timestamp": datetime.now().isoformat()
    }

# ============ FastAPI 端点 ============
@app.get("/api/v3/data")
def get_data(
    link_ids: str = Query(default=""),
    product_code: str = Query(default=""),
    store_name: str = Query(default=""),
    store_person: str = Query(default=""),
):
    """从 MySQL 读取数据并聚合，返回看板所需 JSON。

    筛选条件在数据库层执行，保证所有 Vue 看板使用同一份过滤后的数据。
    link_ids 支持逗号分隔的多个链接 ID。
    """
    where = []
    params = []
    if link_ids:
        ids = [item.strip() for item in link_ids.split(",") if item.strip()]
        if ids:
            placeholders = ",".join(["%s"] * len(ids))
            where.append(f"`链接id` IN ({placeholders})")
            params.extend(ids)
    if product_code:
        where.append("`商品编码` LIKE %s")
        params.append(f"%{product_code.strip()}%")
    if store_name:
        where.append("`店铺名称` LIKE %s")
        params.append(f"%{store_name.strip()}%")
    if store_person:
        where.append("`负责人` = %s")
        params.append(store_person.strip())

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    conn = get_mysql()
    try:
        # 原始明细
        df = pd.read_sql(f"SELECT * FROM {TABLE_NAME}{where_sql}", conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return {"success": False, "error": "数据库无数据，请先运行 ETL"}

    return {
        "success": True,
        "data": aggregate_dashboard_data(df),
        "meta": {
            "total_rows": len(df),
            "date_range": [str(df["数据日期"].min()), str(df["数据日期"].max())],
            "generated_at": datetime.now().isoformat()
        }
    }

@app.get("/api/v3/data/range")
def get_data_range(start: str = Query(...), end: str = Query(...)):
    """按日期范围获取数据"""
    conn = get_mysql()
    try:
        df = pd.read_sql(
            f"SELECT * FROM {TABLE_NAME} WHERE 数据日期 >= %s AND 数据日期 <= %s",
            conn, params=(start, end)
        )
    finally:
        conn.close()

    if df.empty:
        return {"success": False, "error": f"日期范围 {start}~{end} 无数据"}

    return {
        "success": True,
        "data": aggregate_dashboard_data(df),
        "meta": {"rows": len(df)}
    }

@app.post("/api/v3/etl/run")
def trigger_etl(background_tasks: BackgroundTasks):
    """手动触发 ETL"""
    # 同步执行（数据量大时可能需要30-60秒）
    result = run_etl()
    return result


@app.post("/api/v3/etl/promotion")
def trigger_promotion_backfill():
    """只从推广目录补齐现有主表，不重建主表。"""
    return run_promotion_backfill()

@app.post("/api/v3/refresh")
def refresh_data():
    """刷新数据：重新从 MySQL 读取（数据由定时 ETL 或手动 /api/v3/etl/run 更新）"""
    return {"success": True, "message": "数据已就绪，请重新加载 /api/v3/data", "timestamp": datetime.now().isoformat()}

# ============ 下架任务（与 server.py 共享 delist_tasks.json）============
DELIST_FILE = Path(__file__).parent / "delist_tasks.json"
_delist_lock = threading.Lock()

def _read_delist_tasks():
    try:
        with open(DELIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tasks": []}

def _write_delist_tasks(data):
    with open(DELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.post("/api/delist")
async def delist_submit(request: Request):
    """提交下架任务"""
    data = await request.json()
    link_ids = data.get("link_ids", [])
    if not link_ids:
        return {"error": "请提供link_ids"}
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
    return {"success": True, "task": task}

def _add_range(where, params, col, gte, lte):
    """添加数值范围条件"""
    if gte is not None:
        where.append(f"`{col}` >= %s")
        params.append(gte)
    if lte is not None:
        where.append(f"`{col}` <= %s")
        params.append(lte)


def _link_field_kind(column_type):
    """把 MySQL 字段类型转换成前端筛选器可理解的类型。"""
    normalized = str(column_type or '').lower()
    if any(token in normalized for token in ('date', 'time', 'year')):
        return 'date'
    if any(token in normalized for token in ('int', 'decimal', 'numeric', 'double', 'float', 'real', 'bit')):
        return 'number'
    return 'text'


def _link_schema(cursor):
    cursor.execute(f"SHOW COLUMNS FROM {TABLE_NAME}")
    rows = cursor.fetchall()
    return {row[0]: _link_field_kind(row[1]) for row in rows}


def _apply_link_json_filters(where, params, raw_filters, field_types):
    """把高级筛选器安全地转换成参数化 SQL 条件。"""
    if not raw_filters:
        return
    try:
        filters = json.loads(raw_filters)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(filters, list):
        return

    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get('field') or '').strip()
        op = str(item.get('op') or 'contains').strip().lower()
        v1 = str(item.get('v1') or '').strip()
        v2 = str(item.get('v2') or '').strip()
        if not field or (not v1 and not v2):
            continue

        # 品牌是由店铺名称推导的展示字段，不是数据库物理字段。
        if field == '品牌':
            brand = v1 or v2
            if brand == '浪奇':
                where.append("`店铺名称` LIKE %s")
                params.append('%浪奇%')
            elif brand == '威王':
                where.append("`店铺名称` LIKE %s")
                params.append('%威王%')
            elif brand == '舒蕾':
                where.append("`店铺名称` LIKE %s")
                params.append('%舒蕾%')
            elif brand == '白牌':
                where.append("(`店铺名称` IS NULL OR (`店铺名称` NOT LIKE %s AND `店铺名称` NOT LIKE %s AND `店铺名称` NOT LIKE %s))")
                params.extend(['%浪奇%', '%威王%', '%舒蕾%'])
            continue

        if field not in field_types or field == 'id':
            continue
        kind = field_types[field]
        column = f"`{field}`"
        if kind == 'text':
            if op in ('equals', 'eq'):
                where.append(f"{column} = %s")
                params.append(v1)
            else:
                where.append(f"{column} LIKE %s")
                params.append(f"%{v1}%")
            continue

        if kind == 'date':
            if op == 'gte':
                where.append(f"{column} >= %s")
                params.append(v1)
            elif op == 'lte':
                where.append(f"{column} <= %s")
                params.append(v1)
            else:
                if v1:
                    where.append(f"{column} >= %s")
                    params.append(v1)
                if v2:
                    where.append(f"{column} <= %s")
                    params.append(v2)
            continue

        try:
            n1 = float(v1) if v1 else None
            n2 = float(v2) if v2 else None
        except ValueError:
            continue
        if op == 'gte' and n1 is not None:
            _add_range(where, params, field, n1, None)
        elif op == 'lte' and n1 is not None:
            _add_range(where, params, field, None, n1)
        else:
            _add_range(where, params, field, n1, n2)


@app.get("/api/v3/link-fields")
def get_link_fields():
    """返回链接明细表当前数据库字段，供字段选择器动态使用。"""
    conn = get_mysql()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SHOW COLUMNS FROM {TABLE_NAME}")
        fields = [
            {'key': row[0], 'label': row[0], 'type': _link_field_kind(row[1]), 'nullable': row[2] == 'YES'}
            for row in cursor.fetchall()
            if row[0] != 'id'
        ]
        return {'success': True, 'fields': fields}
    finally:
        conn.close()

@app.get("/api/v3/links")
def get_links(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: str = Query(default=""),
    start: str = Query(default=None),
    end: str = Query(default=None),
    link_ids: str = Query(default=None),
    # 多维度筛选参数
    product_code: str = Query(default=None),
    product_name: str = Query(default=None),
    store_name: str = Query(default=None),
    store_person: str = Query(default=None),
    profit_rate_gte: float = Query(default=None),
    profit_rate_lte: float = Query(default=None),
    gm_gte: float = Query(default=None),
    gm_lte: float = Query(default=None),
    promo_pct_gte: float = Query(default=None),
    promo_pct_lte: float = Query(default=None),
    cost_pct_gte: float = Query(default=None),
    cost_pct_lte: float = Query(default=None),
    revenue_gte: float = Query(default=None),
    revenue_lte: float = Query(default=None),
    orders_gte: float = Query(default=None),
    orders_lte: float = Query(default=None),
    filter_json: str = Query(default=""),
):
    """分页返回链接级明细数据（支持多维度筛选）"""
    conn = get_mysql()
    try:
        field_types = _link_schema(conn.cursor())
        where = ["1=1"]
        params = []
        if search:
            where.append("(链接id LIKE %s OR 商品编码 LIKE %s OR 商品标题 LIKE %s OR 店铺名称 LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if link_ids:
            ids = [x.strip() for x in link_ids.split(",") if x.strip()]
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                where.append(f"链接id IN ({placeholders})")
                params.extend(ids)
        if start:
            where.append("数据日期 >= %s")
            params.append(start)
        if end:
            where.append("数据日期 <= %s")
            params.append(end)
        # 多维度筛选
        if product_code:
            where.append("商品编码 LIKE %s")
            params.append(f"%{product_code}%")
        if product_name:
            where.append("商品标题 LIKE %s")
            params.append(f"%{product_name}%")
        if store_name:
            where.append("店铺名称 LIKE %s")
            params.append(f"%{store_name}%")
        if store_person:
            where.append("负责人 = %s")
            params.append(store_person)
        # 数值范围筛选
        _add_range(where, params, "利润率", profit_rate_gte, profit_rate_lte)
        _add_range(where, params, "毛利率", gm_gte, gm_lte)
        _add_range(where, params, "推广费占比", promo_pct_gte, promo_pct_lte)
        _add_range(where, params, "成本占比", cost_pct_gte, cost_pct_lte)
        _add_range(where, params, "收入", revenue_gte, revenue_lte)
        _add_range(where, params, "单量", orders_gte, orders_lte)
        _apply_link_json_filters(where, params, filter_json, field_types)

        where_clause = " AND ".join(where)

        # 总数
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE {where_clause}", params)
        total = cur.fetchone()[0]

        # 分页数据
        offset = (page - 1) * size
        cur.execute(
            f"SELECT * "
            f"FROM {TABLE_NAME} WHERE {where_clause} "
            f"ORDER BY 数据日期 DESC, 收入 DESC "
            f"LIMIT %s OFFSET %s",
            params + [size, offset]
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, row)) for row in rows]

        # 处理浮点数精度
        for item in data:
            for k, v in item.items():
                if isinstance(v, float):
                    item[k] = round(v, 4)

        return {
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size if total > 0 else 0
        }
    finally:
        conn.close()

@app.get("/api/v3/link-dashboard")
def get_link_dashboard(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: str = Query(default=""),
    start: str = Query(default=None),
    end: str = Query(default=None),
    store_person: str = Query(default=None),
    link_ids: str = Query(default=None),
    product_code: str = Query(default=None),
    store_name: str = Query(default=None),
):
    """返回链接明细看板所需的透视数据和连续亏损预警。

    /api/v3/links 保留一行一日的高级明细表接口；本接口按链接聚合，
    将日期利润率透视成 rates，避免前端重新拉取或内嵌整份历史数据。
    """
    conn = get_mysql()
    try:
        where = ["`链接id` IS NOT NULL", "`链接id` <> ''"]
        params = []
        if search:
            where.append("(`链接id` LIKE %s OR `商品编码` LIKE %s OR `商品标题` LIKE %s OR `店铺名称` LIKE %s)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if start:
            where.append("`数据日期` >= %s")
            params.append(start)
        if end:
            where.append("`数据日期` <= %s")
            params.append(end)
        if store_person:
            where.append("`负责人` = %s")
            params.append(store_person)
        if link_ids:
            ids = [item.strip() for item in link_ids.split(",") if item.strip()]
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                where.append(f"`链接id` IN ({placeholders})")
                params.extend(ids)
        if product_code:
            where.append("`商品编码` LIKE %s")
            params.append(f"%{product_code}%")
        if store_name:
            where.append("`店铺名称` LIKE %s")
            params.append(f"%{store_name}%")
        where_clause = " AND ".join(where)

        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(DISTINCT `链接id`) FROM {TABLE_NAME} WHERE {where_clause}", params)
        total = int(cur.fetchone()[0] or 0)

        cur.execute(
            f"SELECT DISTINCT `数据日期` FROM {TABLE_NAME} WHERE {where_clause} ORDER BY `数据日期`",
            params,
        )
        dates = [str(row[0])[:10] for row in cur.fetchall()]

        offset = (page - 1) * size
        cur.execute(
            f"SELECT `链接id`, MAX(`商品编码`), MAX(`商品标题`), MAX(`店铺名称`), "
            f"MAX(`负责人`), MAX(`数据日期`) AS latest_date, SUM(COALESCE(`收入`, 0)) AS revenue_sum "
            f"FROM {TABLE_NAME} WHERE {where_clause} "
            f"GROUP BY `链接id` ORDER BY latest_date DESC, revenue_sum DESC, `链接id` "
            f"LIMIT %s OFFSET %s",
            params + [size, offset],
        )
        page_rows = cur.fetchall()
        link_ids = [str(row[0]) for row in page_rows]

        def brand_of(store):
            store = str(store or "")
            if "浪奇" in store:
                return "浪奇"
            if "威王" in store or "VEWIN" in store.upper():
                return "威王"
            if "舒蕾" in store or "SLEK" in store.upper():
                return "舒蕾"
            return "白牌"

        page_data = {}
        if link_ids:
            placeholders = ",".join(["%s"] * len(link_ids))
            cur.execute(
                f"SELECT `链接id`, `商品编码`, `商品标题`, `店铺名称`, `负责人`, `数据日期`, `利润率` "
                f"FROM {TABLE_NAME} WHERE {where_clause} AND `链接id` IN ({placeholders}) "
                f"ORDER BY `链接id`, `数据日期`",
                params + link_ids,
            )
            for row in cur.fetchall():
                link_id = str(row[0])
                date = str(row[5])[:10]
                item = page_data.setdefault(link_id, {
                    "linkId": link_id,
                    "productCode": row[1] or "",
                    "title": row[2] or "",
                    "storeName": row[3] or "",
                    "person": row[4] or "",
                    "brand": brand_of(row[3]),
                    "rates": {},
                })
                rate = row[6]
                item["rates"][date] = round(float(rate), 6) if rate is not None else None

        # 预警按所有匹配链接计算，沿用原 HTML：只统计从最新日期开始连续为负的天数。
        cur.execute(
            f"SELECT `链接id`, `商品编码`, `店铺名称`, `数据日期`, `利润率` "
            f"FROM {TABLE_NAME} WHERE {where_clause} ORDER BY `链接id`, `数据日期`",
            params,
        )
        alert_rows = defaultdict(lambda: {"code": "", "store": "", "rates": {}})
        for row in cur.fetchall():
            link_id = str(row[0])
            entry = alert_rows[link_id]
            entry["code"] = row[1] or entry["code"]
            entry["store"] = row[2] or entry["store"]
            entry["rates"][str(row[3])[:10]] = row[4]

        alerts = {"a15": [], "a10": [], "a5": []}
        for link_id, entry in alert_rows.items():
            recent_negative_days = 0
            for date in reversed(dates):
                rate = entry["rates"].get(date)
                if rate is not None and float(rate) < 0:
                    recent_negative_days += 1
                else:
                    break
            alert = {"id": link_id, "code": entry["code"], "store": entry["store"], "days": recent_negative_days}
            if recent_negative_days >= 15:
                alerts["a15"].append(alert)
            elif recent_negative_days >= 10:
                alerts["a10"].append(alert)
            elif recent_negative_days >= 5:
                alerts["a5"].append(alert)
        for group in alerts.values():
            group.sort(key=lambda item: item["days"], reverse=True)

        return sanitize_json({
            "success": True,
            "data": [page_data[link_id] for link_id in link_ids if link_id in page_data],
            "dates": dates,
            "alerts": {key: value[:50] for key, value in alerts.items()},
            "alertCounts": {key: len(value) for key, value in alerts.items()},
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size if total > 0 else 0,
        })
    finally:
        conn.close()

@app.get("/api/v3/status")
def get_status():
    """系统状态"""
    conn = get_mysql()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*), MIN(数据日期), MAX(数据日期) FROM {TABLE_NAME}")
        row = cur.fetchone()
        db_info = {"rows": row[0], "min_date": str(row[1]) if row[1] else None, "max_date": str(row[2]) if row[2] else None}
    except Exception as e:
        db_info = {"error": str(e)}
    finally:
        conn.close()

    # 可用的 xlsx 文件
    xlsx_count = len(list_xlsx_files())

    return {
        "database": db_info,
        "xlsx_files_available": xlsx_count,
        "server_time": datetime.now().isoformat()
    }

# ============ 管理中台目标值持久化 ============
@app.get("/api/v3/admin/targets")
def get_admin_targets(month: str = Query(default=None)):
    """读取目标值配置。month 为空则返回所有月份"""
    conn = get_mysql()
    try:
        cur = conn.cursor()
        if month:
            cur.execute("SELECT target_month, config_json FROM admin_targets WHERE target_month = %s", (month,))
        else:
            cur.execute("SELECT target_month, config_json FROM admin_targets ORDER BY target_month")
        rows = cur.fetchall()
        result = {}
        for r in rows:
            try:
                result[r[0]] = json.loads(r[1])
            except:
                result[r[0]] = {}
        return {"success": True, "data": result}
    finally:
        conn.close()

@app.post("/api/v3/admin/targets")
async def save_admin_targets(request: Request):
    """保存目标值配置。body: {month: '2026-07', config: {...}}"""
    data = await request.json()
    month = data.get("month", "")
    config = data.get("config", {})
    if not month:
        return {"error": "请提供 month 参数"}
    config_json = json.dumps(config, ensure_ascii=False)
    conn = get_mysql()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO admin_targets (target_month, config_json) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE config_json = VALUES(config_json)",
            (month, config_json)
        )
        conn.commit()
        return {"success": True, "message": f"已保存 {month} 目标值"}
    finally:
        conn.close()

# ============ 用户认证与权限 ============
import hashlib

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

@app.post("/api/v3/auth/login")
async def auth_login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return {"success": False, "error": "用户名和密码不能为空"}
    conn = get_mysql()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, display_name FROM dashboard_users WHERE username=%s AND password_hash=%s",
                     (username, hash_password(password)))
        row = cur.fetchone()
        if row:
            return {"success": True, "user": {"id": row[0], "username": row[1], "role": row[2], "display_name": row[3] or row[1]}}
        return {"success": False, "error": "用户名或密码错误"}
    finally:
        conn.close()

@app.get("/api/v3/admin/users")
async def admin_list_users(request: Request):
    username = request.query_params.get("username", "")
    password = request.query_params.get("password", "")
    if not username or not password:
        return {"success": False, "error": "请提供认证信息"}
    conn = get_mysql()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM dashboard_users WHERE username=%s AND password_hash=%s",
                     (username, hash_password(password)))
        row = cur.fetchone()
        if not row or row[0] != "admin":
            return {"success": False, "error": "无权限"}
        cur.execute("SELECT id, username, password_plain, role, display_name, created_at FROM dashboard_users ORDER BY id")
        users = [{"id": r[0], "username": r[1], "password": r[2] or "", "role": r[3], "display_name": r[4], "created_at": str(r[5])} for r in cur.fetchall()]
        return {"success": True, "users": users}
    finally:
        conn.close()

@app.post("/api/v3/admin/users")
async def admin_save_user(request: Request):
    data = await request.json()
    username = data.get("auth_username", "")
    password = data.get("auth_password", "")
    if not username or not password:
        return {"success": False, "error": "请提供认证信息"}
    conn = get_mysql()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM dashboard_users WHERE username=%s AND password_hash=%s",
                     (username, password))
        row = cur.fetchone()
        if not row or row[0] != "admin":
            return {"success": False, "error": "无权限"}
        action = data.get("action", "add")
        if action == "add":
            nu = data.get("new_username", "").strip()
            np = data.get("new_password", "")
            nr = data.get("new_role", "user")
            nd = data.get("new_display", nu)
            if not nu or not np:
                return {"success": False, "error": "用户名和密码不能为空"}
            try:
                cur.execute("INSERT INTO dashboard_users (username, password_hash, role, display_name) VALUES (%s,%s,%s,%s)",
                             (nu, hash_password(np), nr, nd))
                conn.commit()
                return {"success": True, "message": f"已添加用户 {nu}"}
            except pymysql.IntegrityError:
                return {"success": False, "error": f"用户名 {nu} 已存在"}
        elif action == "update":
            uid = data.get("user_id")
            nr = data.get("new_role", "")
            nd = data.get("new_display", "")
            if not uid:
                return {"success": False, "error": "请指定用户"}
            parts = []; vals = []
            if nr: parts.append("role=%s"); vals.append(nr)
            if nd: parts.append("display_name=%s"); vals.append(nd)
            if parts:
                vals.append(uid)
                cur.execute(f"UPDATE dashboard_users SET {', '.join(parts)} WHERE id=%s", vals)
                conn.commit()
                return {"success": True, "message": "已更新"}
            return {"success": False, "error": "无更新内容"}
        elif action == "delete":
            uid = data.get("user_id")
            if not uid:
                return {"success": False, "error": "请指定用户"}
            cur.execute("DELETE FROM dashboard_users WHERE id=%s AND username!='admin'", (uid,))
            conn.commit()
            return {"success": True, "message": "已删除" if cur.rowcount else "无法删除（admin不可删除）"}
        elif action == "reset_pw":
            uid = data.get("user_id")
            np = data.get("new_password", "")
            if not uid or not np:
                return {"success": False, "error": "请指定用户和新密码"}
            cur.execute("UPDATE dashboard_users SET password_hash=%s, password_hash=%s WHERE id=%s", (hash_password(np), uid))
            conn.commit()
            return {"success": True, "message": "密码已重置"}
    finally:
        conn.close()

# ============ 数据聚合 ============
def sanitize_json(obj):
    """递归清理 NaN/Inf，确保 JSON 可序列化"""
    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_json(v) for v in obj]
    elif isinstance(obj, float):
        if pd.isna(obj) or np.isinf(obj):
            return 0.0
        return obj
    return obj

def aggregate_dashboard_data(df):
    """从明细 DataFrame 聚合出看板需要的所有数据"""
    # === 全局汇总 grand ===
    grand = {
        "orders": int(df["单量"].sum()),
        "revenue": round(float(df["收入"].sum()), 2),
        "cost": round(float(df["成本"].sum()), 2),
        "shipping": round(float(df["快递"].sum()), 2),
        "gross_profit": round(float(df["毛利"].sum()), 2),
        "promotion": round(float(df["推广费"].sum()), 2),
        "platform_profit": round(float(df["平台利润"].sum()), 2),
        "stores": int(df["店铺名称"].nunique()),
    }
    grand["gross_margin"] = round(grand["gross_profit"] / grand["revenue"] * 100, 1) if grand["revenue"] > 0 else 0
    grand["profit_rate"] = round(grand["platform_profit"] / grand["revenue"] * 100, 1) if grand["revenue"] > 0 else 0

    # === 负责人汇总 ===
    person = df.groupby("负责人").agg(
        revenue=("收入", "sum"), cost=("成本", "sum"), shipping=("快递", "sum"),
        promotion=("推广费", "sum"), platform_profit=("平台利润", "sum"),
        orders=("单量", "sum"), stores=("店铺名称", "nunique")
    ).reset_index()
    person = person[person["负责人"].notna() & (person["负责人"] != "")]
    person["gross_profit"] = person["revenue"] - person["cost"] - person["shipping"]
    person["gross_margin"] = (person["gross_profit"] / person["revenue"] * 100).round(1)
    person["promotion_pct"] = (person["promotion"] / person["revenue"] * 100).round(1)
    person["profit_rate"] = (person["platform_profit"] / person["revenue"] * 100).round(1)
    person = person.rename(columns={"负责人": "name"})
    people_summary = person.sort_values("revenue", ascending=False).to_dict("records")

    # === 商品汇总 ===
    prod = df.groupby("商品编码").agg(
        revenue=("收入", "sum"), cost=("成本", "sum"), shipping=("快递", "sum"),
        promotion=("推广费", "sum"), platform_profit=("平台利润", "sum"),
        orders=("单量", "sum"), name=("商品标题", "first")
    ).reset_index()
    prod = prod[prod["商品编码"].notna() & (prod["商品编码"] != "")]
    prod["cost_pct"] = (prod["cost"] / prod["revenue"] * 100).round(1)
    prod["shipping_pct"] = (prod["shipping"] / prod["revenue"] * 100).round(1)
    prod["gross_profit"] = prod["revenue"] - prod["cost"] - prod["shipping"]
    prod["gross_margin"] = (prod["gross_profit"] / prod["revenue"] * 100).round(1)
    prod["promotion_pct"] = (prod["promotion"] / prod["revenue"] * 100).round(1)
    prod["profit_rate"] = (prod["platform_profit"] / prod["revenue"] * 100).round(1)
    prod = prod.rename(columns={"商品编码": "code"})
    products = prod.sort_values("revenue", ascending=False).to_dict("records")

    # === 店铺汇总 ===
    store = df.groupby(["店铺名称", "负责人"]).agg(
        revenue=("收入", "sum"), cost=("成本", "sum"), shipping=("快递", "sum"),
        promotion=("推广费", "sum"), platform_profit=("平台利润", "sum"),
        orders=("单量", "sum"),
    ).reset_index()
    store = store[store["店铺名称"].notna() & (store["店铺名称"] != "")]
    store["cost_pct"] = (store["cost"] / store["revenue"] * 100).round(1)
    store["shipping_pct"] = (store["shipping"] / store["revenue"] * 100).round(1)
    store["gross_profit"] = store["revenue"] - store["cost"] - store["shipping"]
    store["gross_margin"] = (store["gross_profit"] / store["revenue"] * 100).round(1)
    store["promotion_pct"] = (store["promotion"] / store["revenue"] * 100).round(1)
    store["profit_rate"] = (store["platform_profit"] / store["revenue"] * 100).round(1)
    store = store.rename(columns={"店铺名称": "store", "负责人": "person"})
    all_stores = store.sort_values("revenue", ascending=False).to_dict("records")

    # === 每日趋势 ===
    daily = df.groupby("数据日期").agg(
        revenue=("收入", "sum"), cost=("成本", "sum"), shipping=("快递", "sum"),
        promotion=("推广费", "sum"), profit=("平台利润", "sum"), orders=("单量", "sum"),
    ).reset_index()
    daily["profit_rate"] = (daily["profit"] / daily["revenue"] * 100).round(2)
    daily = daily.rename(columns={"数据日期": "date"})
    daily_overall = daily.sort_values("date").to_dict("records")

    # === 每日×人 ===
    dp = df.groupby(["数据日期", "负责人"]).agg(
        revenue=("收入", "sum"), cost=("成本", "sum"), shipping=("快递", "sum"),
        promotion=("推广费", "sum"), profit=("平台利润", "sum"), orders=("单量", "sum"),
    ).reset_index()
    dp["profit_rate"] = (dp["profit"] / dp["revenue"] * 100).round(2)
    daily_by_person = {}
    for _, r in dp.iterrows():
        d = str(r["数据日期"])[:10]
        daily_by_person.setdefault(d, {})[str(r["负责人"])] = {
            "revenue": round(float(r["revenue"]), 2), "cost": round(float(r["cost"]), 2),
            "shipping": round(float(r["shipping"]), 2), "promotion": round(float(r["promotion"]), 2),
            "profit": round(float(r["profit"]), 2), "orders": int(r["orders"]),
            "profit_rate": round(float(r["profit_rate"]), 2),
        }

    # === 每日×商品 ===
    dpr = df.groupby(["数据日期", "商品编码"]).agg(
        revenue=("收入", "sum"), profit=("平台利润", "sum"),
    ).reset_index()
    daily_by_product = {}
    for _, r in dpr.iterrows():
        daily_by_product.setdefault(str(r["数据日期"])[:10], {})[str(r["商品编码"])] = {
            "revenue": round(float(r["revenue"]), 2), "profit": round(float(r["profit"]), 2),
        }

    # === 每日×店铺 ===
    dst = df.groupby(["数据日期", "店铺名称"]).agg(
        revenue=("收入", "sum"), cost=("成本", "sum"), shipping=("快递", "sum"), promotion=("推广费", "sum"), orders=("单量", "sum"), profit=("平台利润", "sum"),
    ).reset_index()
    daily_by_store = {}
    for _, r in dst.iterrows():
        daily_by_store.setdefault(str(r["数据日期"])[:10], {})[str(r["店铺名称"])] = {
            "revenue": round(float(r["revenue"]), 2),
            "cost": round(float(r["cost"]), 2),
            "shipping": round(float(r["shipping"]), 2),
            "promotion": round(float(r["promotion"]), 2),
            "orders": int(r["orders"]),
            "profit": round(float(r["profit"]), 2),
        }

    result = {
        "grand": grand,
        "peopleSummary": people_summary,
        "products": products,
        "allStores": all_stores,
        "dailyOverall": daily_overall,
        "dailyByPerson": daily_by_person,
        "dailyByProduct": daily_by_product,
        "dailyByStore": daily_by_store,
    }
    return sanitize_json(result)

# ============ 后台调度 ============
def etl_scheduler():
    """每小时自动运行 ETL"""
    while True:
        time.sleep(ETL_INTERVAL)
        try:
            run_etl()
        except Exception as e:
            print(f"⏰ 定时 ETL 失败: {e}")

# ============ 启动 ============
if __name__ == "__main__":
    LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("📊 利润率看板 V3 — 数据管道")
    print(f"   ETL: {NETWORK_BASE} → MySQL {TABLE_NAME}")
    print(f"   API: http://0.0.0.0:{API_PORT}")
    print("=" * 60)
    
    # 检查是否已有数据，跳过重复ETL
    try:
        conn = get_mysql()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
        existing = cur.fetchone()[0]
        conn.close()
        if existing > 0:
            print(f"\n✅ 已有 {existing} 行数据，跳过首次 ETL")
        else:
            print("\n▶ 首次 ETL 数据加载...")
            run_etl()
    except:
        print("\n▶ 首次 ETL 数据加载...")
        run_etl()

    # 后台定时任务
    t = threading.Thread(target=etl_scheduler, daemon=True)
    t.start()
    print(f"⏰ 定时 ETL 已启动 (每 {ETL_INTERVAL//3600} 小时)\n")

    # 挂载静态文件目录
    static_dir = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    
    @app.get("/profit-dashboard-v3")
    @app.get("/v3")
    async def serve_v3():
        return FileResponse(str(static_dir / "profit-dashboard-v3.html"))
    
    uvicorn.run(app, host="0.0.0.0", port=API_PORT, log_level="info")
