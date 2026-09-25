"""In-memory detailed Excel report for migration engineers and planners."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from re import sub

import xlsxwriter

_MAX_CELL_LENGTH = 32_767


def _safe_cell(value: object) -> str:
    """Prevent formula interpretation and constrain untrusted spreadsheet cell text."""

    text = " ".join(str(value).split())[:_MAX_CELL_LENGTH]
    return f"'{text}" if text[:1] in {"=", "+", "-", "@"} else text


def detailed_report_filename(display_name: object, generated_at: datetime) -> str:
    """Return a browser-safe filename with no collector secrets or identifiers."""

    source = sub(r"[^A-Za-z0-9_-]+", "-", str(display_name)).strip("-") or "collector"
    return (
        "migration-discovery-detailed-report_"
        f"{source[:64]}_{generated_at.astimezone(UTC):%Y%m%d-%H%M%S}.xlsx"
    )


def _write_scope(worksheet, title: str, collector: Mapping[str, object], window: str) -> None:
    worksheet.write(0, 0, title)
    worksheet.write(1, 0, "Source")
    worksheet.write(1, 1, _safe_cell(collector.get("display_name", "Collector")))
    worksheet.write(1, 2, "Tenant")
    worksheet.write(1, 3, _safe_cell(collector.get("tenant_id", "Not available")))
    worksheet.write(2, 0, "Observed period")
    worksheet.write(2, 1, _safe_cell(window))


def build_detailed_report(
    collector: Mapping[str, object],
    waves: Sequence[Mapping[str, object]],
    connections: Sequence[Mapping[str, object]],
    reporting_window: str,
) -> bytes:
    """Create a two-sheet workbook without formulas, macros, or external links."""

    output = BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
        },
    )
    try:
        title_format = workbook.add_format({"bold": True, "font_size": 14, "font_color": "#1e3a5f"})
        header_format = workbook.add_format(
            {"bold": True, "bg_color": "#dbeafe", "border": 1, "valign": "vcenter"}
        )
        wrap_format = workbook.add_format({"text_wrap": True, "valign": "top"})
        waves_sheet = workbook.add_worksheet("Migration Waves")
        _write_scope(waves_sheet, "Migration Waves", collector, reporting_window)
        waves_sheet.set_row(0, 24)
        waves_sheet.set_column("A:A", 12)
        waves_sheet.set_column("B:B", 34)
        waves_sheet.set_column("C:C", 12)
        waves_sheet.set_column("D:D", 90)
        waves_sheet.write(0, 0, "Migration Waves", title_format)
        wave_headers = ("Wave", "Classification", "VM count", "VMs")
        for column, header in enumerate(wave_headers):
            waves_sheet.write(4, column, header, header_format)
        for row_number, wave in enumerate(waves, start=5):
            names = [str(name) for name in wave.get("server_names", [])]
            classification = (
                "Observed VM-to-VM TCP connections"
                if len(names) > 1
                else "No observed internal TCP connection"
            )
            waves_sheet.write(row_number, 0, int(wave.get("wave", 0)))
            waves_sheet.write(row_number, 1, classification, wrap_format)
            waves_sheet.write(row_number, 2, len(names))
            waves_sheet.write(row_number, 3, _safe_cell(", ".join(names)), wrap_format)
        waves_sheet.freeze_panes(5, 0)
        waves_sheet.autofilter(4, 0, max(4, 4 + len(waves)), len(wave_headers) - 1)

        connections_sheet = workbook.add_worksheet("TCP-UDP Connections")
        _write_scope(
            connections_sheet,
            "Deduplicated TCP/UDP Connection Details",
            collector,
            reporting_window,
        )
        connections_sheet.set_row(0, 24)
        connections_sheet.set_column("A:A", 26)
        connections_sheet.set_column("B:B", 24)
        connections_sheet.set_column("C:C", 28)
        connections_sheet.set_column("D:D", 20)
        connections_sheet.set_column("E:E", 12)
        connections_sheet.set_column("F:F", 24)
        connections_sheet.write(0, 0, "Deduplicated TCP/UDP Connection Details", title_format)
        connection_headers = (
            "Source",
            "Source IP(s)",
            "Destination",
            "Destination IP",
            "Protocol",
            "Destination port(s)",
        )
        for column, header in enumerate(connection_headers):
            connections_sheet.write(4, column, header, header_format)
        for row_number, connection in enumerate(connections, start=5):
            for column, key in enumerate(
                (
                    "source",
                    "source_ips",
                    "destination",
                    "destination_ip",
                    "protocol",
                    "destination_ports",
                )
            ):
                connections_sheet.write(
                    row_number, column, _safe_cell(connection.get(key, "")), wrap_format
                )
        connections_sheet.freeze_panes(5, 0)
        connections_sheet.autofilter(
            4, 0, max(4, 4 + len(connections)), len(connection_headers) - 1
        )
    finally:
        workbook.close()
    return output.getvalue()
