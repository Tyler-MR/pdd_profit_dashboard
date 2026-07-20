"""Export one dashboard period as anonymized, browser-friendly demo data.

The source files stay local. Only the generated JSON fixtures under
``profit-dashboard-vue/public/demo-data`` are intended to be committed.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_server_v3 as api  # noqa: E402


COL_STORE = "\u5e97\u94fa\u540d\u79f0"
COL_PERSON = "\u8d1f\u8d23\u4eba"
COL_TITLE = "\u5546\u54c1\u6807\u9898"
COL_LINK = "\u94fe\u63a5id"
COL_CODE = "\u5546\u54c1\u7f16\u7801"
COL_DATE = "\u6570\u636e\u65e5\u671f"
COL_RATE = "\u5229\u6da6\u7387"
COL_REVENUE = "\u6536\u5165"
COL_BRAND = "\u54c1\u724c"
DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


def json_safe(value: Any) -> Any:
    """Convert pandas/numpy values to strict JSON-safe Python values."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return 0.0
    return value


def text_value(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def brand_of(store: str) -> str:
    if "\u6d6a\u5947" in store:
        return "\u6d6a\u5947"
    if "\u5a01\u738b" in store or "VEWIN" in store.upper():
        return "\u5a01\u738b"
    if "\u8212\u857e" in store or "SLEK" in store.upper():
        return "\u8212\u857e"
    return "\u767d\u724c"


def period_files(source_dir: Path, start: str, end: str) -> list[Path]:
    files: list[Path] = []
    for path in sorted(source_dir.glob("*.xlsx")):
        if path.name.startswith("~$"):
            continue
        match = DATE_PATTERN.search(path.name)
        if match and start <= match.group(1) <= end:
            files.append(path)
    return files


def load_period(source_dir: Path, start: str, end: str) -> pd.DataFrame:
    files = period_files(source_dir, start, end)
    if not files:
        raise FileNotFoundError(f"No dated .xlsx files found in {source_dir} for {start}..{end}")

    # The API ETL helper is also used here so the fixture has the same field
    # names and cleaning rules as the production API. The helper's normal
    # network copy is intentionally bypassed because the source is local.
    api.safe_copy = lambda remote, _local_dir: Path(remote)
    frames: list[pd.DataFrame] = []
    for path in files:
        frame = api.process_single_xlsx(int(path.name[10:12]), path, source_dir)
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        raise RuntimeError("The selected files did not produce any usable rows")
    return pd.concat(frames, ignore_index=True)


def map_values(values: pd.Series, prefix: str, width: int = 3) -> dict[str, str]:
    unique = sorted({text_value(value) for value in values if text_value(value)})
    return {value: f"{prefix}{index:0{width}d}" for index, value in enumerate(unique, start=1)}


def anonymize(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()

    person_map = map_values(df[COL_PERSON], "\u8d1f\u8d23\u4eba ", 2)
    code_map = map_values(df[COL_CODE], "DEMO-P", 3)
    link_map = map_values(df[COL_LINK], "DEMO-L", 5)
    title_map = map_values(df[COL_TITLE], "\u6f14\u793a\u5546\u54c1 ", 4)

    store_values = sorted({text_value(value) for value in df[COL_STORE] if text_value(value)})
    store_map = {
        value: f"{brand_of(value)}\u6f14\u793a\u5e97\u94fa{index:03d}"
        for index, value in enumerate(store_values, start=1)
    }

    for column, mapping in (
        (COL_PERSON, person_map),
        (COL_CODE, code_map),
        (COL_LINK, link_map),
        (COL_TITLE, title_map),
        (COL_STORE, store_map),
    ):
        df[column] = df[column].map(lambda value: mapping.get(text_value(value), ""))

    df["\u6765\u6e90\u6587\u4ef6"] = df[COL_DATE].map(lambda value: f"demo-{str(value)[:10]}.xlsx")
    df[COL_DATE] = df[COL_DATE].map(lambda value: str(value)[:10])

    # Keep the fixture compact while retaining the precision used by the API.
    numeric_columns = df.select_dtypes(include=["number"]).columns
    df[numeric_columns] = df[numeric_columns].round(4)
    return df


def field_schema(df: pd.DataFrame) -> list[dict[str, Any]]:
    fields = []
    for column in df.columns:
        if column == COL_DATE:
            kind = "date"
        elif pd.api.types.is_numeric_dtype(df[column]):
            kind = "number"
        else:
            kind = "text"
        fields.append({"key": column, "label": column, "type": kind, "nullable": True})
    return fields


def link_dashboard_payload(df: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    dates = sorted(df[COL_DATE].astype(str).str[:10].unique().tolist())
    rows = []
    alerts_by_id = {}

    for link_id, group in df.groupby(COL_LINK, sort=False):
        group = group.sort_values(COL_DATE)
        latest = group.iloc[-1]
        rates = {}
        alert_rates = {}
        for _, item in group.iterrows():
            date = str(item[COL_DATE])[:10]
            rate = item.get(COL_RATE)
            numeric_rate = None if pd.isna(rate) else round(float(rate), 6)
            rates[date] = numeric_rate
            alert_rates[date] = numeric_rate
        rows.append(
            {
                "linkId": str(link_id),
                "productCode": text_value(latest.get(COL_CODE)),
                "title": text_value(latest.get(COL_TITLE)),
                "storeName": text_value(latest.get(COL_STORE)),
                "person": text_value(latest.get(COL_PERSON)),
                "brand": brand_of(text_value(latest.get(COL_STORE))),
                "rates": rates,
            }
        )
        alerts_by_id[str(link_id)] = {
            "code": text_value(latest.get(COL_CODE)),
            "store": text_value(latest.get(COL_STORE)),
            "rates": alert_rates,
        }

    alerts = {"a15": [], "a10": [], "a5": []}
    for link_id, entry in alerts_by_id.items():
        negative_days = 0
        for date in reversed(dates):
            rate = entry["rates"].get(date)
            if rate is not None and float(rate) < 0:
                negative_days += 1
            else:
                break
        alert = {"id": link_id, "code": entry["code"], "store": entry["store"], "days": negative_days}
        if negative_days >= 15:
            alerts["a15"].append(alert)
        elif negative_days >= 10:
            alerts["a10"].append(alert)
        elif negative_days >= 5:
            alerts["a5"].append(alert)
    for group in alerts.values():
        group.sort(key=lambda item: item["days"], reverse=True)

    total = len(rows)
    return {
        "success": True,
        "data": rows,
        "dates": dates,
        "alerts": {key: value[:50] for key, value in alerts.items()},
        "alertCounts": {key: len(value) for key, value in alerts.items()},
        "total": total,
        "page": 1,
        "size": 20,
        "pages": (total + 19) // 20,
        "meta": {"start": start, "end": end, "rows": len(df)},
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def export(source_dir: Path, output_dir: Path, start: str, end: str) -> None:
    raw = load_period(source_dir, start, end)
    df = anonymize(raw)
    data = api.aggregate_dashboard_data(df)
    fields = field_schema(df)
    dashboard = {
        "success": True,
        "data": data,
        "status": {
            "database": {
                "rows": len(df),
                "min_date": start,
                "max_date": end,
            },
            "xlsx_files_available": len(period_files(source_dir, start, end)),
            "server_time": "demo-fixture",
        },
        "targets": {},
        "linkFields": fields,
        "meta": {
            "start": start,
            "end": end,
            "rows": len(df),
            "notice": "公开仓库演示数据：业务标识已匿名化，金额与指标保留用于界面演示。",
        },
    }
    dashboard_name = f"dashboard-{start}_{end}.json"
    links_name = f"links-{start}_{end}.json"
    link_dashboard_name = f"link-dashboard-{start}_{end}.json"

    write_json(output_dir / dashboard_name, dashboard)
    write_json(
        output_dir / links_name,
        {
            "success": True,
            "data": json.loads(df.to_json(orient="records", force_ascii=False)),
            "total": len(df),
            "meta": {"start": start, "end": end, "rows": len(df), "sample": False},
        },
    )
    write_json(output_dir / link_dashboard_name, link_dashboard_payload(df, start, end))
    print(json.dumps({"files": [dashboard_name, links_name, link_dashboard_name], "rows": len(df), "links": int(df[COL_LINK].nunique())}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "cache_v3_etl" / "etl")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "profit-dashboard-vue" / "public" / "demo-data")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-07-14")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be earlier than or equal to --end")
    export(args.source_dir, args.output_dir, args.start, args.end)


if __name__ == "__main__":
    main()
