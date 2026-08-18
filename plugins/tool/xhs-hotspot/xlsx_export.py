"""使用 Python 标准库生成可下载的小红书热点 Excel 简报。"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets>
  <sheet name="热点简报" sheetId="1" r:id="rId1"/>
  <sheet name="数据来源" sheetId="2" r:id="rId2"/>
 </sheets>
</workbook>"""

_WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
 <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
 <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
 <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

# 样式编号依次对应：普通、标题、说明、区块标题、表头、居中、链接、提示语。
_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <fonts count="4">
  <font><sz val="10"/><color rgb="FF262626"/><name val="Arial Unicode MS"/><family val="2"/></font>
  <font><b/><sz val="18"/><color rgb="FFFFFFFF"/><name val="Arial Unicode MS"/><family val="2"/></font>
  <font><sz val="10"/><color rgb="FF666666"/><name val="Arial Unicode MS"/><family val="2"/></font>
  <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Arial Unicode MS"/><family val="2"/></font>
 </fonts>
 <fills count="5">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FFC84832"/><bgColor indexed="64"/></patternFill></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FFFBE9E7"/><bgColor indexed="64"/></patternFill></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF3F3F46"/><bgColor indexed="64"/></patternFill></fill>
 </fills>
 <borders count="2">
  <border><left/><right/><top/><bottom/><diagonal/></border>
  <border><left style="thin"><color rgb="FFE7E7E7"/></left><right style="thin"><color rgb="FFE7E7E7"/></right><top style="thin"><color rgb="FFE7E7E7"/></top><bottom style="thin"><color rgb="FFE7E7E7"/></bottom><diagonal/></border>
 </borders>
 <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
 <cellXfs count="8">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"><alignment vertical="center"/></xf>
  <xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"><alignment vertical="center" wrapText="1"/></xf>
  <xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"><alignment vertical="center"/></xf>
  <xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFill="1" applyFont="1" applyBorder="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
  <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
  <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment vertical="center" wrapText="1"/></xf>
  <xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFill="1" applyFont="1"><alignment vertical="center" wrapText="1"/></xf>
 </cellXfs>
 <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _column_name(index: int) -> str:
    """将从零开始的列序号转换为 Excel 列名。"""
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xml_text(value: Any) -> str:
    """清理 Excel XML 不允许的控制字符，并转义文本。"""
    text = str(value if value is not None else "")
    clean = "".join(
        character
        for character in text
        if character in "\t\n\r" or ord(character) >= 32
    )
    return escape(clean)


def _cell(
    column: int,
    row: int,
    value: Any,
    *,
    style: int = 0,
    numeric: bool = False,
) -> str:
    """创建一个使用内联字符串的单元格 XML，避免维护共享字符串表。"""
    reference = f"{_column_name(column)}{row}"
    style_attribute = f' s="{style}"' if style else ""
    if numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style_attribute}><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"{style_attribute}>'
        f'<is><t xml:space="preserve">{_xml_text(value)}</t></is></c>'
    )


def _worksheet_xml(
    *,
    rows: list[list[tuple[Any, int, bool]]],
    column_widths: list[float],
    merges: list[str],
    frozen_row: int,
    auto_filter: str | None = None,
    hyperlinks: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """生成工作表 XML；链接关系单独返回，供 xlsx 压缩包写入。"""
    row_xml: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(
            _cell(column, row_number, value, style=style, numeric=numeric)
            for column, (value, style, numeric) in enumerate(row)
        )
        height = " ht=\"24\" customHeight=\"1\"" if row_number >= 7 else ""
        row_xml.append(f'<row r="{row_number}"{height}>{cells}</row>')

    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(column_widths, start=1)
    )
    merge_xml = "".join(f'<mergeCell ref="{reference}"/>' for reference in merges)
    pane_xml = (
        f'<pane ySplit="{frozen_row}" topLeftCell="A{frozen_row + 1}" '
        'activePane="bottomLeft" state="frozen"/>'
        if frozen_row
        else ""
    )

    link_xml = ""
    rels_xml: str | None = None
    if hyperlinks:
        relationship_xml: list[str] = []
        hyperlink_xml: list[str] = []
        for index, (reference, target) in enumerate(hyperlinks.items(), start=1):
            relation_id = f"rId{index}"
            hyperlink_xml.append(f'<hyperlink ref="{reference}" r:id="{relation_id}"/>')
            relationship_xml.append(
                '<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/hyperlink" Target="%s" '
                'TargetMode="External"/>' % (relation_id, _xml_text(target))
            )
        link_xml = "<hyperlinks>" + "".join(hyperlink_xml) + "</hyperlinks>"
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationship_xml)
            + "</Relationships>"
        )

    auto_filter_xml = f'<autoFilter ref="{auto_filter}"/>' if auto_filter else ""
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        f'<sheetViews><sheetView workbookViewId="0" showGridLines="0">{pane_xml}</sheetView></sheetViews>'
        f'<cols>{columns}</cols><sheetData>{"".join(row_xml)}</sheetData>'
        f'{auto_filter_xml}<mergeCells count="{len(merges)}">{merge_xml}</mergeCells>{link_xml}'
        '<pageMargins left="0.25" right="0.25" top="0.45" bottom="0.45" '
        'header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/>'
        '</worksheet>'
    )
    return worksheet, rels_xml


