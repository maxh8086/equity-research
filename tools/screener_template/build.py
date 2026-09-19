"""Build the merged Screener.in Excel template (see README.md).

    python tools/screener_template/build.py --base <Technofunda template.xlsx> --out <merged.xlsx>

The base workbook supplies Screener's "Data Sheet" untouched. Every other sheet
is generated from the row definitions below, so merging a future template
means editing these definitions and rebuilding, never hand-editing the output.

These formulas are for reading in Excel only. The Session 6 validation computes
every ratio in code from the Data Sheet's raw values (R1); nothing here feeds
the system.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

TEMPLATE_VERSION = "merged-1 (2026-09-19)"
DATA = "Data Sheet"

# --------------------------------------------------------------------------- #
# Screener's Data Sheet, version 2.1 (cells B2/B3). Screener overwrites this
# sheet on every export. If its layout changes, only these two tables change;
# the build fails if the base workbook's labels disagree with them.
# --------------------------------------------------------------------------- #

META = {"name": "B1", "shares": "B6", "face_value": "B7", "cmp": "B8", "mcap": "B9"}

DS_ROWS = {
    "pl_date": (16, "Report Date"),
    "sales": (17, "Sales"),
    "raw_material": (18, "Raw Material Cost"),
    "inventory_change": (19, "Change in Inventory"),
    "power_fuel": (20, "Power and Fuel"),
    "other_mfr": (21, "Other Mfr. Exp"),
    "employee": (22, "Employee Cost"),
    "selling_admin": (23, "Selling and admin"),
    "other_exp": (24, "Other Expenses"),
    "other_income": (25, "Other Income"),
    "depreciation": (26, "Depreciation"),
    "interest": (27, "Interest"),
    "pbt": (28, "Profit before tax"),
    "tax": (29, "Tax"),
    "net_profit": (30, "Net profit"),
    "dividend": (31, "Dividend Amount"),
    "q_date": (41, "Report Date"),
    "q_sales": (42, "Sales"),
    "q_expenses": (43, "Expenses"),
    "q_other_income": (44, "Other Income"),
    "q_depreciation": (45, "Depreciation"),
    "q_interest": (46, "Interest"),
    "q_pbt": (47, "Profit before tax"),
    "q_tax": (48, "Tax"),
    "q_net_profit": (49, "Net profit"),
    "q_op": (50, "Operating Profit"),
    "bs_date": (56, "Report Date"),
    "share_capital": (57, "Equity Share Capital"),
    "reserves": (58, "Reserves"),
    "borrowings": (59, "Borrowings"),
    "other_liabilities": (60, "Other Liabilities"),
    "total_liabilities": (61, "Total"),
    "net_block": (62, "Net Block"),
    "cwip": (63, "Capital Work in Progress"),
    "investments": (64, "Investments"),
    "other_assets": (65, "Other Assets"),
    "total_assets": (66, "Total"),
    "receivables": (67, "Receivables"),
    "inventory": (68, "Inventory"),
    "cash_bank": (69, "Cash & Bank"),
    "share_count": (70, "No. of Equity Shares"),
    "face_value": (72, "Face value"),
    "cf_date": (81, "Report Date"),
    "cfo": (82, "Cash from Operating Activity"),
    "cfi": (83, "Cash from Investing Activity"),
    "cff": (84, "Cash from Financing Activity"),
    "net_cash_flow": (85, "Net Cash Flow"),
    "price": (90, "PRICE:"),
    "adj_shares": (93, "Adjusted Equity Shares in Cr"),
}

# Ten periods sit in B..K, oldest first; K is the latest year or quarter.
PERIODS = [get_column_letter(i) for i in range(2, 12)]
TTM_COL = "L"  # annual sheets: trailing four quarters
TREND_COLS = {"N": 9, "O": 5, "P": 3}  # CAGR years; averages use 10, 5 and 3 periods
KIND_COL = "M"
NOTE_COL = "R"
LAST_FOUR_QUARTERS = "$H${r}:$K${r}"

# The trailing four quarters are blank unless all four are reported (VM's guard).
TTM_INCOMPLETE = f"COUNT('{DATA}'!$H${DS_ROWS['q_net_profit'][0]}:$K${DS_ROWS['q_net_profit'][0]})<4"

# Where each row came from, shown on the Notes sheet.
SOURCES = {
    "VM": "Dr Vijay Malik template V2.0, sheet 'Dr Vijay Malik Analysis'",
    "TF": "Technofunda template v3, statement sheets",
    "AN": "Analysis.xlsx (Screener tables pasted by hand)",
    "SYS": "This system's rule ratios/1 (core/compute/ratios.py)",
    "NEW": "Added in the merge",
}


@dataclass(frozen=True)
class Row:
    key: str | None
    label: str
    f: str | None = None  # formula template; None makes a section heading
    fmt: str = "num"
    ttm: str | None = None  # annual sheets: "same" reuses f in column L
    trend: str | None = None  # "cagr" | "avg" | "sum" | "ratio:num/den"
    high: bool = False  # quarterly sheets: flag a 10-quarter high in column L
    start: int = 0  # first period index to fill (0 = column B)
    src: str = "NEW"
    note: str = ""
    raw: bool = False  # a plain link: no IFERROR wrapper


def H(label: str) -> Row:
    return Row(None, label)


@dataclass(frozen=True)
class Sheet:
    code: str
    title: str
    heading: str
    date_row: str | None  # Data Sheet row for the period header
    quarterly: bool
    rows: tuple[Row, ...]


# --------------------------------------------------------------------------- #
# Row definitions
# --------------------------------------------------------------------------- #

COST_LINES = (
    ("material", "Material cost (net of inventory change)"),
    ("power_fuel", "Power and fuel"),
    ("other_mfr", "Other manufacturing expenses"),
    ("employee", "Employee cost"),
    ("selling_admin", "Selling and admin"),
    ("other_exp", "Other expenses"),
)

INCOME = Sheet("IS", "Income Statement", "Income statement, annual (₹ crore)", "pl_date", False, (
    H("Profit and loss"),
    Row("sales", "Sales", "{ds:sales}", ttm="{ttm:q_sales}", trend="cagr", src="VM", raw=True),
    Row("sales_growth", "Sales growth YoY", "{sales}/{sales@-1}-1", "pct", ttm="{sales}/{sales@K}-1", start=1, src="TF",
        note="TTM column: TTM against the latest full year"),
    Row("material", "Material cost (net of inventory change)", "{ds:raw_material}-{ds:inventory_change}", src="VM"),
    Row("power_fuel", "Power and fuel", "{ds:power_fuel}", src="VM", raw=True),
    Row("other_mfr", "Other manufacturing expenses", "{ds:other_mfr}", src="VM", raw=True),
    Row("employee", "Employee cost", "{ds:employee}", src="VM", raw=True),
    Row("selling_admin", "Selling and admin", "{ds:selling_admin}", src="VM", raw=True),
    Row("other_exp", "Other expenses", "{ds:other_exp}", src="VM", raw=True),
    Row("opex", "Operating expenses", "{material}+{power_fuel}+{other_mfr}+{employee}+{selling_admin}+{other_exp}",
        ttm="{ttm:q_expenses}", src="TF"),
    Row("op", "Operating profit", "{sales}-{opex}", ttm="same", trend="cagr", src="VM",
        note="Excludes other income. Not meaningful for banks, NBFCs and insurers"),
    Row("opm", "Operating profit margin", "{op}/{sales}", "pct", ttm="same", trend="ratio:op/sales", src="VM",
        note="Negative margins shown as negative (VM showed 0). Trend = sales-weighted"),
    Row("other_income", "Other income", "{ds:other_income}", ttm="{ttm:q_other_income}", src="VM", raw=True),
    Row("ebitda", "EBITDA (operating profit + other income)", "{op}+{other_income}", ttm="same", src="VM"),
    Row("depreciation", "Depreciation", "{ds:depreciation}", ttm="{ttm:q_depreciation}", src="VM", raw=True),
    Row("interest", "Interest", "{ds:interest}", ttm="{ttm:q_interest}", src="VM", raw=True),
    Row("pbt", "Profit before tax (reported)", "{ds:pbt}", ttm="{ttm:q_pbt}", trend="cagr", src="VM", raw=True,
        note="Reported PBT, after exceptional items. TF recomputed it and lost them"),
    Row("exceptional", "Exceptional and other items", "{pbt}-({ebitda}-{depreciation}-{interest})", ttm="same",
        note="Reported PBT less EBITDA − depreciation − interest; positive = gain"),
    Row("ebit", "EBIT (PBT + interest)", "{pbt}+{interest}", ttm="same", trend="cagr", src="SYS"),
    Row("tax", "Tax", "{ds:tax}", ttm="{ttm:q_tax}", src="VM", raw=True),
    Row("tax_rate", "Tax rate", "{tax}/{pbt}", "pct", ttm="same", trend="avg", src="VM"),
    Row("pat", "Net profit", "{ds:net_profit}", ttm="{ttm:q_net_profit}", trend="cagr", src="VM", raw=True,
        note="Screener's net profit; consolidated figures include minority interest"),
    Row("pat_growth", "Net profit growth YoY", "{pat}/{pat@-1}-1", "pct", ttm="{pat}/{pat@K}-1", start=1, src="TF"),
    Row("npm", "Net profit margin", "{pat}/{sales}", "pct", ttm="same", trend="ratio:pat/sales", src="VM"),
    H("Per share and valuation"),
    Row("dividend", "Dividend", "{ds:dividend}", trend="cagr", src="VM", raw=True),
    Row("payout", "Dividend payout", "{dividend}/{pat}", "pct", trend="ratio:dividend/pat", src="VM"),
    Row("retained", "Retained earnings (net profit − dividend)", "{pat}-{dividend}", trend="sum", src="VM"),
    Row("shares", "Adjusted equity shares (crore)", "{ds:adj_shares}", ttm="{meta:shares}", src="VM", raw=True),
    Row("eps", "EPS (₹)", "{pat}/{shares}", ttm="same", trend="cagr", src="TF"),
    Row("dps", "Dividend per share (₹)", "{dividend}/{shares}", trend="cagr", src="NEW"),
    Row("price", "Share price (year end; TTM = current)", "{ds:price}", ttm="{meta:cmp}", src="VM", raw=True),
    Row("mcap", "Market cap", "{price}*{shares}", ttm="{meta:mcap}", src="VM"),
    Row("pe", "P/E", 'IF({pat}>0,{mcap}/{pat},"n/m")', "x", ttm="same", trend="avg", src="VM",
        note="n/m when loss-making; averages skip those years"),
    Row("int_cover", "Interest coverage (EBIT ÷ interest)", 'IF({interest}>0,{ebit}/{interest},"n/m")', "x",
        ttm="same", src="TF", note="n/m with no interest; not meaningful for lenders"),
    H("Costs as % of sales"),
    *(Row(f"{k}_pct", label, f"{{{k}}}/{{sales}}", "pct", ttm=None, trend="avg", src="VM")
      for k, label in COST_LINES),
))

QUARTERLY = Sheet("Q", "Quarterly", "Quarterly results (₹ crore)", "q_date", True, (
    H("Quarterly profit and loss"),
    Row("sales", "Sales", "{ds:q_sales}", high=True, src="TF", raw=True),
    Row("sales_qoq", "Sales growth QoQ", "{sales}/{sales@-1}-1", "pct", start=1, src="AN"),
    Row("sales_yoy", "Sales growth YoY", "{sales}/{sales@-4}-1", "pct", start=4, src="AN"),
    Row("expenses", "Expenses", "{ds:q_expenses}", src="TF", raw=True),
    Row("op", "Operating profit", "{ds:q_op}", high=True, src="TF", raw=True),
    Row("opm", "Operating profit margin", "{op}/{sales}", "pct", src="TF"),
    Row("op_yoy", "Operating profit growth YoY", "{op}/{op@-4}-1", "pct", start=4, src="AN"),
    Row("other_income", "Other income", "{ds:q_other_income}", src="TF", raw=True),
    Row("depreciation", "Depreciation", "{ds:q_depreciation}", src="TF", raw=True),
    Row("interest", "Interest", "{ds:q_interest}", src="TF", raw=True),
    Row("pbt", "Profit before tax", "{ds:q_pbt}", high=True, src="TF", raw=True),
    Row("tax", "Tax", "{ds:q_tax}", src="TF", raw=True),
    Row("tax_rate", "Tax rate", "{tax}/{pbt}", "pct", src="AN"),
    Row("pat", "Net profit", "{ds:q_net_profit}", high=True, src="TF", raw=True),
    Row("pat_qoq", "Net profit growth QoQ", "{pat}/{pat@-1}-1", "pct", start=1, src="AN"),
    Row("pat_yoy", "Net profit growth YoY", "{pat}/{pat@-4}-1", "pct", start=4, src="AN"),
    Row("npm", "Net profit margin", "{pat}/{sales}", "pct", src="TF"),
    Row("eps", "EPS on today's share count (₹)", "{pat}/{meta:shares}", src="AN",
        note="Uses today's share count: older quarters are approximate after splits or issues"),
))

BALANCE = Sheet("BS", "Balance Sheet", "Balance sheet, year end (₹ crore)", "bs_date", False, (
    H("Liabilities"),
    Row("share_capital", "Equity share capital", "{ds:share_capital}", src="TF", raw=True),
    Row("reserves", "Reserves", "{ds:reserves}", src="TF", raw=True),
    Row("net_worth", "Net worth (capital + reserves)", "{share_capital}+{reserves}", trend="cagr", src="TF",
        note="Screener shows minority interest under other liabilities, so it is not in net worth"),
    Row("borrowings", "Borrowings", "{ds:borrowings}", trend="cagr", src="TF", raw=True),
    Row("other_liabilities", "Other liabilities", "{ds:other_liabilities}", src="TF", raw=True),
    Row("total_liabilities", "Total liabilities", "{ds:total_liabilities}", src="TF", raw=True),
    H("Assets"),
    Row("net_block", "Net block (net fixed assets)", "{ds:net_block}", trend="cagr", src="TF", raw=True),
    Row("cwip", "Capital work in progress", "{ds:cwip}", src="TF", raw=True),
    Row("investments", "Investments", "{ds:investments}", src="TF", raw=True),
    Row("other_assets", "Other assets", "{ds:other_assets}", src="TF", raw=True),
    Row("total_assets", "Total assets", "{ds:total_assets}", src="TF", raw=True),
    Row("balance_check", "Check: liabilities − assets", "{total_liabilities}-{total_assets}", note="Should be 0"),
    Row("receivables", "Receivables", "{ds:receivables}", src="TF", raw=True),
    Row("inventory", "Inventory", "{ds:inventory}", src="TF", raw=True),
    Row("cash_bank", "Cash and bank", "{ds:cash_bank}", src="TF", raw=True),
    H("Derived"),
    Row("capital_employed", "Capital employed (net worth + borrowings)", "{net_worth}+{borrowings}", src="TF"),
    Row("cash_investments", "Cash + investments", "{cash_bank}+{investments}", src="VM",
        note="Includes long-term and strategic investments"),
    Row("net_debt", "Net debt (borrowings − cash and bank)", "{borrowings}-{cash_bank}", src="SYS",
        note="Negative = net cash. The system also nets current investments, which Screener does not show"),
    Row("working_capital", "Working capital (other assets − other liabilities)", "{other_assets}-{other_liabilities}",
        src="TF"),
    Row("bvps", "Book value per share (₹)", "{net_worth}/{IS.shares}", trend="cagr", src="NEW"),
    Row("share_count", "Equity shares (crore, as reported)", "{ds:share_count}/10000000", src="NEW"),
    Row("face_value", "Face value (₹)", "{ds:face_value}", src="NEW", raw=True),
    H("Common size: % of total liabilities"),
    *(Row(f"{k}_pct", label, f"{{{k}}}/{{total_liabilities}}", "pct", src="TF") for k, label in (
        ("share_capital", "Equity share capital"), ("reserves", "Reserves"), ("borrowings", "Borrowings"),
        ("other_liabilities", "Other liabilities"))),
    H("Common size: % of total assets"),
    *(Row(f"{k}_pct", label, f"{{{k}}}/{{total_assets}}", "pct", src="TF") for k, label in (
        ("net_block", "Net block"), ("cwip", "Capital work in progress"), ("investments", "Investments"),
        ("other_assets", "Other assets"), ("receivables", "Receivables"), ("inventory", "Inventory"),
        ("cash_bank", "Cash and bank"))),
))

CASH = Sheet("CF", "Cash Flow", "Cash flow (₹ crore)", "cf_date", False, (
    H("Reported"),
    Row("cfo", "Cash from operations (CFO)", "{ds:cfo}", trend="sum", src="TF", raw=True),
    Row("cfi", "Cash from investing (CFI)", "{ds:cfi}", trend="sum", src="TF", raw=True),
    Row("cff", "Cash from financing (CFF)", "{ds:cff}", trend="sum", src="TF", raw=True),
    Row("net_cash_flow", "Net cash flow", "{ds:net_cash_flow}", trend="sum", src="VM", raw=True),
    Row("cash_end", "Cash and bank at year end", "{BS.cash_bank}", src="TF"),
    H("Quality and free cash flow"),
    Row("pat", "Net profit", "{IS.pat}", trend="sum", src="TF"),
    Row("cfo_pat", "CFO ÷ net profit (aim ≥ 80%)", "{cfo}/{pat}", "pct", trend="ratio:cfo/pat", src="TF",
        note="Trend = cumulative CFO ÷ cumulative profit"),
    Row("capex", "Capex (Δ net block + Δ CWIP + depreciation)",
        "{BS.net_block}-{BS.net_block@-1}+{BS.cwip}-{BS.cwip@-1}+{IS.depreciation}", trend="sum", start=1,
        src="VM", note="Estimated from the balance sheet; acquisitions and revaluations distort it"),
    Row("fcf", "Free cash flow (CFO − capex)", "{cfo}-{capex}", trend="sum", start=1, src="VM"),
    Row("fcfe", "Free cash flow after interest", "{fcf}-{IS.interest}", trend="sum", start=1, src="VM"),
    Row("reinvestment", "Reinvestment (capex ÷ CFO)", "{capex}/{cfo}", "pct", start=1, src="TF"),
    Row("fcf_cfo", "FCF ÷ CFO", "{fcf}/{cfo}", "pct", trend="ratio:fcf/cfo", start=1, src="TF"),
))

RATIOS = Sheet("RA", "Ratios", "Return, efficiency and leverage ratios", "pl_date", False, (
    H("Returns"),
    Row("roe", "Return on average equity", "{IS.pat}/AVERAGE({BS.net_worth@-1},{BS.net_worth})", "pct",
        trend="avg", start=1, src="TF"),
    Row("roace", "Return on average capital employed", "{IS.ebit}/AVERAGE({BS.capital_employed@-1},{BS.capital_employed})",
        "pct", trend="avg", start=1, src="TF", note="EBIT ÷ average (net worth + borrowings). Not for lenders"),
    Row("roaa", "EBIT ÷ average total assets", "{IS.ebit}/AVERAGE({BS.total_assets@-1},{BS.total_assets})", "pct",
        trend="avg", start=1, src="VM", note="VM labelled this ROCE; total assets include non-debt liabilities"),
    Row("pbt_nfa", "PBT ÷ average net fixed assets", "{IS.pbt}/AVERAGE({BS.net_block@-1},{BS.net_block})", "pct",
        trend="avg", start=1, src="VM", note="VM bands: < 10% weak, > 25% strong"),
    Row("inc_roe", "Incremental ROE, 3Y (Δ net profit ÷ retained earnings)",
        "({IS.pat}-{IS.pat@-3})/SUM({rng:IS.retained@-3:-1})", "pct", start=3, src="VM"),
    Row("inc_roce", "Incremental ROCE, 3Y (Δ EBIT ÷ Δ capital employed)",
        'IF({BS.capital_employed}>{BS.capital_employed@-3},({IS.ebit}-{IS.ebit@-3})/({BS.capital_employed}-{BS.capital_employed@-3}),"n/m")',
        "pct", start=3, src="SYS", note="n/m when capital employed did not grow"),
    H("Efficiency"),
    Row("nfa_turn", "Net fixed asset turnover", "{IS.sales}/AVERAGE({BS.net_block@-1},{BS.net_block})", "x",
        trend="avg", start=1, src="VM"),
    Row("recv_days", "Receivable days", "365*AVERAGE({BS.receivables@-1},{BS.receivables})/{IS.sales}", "days",
        trend="avg", start=1, src="VM"),
    Row("inv_turn", "Inventory turnover", "{IS.sales}/AVERAGE({BS.inventory@-1},{BS.inventory})", "x", trend="avg",
        start=1, src="VM"),
    Row("inv_days", "Inventory days", "365/{inv_turn}", "days", trend="avg", start=1, src="NEW"),
    Row("wc_days", "Working capital cycle (receivable + inventory days)", "{recv_days}+{inv_days}", "days",
        trend="avg", start=1, src="VM"),
    Row("dep_nfa", "Depreciation ÷ net fixed assets", "{IS.depreciation}/{BS.net_block}", "pct", trend="avg", src="VM"),
    H("Leverage"),
    Row("de", "Debt ÷ equity", "{BS.borrowings}/{BS.net_worth}", "x", trend="avg", src="VM"),
    Row("net_de", "Net debt ÷ equity", "{BS.net_debt}/{BS.net_worth}", "x", trend="avg", src="SYS"),
    Row("int_cover", "Interest coverage (EBIT ÷ interest)", "{IS.int_cover}", "x", src="TF"),
    Row("notional_interest", "Notional interest (average debt × cost of funds)",
        "AVERAGE({BS.borrowings@-1},{BS.borrowings})*{S.cost_of_funds}", trend="sum", start=1, src="VM",
        note="Cost of funds is an input on Summary"),
    Row("notional_cover", "Operating profit ÷ notional interest", "{IS.op}/{notional_interest}", "x", start=1,
        src="VM"),
    H("Growth capacity"),
    Row("ssgr", "Self-sustainable growth rate (SSGR), 3Y",
        "AVERAGE({rng:nfa_turn@-2:0})*AVERAGE({rng:IS.npm@-2:0})*(1-AVERAGE({rng:IS.payout@-2:0}))"
        "-AVERAGE({rng:dep_nfa@-2:0})", "pct", start=3, src="VM",
        note="Growth fundable from internal accruals; compare with sales CAGR below"),
    Row("sales_cagr3", "Sales CAGR, 3Y", "({IS.sales}/{IS.sales@-3})^(1/3)-1", "pct", start=3, src="NEW"),
    Row("cfo_pat", "CFO ÷ net profit", "{CF.cfo_pat}", "pct", src="TF"),
    Row("payout", "Dividend payout", "{IS.payout}", "pct", src="VM"),
))

DUPONT = Sheet("DU", "DuPont", "DuPont analysis on average balances", "pl_date", False, (
    H("Inputs"),
    Row("sales", "Sales", "{IS.sales}", src="TF"),
    Row("ebit", "EBIT (PBT + interest)", "{IS.ebit}", src="SYS", note="TF used OP + other income − depreciation"),
    Row("pbt", "Profit before tax", "{IS.pbt}", src="TF"),
    Row("pat", "Net profit", "{IS.pat}", src="TF"),
    Row("avg_assets", "Average total assets", "AVERAGE({BS.total_assets@-1},{BS.total_assets})", start=1,
        note="TF used year-end balances; averages tie ROE to the Ratios sheet"),
    Row("avg_equity", "Average net worth", "AVERAGE({BS.net_worth@-1},{BS.net_worth})", start=1),
    H("Three-stage"),
    Row("npm", "Net profit margin", "{pat}/{sales}", "pct", start=1, src="TF"),
    Row("asset_turn", "Asset turnover", "{sales}/{avg_assets}", "x", start=1, src="TF"),
    Row("eq_mult", "Equity multiplier", "{avg_assets}/{avg_equity}", "x", start=1, src="TF"),
    Row("roe3", "ROE", "{npm}*{asset_turn}*{eq_mult}", "pct", trend="avg", start=1, src="TF"),
    H("Five-stage"),
    Row("tax_burden", "Tax burden (net profit ÷ PBT)", "{pat}/{pbt}", "pct", start=1, src="TF"),
    Row("int_burden", "Interest burden (PBT ÷ EBIT)", "{pbt}/{ebit}", "pct", start=1, src="TF"),
    Row("ebit_margin", "EBIT margin", "{ebit}/{sales}", "pct", start=1, src="TF"),
    Row("asset_turn5", "Asset turnover", "{asset_turn}", "x", start=1, src="TF"),
    Row("eq_mult5", "Equity multiplier", "{eq_mult}", "x", start=1, src="TF"),
    Row("roe5", "ROE", "{tax_burden}*{int_burden}*{ebit_margin}*{asset_turn5}*{eq_mult5}", "pct", start=1, src="TF"),
    Row("check", "Check: 3-stage = 5-stage = Ratios ROE",
        'IF(AND(ABS({roe3}-{roe5})<1E-9,ABS({roe3}-{RA.roe})<1E-9),"OK","CHECK")', "text", start=1, src="TF"),
))

SYSTEM = Sheet("SYS", "System ratios", "This system's ratios (rule ratios/1) on Screener's lines", "pl_date", False, (
    H("Read-only mirror of core/compute/ratios.py; the validation computes these in code"),
    Row("op_margin", "Operating margin", 'IF({IS.sales}>0,{IS.op}/{IS.sales},"n/m")', "pct", src="SYS",
        note="System: revenue − (expenses − finance costs − depreciation)"),
    Row("ebit_margin", "EBIT margin", 'IF({IS.sales}>0,{IS.ebit}/{IS.sales},"n/m")', "pct", src="SYS"),
    Row("net_margin", "Net margin", 'IF({IS.sales}>0,{IS.pat}/{IS.sales},"n/m")', "pct", src="SYS"),
    Row("int_cover", "Interest coverage", 'IF({IS.interest}>0,{IS.ebit}/{IS.interest},"n/m")', "x", src="SYS"),
    Row("de", "Debt ÷ equity", 'IF({BS.net_worth}>0,{BS.borrowings}/{BS.net_worth},"n/m")', "x", src="SYS",
        note="System equity includes minority interest; system borrowings exclude lease liabilities"),
    Row("net_de", "Net debt ÷ equity", 'IF({BS.net_worth}>0,{BS.net_debt}/{BS.net_worth},"n/m")', "x", src="SYS",
        note="System also subtracts current investments"),
    Row("roce", "ROCE (EBIT ÷ average capital employed)",
        'IF(AVERAGE({BS.capital_employed@-1},{BS.capital_employed})>0,'
        '{IS.ebit}/AVERAGE({BS.capital_employed@-1},{BS.capital_employed}),"n/m")', "pct", start=1, src="SYS"),
    Row("inc_roce", "Incremental ROCE, 3Y", "{RA.inc_roce}", "pct", start=3, src="SYS"),
))

SHEETS = (INCOME, QUARTERLY, BALANCE, CASH, RATIOS, DUPONT, SYSTEM)

# Summary: one value per line, in column C. Tokens resolve with column C as
# "the current column", so everything here names its column explicitly.
SUMMARY: tuple[Row, ...] = (
    H("Snapshot"),
    Row("company", "Company", "{meta:name}", "text", src="TF", raw=True),
    Row("latest_fy", "Latest financial year", f"'{DATA}'!K{DS_ROWS['pl_date'][0]}", "date", src="NEW", raw=True),
    Row("latest_q", "Latest quarter", f"'{DATA}'!K{DS_ROWS['q_date'][0]}", "date", src="VM", raw=True),
    Row("ttm_ok", "Trailing four quarters complete?", f'IF({TTM_INCOMPLETE},"NO: TTM figures left blank","Yes")',
        "text", src="VM"),
    Row("cmp", "Current price (₹)", "{meta:cmp}", src="TF", raw=True),
    Row("shares", "Shares (crore)", "{meta:shares}", src="TF", raw=True),
    Row("mcap", "Market cap (₹ crore)", "{meta:mcap}", src="TF", raw=True),
    H("Trailing four quarters"),
    Row("ttm_sales", "Sales", "{IS.sales@L}", src="VM"),
    Row("ttm_op", "Operating profit", "{IS.op@L}", src="VM"),
    Row("ttm_opm", "Operating profit margin", "{IS.opm@L}", "pct", src="TF"),
    Row("ttm_pat", "Net profit", "{IS.pat@L}", src="TF"),
    Row("ttm_eps", "EPS (₹)", "{IS.eps@L}", src="TF"),
    H("Valuation"),
    Row("pe", "P/E (TTM; latest year if TTM incomplete)",
        f'IF({TTM_INCOMPLETE},{{IS.pe@K}},{{IS.pe@L}})', "x", src="VM"),
    Row("pb", "P/B", "{meta:mcap}/{BS.net_worth@K}", "x", src="VM"),
    Row("pe_pb", "P/E × P/B", "{S.pe}*{S.pb}", "x", src="VM"),
    Row("earnings_yield", "Earnings yield", "1/{S.pe}", "pct", src="NEW"),
    Row("div_yield", "Dividend yield (latest year's dividend)", "{IS.dividend@K}/{meta:mcap}", "pct", src="VM"),
    Row("avg_pe_long", "Average P/E, 10 years", "{IS.pe@N}", "x", src="VM"),
    Row("avg_pe_5", "Average P/E, 5 years", "{IS.pe@O}", "x", src="VM"),
    H("Latest financial year"),
    Row("roe", "Return on average equity", "{RA.roe@K}", "pct", src="TF"),
    Row("roace", "Return on average capital employed", "{RA.roace@K}", "pct", src="TF",
        note="Not meaningful for banks, NBFCs and insurers"),
    Row("de", "Debt ÷ equity", "{RA.de@K}", "x", src="VM"),
    Row("net_debt", "Net debt (₹ crore; negative = net cash)", "{BS.net_debt@K}", src="SYS"),
    Row("int_cover", "Interest coverage", "{IS.int_cover@K}", "x", src="TF"),
    Row("cfo_pat", "CFO ÷ net profit, 10 years", "{CF.cfo_pat@N}", "pct", src="TF"),
    Row("fcf", "Free cash flow, 10 years (₹ crore)", "{CF.fcf@N}", src="VM"),
    Row("ssgr", "SSGR, latest 3 years", "{RA.ssgr@K}", "pct", src="VM"),
    H("Growth (CAGR: 9Y | 5Y | 3Y in columns C | D | E)"),
    *(Row(f"g_{code}_{key}", label, f"{{{code}.{key}@N}}", "pct", src=src) for code, key, label, src in (
        ("IS", "sales", "Sales", "VM"), ("IS", "op", "Operating profit", "NEW"), ("IS", "pat", "Net profit", "VM"),
        ("IS", "eps", "EPS", "TF"), ("IS", "dividend", "Dividend", "VM"), ("BS", "bvps", "Book value per share", "VM"),
        ("BS", "borrowings", "Borrowings", "VM"))),
    H("Value created by retained earnings (10 years)"),
    Row("re_total", "Retained earnings, 10 years (A)", "{IS.retained@N}", src="VM"),
    Row("mcap_then", "Market cap 10 years ago",
        "IF({S.price_then}>0,{S.price_then}*{meta:shares},{IS.mcap@B})", src="VM",
        note="Uses the input price below if given, else Screener's year-end price"),
    Row("mcap_gain", "Increase in market cap (B)", "{meta:mcap}-{S.mcap_then}", src="VM"),
    Row("value_per_re", "Value created per ₹ retained (B ÷ A)", "{S.mcap_gain}/{S.re_total}", "x", src="VM",
        note="Above 1: retained earnings created more than their value"),
    H("Inputs (edit the yellow cells)"),
    Row("cost_of_funds", "Cost of funds for notional interest", "0.12", "pct", src="VM", raw=True),
    Row("price_then", "Share price 10 years ago (optional, ₹)", "0", src="VM", raw=True,
        note="Leave 0 to use Screener's price for the oldest year"),
)
SUMMARY_INPUTS = {"cost_of_funds", "price_then"}
SUMMARY_TREND_ROWS = {r.key for r in SUMMARY if r.key and r.key.startswith("g_")}

# --------------------------------------------------------------------------- #
# Formula resolution
# --------------------------------------------------------------------------- #

TOKEN = re.compile(r"\{([^{}]+)\}")


class Blank(Exception):
    """The formula needs a period before column B: leave the cell empty."""


def _shift(col: str, spec: str | None) -> str:
    if not spec:
        return col
    if spec.lstrip("-").isdigit():
        idx = column_index_from_string(col) + int(spec)
        if idx < 2:
            raise Blank
        return get_column_letter(idx)
    return spec  # an absolute column letter


class Layout:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], int] = {}
        self.titles = {s.code: s.title for s in SHEETS}
        self.titles["S"] = "Summary"

    def place(self, code: str, key: str, row: int) -> None:
        if (code, key) in self.rows:
            raise ValueError(f"duplicate row key {code}.{key}")
        self.rows[(code, key)] = row

    def ref(self, code: str, key: str, col: str, here: str) -> str:
        try:
            row = self.rows[(code, key)]
        except KeyError:
            raise ValueError(f"unknown row {code}.{key}") from None
        if code == "S":
            return f"'Summary'!$C${row}"
        cell = f"{col}{row}"
        return cell if code == here else f"'{self.titles[code]}'!{cell}"

    def resolve(self, template: str, here: str, col: str) -> str:
        def one(m: re.Match[str]) -> str:
            token = m.group(1)
            if token.startswith("rng:"):
                target, _, span = token[4:].partition("@")
                a, _, b = span.partition(":")
                code, key = target.split(".", 1) if "." in target else (here, target)
                first = self.ref(code, key, _shift(col, a), here)
                last = self.ref(code, key, _shift(col, b), here).split("!")[-1]
                return f"{first}:{last}"
            target, _, spec = token.partition("@")
            kind, sep, name = target.partition(":")
            if sep:
                if kind == "ds":
                    return f"'{DATA}'!{_shift(col, spec)}{DS_ROWS[name][0]}"
                if kind == "ttm":
                    r = DS_ROWS[name][0]
                    return f"SUM('{DATA}'!{LAST_FOUR_QUARTERS.format(r=r)})"
                if kind == "meta":
                    c = META[name]
                    return f"'{DATA}'!${c[0]}${c[1:]}"
                raise ValueError(f"unknown token {token}")
            code, key = target.split(".", 1) if "." in target else (here, target)
            return self.ref(code, key, _shift(col, spec), here)

        return TOKEN.sub(one, template)


def _formula(row: Row, body: str) -> str:
    if row.raw:
        return f"={body}" if not body.replace(".", "").isdigit() else body
    return f'=IFERROR({body},"n/m")'


# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #

FORMATS = {
    "num": '#,##0.00;[Red]-#,##0.00;"-"',
    "pct": '0.0%;[Red]-0.0%;"-"',
    "x": '0.00"x";[Red]-0.00"x";"-"',
    "days": '0;[Red]-0;"-"',
    "date": "mmm-yy",
    "text": "General",
}
TITLE_FONT = Font(bold=True, size=14)
BOLD = Font(bold=True)
NOTE_FONT = Font(italic=True, color="666666", size=9)
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
SECTION_FILL = PatternFill("solid", fgColor="DDEBF7")
TTM_FILL = PatternFill("solid", fgColor="FFF2CC")
TREND_FILL = PatternFill("solid", fgColor="E2EFDA")
INPUT_FILL = PatternFill("solid", fgColor="FFFF00")


def _header(ws: Worksheet, sheet: Sheet, layout: Layout) -> None:
    ws["A1"] = sheet.heading
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"='{DATA}'!{META['name']}"
    ws["A2"].font = BOLD
    ws["A3"] = "Period"
    date_row = DS_ROWS[sheet.date_row][0]
    for col in PERIODS:
        ws[f"{col}3"] = f"='{DATA}'!{col}{date_row}"
        ws[f"{col}3"].number_format = FORMATS["date"]
    if sheet.quarterly:
        ws[f"{TTM_COL}3"] = "Latest = 10Q high?"
    else:
        ws[f"{TTM_COL}3"] = "TTM"
        ws[f"{KIND_COL}3"] = "Trend"
        for col, years in TREND_COLS.items():
            ws[f"{col}3"] = f"{years}Y" if years != 9 else "9Y / 10Y"
    ws[f"{NOTE_COL}3"] = "Notes"
    for cell in ws[3]:
        if cell.value is not None:
            cell.fill, cell.font = HEAD_FILL, HEAD_FONT
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
    ws["A3"].alignment = Alignment(horizontal="left")


def _trend(row: Row, r: int, col: str, years: int) -> str | None:
    last = PERIODS[-1]
    first_avg = PERIODS[max(row.start, len(PERIODS) - (10 if years == 9 else years))]
    kind = row.trend or ""
    if kind == "cagr":
        start = PERIODS[len(PERIODS) - 1 - years]
        return f'=IFERROR(IF(AND({start}{r}>0,{last}{r}>0),({last}{r}/{start}{r})^(1/{years})-1,"n/m"),"n/m")'
    if kind == "avg":
        return f'=IFERROR(AVERAGE({first_avg}{r}:{last}{r}),"n/m")'
    if kind == "sum":
        return f"=SUM({first_avg}{r}:{last}{r})" if years == 9 else None
    return None


def _write_sheet(ws: Worksheet, sheet: Sheet, layout: Layout) -> None:
    _header(ws, sheet, layout)
    for row in sheet.rows:
        r = layout.rows[(sheet.code, row.key)] if row.key else None
        if row.key is None:
            continue
        ws[f"A{r}"] = row.label
        for i, col in enumerate(PERIODS):
            if i < row.start:
                continue
            try:
                body = layout.resolve(row.f, sheet.code, col)
            except Blank:
                continue
            cell = ws[f"{col}{r}"]
            cell.value = _formula(row, body)
            cell.number_format = FORMATS[row.fmt]
        if sheet.quarterly and row.high:
            span = f"{PERIODS[0]}{r}:{PERIODS[-1]}{r}"
            ws[f"{TTM_COL}{r}"] = f'=IF(COUNT({span})=0,"",IF({PERIODS[-1]}{r}=MAX({span}),"Yes","No"))'
            ws[f"{TTM_COL}{r}"].alignment = Alignment(horizontal="center")
        if not sheet.quarterly and row.ttm:
            template = row.f if row.ttm == "same" else row.ttm
            body = layout.resolve(template, sheet.code, TTM_COL)
            cell = ws[f"{TTM_COL}{r}"]
            cell.value = f'=IF({TTM_INCOMPLETE},"",IFERROR({body},"n/m"))'
            cell.number_format = FORMATS[row.fmt]
            cell.fill = TTM_FILL
        if not sheet.quarterly and row.trend:
            if row.trend.startswith("ratio:"):
                num, den = row.trend[6:].split("/")
                n_row, d_row = layout.rows[(sheet.code, num)], layout.rows[(sheet.code, den)]
                ws[f"{KIND_COL}{r}"] = "Σ÷Σ"
                for col, years in TREND_COLS.items():
                    first = PERIODS[max(row.start, len(PERIODS) - (10 if years == 9 else years))]
                    ws[f"{col}{r}"] = (f'=IFERROR(SUM({first}{n_row}:K{n_row})/SUM({first}{d_row}:K{d_row}),"n/m")')
                    ws[f"{col}{r}"].number_format = FORMATS["pct"]
                    ws[f"{col}{r}"].fill = TREND_FILL
            else:
                ws[f"{KIND_COL}{r}"] = {"cagr": "CAGR", "avg": "Avg", "sum": "Σ 10Y"}[row.trend]
                for col, years in TREND_COLS.items():
                    value = _trend(row, r, col, years)
                    if value is None:
                        continue
                    ws[f"{col}{r}"] = value
                    ws[f"{col}{r}"].number_format = FORMATS["pct" if row.trend == "cagr" else row.fmt]
                    ws[f"{col}{r}"].fill = TREND_FILL
            ws[f"{KIND_COL}{r}"].font = NOTE_FONT
        if row.note:
            ws[f"{NOTE_COL}{r}"] = row.note
            ws[f"{NOTE_COL}{r}"].font = NOTE_FONT
    for row in sheet.rows:
        if row.key is None:
            r = _section_row(sheet, row)
            ws[f"A{r}"] = row.label
            for col in ["A", *PERIODS, TTM_COL]:
                ws[f"{col}{r}"].fill = SECTION_FILL
            ws[f"A{r}"].font = BOLD
    ws.column_dimensions["A"].width = 52
    for col in [*PERIODS, TTM_COL, *TREND_COLS]:
        ws.column_dimensions[col].width = 11
    ws.column_dimensions[KIND_COL].width = 7
    ws.column_dimensions[NOTE_COL].width = 70
    ws.freeze_panes = "B4"


_SECTION_ROWS: dict[tuple[str, int], int] = {}


def _section_row(sheet: Sheet, row: Row) -> int:
    return _SECTION_ROWS[(sheet.code, id(row))]


def _allocate(layout: Layout) -> None:
    for sheet in SHEETS:
        r = 4
        for row in sheet.rows:
            if row.key is None:
                r += 0 if r == 4 else 1  # a blank line before each later section
                _SECTION_ROWS[(sheet.code, id(row))] = r
            else:
                layout.place(sheet.code, row.key, r)
            r += 1
    r = 3
    for row in SUMMARY:
        if row.key is None:
            r += 1
        else:
            layout.place("S", row.key, r)
        r += 1


def _write_summary(ws: Worksheet, layout: Layout) -> None:
    ws["A1"] = "Summary"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Merged Screener template {TEMPLATE_VERSION}. Figures in ₹ crore unless stated."
    ws["A2"].font = NOTE_FONT
    r = 3
    for row in SUMMARY:
        if row.key is None:
            r += 1
            ws[f"A{r}"] = row.label
            ws[f"A{r}"].font = BOLD
            for col in "ABCDE":
                ws[f"{col}{r}"].fill = SECTION_FILL
            r += 1
            continue
        ws[f"A{r}"] = row.label
        cols = ("C", "D", "E") if row.key in SUMMARY_TREND_ROWS else ("C",)
        for col, trend_col in zip(cols, TREND_COLS, strict=False):
            template = row.f.replace("@N}", f"@{trend_col}}}") if row.key in SUMMARY_TREND_ROWS else row.f
            cell = ws[f"{col}{r}"]
            cell.value = _formula(row, layout.resolve(template, "S", "C"))
            cell.number_format = FORMATS[row.fmt]
        if row.key in SUMMARY_INPUTS:
            ws[f"C{r}"].value = float(row.f) if "." in row.f else int(row.f)
            ws[f"C{r}"].fill = INPUT_FILL
        if row.note:
            ws[f"F{r}"] = row.note
            ws[f"F{r}"].font = NOTE_FONT
        r += 1
    ws.column_dimensions["A"].width = 46
    for col in "CDE":
        ws.column_dimensions[col].width = 16
    ws.column_dimensions["F"].width = 70


NOTES_TEXT = (
    ("How to use", (
        "Upload this workbook at https://www.screener.in/excel/ ; 'Export to Excel' then returns it with the "
        "company's figures in 'Data Sheet'.",
        "Never edit 'Data Sheet': Screener replaces it on every export. The sample figures in it (Vinati "
        "Organics) only show the formulas working.",
        "Yellow cells on Summary are inputs. 'n/m' means not meaningful (zero or negative denominator, or a "
        "missing year). Trailing-four-quarter (TTM) figures are left blank unless all four quarters are reported.",
        "Banks, NBFCs and insurers: operating margin, ROCE, interest coverage and working-capital ratios do not "
        "apply; read ROE, book value and the quarterly trend instead.",
        "This workbook is for reading only. The system's Screener validation computes its figures in code from "
        "Data Sheet's raw values, never from these formulas.",
    )),
    ("What was merged", (
        "Dr Vijay Malik template V2.0: SSGR, incremental ROE, value per ₹ retained, notional interest, costs as % "
        "of sales, P/E history, trend CAGRs. It is a paid product: keep the merged file within the family.",
        "Technofunda template v3: statement layouts, common size, DuPont, CFO/PAT, capex and FCF, key-items box.",
        "Analysis.xlsx: quarterly QoQ and YoY growth, and the 'latest is the highest' flags.",
    )),
    ("Fixes made in the merge", (
        "ROCE is EBIT ÷ average (net worth + borrowings). VM divided by average total assets (Data Sheet row 61); "
        "that figure is kept, labelled 'EBIT ÷ average total assets'.",
        "PBT is Screener's reported PBT, after exceptional items; TF rebuilt PBT and lost them. The difference is "
        "shown as 'Exceptional and other items'.",
        "Negative operating margins are shown as negative (VM showed 0).",
        "VM's dividend TTM-growth cell (S41) referred to a deleted cell (#REF!); dropped.",
        "DuPont uses average balances, so its ROE ties to 'Return on average equity' (TF used year-end balances "
        "and its check compared two products exactly).",
        "Every computed cell is wrapped in IFERROR, so missing years show 'n/m', not errors.",
        "Averages over years skip 'n/m' years; margin trends are sales-weighted (Σ profit ÷ Σ sales).",
    )),
    ("Left out, and why", (
        "Portfolio, stop-loss and watchlist sheets (Analysis.xlsx): GOOGLEFINANCE works only in Google Sheets, and "
        "portfolio tracking is not part of a per-company template.",
        "Shareholding pattern (Analysis.xlsx): not in Screener's Data Sheet. The system reads it from exchange "
        "filings (shareholding_pattern).",
        "Technofunda 'Graphs' sheet: charts are not carried over by the generator; add charts by hand if wanted.",
        "VM 'Description' sheet: explanatory text with links to the author's articles (see the original file).",
    )),
)


def _write_notes(ws: Worksheet) -> None:
    ws["A1"] = "Notes"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Template version {TEMPLATE_VERSION}. Built by tools/screener_template/build.py."
    ws["A2"].font = NOTE_FONT
    r = 4
    for heading, lines in NOTES_TEXT:
        ws[f"A{r}"] = heading
        ws[f"A{r}"].font = BOLD
        ws[f"A{r}"].fill = SECTION_FILL
        r += 1
        for line in lines:
            ws[f"A{r}"] = f"• {line}"
            ws[f"A{r}"].alignment = Alignment(wrap_text=True, vertical="top")
            r += 1
        r += 1
    ws[f"A{r}"] = "Row map: where every row came from"
    ws[f"A{r}"].font = BOLD
    ws[f"A{r}"].fill = SECTION_FILL
    r += 1
    for i, head in enumerate(("Sheet", "Row", "Source", "Definition"), start=1):
        cell = ws.cell(row=r, column=i, value=head)
        cell.font, cell.fill = HEAD_FONT, HEAD_FILL
    r += 1
    for sheet_title, rows in (("Summary", SUMMARY), *((s.title, s.rows) for s in SHEETS)):
        for row in rows:
            if row.key is None:
                continue
            ws.cell(row=r, column=1, value=sheet_title)
            ws.cell(row=r, column=2, value=row.label)
            ws.cell(row=r, column=3, value=SOURCES[row.src])
            ws.cell(row=r, column=4, value=row.f)
            r += 1
    ws.column_dimensions["A"].width = 120
    for col, width in (("B", 50), ("C", 55), ("D", 90)):
        ws.column_dimensions[col].width = width


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def _check_data_sheet(ws: Worksheet) -> None:
    wrong = [
        f"row {row}: expected {label!r}, found {ws[f'A{row}'].value!r}"
        for row, label in DS_ROWS.values()
        if (ws[f"A{row}"].value or "").strip() != label
    ]
    if wrong:
        raise SystemExit("Data Sheet layout differs from DS_ROWS:\n  " + "\n  ".join(wrong))


def build(base: Path, out: Path) -> None:
    wb = load_workbook(base)
    if DATA not in wb.sheetnames:
        raise SystemExit(f"{base} has no '{DATA}' sheet")
    _check_data_sheet(wb[DATA])
    for name in list(wb.sheetnames):
        if name != DATA:
            del wb[name]
    for name in list(wb.defined_names):
        del wb.defined_names[name]

    layout = Layout()
    _allocate(layout)
    _write_summary(wb.create_sheet("Summary", 0), layout)
    for i, sheet in enumerate(SHEETS, start=1):
        _write_sheet(wb.create_sheet(sheet.title, i), sheet, layout)
    _write_notes(wb.create_sheet("Notes", len(SHEETS) + 1))
    wb.active = 0
    for ws in wb.worksheets:
        ws.sheet_view.tabSelected = ws.title == "Summary"
    wb.save(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", type=Path, required=True, help="a Screener template whose Data Sheet to keep")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    build(args.base, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
