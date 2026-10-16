"""Version-specific migration-plan exports for planners and approvers."""
# ruff: noqa: E501

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from re import sub
from xml.sax.saxutils import escape

import xlsxwriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _safe_text(value: object, limit: int = 500) -> str:
    return escape(" ".join(str(value or "").split())[:limit], {"'": "&apos;"})


def _safe_cell(value: object) -> str:
    text = " ".join(str(value or "").split())[:32_767]
    return f"'{text}" if text[:1] in {"=", "+", "-", "@"} else text


def plan_report_filename(display_name: object, plan: Mapping[str, object], extension: str) -> str:
    source = sub(r"[^A-Za-z0-9_-]+", "-", str(display_name)).strip("-") or "collector"
    status = sub(r"[^A-Za-z0-9_-]+", "-", str(plan.get("status", "plan"))).strip("-")
    return f"migration-plan-v{int(plan['version'])}-{status}_{source[:64]}.{extension}"


def _plan_status(plan: Mapping[str, object]) -> str:
    return f"Migration Plan V{int(plan['version'])} — {str(plan['status']).title()}"


def _assignments_by_wave(assignments: Sequence[Mapping[str, object]]) -> dict[object, list[str]]:
    result: dict[object, list[str]] = defaultdict(list)
    for item in assignments:
        wave = item.get("wave_number") if item.get("disposition") == "included" else "Excluded"
        result[wave].append(str(item.get("vm_name", "Unknown VM")))
    return result


