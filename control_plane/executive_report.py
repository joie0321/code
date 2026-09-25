"""In-memory executive PDF reporting for one collector and observed-time window."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from re import sub
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_MAX_VALUE_LENGTH = 160
_VM_NAMES_PER_LINE = 12


def _safe_text(value: object, limit: int = _MAX_VALUE_LENGTH) -> str:
    """Make untrusted inventory text safe for ReportLab's paragraph markup."""

    raw = " ".join(str(value).split())[:limit]
    return escape(raw, {"'": "&apos;"})


def executive_report_filename(display_name: object, generated_at: datetime) -> str:
    """Return a browser-safe, non-secret filename for a generated report."""

    safe_name = sub(r"[^A-Za-z0-9_-]+", "-", str(display_name)).strip("-") or "collector"
    return (
        "migration-discovery-executive-report_"
        f"{safe_name[:64]}_{generated_at.astimezone(UTC):%Y%m%d-%H%M%S}.pdf"
    )


def _page_footer(canvas, document) -> None:  # type: ignore[no-untyped-def]
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.drawRightString(A4[0] - 1.5 * cm, 1.1 * cm, f"Page {document.page}")
    canvas.restoreState()


def build_executive_report(
    collector: Mapping[str, object],
    summary: Mapping[str, int],
    waves: Sequence[Mapping[str, object]],
    reporting_window: str,
    generated_at: datetime | None = None,
) -> bytes:
    """Build a download-only PDF; credentials and raw connection rows are excluded."""

    created_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.8 * cm,
        title="Migration Discovery Executive Report",
        author="Migration Discovery",
        pageCompression=0,
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ExecutiveTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=8,
    )
    heading = ParagraphStyle(
        "ExecutiveHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#1e3a5f"),
        spaceBefore=14,
        spaceAfter=7,
    )
    body = ParagraphStyle(
        "ExecutiveBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#1f2937"),
    )
    small = ParagraphStyle(
        "ExecutiveSmall",
        parent=body,
        fontSize=8,
        leading=10,
    )

    story = [Paragraph("Migration Discovery Executive Report", title)]
    source_name = _safe_text(collector.get("display_name", "Collector"))
    tenant_id = _safe_text(collector.get("tenant_id", "Not available"))
    story.append(
        Paragraph(
            f"<b>Source:</b> {source_name} &nbsp;&nbsp; <b>Tenant:</b> {tenant_id}<br/>"
            f"<b>Observed period:</b> {_safe_text(reporting_window)}<br/>"
            f"<b>Generated:</b> {created_at:%Y-%m-%d %H:%M UTC}",
            body,
        )
    )

    scorecards = [
        ("Discovered VMs", summary["active_inventory_vm_count"]),
        ("Eligible powered-on VMs", summary["eligible_vm_count"]),
        ("Migration waves", summary["wave_count"]),
        ("Waves with VM-to-VM connections", summary["waves_with_observed_connections"]),
        ("Waves without observed connections", summary["waves_without_observed_connections"]),
        ("VMs without observations", summary["vms_without_observed_connections"]),
    ]
    scorecard_rows = [
        [
            Paragraph(f"<b>{label}</b><br/>{value:,}", small)
            for label, value in scorecards[index : index + 3]
        ]
        for index in range(0, len(scorecards), 3)
    ]
    scorecard_table = Table(scorecard_rows, colWidths=[5.65 * cm] * 3, hAlign="LEFT")
    scorecard_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bfdbfe")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bfdbfe")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend([Spacer(1, 10), scorecard_table, Paragraph("Migration-wave detail", heading)])

    if not waves:
        story.append(
            Paragraph("No eligible powered-on VMs are available in this reporting window.", body)
        )
    for wave in waves:
        names = [str(name) for name in wave.get("server_names", [])]
        classification = (
            "Observed VM-to-VM TCP connections"
            if len(names) > 1
            else "No observed internal TCP connection"
        )
        story.append(
            Paragraph(
                f"<b>Wave {int(wave.get('wave', 0))}</b> &mdash; {len(names):,} VM(s) "
                f"&mdash; {_safe_text(classification)}",
                body,
            )
        )
        for start in range(0, len(names), _VM_NAMES_PER_LINE):
            vm_names = ", ".join(
                _safe_text(name) for name in names[start : start + _VM_NAMES_PER_LINE]
            )
            label = "VMs" if start == 0 else "VMs (continued)"
            story.append(Paragraph(f"<b>{label}:</b> {vm_names}", small))
        story.append(Spacer(1, 6))

    excluded = summary["active_inventory_vm_count"] - summary["eligible_vm_count"]
    story.extend(
        [
            Paragraph("Planning note", heading),
            Paragraph(
                "Migration-wave assignment uses internal TCP observations between active, "
                "powered-on inventory VMs. UDP is reporting-only and does not affect wave "
                "membership. "
                f"{excluded:,} discovered VM(s) are excluded from wave eligibility because they "
                "are powered off.",
                body,
            ),
        ]
    )
    document.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return output.getvalue()
