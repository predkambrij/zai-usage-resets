#!/usr/bin/env python3
"""Compare daily Z.ai billing and usage; save a chart and an exact CSV report."""

import argparse
import csv
import calendar
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# API helpers are kept here so this script can run on its own.
class BillingError(Exception):
    pass


def load_env():
    path = Path(os.environ.get("ZAI_ENV_FILE", Path(__file__).resolve().with_name(".env")))
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ")
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key, value)


def request(base, path, key, params=None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as response:
            body = json.load(response, parse_float=Decimal)
    except HTTPError as exc:
        raise BillingError(f"API request failed: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise BillingError("API request failed or returned invalid JSON") from exc
    if not isinstance(body, dict) or body.get("success") is not True:
        raise BillingError("API reported failure (use the Z.ai platform API key)")
    if not isinstance(body.get("data"), dict):
        raise BillingError("Unexpected API data structure")
    return body["data"]


def collect_rows(base, key, customer_id, period, *, granularity="day", billing_day=None):
    """Fetch ledger pages, stopping below billing_day for newest-first minute data."""
    if granularity not in ("day", "minutely"):
        raise BillingError("Billing granularity must be day or minutely")

    def fetch_page(page):
        data = request(base, f"/api/platform-charge-zai/bill/{granularity}", key, {
            "customerId": customer_id, "billingPeriod": period,
            "pageNum": page, "pageSize": 100,  # The server caps larger requests at 100.
        })
        records, pages, total = data.get("records"), data.get("pages"), data.get("total")
        if (not isinstance(records, list) or type(pages) is not int or pages < 0
                or type(total) is not int or total < 0):
            raise BillingError("Unexpected billing pagination structure")
        if "current" in data and data["current"] != page:
            raise BillingError("Billing pagination changed during retrieval; retry")
        if any(not isinstance(row, dict) for row in records):
            raise BillingError("Unexpected billing row structure")
        if not records and total > 0:
            raise BillingError("Incomplete billing ledger; retry")
        return data

    # Read the first page to discover the page count before scheduling the rest.
    first = fetch_page(1)
    expected_total, expected_pages = first["total"], first["pages"]
    if expected_pages > 10000:
        raise BillingError("Billing pagination exceeded 10000 pages")
    rows = []
    scanned_count = 0
    previous_time = None

    def append_page(data):
        nonlocal scanned_count, previous_time
        if data["total"] != expected_total or data["pages"] != expected_pages:
            raise BillingError("Billing pagination changed during retrieval; retry")
        scanned_count += len(data["records"])
        passed_day = False
        for row in data["records"]:
            if billing_day is None:
                rows.append(row)
                continue
            try:
                timestamp = datetime.fromisoformat(row["billingDate"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BillingError("Invalid minutely billingDate; cannot select day") from exc
            if timestamp.tzinfo is not None:
                raise BillingError("Expected minutely timestamps in local billing time (UTC+8)")
            if previous_time is not None and timestamp > previous_time:
                raise BillingError("Minutely rows are not newest first; cannot stop safely")
            previous_time = timestamp
            day = timestamp.date().isoformat()
            if day == billing_day:
                rows.append(row)
            elif day < billing_day:
                passed_day = True
        return passed_day

    if append_page(first):
        return rows
    workers = 4 if granularity == "minutely" else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Schedule only one small batch at a time, avoiding the rest of the month
        # once an older day is reached. At most three extra pages are in flight.
        for batch_start in range(2, expected_pages + 1, workers):
            pages = range(batch_start, min(batch_start + workers, expected_pages + 1))
            for data in executor.map(fetch_page, pages):
                if append_page(data):
                    return rows
    if scanned_count != expected_total:
        raise BillingError("Incomplete billing ledger; retry")
    return rows


def number(row, field):
    value = row.get(field)
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result
    except InvalidOperation as exc:
        raise BillingError(f"Invalid or missing numeric billing field: {field}") from exc


def empty_summary():
    return {name: Decimal(0) for name in (
        "inputTokens", "cachedInputTokens", "outputTokens", "otherTokens", "totalTokens", "calls",
        "listPriceCost", "cashAmount", "creditAmount", "giftAmount", "actualBilledAmount",
        "planCoveredEstimate",
    )}


def row_list_price_cost(row):
    """Apply the ledger's own unit price, without hardcoding model rates."""
    unit = row.get("costUnit")
    if unit not in ("kToken", "token", "time"):
        raise BillingError(f"Unsupported billing cost unit: {unit!r}")
    divisor = 1000 if unit == "kToken" else 1
    return number(row, "usageCount") * number(row, "costPrice") / divisor


def summarize(rows):
    models = defaultdict(empty_summary)
    for row in rows:
        usage = number(row, "usageCount")
        cost = row_list_price_cost(row)
        summary = models[str(row.get("modelCode") or row.get("productCode") or "unknown")]
        if row.get("usageUnit") == "token":
            field = {"INPUT": "inputTokens", "CACHE": "cachedInputTokens",
                     "OUTPUT": "outputTokens"}.get(row.get("tokenType"), "otherTokens")
            summary[field] += usage
        summary["calls"] += number(row, "apiUsage")
        summary["listPriceCost"] += cost
        cash = number(row, "cashAmount")
        credit = number(row, "creditPayAmount")
        gift = number(row, "giftDeductAmount")
        billed = cash + credit + gift
        summary["cashAmount"] += cash
        summary["creditAmount"] += credit
        summary["giftAmount"] += gift
        summary["actualBilledAmount"] += billed
    totals = empty_summary()
    for summary in models.values():
        summary["totalTokens"] = sum(summary[field] for field in
                                     ("inputTokens", "cachedInputTokens", "outputTokens", "otherTokens"))
        summary["planCoveredEstimate"] = max(Decimal(0), summary["listPriceCost"] - summary["actualBilledAmount"])
        for field, value in summary.items():
            totals[field] += value
    return totals, dict(sorted(models.items()))


def format_tokens(value):
    for divisor, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if value >= divisor:
            return f"{value / Decimal(divisor):.2f} {suffix}"
    return f"{value:f}"


def usage_dates(period, start=None, end=None):
    year, month = map(int, period.split("-"))
    try:
        first = date.fromisoformat(start) if start else date(year, month, 1)
        last = date.fromisoformat(end) if end else date(year, month, calendar.monthrange(year, month)[1])
    except ValueError as exc:
        raise BillingError("Invalid usage date; expected YYYY-MM-DD") from exc
    if not 0 <= (last - first).days < 31:
        raise BillingError("Usage range must contain 1 to 31 days, inclusive")
    return first.isoformat(), last.isoformat()


def fetch_usage_report(base, key, start, end):
    data = request(base, "/api/monitor/usage/model-usage", key, {
        "startTime": start + " 00:00:00", "endTime": end + " 23:59:59",
    })
    rollup = data.get("totalUsage")
    if not isinstance(rollup, dict) or not isinstance(rollup.get("modelSummaryList"), list):
        raise BillingError("Unexpected usage-report summary structure")
    models = {}
    for model in rollup["modelSummaryList"]:
        if not isinstance(model, dict) or not isinstance(model.get("modelName"), str):
            raise BillingError("Unexpected usage-report model structure")
        name = model["modelName"]
        if name in models:
            raise BillingError("Duplicate usage-report model")
        models[name] = number(model, "totalTokens")
    total = sum(models.values(), Decimal(0))
    if total != number(rollup, "totalTokensUsage"):
        raise BillingError("Usage-report model totals do not match overall consumption")
    days = defaultdict(lambda: defaultdict(Decimal))
    times, series = data.get("x_time"), data.get("modelDataList")
    if not isinstance(times, list) or not isinstance(series, list):
        raise BillingError("Usage report is missing daily series for billing overlay")
    seen = set()
    for model in series:
        if not isinstance(model, dict):
            raise BillingError("Unexpected usage-report series")
        name, values = model.get("modelName"), model.get("tokensUsage")
        if name not in models or name in seen or not isinstance(values, list) or len(values) != len(times):
            raise BillingError("Usage-report series does not match model summary")
        seen.add(name)
        model_total = Decimal(0)
        for timestamp, value in zip(times, values):
            try:
                day = date.fromisoformat(timestamp[:10]).isoformat()
            except (ValueError, TypeError) as exc:
                raise BillingError("Invalid usage-report bucket date") from exc
            if not start <= day <= end:
                raise BillingError("Usage-report bucket outside requested interval")
            tokens = number({"tokens": value}, "tokens")
            days[day][name] += tokens
            model_total += tokens
        if model_total != models[name]:
            raise BillingError("Usage-report series does not sum to model total")
    if seen != set(models):
        raise BillingError("Usage report is missing model series")
    return {"startDate": start, "endDate": end, "timezone": "UTC+8",
            "totalTokens": total, "formattedConsumption": format_tokens(total),
            "calls": number(rollup, "totalModelCallCount"),
            "byModel": {name: {"totalTokens": tokens, "formattedConsumption": format_tokens(tokens)}
                        for name, tokens in models.items()},
            "byDay": {day: dict(values) for day, values in sorted(days.items())}}


def collect_interval_rows(base, key, customer_id, start, end):
    """Fetch each intersecting billing month and retain only requested days."""
    current = date.fromisoformat(start).replace(day=1)
    periods, rows = [], []
    while current.isoformat() <= end:
        period = current.strftime("%Y-%m")
        periods.append(period)
        for row in collect_rows(base, key, customer_id, period):
            try:
                day = date.fromisoformat(row.get("billingDate", "")).isoformat()
            except (ValueError, TypeError) as exc:
                raise BillingError("Invalid ledger billingDate; cannot align interval") from exc
            if start <= day <= end:
                rows.append(row)
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return rows, periods


def build_overlay(report, rows):
    """Join usage and ledger by UTC+8 calendar day and case-insensitive model."""
    def entry(usage_tokens, selected):
        totals, _ = summarize(selected)
        return {"usageTokens": usage_tokens, "billing": totals, "billingRows": len(selected),
                "tokenDifference": usage_tokens - totals["totalTokens"]}

    by_model = defaultdict(list)
    by_day = defaultdict(list)
    for row in rows:
        name = str(row.get("modelCode") or row.get("productCode") or "unknown").casefold()
        by_model[name].append(row)
        by_day[row["billingDate"]].append(row)
    usage_models = {name.casefold(): summary["totalTokens"] for name, summary in report["byModel"].items()}
    if len(usage_models) != len(report["byModel"]):
        raise BillingError("Ambiguous usage model names after case normalization")
    models = {name: entry(usage_models.get(name, Decimal(0)), by_model[name])
              for name in sorted(set(usage_models) | set(by_model))}
    days = {}
    current = date.fromisoformat(report["startDate"])
    while current.isoformat() <= report["endDate"]:
        day = current.isoformat()
        day_usage = sum(report["byDay"].get(day, {}).values(), Decimal(0))
        days[day] = entry(day_usage, by_day[day])
        current += timedelta(days=1)
    return {"startDate": report["startDate"], "endDate": report["endDate"], "timezone": "UTC+8",
            "totals": entry(report["totalTokens"], rows), "byModel": models, "byDay": days}


@dataclass(frozen=True)
class DailyReport:
    day: date
    usage_tokens: Decimal
    billing_tokens: Decimal
    billing_rows: int
    input_tokens: Decimal
    cached_input_tokens: Decimal
    output_tokens: Decimal
    other_tokens: Decimal
    list_price_cost: Decimal
    actual_billed_amount: Decimal
    billing_source: str = "day"

    @property
    def gap_tokens(self):
        """Positive means usage is higher; negative means billing is higher."""
        return self.usage_tokens - self.billing_tokens


def fetch_daily_reports(start, end):
    """Return daily summaries and ledger rows for category/model cost breakdowns."""
    load_env()
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        raise BillingError("Set ZAI_API_KEY in the environment or .env")
    base = os.environ.get("ZAI_API_BASE", "https://api.z.ai")

    usage = fetch_usage_report(base, key, start, end)
    customer_id = os.environ.get("ZAI_CUSTOMER_ID")
    if customer_id is None:
        customer = request(base, "/api/biz/customer-package-reset/list", key,
                               {"targetType": "PERSONAL"})
        customer_id = str(customer.get("customerId", ""))
    if not re.fullmatch(r"[0-9]+", customer_id):
        raise BillingError("Cannot discover customer ID; set ZAI_CUSTOMER_ID")

    rows, _ = collect_interval_rows(base, key, customer_id, start, end)
    rows = fill_last_day_from_minutely(base, key, customer_id, rows, end)
    sources = {row["billingDate"]: row.get("_billingSource", "day") for row in rows}
    overlay = build_overlay(usage, rows)
    return daily_reports_from_overlay(overlay, sources), rows


def fill_last_day_from_minutely(base, key, customer_id, daily_rows, last_day):
    """Use minute records only when the final day has no daily ledger rows."""
    if any(row["billingDate"] == last_day for row in daily_rows):
        return daily_rows

    minute_rows = collect_rows(base, key, customer_id, last_day[:7], granularity="minutely", billing_day=last_day)
    fallback = []
    for row in minute_rows:
        try:
            day = datetime.fromisoformat(row["billingDate"]).date().isoformat()
        except (KeyError, TypeError, ValueError) as exc:
            raise BillingError("Invalid minutely billingDate; cannot align final day") from exc
        if day == last_day:
            fallback.append({**row, "billingDate": day, "_billingSource": "minutely"})
    return daily_rows + fallback


def daily_reports_from_overlay(overlay, sources=None):
    """Flatten the shared overlay into named fields used by the table and chart."""
    reports = []
    for day, item in sorted(overlay["byDay"].items()):
        bill = item["billing"]
        reports.append(DailyReport(
            day=date.fromisoformat(day),
            usage_tokens=item["usageTokens"],
            billing_tokens=bill["totalTokens"],
            billing_rows=item["billingRows"],
            input_tokens=bill["inputTokens"],
            cached_input_tokens=bill["cachedInputTokens"],
            output_tokens=bill["outputTokens"],
            other_tokens=bill["otherTokens"],
            list_price_cost=bill["listPriceCost"],
            actual_billed_amount=bill["actualBilledAmount"],
            billing_source=(sources or {}).get(day, "day") if item["billingRows"] else "none",
        ))
    return reports


def total_tokens(reports):
    usage = sum((report.usage_tokens for report in reports), Decimal(0))
    billing = sum((report.billing_tokens for report in reports), Decimal(0))
    return usage, billing, usage - billing


def print_daily_table(reports):
    print("Daily token report · UTC+8 · gap = usage − billing")
    print(f"{'Date':<12} {'Billing':>15} {'Usage':>15} {'Gap':>15} {'List cost':>12} {'Billed':>12}")
    for report in reports:
        billing = f"{report.billing_tokens:,.0f}" if report.billing_rows else "no rows"
        cost = f"{report.list_price_cost:.6f}" if report.billing_rows else "no rows"
        billed = f"{report.actual_billed_amount:.6f}" if report.billing_rows else "no rows"
        print(f"{report.day.isoformat():<12} {billing:>15} {report.usage_tokens:>15,.0f} "
              f"{report.gap_tokens:>+15,.0f} {cost:>12} {billed:>12}")

    usage, billing, gap = total_tokens(reports)
    print(f"{'TOTAL':<12} {billing:>15,.0f} {usage:>15,.0f} {gap:>+15,.0f}")
    if usage:
        print(f"Gap as a percentage of usage: {gap / usage * 100:+.4f}%")
    else:
        print("Gap percentage: unavailable (zero usage in comparison)")
    for report in reports:
        if report.billing_source == "minutely":
            print(f"{report.day}: billing tokens and costs aggregated from minutely records (no daily rows).")
    print("No billing rows means no recorded ledger data; the token gap uses a ledger sum of zero.")
    print("Costs use the account currency.")


@dataclass
class TokenCost:
    tokens: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)


def empty_cost_breakdown():
    return {category: TokenCost() for category in ("Input", "Cached input", "Output", "Other tokens")}


def summarize_token_costs(rows):
    """Accumulate model/category costs from ledger prices, including minute rows."""
    totals = empty_cost_breakdown()
    models = {}
    for row in rows:
        model = str(row.get("modelCode") or row.get("productCode") or "unknown").casefold()
        if model not in models:
            models[model] = empty_cost_breakdown()
        if row.get("usageUnit") == "token":
            category = {"INPUT": "Input", "CACHE": "Cached input", "OUTPUT": "Output"}.get(
                row.get("tokenType"), "Other tokens",
            )
            tokens = number(row, "usageCount")
        else:
            category = "Non-token products"
            tokens = Decimal(0)
        cost = row_list_price_cost(row)
        for breakdown in (totals, models[model]):
            if category not in breakdown:
                breakdown[category] = TokenCost()
            breakdown[category].tokens += tokens
            breakdown[category].cost += cost
    return totals, dict(sorted(models.items()))


def print_cost_breakdown(rows, reports):
    """Print category costs overall and per model for the same comparison days."""
    requested_days = {report.day.isoformat() for report in reports}
    selected = [row for row in rows if row["billingDate"] in requested_days]
    print("\nList-price cost breakdown · requested dates · account currency")
    if not selected:
        print("No billing records for the requested dates.")
        return
    totals, models = summarize_token_costs(selected)
    expected_cost = sum((report.list_price_cost for report in reports), Decimal(0))
    if sum((item.cost for item in totals.values()), Decimal(0)) != expected_cost:
        raise BillingError("Category costs do not match the daily report total")

    for name, breakdown in [("ALL MODELS", totals), *models.items()]:
        print(f"\n{name}")
        print(f"{'Category':<22} {'Tokens':>18} {'List-price cost':>20}")
        for category, item in breakdown.items():
            if category == "Other tokens" and item.tokens == 0 and item.cost == 0:
                continue
            count = "—" if category == "Non-token products" else f"{item.tokens:,f}"
            print(f"{category:<22} {count:>18} {item.cost:>20,.8f}")
        tokens = sum((item.tokens for item in breakdown.values()), Decimal(0))
        cost = sum((item.cost for item in breakdown.values()), Decimal(0))
        print(f"{'TOTAL':<22} {tokens:>18,f} {cost:>20,.8f}")


def save_csv(reports, path):
    """Include every requested day and preserve exact decimal values."""
    fields = list(DailyReport.__dataclass_fields__) + ["gap_tokens", "included_in_comparison"]
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            values = asdict(report)
            # Empty cells distinguish absent billing data from a recorded zero.
            if not report.billing_rows:
                for field in values:
                    if field not in ("day", "usage_tokens", "billing_rows", "billing_source"):
                        values[field] = ""
            values["gap_tokens"] = report.gap_tokens
            values["included_in_comparison"] = True
            writer.writerow(values)


# Field name, chart label, and color for each disjoint ledger category.
BILLING_CATEGORIES = (
    ("input_tokens", "Input", "#2563eb"),
    ("cached_input_tokens", "Cached input", "#14b8a6"),
    ("output_tokens", "Output", "#9333ea"),
    ("other_tokens", "Other token categories", "#94a3b8"),
)


def chart_cost_cells(reports):
    """Format recorded ledger amounts; absent billing is different from zero."""
    recorded = [report for report in reports if report.billing_rows]
    if not recorded:
        return ["no rows"]
    fields = ("list_price_cost",)
    return [f"{sum((getattr(report, field) for report in recorded), Decimal(0)):,.6f}"
            for field in fields]


def chart_table_rows(reports):
    """Return exact token counts and formatted ledger costs, including totals."""
    fields = [field for field, _, _ in BILLING_CATEGORIES]
    fields += ["billing_tokens", "gap_tokens", "usage_tokens"]
    rows = []
    for report in reports:
        day = report.day.isoformat() + (" *" if not report.billing_rows else "")
        if report.billing_source == "minutely":
            day += " †"
        counts = [f"{getattr(report, field):,f}" for field in fields]
        rows.append([day] + counts + chart_cost_cells([report]))
    totals = [sum((getattr(report, field) for report in reports), Decimal(0)) for field in fields]
    rows.append(["TOTAL"] + [f"{value:,f}" for value in totals] + chart_cost_cells(reports))
    return rows


def draw_token_breakdown(axis, reports):
    """Stack disjoint billing categories; overlay the independent usage total."""
    days = list(range(len(reports)))
    bottom = [0.0] * len(reports)
    for field, label, color in BILLING_CATEGORIES:
        values = [float(getattr(report, field)) for report in reports]
        axis.bar(days, values, bottom=bottom, width=0.6, label=label, color=color)
        bottom = [previous + value for previous, value in zip(bottom, values)]

    usage = [float(report.usage_tokens) for report in reports]
    axis.plot(days, bottom, "o:", label="Billing total (top of stack)",
              color="#334155", linewidth=1, markersize=4)
    axis.plot(days, usage, "D--", label="Usage API total", color="#ea580c", linewidth=2)
    missing = [index for index, report in enumerate(reports) if not report.billing_rows]
    if missing:
        axis.scatter(missing, [0] * len(missing), marker="x", color="#64748b",
                     label="No billing rows (sum = 0)", zorder=4)
    highest_total = max([1, *bottom, *usage])
    axis.set_ylim(0, highest_total * 1.15)
    axis.set_ylabel("Daily tokens")
    axis.legend(loc="upper right", fontsize=9, ncols=2)


def draw_gap(axis, reports):
    """Show the signed residual separately so small discrepancies are visible."""
    days = list(range(len(reports)))
    gaps = [float(report.gap_tokens) for report in reports]
    colors = ["#ea580c" if gap >= 0 else "#475569" for gap in gaps]
    bars = axis.bar(days, gaps, color=colors, width=0.6)
    axis.bar_label(bars, labels=[f"{report.gap_tokens:+,f}" if report.gap_tokens else ""
                                 for report in reports], padding=3, fontsize=8)
    axis.margins(y=0.25)
    axis.axhline(0, color="#334155", linewidth=0.8)
    axis.set_ylabel("Gap in tokens\nusage − billing")
    axis.set_xticks(days, [report.day.isoformat() for report in reports], rotation=45, ha="right")
    axis.set_xlabel("Billing day (UTC+8)")


def draw_detail_table(axis, reports):
    """Make the exact category sums readable even when a segment is very small."""
    axis.axis("off")
    columns = ["Day (UTC+8)", "Input", "Cached input", "Output", "Other",
               "Billing sum", "Gap (+/−)", "Usage total", "List-price cost"]
    table = axis.table(cellText=chart_table_rows(reports), colLabels=columns,
                       cellLoc="right", loc="center", bbox=(0, 0, 1, 1))
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    for (row, column), cell in table.get_celld().items():
        cell.set_edgecolor("#e2e8f0")
        if row == 0 or row == len(reports) + 1:
            cell.set_facecolor("#f1f5f9")
            cell.set_text_props(weight="bold")
        if row == 0 and 1 <= column <= 4:
            cell.set_text_props(color=BILLING_CATEGORIES[column - 1][2])


def save_chart(reports, path):
    """Plot the composition, residual, and exact reconciliation in one figure."""
    import matplotlib
    matplotlib.use("Agg")  # Works on servers without a desktop display.
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    # Fail rather than draw a stack whose height misrepresents the ledger total.
    for report in reports:
        category_sum = sum((getattr(report, field) for field, _, _ in BILLING_CATEGORIES), Decimal(0))
        if category_sum != report.billing_tokens:
            raise BillingError(f"Billing categories do not sum to total on {report.day}")

    table_height = 0.25 * (len(reports) + 2)
    figure = plt.figure(figsize=(16, 8 + table_height), layout="constrained")
    grid = figure.add_gridspec(3, 1, height_ratios=[3, 1.5, table_height], hspace=0.12)
    counts_axis = figure.add_subplot(grid[0])
    gap_axis = figure.add_subplot(grid[1], sharex=counts_axis)
    table_axis = figure.add_subplot(grid[2])
    draw_token_breakdown(counts_axis, reports)
    draw_gap(gap_axis, reports)
    draw_detail_table(table_axis, reports)
    counts_axis.tick_params(axis="x", labelbottom=False)
    for axis in (counts_axis, gap_axis):
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1_000_000:g} M"))
        axis.grid(axis="y", alpha=0.2)
        axis.set_axisbelow(True)

    period = f"{reports[0].day} through {reports[-1].day}" if reports else "No comparison days remain"
    usage_total, billing_total, gap_total = total_tokens(reports)
    figure.suptitle(
        f"Z.ai daily token reconciliation · {period}\n"
        f"Input + cached input + output + other = billing; billing + gap = usage\n"
        f"{billing_total:,f} billing {gap_total:+,f} gap = {usage_total:,f} usage tokens",
        fontsize=12,
    )
    footer = "* No billing rows: category sums are zero, not confirmed zero consumption. Gap is an unexplained difference."
    if any(report.billing_source == "minutely" for report in reports):
        footer += "\n† Minutely records aggregated for the final day because daily billing rows are absent."
    figure.supxlabel(footer, fontsize=9)
    figure.savefig(path, dpi=160)
    plt.close(figure)


def main():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default=today.replace(day=1).isoformat(), help="first UTC+8 day (default: month start)")
    parser.add_argument("--end-date", default=today.isoformat(), help="last UTC+8 day (default: today)")
    parser.add_argument("--output", type=Path, default=Path("reports/zai-daily.png"), help="chart path (.png, .svg, or .pdf); CSV saved beside it")
    args = parser.parse_args()
    start, end = usage_dates(today.strftime("%Y-%m"), args.start_date, args.end_date)
    if args.output.suffix.lower() not in (".png", ".svg", ".pdf"):
        parser.error("--output must end in .png, .svg, or .pdf")
    try:
        import matplotlib  # Check the optional dependency before making API calls.
    except ImportError as exc:
        raise BillingError("Install chart dependencies: python3 -m pip install -r requirements-daily.txt") from exc

    reports, billing_rows = fetch_daily_reports(start, end)
    print_daily_table(reports)
    print_cost_breakdown(billing_rows, reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_chart(reports, args.output)
    csv_path = args.output.with_suffix(".csv")
    save_csv(reports, csv_path)
    print(f"Chart: {args.output.resolve()}\nCSV:   {csv_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except (BillingError, OSError) as exc:
        print(f"daily report: {exc}", file=sys.stderr)
        sys.exit(2)