def build_migration_plan_pdf(
    collector: Mapping[str, object],
    plan: Mapping[str, object],
    readiness: Sequence[Mapping[str, object]],
    dependencies: Sequence[Mapping[str, object]],
    drift: Mapping[str, object] | None,
    generated_at: datetime | None = None,
) -> bytes:
    """Create a readable, selected-version migration-plan PDF package."""

    created_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    assignments = list(plan["assignments"])
    included = [item for item in assignments if item.get("disposition") == "included"]
    excluded = [item for item in assignments if item.get("disposition") == "excluded"]
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, rightMargin=1.5 * cm, leftMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, title="Migration Plan Package",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("PlanTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20)
    heading = ParagraphStyle("PlanHeading", parent=styles["Heading2"], textColor=colors.HexColor("#1e3a5f"))
    body = ParagraphStyle("PlanBody", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("PlanSmall", parent=body, fontSize=8, leading=10)
    story = [Paragraph("Migration Plan Package", title), Spacer(1, 6)]
    story.append(Paragraph(f"<b>{_safe_text(_plan_status(plan))}</b>", heading))
    story.append(Paragraph(
        f"<b>Source:</b> {_safe_text(collector.get('display_name', 'Collector'))}<br/>"
        f"<b>Tenant:</b> {_safe_text(collector.get('tenant_id', 'Not available'))}<br/>"
        f"<b>Planner:</b> {_safe_text(plan.get('planner_name'))}<br/>"
        f"<b>Approved:</b> {_safe_text(plan.get('approved_at') or 'Not approved')}<br/>"
        f"<b>Generated:</b> {created_at:%Y-%m-%d %H:%M UTC}", body))
    if plan.get("note"):
        story.append(Paragraph(f"<b>Plan note:</b> {_safe_text(plan['note'])}", body))
    counts = [[Paragraph(f"<b>Included VMs</b><br/>{len(included):,}", small),
               Paragraph(f"<b>Planned waves</b><br/>{len({x.get('wave_number') for x in included}):,}", small),
               Paragraph(f"<b>Excluded VMs</b><br/>{len(excluded):,}", small)]]
    table = Table(counts, colWidths=[5.6 * cm] * 3)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
                               ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#bfdbfe")),
                               ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor("#bfdbfe")),
                               ("PADDING", (0, 0), (-1, -1), 8)]))
    story.extend([Spacer(1, 10), table, Paragraph("Planned wave membership", heading)])
    for wave, names in sorted(_assignments_by_wave(assignments).items(), key=lambda item: str(item[0])):
        story.append(Paragraph(f"<b>{'Wave ' + str(wave) if wave != 'Excluded' else 'Excluded'}</b> — {len(names)} VM(s)", body))
        story.append(Paragraph(_safe_text(", ".join(names)), small))
    story.append(Paragraph("Confidence and risk criteria", heading))
    story.append(Paragraph(
        "Confidence is <b>High</b> when observation coverage is at least 80%, inventory is no more than 7 days old, "
        "the collector checked in within 180 seconds, and observations exist. It is <b>Low</b> when coverage is below 40%, "
        "inventory is over 30 days old, or no observations exist; otherwise it is Medium. "
        "Risk is <b>High</b> for high-impact dependencies, <b>Medium</b> for action-required or investigate items, and <b>Low</b> when none remain.", body))
    if readiness:
        story.append(Paragraph("Current wave readiness", heading))
        rows = [[Paragraph("Wave", small), Paragraph("Confidence", small), Paragraph("Risk", small), Paragraph("Reason", small)]]
        for item in readiness:
            rows.append([Paragraph(_safe_text(f"Wave {item['wave']}"), small), Paragraph(_safe_text(item['confidence']), small),
                         Paragraph(_safe_text(item['risk']), small), Paragraph(_safe_text((item.get('risk_reasons') or item.get('confidence_reasons') or [''])[0]), small)])
        ready_table = Table(rows, colWidths=[2 * cm, 3 * cm, 2 * cm, 10 * cm], repeatRows=1)
        ready_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
                                         ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#cbd5e1")), ("PADDING", (0, 0), (-1, -1), 5)]))
        story.append(ready_table)
    if dependencies:
        story.append(Paragraph("Reviewed dependency evidence", heading))
        rows = [[Paragraph("Source", small), Paragraph("Destination", small), Paragraph("Service", small), Paragraph("Decision", small)]]
        for item in dependencies:
            rows.append([Paragraph(_safe_text(item.get("source_vm")), small), Paragraph(_safe_text(item.get("destination")), small),
                         Paragraph(_safe_text(item.get("port_service")), small), Paragraph(_safe_text(item.get("planner_decision") or "Not reviewed"), small)])
        dep_table = Table(rows, colWidths=[4.2 * cm, 4.2 * cm, 3.2 * cm, 5.4 * cm], repeatRows=1)
        dep_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
                                       ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#cbd5e1")), ("PADDING", (0, 0), (-1, -1), 4)]))
        story.append(dep_table)
    if drift is not None:
        items = [item for item in drift.get("items", []) if isinstance(item, Mapping)]
        if items:
            story.append(PageBreak())
            story.append(Paragraph("Plan drift detail", heading))
            story.append(
                Paragraph(
                    f"{len(items)} drift item(s) require review before cutover.", body
                )
            )
            drift_rows = [
                [
                    Paragraph("Severity", small),
                    Paragraph("VM", small),
                    Paragraph("Reason", small),
                ]
            ]
            for item in items:
                drift_rows.append(
                    [
                        Paragraph(_safe_text(item.get("severity")), small),
                        Paragraph(_safe_text(item.get("vm_name")), small),
                        Paragraph(_safe_text(item.get("reason")), small),
                    ]
                )
            drift_table = Table(drift_rows, colWidths=[2.5 * cm, 5 * cm, 11.3 * cm], repeatRows=1)
            drift_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fee2e2")),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#fecaca")),
                        ("PADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(drift_table)
        else:
            story.append(Paragraph("Current plan drift", heading))
            story.append(Paragraph("No drift was detected.", body))
    story.append(Spacer(1, 8))
    story.append(Paragraph("This package records the selected plan version. Draft and superseded versions are not approved for execution.", small))
    document.build(story)
    return output.getvalue()


def build_migration_plan_excel(
    collector: Mapping[str, object],
    plan: Mapping[str, object],
    readiness: Sequence[Mapping[str, object]],
    dependencies: Sequence[Mapping[str, object]],
    drift: Mapping[str, object] | None,
    connections: Sequence[Mapping[str, object]],
) -> bytes:
    """Create a sortable workbook for the selected plan version."""

    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False})
    try:
        title = workbook.add_format({"bold": True, "font_size": 14, "font_color": "#1e3a5f"})
        header = workbook.add_format({"bold": True, "bg_color": "#dbeafe", "border": 1})
        wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
        overview = workbook.add_worksheet("Plan Overview")
        overview.set_column("A:A", 24)
        overview.set_column("B:B", 80)
        overview.write(0, 0, "Migration Plan Package", title)
        for row, (key, value) in enumerate((("Plan", _plan_status(plan)), ("Source", collector.get("display_name")), ("Tenant", collector.get("tenant_id")), ("Planner", plan.get("planner_name")), ("Approved", plan.get("approved_at") or "Not approved"), ("Plan note", plan.get("note") or "")), start=2):
            overview.write(row, 0, key, header)
            overview.write(row, 1, _safe_cell(value), wrap)
        overview.write(10, 0, "Confidence and risk criteria", header)
        overview.write(10, 1, _safe_cell("High confidence: >=80% coverage, inventory <=7 days old, collector checked in <=180 seconds, and observations exist. Low: <40% coverage, inventory >30 days old, or no observations. High risk: high-impact dependency. Medium: action-required or investigate dependency. Low: none unresolved."), wrap)
        sheets = (("VM Assignments", plan["assignments"], ("VM", "Disposition", "Wave", "Recommended wave", "Planner note"), ("vm_name", "disposition", "wave_number", "recommended_wave_number", "note")),
                  ("Readiness", readiness, ("Wave", "Confidence", "Risk", "Review dependencies", "Reason"), ("wave", "confidence", "risk", "review_dependency_count", "risk_reasons")),
                  ("Dependencies", dependencies, ("Category", "Source VM", "Destination", "IP", "Service", "Decision", "Planner note"), ("category", "source_vm", "destination", "destination_ip", "port_service", "planner_decision", "planner_note")),
                  ("Plan Drift", list(drift.get("items", [])) if drift else [], ("Severity", "VM", "Reason"), ("severity", "vm_name", "reason")),
                  ("TCP-UDP Connections", connections, ("Source", "Source IP(s)", "Destination", "Destination IP", "Protocol", "Destination port(s)"), ("source", "source_ips", "destination", "destination_ip", "protocol", "destination_ports")))
        for name, rows, headers, keys in sheets:
            sheet = workbook.add_worksheet(name)
            sheet.set_column("A:Z", 22)
            sheet.freeze_panes(1, 0)
            for column, label in enumerate(headers):
                sheet.write(0, column, label, header)
            for row_number, item in enumerate(rows, start=1):
                for column, key in enumerate(keys):
                    value = item.get(key, "")
                    if isinstance(value, list):
                        value = "; ".join(str(entry) for entry in value)
                    sheet.write(row_number, column, _safe_cell(value), wrap)
            sheet.autofilter(0, 0, max(0, len(rows)), len(headers) - 1)
    finally:
        workbook.close()
    return output.getvalue()
