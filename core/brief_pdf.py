"""
Opportunity Brief: a one-buyer PDF a bid team can circulate internally.

Built from core.buyer_intel.buyer_profile output only, so every number in
the brief traces back to a published CPPP award record.
"""
from __future__ import annotations

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PRODUCT_NAME = "Procurement Opportunity Radar"
NAVY = colors.HexColor("#173451")
INK = colors.HexColor("#18304a")
MUTED = colors.HexColor("#536b82")
LINE = colors.HexColor("#d7e3ed")
BAND = colors.HexColor("#f2f7fb")


def format_inr(value, symbol: str = "₹") -> str:
    """Indian-style amount: crore above 1 Cr, lakh above 1 L, else rupees."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v or v <= 0:
        return "—"
    if v >= 1e7:
        return f"{symbol}{v / 1e7:,.2f} Cr"
    if v >= 1e5:
        return f"{symbol}{v / 1e5:,.2f} L"
    return f"{symbol}{v:,.0f}"


def _fmt_date(value) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "—"
    try:
        return value.strftime("%d %b %Y")
    except AttributeError:
        return str(value)


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=20,
                                leading=24, textColor=INK, alignment=TA_LEFT, spaceAfter=2),
        "eyebrow": ParagraphStyle("eyebrow", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8,
                                  textColor=colors.HexColor("#28638f"), leading=10),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12,
                             leading=15, textColor=INK, spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("body", parent=base["Normal"], fontName="Helvetica", fontSize=8.5,
                               leading=11.5, textColor=INK),
        "muted": ParagraphStyle("muted", parent=base["Normal"], fontName="Helvetica", fontSize=7.5,
                                leading=10, textColor=MUTED),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontName="Helvetica", fontSize=7.5,
                               leading=9.5, textColor=INK),
        "head": ParagraphStyle("head", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.5,
                               leading=9.5, textColor=colors.white),
        "kpi_value": ParagraphStyle("kpi_value", parent=base["Normal"], fontName="Helvetica-Bold",
                                    fontSize=14, leading=17, textColor=INK),
        "kpi_label": ParagraphStyle("kpi_label", parent=base["Normal"], fontName="Helvetica", fontSize=7.5,
                                    leading=9, textColor=MUTED),
    }


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _table(header, rows, widths, st):
    data = [[Paragraph(_esc(h), st["head"]) for h in header]]
    data += [[Paragraph(_esc(c), st["cell"]) for c in row] for row in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def build_buyer_brief(profile: dict, prepared_for: str = "", generated: date | None = None,
                      max_rows: int = 25) -> bytes:
    """Render ``buyer_profile`` output as a landscape A4 PDF and return its bytes."""
    generated = generated or date.today()
    st = _styles()
    s = profile["summary"]
    buf = io.BytesIO()
    page_w, _ = landscape(A4)
    margin = 14 * mm
    width = page_w - 2 * margin

    def _frame(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, landscape(A4)[1] - 8 * mm, page_w, 8 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(margin, landscape(A4)[1] - 5.4 * mm, PRODUCT_NAME.upper())
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(margin, 8 * mm, f"Opportunity Brief · {s['buyer']} · generated {generated:%d %b %Y}")
        canvas.drawRightString(page_w - margin, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=margin, rightMargin=margin,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Opportunity Brief - {s['buyer']}", author=PRODUCT_NAME)
    story = [
        Paragraph("OPPORTUNITY BRIEF", st["eyebrow"]),
        Paragraph(_esc(s["buyer"]), st["title"]),
        Paragraph(
            f"Source: Central Public Procurement Portal award records, {_fmt_date(s['first_award'])} to "
            f"{_fmt_date(s['last_award'])}." + (f" Prepared for {_esc(prepared_for)}." if prepared_for else ""),
            st["muted"]),
        Spacer(1, 6),
    ]

    ending = profile["ending"]
    kpis = [
        (f"{s['awards']:,}", "contracts awarded"),
        (format_inr(s["total_value"], "Rs "), "total awarded value"),
        (f"{s['distinct_suppliers']:,}", "distinct suppliers"),
        (f"{s['median_bids']:.0f}" if s["median_bids"] else "—", "median bids per tender"),
        (f"{len(ending):,}", "contracts ending in the next 12 months"),
    ]
    kpi_table = Table([[Paragraph(v, st["kpi_value"]) for v, _ in kpis],
                       [Paragraph(l, st["kpi_label"]) for _, l in kpis]],
                      colWidths=[width / len(kpis)] * len(kpis))
    kpi_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(kpi_table)

    story.append(Paragraph("Contracts ending soon", st["h2"]))
    if ending.empty:
        story.append(Paragraph("This buyer has not published completion dates for contracts ending in the next 12 months.", st["body"]))
    else:
        rows = [[_fmt_date(r.end_date), r.description, r.incumbent or "—", format_inr(r.contract_value, "Rs "),
                 f"{int(r.duration_days / 30.44)} mo", str(int(r.prior_awards)) if r.prior_awards else "—",
                 r.tender_id or r.tender_ref_no or "—"]
                for r in ending.head(max_rows).itertuples()]
        story.append(_table(["Ends", "Contract", "Current holder", "Value", "Term", "Earlier awards", "Tender ID"],
                            rows, [20 * mm, 92 * mm, 58 * mm, 22 * mm, 14 * mm, 18 * mm, width - 224 * mm], st))
        if len(ending) > max_rows:
            story.append(Paragraph(f"Showing the first {max_rows} of {len(ending):,}. The full list is in the CSV export.", st["muted"]))

    repeat = profile["repeat_requirements"]
    if not repeat.empty:
        rows = [[r.requirement, str(int(r.awards)), format_inr(r.value, "Rs "), _fmt_date(r.first_award), _fmt_date(r.last_award)]
                for r in repeat.head(12).itertuples()]
        story.append(KeepTogether([
            Paragraph("Requirements this buyer awards repeatedly", st["h2"]),
            _table(["Requirement (latest wording)", "Awards", "Total value", "First", "Latest"], rows,
                   [125 * mm, 18 * mm, 30 * mm, 26 * mm, width - 199 * mm], st),
        ]))

    suppliers = profile["suppliers"]
    if not suppliers.empty:
        rows = [[r.supplier, str(int(r.contracts_won)), format_inr(r.value_involved, "Rs "), _fmt_date(r.last_win)]
                for r in suppliers.head(12).itertuples()]
        story.append(KeepTogether([
            Paragraph("Most frequent winning suppliers", st["h2"]),
            _table(["Supplier", "Contracts won", "Value of contracts won", "Latest win"], rows,
                   [125 * mm, 25 * mm, 40 * mm, width - 190 * mm], st),
            Paragraph("Joint awards count once for each named winner; value is that of the whole contract.", st["muted"]),
        ]))

    story += [
        Paragraph("How to read this brief", st["h2"]),
        Paragraph(
            "End dates are the completion dates the buyer itself published in its award notices; none are estimated. "
            "A contract ending is the earliest public signal that its replacement will be tendered, typically before the "
            "end date. In back-tests on 2023–2025 data, when the same buyer re-awarded the same work, 38–62% of those "
            "re-awards fell within 90 days of the published end date. In three of four test periods a new award landed "
            "near the end date 1.4–2.7 times as often as in an equally wide window at the wrong time; in the earliest "
            "period there was no difference. Many contracts are one-off works that are never re-awarded, so treat each "
            "row as a lead to watch, not a guaranteed tender. Verify any row before bidding: search its Tender ID on "
            "eprocure.gov.in/cppp/resultoftendersnew (Result of Tenders).", st["body"]),
    ]
    doc.build(story, onFirstPage=_frame, onLaterPages=_frame)
    return buf.getvalue()