def render_personalized_report_xlsx(report: dict[str, Any]) -> bytes:
    """将已经校验过的真实热点报告渲染成两页、可追溯的 Excel 文件。"""
    requested_days = int(report.get("requested_days") or 0)
    available_days = int(report.get("available_days") or 0)
    coverage = f"最近请求 {requested_days} 天，已获得 {available_days} 天真实快照"
    if not report.get("coverage_complete"):
        coverage += "；历史数据尚未积累完整。"

    topic_names = [
        str(topic.get("topic"))
        for topic in report.get("preference_profile", {}).get("topics", [])
        if isinstance(topic, dict) and topic.get("topic")
    ]
    report_items = report.get("priority_items") or report.get("latest_items") or []
    report_items = [item for item in report_items if isinstance(item, dict)][:10]

    main_rows: list[list[tuple[Any, int, bool]]] = [
        [("小红书热点简报", 1, False)] + [("", 1, False)] * 6,
        [(f"生成时间：{report.get('generated_at', '')}", 2, False)] + [("", 2, False)] * 6,
        [(coverage, 7, False)] + [("", 7, False)] * 6,
        [(f"关注方向：{'、'.join(topic_names) or '综合热点'}", 3, False)] + [("", 3, False)] * 6,
        [("", 0, False)] * 7,
        [("本次热点", 3, False)] + [("", 3, False)] * 6,
        [
            ("优先顺序", 4, False),
            ("热点标题", 4, False),
            ("原榜单排名", 4, False),
            ("热度", 4, False),
            ("趋势", 4, False),
            ("匹配关注方向", 4, False),
            ("来源", 4, False),
        ],
    ]
    main_links: dict[str, str] = {}
    for index, item in enumerate(report_items, start=1):
        row_number = len(main_rows) + 1
        matched_topics = "、".join(
            str(topic) for topic in item.get("matched_topics", []) if topic
        ) or "综合呈现"
        main_rows.append(
            [
                (index, 5, True),
                (item.get("title") or "未命名热点", 6, False),
                (item.get("source_rank") or "-", 5, False),
                (item.get("hot_text") or item.get("hot_value") or "-", 5, False),
                (item.get("trend_label") or "-", 5, False),
                (matched_topics, 6, False),
                ("查看来源", 5, False),
            ]
        )
        source_url = item.get("source_url")
        if isinstance(source_url, str) and source_url.startswith(("https://", "http://")):
            main_links[f"G{row_number}"] = source_url
    if not report_items:
        main_rows.append(
            [("暂无可展示热点，请先完成一次真实采集。", 7, False)]
            + [("", 7, False)] * 6
        )

    source_rows: list[list[tuple[Any, int, bool]]] = [
        [("小红书热点数据来源", 1, False)] + [("", 1, False)] * 4,
        [("每一行对应一次已保存的真实快照，可用于人工核验。", 7, False)]
        + [("", 7, False)] * 4,
        [("", 0, False)] * 5,
        [
            ("采集时间", 4, False),
            ("来源更新时间", 4, False),
            ("数据来源", 4, False),
            ("快照编号", 4, False),
            ("快照文件", 4, False),
        ],
    ]
    source_links: dict[str, str] = {}
    for source in report.get("sources", []):
        if not isinstance(source, dict):
            continue
        row_number = len(source_rows) + 1
        source_rows.append(
            [
                (source.get("captured_at") or "-", 6, False),
                (source.get("source_updated_at") or "-", 6, False),
                ("打开数据来源", 5, False),
                (source.get("capture_id") or "-", 6, False),
                (source.get("snapshot_path") or "-", 6, False),
            ]
        )
        source_url = source.get("source_url")
        if isinstance(source_url, str) and source_url.startswith(("https://", "http://")):
            source_links[f"C{row_number}"] = source_url

    main_sheet, main_rels = _worksheet_xml(
        rows=main_rows,
        column_widths=[12, 40, 13, 13, 10, 20, 13],
        merges=["A1:G1", "A2:G2", "A3:G3", "A4:G4", "A6:G6"],
        frozen_row=7,
        auto_filter=f"A7:G{max(len(main_rows), 7)}",
        hyperlinks=main_links,
    )
    source_sheet, source_rels = _worksheet_xml(
        rows=source_rows,
        column_widths=[28, 28, 18, 28, 52],
        merges=["A1:E1", "A2:E2"],
        frozen_row=4,
        auto_filter=f"A4:E{max(len(source_rows), 4)}",
        hyperlinks=source_links,
    )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", _ROOT_RELS_XML)
        archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
        archive.writestr("xl/styles.xml", _STYLES_XML)
        archive.writestr("xl/worksheets/sheet1.xml", main_sheet)
        archive.writestr("xl/worksheets/sheet2.xml", source_sheet)
        if main_rels:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", main_rels)
        if source_rels:
            archive.writestr("xl/worksheets/_rels/sheet2.xml.rels", source_rels)
    return output.getvalue()
