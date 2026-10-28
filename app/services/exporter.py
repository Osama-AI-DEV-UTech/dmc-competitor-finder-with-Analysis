"""Excel export (XlsxWriter): live formulas WITH cached values, so previews show numbers and Excel stays editable."""
from __future__ import annotations

import io
import statistics
from datetime import datetime
from typing import Any, Optional

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell

from ..models import Offer, ReferencePackage

FONT = "Arial"
NAVY = "#1F3864"


def _excluded_reason(o: Offer) -> str:
    if o.in_stats:
        return ""
    bits = []
    if o.match_level != "same":
        bits.append(f"not a 'Same' match ({o.match_level})")
    if not o.price_verified:
        bits.append("price not verified on page" if o.price_from is not None else "no price")
    bits += [f for f in o.flags if f.startswith(("Flights", "Price far", "Page warns", "Price is for", "No FX", "Price basis"))]
    return "; ".join(dict.fromkeys(bits)) or "not eligible"


def build_workbook(analysis: dict[str, Any], ref: ReferencePackage, offers: list[Offer], summary: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_urls": False})
    wb.set_calc_mode("auto")

    def fmt(**kw):
        base = {"font_name": FONT, "font_size": 10, "valign": "top"}
        base.update(kw)
        return wb.add_format(base)

    f_title = fmt(bold=True, font_size=14)
    f_hdr = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, text_wrap=True, align="center", valign="vcenter", border=1)
    f_txt = fmt(text_wrap=True, border=1)
    f_ctr = fmt(align="center", text_wrap=True, border=1)
    f_in_num = fmt(font_color="#0000FF", num_format="#,##0.00", border=1)
    f_in_int = fmt(font_color="#0000FF", num_format="#,##0", border=1, align="center")
    f_fx = fmt(num_format="0.000000", border=1, align="center")
    f_usd = fmt(num_format='"$"#,##0', border=1)
    f_usd2 = fmt(num_format='"$"#,##0.00', border=1)
    f_diff = fmt(num_format='"$"#,##0;[Red]-"$"#,##0;"$"0', border=1)
    f_pct = fmt(num_format="0%", border=1, align="center")
    f_link = fmt(font_color="#0563C1", underline=1, border=1, text_wrap=True)
    f_bold = fmt(bold=True)
    f_note = fmt(text_wrap=True, bg_color="#FFF2CC", border=1)
    f_green = wb.add_format({"bg_color": "#C6EFCE"})
    f_red = wb.add_format({"bg_color": "#F8CBAD"})
    f_amber = wb.add_format({"bg_color": "#FFEB9C"})

    # ------------------------------------------------------------ data prep
    page = [o for o in offers if o.verification == "page"]
    leads = [o for o in offers if o.verification == "snippet"]
    main = [o for o in page if o.match_level in ("same", "near")]
    partial = [o for o in page if o.match_level == "partial"]
    rejected = [o for o in page if o.match_level == "rejected"]
    main.sort(key=lambda o: (o.match_level != "same", not o.in_stats, -o.match_score, o.price_usd_from if o.price_usd_from is not None else 1e12))
    partial.sort(key=lambda o: -o.match_score)
    comps = [c.name for c in ref.components if c.required]
    fx_snap: dict[str, list] = summary.get("fx_snapshot") or {"USD": [1.0, "base"]}
    fx_snap.setdefault("USD", [1.0, "base"])
    for o in offers:
        fx_snap.setdefault(o.currency, [None, "no rate available"])
    fx_snap.setdefault(ref.price_currency, [None, "no rate available"])

    def rate(cur: str) -> Optional[float]:
        r = fx_snap.get(cur, [None])[0]
        return float(r) if r else None

    def divisor(o: Offer) -> float:
        b = (o.price_basis or "").lower()
        if b == "per couple":
            return 2.0
        if b in ("per group", "total") and o.price_pax and o.price_pax > 0:
            return float(o.price_pax)
        return 1.0

    ref_div = 2.0 if (ref.price_basis or "").lower() == "per couple" else 1.0
    ref_usd = (round(ref.price_amount * rate(ref.price_currency) / ref_div, 2)
               if ref.price_amount is not None and rate(ref.price_currency) else None)

    # ------------------------------------------------------------ Assumptions (created first so others can reference it)
    sh_sum = wb.add_worksheet("Summary")
    sh_main = wb.add_worksheet("Closest Matches")
    sh_part = wb.add_worksheet("Partial Matches")
    sh_gap = wb.add_worksheet("Gap Analysis")
    sh_rej = wb.add_worksheet("Rejected")
    sh_lead = wb.add_worksheet("Pages To Check Manually")
    sh_evid = wb.add_worksheet("Evidence")
    sh_ref = wb.add_worksheet("Reference Package")
    sh_as = wb.add_worksheet("Assumptions")

    fx_rows = sorted(fx_snap.items())
    FX0 = 10  # first FX data row (1-based)
    FX1 = FX0 + len(fx_rows) - 1
    fx_key = f"Assumptions!$A${FX0}:$A${FX1}"
    fx_val = f"Assumptions!$B${FX0}:$B${FX1}"

    sh_as.write("A1", "Assumptions, FX rates and method", f_title)
    sh_as.write("A3", "Reference package price (native)", f_bold)
    if ref.price_amount is not None:
        sh_as.write_number("B3", ref.price_amount, f_in_num)
    else:
        sh_as.write_blank("B3", None, f_in_num)
    sh_as.write("A4", "Reference currency", f_bold)
    sh_as.write("B4", ref.price_currency, fmt(font_color="#0000FF", border=1, align="center"))
    sh_as.write("A5", "Reference price divisor (2 if quoted per couple)", f_bold)
    sh_as.write_number("B5", ref_div, f_in_int)
    sh_as.write("A6", "Reference price (USD per person)", f_bold)
    sh_as.write_formula("B6", f'=IF(B3="","",ROUND(B3*INDEX({fx_val},MATCH(B4,{fx_key},0))/B5,2))', f_usd2,
                        ref_usd if ref_usd is not None else "")
    for i, h in enumerate(("Currency", "USD per 1 unit", "Source / note")):
        sh_as.write(FX0 - 2, i, h, f_hdr)
    for i, (cur, (r, src)) in enumerate(fx_rows):
        row = FX0 - 1 + i
        sh_as.write(row, 0, cur, f_ctr)
        if r:
            sh_as.write_number(row, 1, float(r), f_fx)
        else:
            sh_as.write_blank(row, 1, None, f_fx)
        sh_as.write(row, 2, src, f_txt)
    notes_row = FX1 + 2
    sh_as.write(notes_row, 0, "METHOD AND RULES", f_bold)
    rules = [
        f"Run {analysis.get('id')} created {analysis.get('created_at')}; exported {datetime.now().strftime('%Y-%m-%d %H:%M')}.",
        "Prices are what each page displayed at fetch time (usually 'from' prices); they are not live quotes and may not apply to your travel dates. Confirm before relying on them.",
        "'Same Package Competitors' = a whole multi-day package (with accommodation, one price for everything) where EVERY component of your package is proven on the page. 'Near Matches' = nearly the same but something is missing/unproven (listed in the 'Missing' column). 'Rejected' = single activities, hotel/flight-only items, other destinations or too few matching components, with the reason.",
        "A component is 'Yes' only when the page contained a verbatim quote proving it (see Evidence sheet). 'Not stated' means the page did not mention it, which is not proof it is excluded.",
        "In stats? = Yes only for Same/Near offers (Same only when the run used 'strict') whose price was found verbatim on the page, per-person basis resolved, no unavailable/expired warning, not an extreme price outlier vs. yours, and flights not bundled (unless the run option allowed it).",
        "Per-person divisor: prices quoted per couple are divided by 2; group/total prices are divided by the traveller count when the page states it.",
        "USD columns are formulas: price x FX rate (this sheet) / divisor. Blue cells are inputs you may edit; the sheet recalculates.",
        "FX: rates marked 'default' are approximate fallbacks, not live. Live rates (open.er-api.com) are used when reachable.",
        "'Pages To Check Manually' are pages the search found but could not be read automatically (blocked, JavaScript-only, timeouts). Real DMC B2B net rates are usually behind agent logins and will not appear here.",
    ]
    for i, t in enumerate(rules):
        sh_as.merge_range(notes_row + 1 + i, 0, notes_row + 1 + i, 2, t, f_note)
        sh_as.set_row(notes_row + 1 + i, 44)
    sh_as.set_column("A:A", 46)
    sh_as.set_column("B:B", 16)
    sh_as.set_column("C:C", 70)
    REF_USD_CELL = "Assumptions!$B$6"

    # ------------------------------------------------------------ competitor sheets
    fixed_hdr = ["#", "Seller / operator", "Type", "Listed on", "Package name", "Hotel", "Stars", "Nights",
                 "Price from (native)", "Price to (native)", "Currency", "FX to USD", "Per-person divisor",
                 "USD from (pp)", "USD to (pp)", "Flights included?", "Transfers", "Visa included?"]
    n_fixed = len(fixed_hdr)
    tail_hdr = ["Components matched", "Match %", "Match level", "Score (0-100)", "Diff vs your price (USD)",
                "In stats?", "Stats price (USD)", "Price verified?", "Why excluded from stats", "Missing / unproven components", "Extras they offer (not in your package)", "Price vs content",
                "Flags", "Notes", "Source URL"]

    def tri(v: Optional[bool]) -> str:
        return "Yes" if v is True else ("No" if v is False else "Not stated")

    def write_offers(ws, rows: list[Offer], title: str, stats_cols: bool) -> tuple[int, int]:
        ws.write(0, 0, title, f_title)
        ws.merge_range(1, 0, 1, 12, f"Reference: {ref.title or ref.destination} | {ref.nights or '?'}N/{ref.days or '?'}D | "
                       f"{ref.hotel_star or '?'}-star | your price: {ref.price_currency} {ref.price_amount if ref.price_amount is not None else 'n/a'}. "
                       "Prices as displayed on each page; see Assumptions.", fmt(text_wrap=True))
        ws.set_row(1, 30)
        hdr = fixed_hdr + comps + tail_hdr
        HR = 3
        for i, h in enumerate(hdr):
            ws.write(HR, i, h, f_hdr)
        ws.set_row(HR, 48)
        c_first = n_fixed
        c_last = n_fixed + len(comps) - 1
        t = n_fixed + len(comps)  # first tail column index
        col = {name: t + i for i, name in enumerate(tail_hdr)}
        for i, o in enumerate(rows):
            r = HR + 1 + i
            cell = lambda c: xl_rowcol_to_cell(r, c)
            ws.write_number(r, 0, i + 1, f_ctr)
            ws.write(r, 1, o.seller, f_txt)
            ws.write(r, 2, o.seller_type, f_ctr)
            ws.write(r, 3, o.listed_on, f_txt)
            ws.write(r, 4, o.package_name, f_txt)
            ws.write(r, 5, o.hotel or "", f_txt)
            ws.write(r, 6, o.hotel_star if o.hotel_star else "", f_ctr)
            ws.write(r, 7, o.nights if o.nights else "", f_ctr)
            if o.price_from is not None:
                ws.write_number(r, 8, o.price_from, f_in_num)
            else:
                ws.write_blank(r, 8, None, f_in_num)
            if o.price_to is not None:
                ws.write_number(r, 9, o.price_to, f_in_num)
            else:
                ws.write_blank(r, 9, None, f_in_num)
            ws.write(r, 10, o.currency, fmt(font_color="#0000FF", border=1, align="center"))
            rt = rate(o.currency)
            ws.write_formula(r, 11, f'=IFERROR(INDEX({fx_val},MATCH({cell(10)},{fx_key},0)),"")', f_fx, rt if rt else "")
            dv = divisor(o)
            ws.write_number(r, 12, dv, f_in_int)
            uf = round(o.price_from * rt / dv, 2) if (o.price_from is not None and rt) else ""
            ut = round(o.price_to * rt / dv, 2) if (o.price_to is not None and rt) else ""
            ws.write_formula(r, 13, f'=IF(OR({cell(8)}="",{cell(11)}=""),"",ROUND({cell(8)}*{cell(11)}/{cell(12)},2))', f_usd, uf)
            ws.write_formula(r, 14, f'=IF(OR({cell(9)}="",{cell(11)}=""),"",ROUND({cell(9)}*{cell(11)}/{cell(12)},2))', f_usd, ut)
            ws.write(r, 15, tri(o.flights_included), f_ctr)
            ws.write(r, 16, o.transfers or "Not stated", f_ctr)
            ws.write(r, 17, tri(o.visa_included), f_ctr)
            for j, name in enumerate(comps):
                st = o.component_matches.get(name, "not_stated")
                ws.write(r, c_first + j, {"yes": "Yes", "no": "No"}.get(st, "Not stated"), f_ctr)
            yes = sum(1 for n in comps if o.component_matches.get(n) == "yes")
            if comps:
                rng = f"{xl_rowcol_to_cell(r, c_first)}:{xl_rowcol_to_cell(r, c_last)}"
                ws.write_formula(r, col["Components matched"], f'=COUNTIF({rng},"Yes")', f_ctr, yes)
                ws.write_formula(r, col["Match %"], f"={cell(col['Components matched'])}/{len(comps)}", f_pct, yes / len(comps))
            else:
                ws.write(r, col["Components matched"], 0, f_ctr)
                ws.write(r, col["Match %"], 0, f_pct)
            ws.write(r, col["Match level"], o.match_level.capitalize(), f_ctr)
            ws.write_number(r, col["Score (0-100)"], o.match_score, f_ctr)
            diff = (uf - ref_usd) if (uf != "" and ref_usd is not None and o.price_verified) else ""
            ws.write_formula(r, col["Diff vs your price (USD)"],
                             f'=IF(OR({cell(13)}="",{REF_USD_CELL}="",{cell(col["Price verified?"])}<>"Yes"),"",{cell(13)}-{REF_USD_CELL})',
                             f_diff, diff)
            ws.write(r, col["In stats?"], "Yes" if o.in_stats else "No", f_ctr)
            ws.write_formula(r, col["Stats price (USD)"],
                             f'=IF({cell(col["In stats?"])}="Yes",{cell(13)},"")', f_usd, uf if (o.in_stats and uf != "") else "")
            ws.write(r, col["Price verified?"], "Yes" if o.price_verified else "No", f_ctr)
            ws.write(r, col["Why excluded from stats"], _excluded_reason(o), f_txt)
            ws.write(r, col["Missing / unproven components"], "; ".join(o.missing) if o.match_level != "same" else "", f_txt)
            ws.write(r, col["Extras they offer (not in your package)"], "; ".join(o.extras), f_txt)
            ws.write(r, col["Price vs content"], o.value_note, f_txt)
            ws.write(r, col["Flags"], "; ".join(o.flags), f_txt)
            ws.write(r, col["Notes"], o.notes, f_txt)
            if o.url:
                ws.write_url(r, col["Source URL"], o.url, f_link, o.url)
            else:
                ws.write(r, col["Source URL"], "", f_txt)
            ws.set_row(r, 62)
        last = HR + len(rows)
        if rows and comps:
            rng = f"{xl_rowcol_to_cell(HR + 1, c_first)}:{xl_rowcol_to_cell(last, c_last)}"
            ws.conditional_format(rng, {"type": "cell", "criteria": "==", "value": '"Yes"', "format": f_green})
            ws.conditional_format(rng, {"type": "cell", "criteria": "==", "value": '"No"', "format": f_red})
        if rows:
            fl = xl_rowcol_to_cell(HR + 1, 15)
            ws.conditional_format(f"{fl}:{xl_rowcol_to_cell(last, 15)}", {"type": "cell", "criteria": "==", "value": '"Yes"', "format": f_amber})
            sc = col["In stats?"]
            ws.conditional_format(f"{xl_rowcol_to_cell(HR + 1, sc)}:{xl_rowcol_to_cell(last, sc)}",
                                  {"type": "cell", "criteria": "==", "value": '"Yes"', "format": f_green})
        widths = [4, 24, 14, 20, 32, 24, 7, 8, 12, 12, 9, 10, 10, 12, 12, 11, 14, 10] + [13] * len(comps) + \
                 [11, 9, 10, 9, 13, 9, 12, 10, 34, 40, 40, 44, 44, 44, 50]
        for i, w in enumerate(widths):
            ws.set_column(i, i, w)
        ws.freeze_panes(HR + 1, 3)
        if rows:
            ws.autofilter(HR, 0, last, len(hdr) - 1)
        ws.set_landscape()
        ws.fit_to_pages(1, 0)
        return HR, last

    HRm, lastm = write_offers(sh_main, main, "Closest competitor packages (Same = every component confirmed; Near = most components, gaps listed in the Missing column)", True)
    write_offers(sh_part, partial, "Partial matches (fewer components or different length/class - for context, not in price statistics)", False)

    # ------------------------------------------------------------ gap analysis
    sh_gap.write(0, 0, "Gap analysis: what you sell vs what competitors sell", f_title)
    gap = summary or {}
    r = 2
    if gap.get("value_verdict_text"):
        sh_gap.merge_range(r, 0, r, 7, gap["value_verdict_text"], f_note)
        sh_gap.set_row(r, 36)
        r += 2
    sh_gap.write(r, 0, "A. How many competitors show each of YOUR components", f_bold)
    r += 1
    for i, h in enumerate(["Your component", "Competitors showing it", "Competitors compared", "Share"]):
        sh_gap.write(r, i, h, f_hdr)
    for cv in gap.get("component_coverage") or []:
        r += 1
        sh_gap.write(r, 0, cv["name"], f_txt)
        sh_gap.write_number(r, 1, cv["have"], f_ctr)
        sh_gap.write_number(r, 2, cv["total"], f_ctr)
        sh_gap.write_formula(r, 3, f"=IF(C{r + 1}=0,0,B{r + 1}/C{r + 1})", f_pct, (cv["have"] / cv["total"]) if cv["total"] else 0)
    r += 2
    sh_gap.write(r, 0, "B. Extras competitors offer that your package does not", f_bold)
    r += 1
    for i, h in enumerate(["Extra", "Competitors offering it", "Median price of those (USD pp)"]):
        sh_gap.write(r, i, h, f_hdr)
    for ex in gap.get("top_extras") or []:
        r += 1
        sh_gap.write(r, 0, ex["name"], f_txt)
        sh_gap.write_number(r, 1, ex["count"], f_ctr)
        if ex.get("median_usd") is not None:
            sh_gap.write_number(r, 2, ex["median_usd"], f_usd)
        else:
            sh_gap.write(r, 2, "", f_txt)
    r += 2
    sh_gap.write(r, 0, "C. Competitor by competitor", f_bold)
    r += 1
    heads = ["Seller", "Package", "Match", "USD pp", "Diff vs your price (USD)", "Your components they show",
             "YOUR components they do NOT show", "THEIR extras (you do not offer)", "Price vs content", "URL"]
    for i, h in enumerate(heads):
        sh_gap.write(r, i, h, f_hdr)
    sh_gap.set_row(r, 36)
    gap_rows = [o for o in page if o.match_level in ("same", "near", "partial")]
    gap_rows.sort(key=lambda o: (o.price_usd_from is None, o.price_usd_from or 0))
    first = r + 1
    for o in gap_rows:
        r += 1
        usd = o.price_usd_from if o.price_verified else None
        sh_gap.write(r, 0, o.seller, f_txt)
        sh_gap.write(r, 1, o.package_name, f_txt)
        sh_gap.write(r, 2, o.match_level.capitalize(), f_ctr)
        if usd is not None:
            sh_gap.write_number(r, 3, usd, f_usd)
            if ref_usd is not None:
                sh_gap.write_formula(r, 4, f'=IF(OR(D{r + 1}="",{REF_USD_CELL}=""),"",D{r + 1}-{REF_USD_CELL})', f_diff, usd - ref_usd)
            else:
                sh_gap.write(r, 4, "", f_txt)
        else:
            sh_gap.write(r, 3, "no verified price", f_txt)
            sh_gap.write(r, 4, "", f_txt)
        sh_gap.write(r, 5, f"{o.components_matched}/{o.components_total}", f_ctr)
        sh_gap.write(r, 6, "; ".join(o.missing), f_txt)
        sh_gap.write(r, 7, "; ".join(o.extras), f_txt)
        sh_gap.write(r, 8, o.value_note, f_txt)
        if o.url:
            sh_gap.write_url(r, 9, o.url, f_link, o.url)
        sh_gap.set_row(r, 48)
    if gap_rows:
        sh_gap.autofilter(first - 1, 0, r, len(heads) - 1)
    for i, w in enumerate([34, 36, 10, 14, 14, 14, 48, 48, 52, 50]):
        sh_gap.set_column(i, i, w)
    sh_gap.write(r + 2, 0, "'Not shown' means the competitor's page did not prove the item - it may still include it. Extras come from verbatim quotes on their page.", f_note)

    # ------------------------------------------------------------ rejected
    sh_rej.write(0, 0, "Pages that were checked and rejected - and why", f_title)
    for i, h in enumerate(["#", "Seller", "Page / package", "Type found", "Price (native)", "Currency", "Reason rejected", "URL"]):
        sh_rej.write(2, i, h, f_hdr)
    for i, o in enumerate(rejected):
        r = 3 + i
        sh_rej.write_number(r, 0, i + 1, f_ctr)
        sh_rej.write(r, 1, o.seller, f_txt)
        sh_rej.write(r, 2, o.package_name, f_txt)
        sh_rej.write(r, 3, o.offer_kind.replace("_", " "), f_ctr)
        if o.price_from is not None:
            sh_rej.write_number(r, 4, o.price_from, f_in_num)
        else:
            sh_rej.write(r, 4, "", f_txt)
        sh_rej.write(r, 5, o.currency, f_ctr)
        sh_rej.write(r, 6, o.reject_reason, f_txt)
        sh_rej.write_url(r, 7, o.url, f_link, o.url) if o.url else sh_rej.write(r, 7, "", f_txt)
        sh_rej.set_row(r, 30)
    for i, w in enumerate([4, 24, 44, 18, 14, 9, 44, 60]):
        sh_rej.set_column(i, i, w)
    sh_rej.freeze_panes(3, 0)
    if rejected:
        sh_rej.autofilter(2, 0, 2 + len(rejected), 7)

    # ------------------------------------------------------------ leads
    sh_lead.write(0, 0, "Pages the search found but that could not be read automatically", f_title)
    sh_lead.merge_range(1, 0, 1, 4, "Open each link and check the package by hand; many are blocked, JavaScript-only or login-gated.", fmt(text_wrap=True))
    for i, h in enumerate(["#", "Site", "Page title", "Why not read", "URL"]):
        sh_lead.write(3, i, h, f_hdr)
    for i, o in enumerate(leads):
        r = 4 + i
        sh_lead.write_number(r, 0, i + 1, f_ctr)
        sh_lead.write(r, 1, o.listed_on, f_txt)
        sh_lead.write(r, 2, o.package_name, f_txt)
        sh_lead.write(r, 3, "; ".join(f for f in o.flags if f.startswith("Could not")), f_txt)
        sh_lead.write_url(r, 4, o.url, f_link, o.url)
        sh_lead.set_row(r, 32)
    for i, w in enumerate([4, 26, 50, 50, 70]):
        sh_lead.set_column(i, i, w)
    sh_lead.freeze_panes(4, 0)

    # ------------------------------------------------------------ evidence
    sh_evid.write(0, 0, "Evidence: verbatim quotes from each competitor page", f_title)
    for i, h in enumerate(["Seller", "Package", "Component", "Status", "Quote from page", "URL"]):
        sh_evid.write(2, i, h, f_hdr)
    r = 3
    for o in main + partial:
        for name in comps:
            st = o.component_matches.get(name, "not_stated")
            sh_evid.write(r, 0, o.seller, f_txt)
            sh_evid.write(r, 1, o.package_name, f_txt)
            sh_evid.write(r, 2, name, f_txt)
            sh_evid.write(r, 3, {"yes": "Yes", "no": "No"}.get(st, "Not stated"), f_ctr)
            sh_evid.write(r, 4, o.evidence.get(name, ""), f_txt)
            sh_evid.write_url(r, 5, o.url, f_link, o.url) if o.url else sh_evid.write(r, 5, "", f_txt)
            r += 1
    for i, w in enumerate([24, 36, 36, 11, 70, 50]):
        sh_evid.set_column(i, i, w)
    sh_evid.freeze_panes(3, 0)
    if r > 3:
        sh_evid.autofilter(2, 0, r - 1, 5)
        sh_evid.conditional_format(3, 3, r - 1, 3, {"type": "cell", "criteria": "==", "value": '"Yes"', "format": f_green})
        sh_evid.conditional_format(3, 3, r - 1, 3, {"type": "cell", "criteria": "==", "value": '"No"', "format": f_red})

    # ------------------------------------------------------------ reference
    sh_ref.write(0, 0, "Your package, as read from the screenshot", f_title)
    fields = [
        ("Title", ref.title), ("Destination", ref.destination), ("Country", ref.country),
        ("Cities", ", ".join(ref.cities)), ("Nights", ref.nights), ("Days", ref.days), ("Hotel stars", ref.hotel_star),
        ("Hotels", ", ".join(ref.hotel_names)), ("Price (native)", ref.price_amount), ("Currency", ref.price_currency),
        ("Price basis", ref.price_basis), ("Operator", ref.operator), ("Transfers", ref.transfers),
        ("Flights included", tri(ref.flights_included)), ("Visa included", tri(ref.visa_included)),
        ("Meals", ref.meals_summary), ("Group size", ref.group_size), ("Travel month", ref.travel_month),
        ("Availability range", ref.availability_range), ("Cancellation", ref.cancellation_policy),
        ("Not included", "; ".join(ref.exclusions)), ("Extraction notes", ref.extraction_notes),
    ]
    for i, (k, v) in enumerate(fields):
        sh_ref.write(2 + i, 0, k, fmt(bold=True, border=1))
        sh_ref.write(2 + i, 1, "" if v is None else v, f_txt)
    r0 = 2 + len(fields) + 1
    for i, h in enumerate(["Component used for matching", "Category", "Required"]):
        sh_ref.write(r0, i, h, f_hdr)
    for i, c in enumerate(ref.components):
        sh_ref.write(r0 + 1 + i, 0, c.name, f_txt)
        sh_ref.write(r0 + 1 + i, 1, c.category, f_ctr)
        sh_ref.write(r0 + 1 + i, 2, "Yes" if c.required else "No", f_ctr)
    sh_ref.set_column("A:A", 38)
    sh_ref.set_column("B:B", 70)
    sh_ref.set_column("C:C", 12)

    # ------------------------------------------------------------ summary
    sh_sum.write("A1", "Competitor analysis summary", f_title)
    sh_sum.write("A2", f"{ref.title or ref.destination}  |  run {analysis.get('id')}", fmt(italic=True))
    sh_sum.write("A4", "Metric", f_hdr)
    sh_sum.write("B4", "Value", f_hdr)
    stat_col = n_fixed + len(comps) + tail_hdr.index("Stats price (USD)")
    sc_name = xl_col_to_name(stat_col)
    s_rng = f"'Closest Matches'!${sc_name}${HRm + 2}:${sc_name}${max(lastm + 1, HRm + 2)}"
    nm_col = xl_col_to_name(1)
    n_rng = f"'Closest Matches'!${nm_col}${HRm + 2}:${nm_col}${max(lastm + 1, HRm + 2)}"
    usd_col = xl_col_to_name(13)
    u_rng = f"'Closest Matches'!${usd_col}${HRm + 2}:${usd_col}${max(lastm + 1, HRm + 2)}"
    fl_col = xl_col_to_name(15)
    fl_rng = f"'Closest Matches'!${fl_col}${HRm + 2}:${fl_col}${max(lastm + 1, HRm + 2)}"
    prices = sorted(o.price_usd_from for o in main if o.in_stats and o.price_usd_from is not None)
    prices = [round(p, 2) for p in prices]
    have = bool(prices)
    med = statistics.median(prices) if have else "n/a"
    na = '"n/a"'
    rows_s = [
        ("Your price (USD per person)", f"={REF_USD_CELL}", f_usd2, ref_usd if ref_usd is not None else ""),
        ("Comparable competitor packages (Same + Near)", f"=COUNTA({n_rng})", fmt(border=1, num_format="0"), len(main)),
        ("  of which show a USD price", f"=COUNT({u_rng})", fmt(border=1, num_format="0"),
         sum(1 for o in main if o.price_usd_from is not None)),
        ("  of which include flights", f'=COUNTIF({fl_rng},"Yes")', fmt(border=1, num_format="0"),
         sum(1 for o in main if o.flights_included is True)),
        ("Offers used in price statistics", f"=COUNT({s_rng})", fmt(border=1, num_format="0"), len(prices)),
        ("Lowest price (USD pp)", f"=IF(COUNT({s_rng})=0,{na},MIN({s_rng}))", f_usd2, prices[0] if have else "n/a"),
        ("Highest price (USD pp)", f"=IF(COUNT({s_rng})=0,{na},MAX({s_rng}))", f_usd2, prices[-1] if have else "n/a"),
        ("Median price (USD pp)", f"=IF(COUNT({s_rng})=0,{na},MEDIAN({s_rng}))", f_usd2, med),
        ("Average price (USD pp)", f"=IF(COUNT({s_rng})=0,{na},ROUND(AVERAGE({s_rng}),2))", f_usd2,
         round(statistics.fmean(prices), 2) if have else "n/a"),
    ]
    for i, (lab, fm, f, val) in enumerate(rows_s):
        r = 4 + i
        sh_sum.write(r, 0, lab, fmt(border=1, bold=not lab.startswith("  ")))
        sh_sum.write_formula(r, 1, fm, f, val)
    r = 4 + len(rows_s)
    vs_med = (ref_usd - med) if (have and ref_usd is not None) else "n/a"
    sh_sum.write(r, 0, "Your price vs median (USD)", fmt(border=1, bold=True))
    sh_sum.write_formula(r, 1, f'=IF(OR(COUNT({s_rng})=0,{REF_USD_CELL}=""),{na},{REF_USD_CELL}-B12)', f_diff, vs_med)
    sh_sum.write(r + 1, 0, "Your price vs median (%)", fmt(border=1, bold=True))
    vs_pct = ((ref_usd - med) / med) if (have and ref_usd is not None and med) else "n/a"
    sh_sum.write_formula(r + 1, 1, f'=IF(OR(COUNT({s_rng})=0,{REF_USD_CELL}=""),{na},IF(B12=0,{na},B{r + 1}/B12))',
                         fmt(border=1, num_format="0.0%"), vs_pct)
    rank_txt = "n/a"
    if have and ref_usd is not None:
        rank_txt = f"{sum(1 for p in prices if p < ref_usd) + 1} of {len(prices) + 1}"
    sh_sum.write(r + 2, 0, "Your price rank (1 = cheapest)", fmt(border=1, bold=True))
    sh_sum.write_formula(r + 2, 1,
                         f'=IF(OR(COUNT({s_rng})=0,{REF_USD_CELL}=""),{na},(COUNTIF({s_rng},"<"&{REF_USD_CELL})+1)&" of "&(COUNT({s_rng})+1))',
                         fmt(border=1, align="right"), rank_txt)
    r += 4
    sh_sum.write(r, 0, "Match levels (pages read)", f_hdr)
    sh_sum.write(r, 1, "Count", f_hdr)
    for i, lv in enumerate(("same", "near", "partial", "rejected")):
        sh_sum.write(r + 1 + i, 0, lv.capitalize(), fmt(border=1))
        sh_sum.write_number(r + 1 + i, 1, sum(1 for o in page if o.match_level == lv), fmt(border=1, num_format="0"))
    r += 6
    sh_sum.write(r, 0, "Pages needing a manual check", fmt(border=1, bold=True))
    sh_sum.write_number(r, 1, len(leads), fmt(border=1, num_format="0"))
    wide = sorted(o.price_usd_from for o in page if o.in_wide and o.price_usd_from is not None)
    if wide:
        for lab, val in (("All whole-package offers with a verified price (incl. partial)", len(wide)),
                         ("  lowest (USD pp)", wide[0]), ("  median (USD pp)", statistics.median(wide)), ("  highest (USD pp)", wide[-1])):
            r += 1
            sh_sum.write(r, 0, lab, fmt(border=1))
            sh_sum.write_number(r, 1, val, f_usd2 if "USD" in lab else fmt(border=1, num_format="0"))
    r += 2
    msgs = [
        "READ THIS",
        "Statistics use only offers marked 'In stats? = Yes' (Same/Near package, verified whole-package price, no flights bundled). See Assumptions for the full rules.",
        "Prices are 'as displayed' on public pages. No page is guaranteed to quote your travel dates. DMC net rates are usually behind logins and need a direct request.",
    ]
    for i, t in enumerate(msgs):
        if i == 0:
            sh_sum.write(r, 0, t, f_bold)
        else:
            sh_sum.merge_range(r + i, 0, r + i, 1, t, f_note)
            sh_sum.set_row(r + i, 44)
    sh_sum.set_column("A:A", 52)
    sh_sum.set_column("B:B", 22)
    sh_sum.activate()

    wb.close()
    return buf.getvalue()


def offers_from_rows(rows: list[dict[str, Any]]) -> list[Offer]:
    out = []
    for r in rows:
        try:
            out.append(Offer(**r))
        except Exception:
            continue
    return out
