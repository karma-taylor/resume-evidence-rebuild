#!/usr/bin/env python3
"""Validate skill structure and enforce quantified one-page resume QA contracts."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import yaml


REQUIRED = (
    "SKILL.md", "agents/openai.yaml", "references/claim-ledger.md",
    "references/evidence-policy.md", "references/one-page-layout-qa.md",
)
MIN_MARGIN_CM, MAX_MARGIN_CM, MIN_BODY_FONT_PT = 1.27, 2.54, 10.0
MIN_CHINESE_BULLET_CHARS, MAX_CHINESE_BULLET_CHARS = 60, 70
A4_WIDTH_PT, A4_HEIGHT_PT, PAGE_SIZE_TOLERANCE_PT = 595.28, 841.89, 1.5
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class ResumeQAError(Exception):
    """A deterministic, machine-readable delivery-blocking QA exception."""

    def __init__(self, code: str, detail: str) -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def hard_fail(code: str, detail: str) -> None:
    raise ResumeQAError(code, detail)


def require_optional_dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from PIL import Image, ImageStat
        from pypdf import PdfReader
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - depends on local runtime
        hard_fail("QA_DEPENDENCY_ERROR", f"install scripts/requirements.txt ({exc})")
    return Document, qn, Image, ImageStat, (PdfReader, pdfplumber)


def check_skill(skill_dir: Path) -> None:
    for relative in REQUIRED:
        if not (skill_dir / relative).is_file():
            hard_fail("SKILL_STRUCTURE_ERROR", f"missing required file: {relative}")
    skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---\n") or "name: resume-evidence-rebuild" not in skill:
        hard_fail("SKILL_STRUCTURE_ERROR", "SKILL.md must contain resume-evidence-rebuild frontmatter")
    if "[TODO" in skill:
        hard_fail("SKILL_STRUCTURE_ERROR", "SKILL.md contains unfinished placeholder text")


def check_file_signature(path: Path, signature: bytes, label: str) -> None:
    if not path.is_file() or path.read_bytes()[:len(signature)] != signature:
        hard_fail("ARTIFACT_FORMAT_ERROR", f"invalid {label}: {path}")


def cm_from_emu(value: int | None) -> float:
    return float(value or 0) / 360000.0  # 1 cm = 360000 EMU


def is_body_paragraph(paragraph: Any) -> bool:
    if not paragraph.text.strip():
        return False
    style = (getattr(paragraph.style, "name", "") or "").lower()
    return not any(token in style for token in ("title", "heading", "header", "footer"))


def resolved_font_size_pt(paragraph: Any, run: Any) -> float | None:
    for candidate in (run.font.size, getattr(paragraph.style.font, "size", None),
                      getattr(paragraph.part.document.styles["Normal"].font, "size", None)):
        if candidate is not None:
            return float(candidate.pt)
    return None


def paragraph_spacing_is_valid(paragraph: Any) -> bool:
    fmt = paragraph.paragraph_format
    if isinstance(fmt.line_spacing, (int, float)) and float(fmt.line_spacing) >= 1.5:
        return True
    return fmt.space_after is not None and 8.0 <= float(fmt.space_after.pt) <= 10.0


def iter_docx_image_blobs(document: Any) -> Iterable[bytes]:
    for relation in document.part.rels.values():
        if "image" in relation.reltype:
            yield relation.target_part.blob


def image_is_three_by_four_solid(blob: bytes, Image: Any, ImageStat: Any) -> bool:
    with Image.open(io.BytesIO(blob)).convert("RGB") as image:
        width, height = image.size
        if not width or not height or abs(width / height - 3 / 4) > 0.03:
            return False
        edge = max(1, min(width, height) // 12)
        boxes = ((0, 0, edge, edge), (width-edge, 0, width, edge),
                 (0, height-edge, edge, height), (width-edge, height-edge, width, height))
        means = []
        for box in boxes:
            stat = ImageStat.Stat(image.crop(box))
            if max(stat.var) > 225:  # Corner standard deviation must be <= 15.
                return False
            means.append(stat.mean)
        baseline = means[0]
        return all(max(abs(value - baseline[i]) for i, value in enumerate(mean)) <= 25 for mean in means[1:])


def check_docx_layout_and_photo(docx_path: Path, market: str) -> None:
    Document, qn, Image, ImageStat, _ = require_optional_dependencies()
    document = Document(str(docx_path))
    for section_number, section in enumerate(document.sections, 1):
        columns = section._sectPr.find(qn("w:cols"))
        if columns is not None and int(columns.get(qn("w:num"), "1")) != 1:
            hard_fail("MULTI_COLUMN_LAYOUT_ERROR", f"DOCX section {section_number} declares multiple text columns")
        for side, value in {
            "top": cm_from_emu(section.top_margin), "bottom": cm_from_emu(section.bottom_margin),
            "left": cm_from_emu(section.left_margin), "right": cm_from_emu(section.right_margin),
        }.items():
            if not MIN_MARGIN_CM <= value <= MAX_MARGIN_CM:
                code = "BOTTOM_WHITESPACE_EXCESS" if value > MAX_MARGIN_CM else "MARGIN_OUT_OF_RANGE_ERROR"
                hard_fail(code, f"DOCX section {section_number} {side} margin is {value:.2f} cm; expected 1.27-2.54 cm")
    for table_number, table in enumerate(document.tables, 1):
        if any(len(row.cells) > 1 for row in table.rows):
            hard_fail("MULTI_COLUMN_LAYOUT_ERROR", f"DOCX body table {table_number} has multiple columns")
    for paragraph_number, paragraph in enumerate(document.paragraphs, 1):
        if not is_body_paragraph(paragraph):
            continue
        for run in paragraph.runs:
            if run.text.strip():
                size = resolved_font_size_pt(paragraph, run)
                if size is None or size < MIN_BODY_FONT_PT:
                    hard_fail("FONT_TOO_SMALL_ERROR", f"DOCX paragraph {paragraph_number} uses {size or 0:.1f} pt; body minimum is 10 pt")
        if not paragraph_spacing_is_valid(paragraph):
            hard_fail("PARAGRAPH_SPACING_ERROR", f"DOCX paragraph {paragraph_number} must use 1.5x line spacing or 8-10 pt after-spacing")
    images = list(iter_docx_image_blobs(document))
    if market == "CN":
        if not images or not any(image_is_three_by_four_solid(blob, Image, ImageStat) for blob in images):
            hard_fail("COMPLIANCE_PHOTO_ERROR", "CN photo must exist, be 3:4, and have a solid background")
    elif images:
        hard_fail("COMPLIANCE_PHOTO_ERROR", f"{market} route prohibits photos")


def text_integrity_check(text: str, expected_identity: dict[str, Any] | None = None) -> None:
    """Check contacts against authorized private profile data when supplied."""
    if expected_identity is None:
        if not PHONE_PATTERN.search(text) or not EMAIL_PATTERN.search(text):
            hard_fail("CONTACT_MISSING_ERROR", "final artifact has no recognizable phone number and email")
        return
    for field in ("phone", "email", "portfolio_url"):
        value = str(expected_identity.get(field, "")).strip()
        if not value:
            hard_fail("CONTACT_MISSING_ERROR", f"private profile is missing immutable {field}")
        if value not in text:
            code = "LINK_TAMPERING_ERROR" if field == "portfolio_url" else "CONTACT_MISSING_ERROR"
            hard_fail(code, f"final artifact does not contain authorized immutable {field}")


def pdf_image_blobs(reader: Any) -> list[bytes]:
    blobs: list[bytes] = []
    for page in reader.pages:
        try:
            blobs.extend(image.data for image in page.images)
        except Exception:
            continue
    return blobs


def check_pdf_layout_and_integrity(pdf_path: Path, market: str, expected_identity: dict[str, Any] | None = None) -> None:
    _, _, _, _, (PdfReader, pdfplumber) = require_optional_dependencies()
    reader = PdfReader(str(pdf_path))
    if len(reader.pages) != 1:
        hard_fail("PAGE_COUNT_ERROR", f"a resume must contain exactly one A4 page; found {len(reader.pages)} pages")
    media_box = reader.pages[0].mediabox
    width, height = float(media_box.width), float(media_box.height)
    if abs(width - A4_WIDTH_PT) > PAGE_SIZE_TOLERANCE_PT or abs(height - A4_HEIGHT_PT) > PAGE_SIZE_TOLERANCE_PT:
        hard_fail("PAGE_SIZE_ERROR", f"expected A4 {A4_WIDTH_PT:.2f}x{A4_HEIGHT_PT:.2f} pt; found {width:.2f}x{height:.2f} pt")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not text.strip():
        hard_fail("TEXT_EXTRACTION_ERROR", "PDF has no extractable text")
    text_integrity_check(text, expected_identity)
    images = pdf_image_blobs(reader)
    if market == "CN" and not images:
        hard_fail("COMPLIANCE_PHOTO_ERROR", "final PDF has no embedded photo for CN route")
    if market != "CN" and images:
        hard_fail("COMPLIANCE_PHOTO_ERROR", f"final PDF contains a photo for {market} route")
    with pdfplumber.open(str(pdf_path)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            chars = [char for char in page.chars if char.get("text", "").strip()]
            if not chars:
                hard_fail("TEXT_EXTRACTION_ERROR", f"PDF page {number} has no measurable text")
            words = page.extract_words(x_tolerance=1, y_tolerance=2)
            # A true two-column body has independently aligned line starts on both
            # halves of the page. The header is excluded because a headshot may sit there.
            body_line_starts: dict[int, float] = {}
            for word in words:
                if word["top"] <= 150 or word["bottom"] >= page.height - 45:
                    continue
                key = round(word["top"])
                body_line_starts[key] = min(body_line_starts.get(key, word["x0"]), word["x0"])
            midpoint = page.width / 2
            left_lines = sum(start < midpoint - 20 for start in body_line_starts.values())
            right_lines = sum(start > midpoint + 20 for start in body_line_starts.values())
            if left_lines >= 4 and right_lines >= 4:
                hard_fail("MULTI_COLUMN_LAYOUT_ERROR", f"PDF page {number} has {left_lines} left and {right_lines} right body-column line starts")
            boundaries = {"left": min(char["x0"] for char in chars), "right": page.width-max(char["x1"] for char in chars),
                          "top": min(char["top"] for char in chars), "bottom": page.height-max(char["bottom"] for char in chars)}
            for side, value in boundaries.items():
                cm = value * 2.54 / 72.0
                if not MIN_MARGIN_CM <= cm <= MAX_MARGIN_CM:
                    code = "BOTTOM_WHITESPACE_EXCESS" if cm > MAX_MARGIN_CM else "MARGIN_OUT_OF_RANGE_ERROR"
                    hard_fail(code, f"PDF page {number} {side} content boundary is {cm:.2f} cm; expected 1.27-2.54 cm")
            for char in (char for char in chars if 110 < char["top"] < page.height - 55):
                if float(char["size"]) < MIN_BODY_FONT_PT:
                    hard_fail("FONT_TOO_SMALL_ERROR", f"PDF page {number} body text uses {float(char['size']):.1f} pt; body minimum is 10 pt")


def load_project_bullets(manifest_path: Path) -> list[tuple[str, int, str, list[str]]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        hard_fail("INSUFFICIENT_PROJECT_EVIDENCE", f"invalid project-node manifest: {exc}")
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, list) or not 3 <= len(projects) <= 4:
        hard_fail("INSUFFICIENT_PROJECT_EVIDENCE", "manifest must contain exactly 3-4 evidence-backed projects")
    bullets: list[tuple[str, int, str, list[str]]] = []
    for project_number, project in enumerate(projects, 1):
        name = project.get("name") if isinstance(project, dict) else None
        points = project.get("bullets") if isinstance(project, dict) else None
        if not isinstance(name, str) or not isinstance(points, list) or not 3 <= len(points) <= 4:
            hard_fail("INSUFFICIENT_PROJECT_EVIDENCE", f"project {project_number} must have a name and 3-4 bullets")
        for bullet_number, bullet in enumerate(points, 1):
            if not isinstance(bullet, dict) or not isinstance(bullet.get("text"), str):
                hard_fail("BULLET_LENGTH_ERROR", f"project {name!r} bullet {bullet_number} must be an object with text")
            bold_phrases = bullet.get("bold_phrases")
            if not isinstance(bold_phrases, list) or not 1 <= len(bold_phrases) <= 2 or not all(isinstance(item, str) and item for item in bold_phrases):
                hard_fail("BULLET_BOLD_MISSING_ERROR", f"project {name!r} bullet {bullet_number} must declare 1-2 non-empty bold_phrases")
            if any(phrase not in bullet["text"] for phrase in bold_phrases):
                hard_fail("BULLET_BOLD_MISSING_ERROR", f"project {name!r} bullet {bullet_number} declares a phrase absent from its text")
            bullets.append((name, bullet_number, bullet["text"], bold_phrases))
    return bullets


def check_bullet_lengths(manifest_path: Path) -> None:
    for project_name, bullet_number, bullet, _ in load_project_bullets(manifest_path):
        count = len(CJK_PATTERN.findall(bullet))
        if not MIN_CHINESE_BULLET_CHARS <= count <= MAX_CHINESE_BULLET_CHARS:
            hard_fail("BULLET_LENGTH_ERROR", f"project {project_name!r} bullet {bullet_number} has {count} Chinese characters; expected 60-70")


def check_docx_project_bold_emphasis(docx_path: Path, manifest_path: Path) -> None:
    """Verify declared important phrases are visibly bold in the editable source."""
    Document, _, _, _, _ = require_optional_dependencies()
    document = Document(str(docx_path))
    paragraphs = [(paragraph.text.replace("• ", ""), paragraph) for paragraph in document.paragraphs]
    for project_name, bullet_number, text, bold_phrases in load_project_bullets(manifest_path):
        matched = next((paragraph for paragraph_text, paragraph in paragraphs if text in paragraph_text), None)
        if matched is None:
            hard_fail("BULLET_BOLD_MISSING_ERROR", f"project {project_name!r} bullet {bullet_number} is not present in the DOCX")
        bold_text = "".join(run.text if run.bold else "\0" * len(run.text) for run in matched.runs)
        if not any(phrase in bold_text for phrase in bold_phrases):
            hard_fail("BULLET_BOLD_MISSING_ERROR", f"project {project_name!r} bullet {bullet_number} has no declared phrase in a bold DOCX run")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--docx", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--project-manifest", type=Path)
    parser.add_argument("--profile", type=Path, help="Private profile used only to check immutable contact values")
    parser.add_argument("--market", choices=("CN", "NA", "FOREIGN"), default="CN")
    args = parser.parse_args()
    try:
        check_skill(args.skill_dir)
        expected_identity = None
        if args.profile:
            profile = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
            if not isinstance(profile, dict) or not isinstance(profile.get("identity"), dict):
                hard_fail("ARTIFACT_ARGUMENT_ERROR", "profile must contain an identity mapping")
            expected_identity = profile["identity"]
        artifacts = (args.docx, args.pdf, args.project_manifest)
        if any(artifacts) and not all(artifacts):
            hard_fail("ARTIFACT_ARGUMENT_ERROR", "provide --docx, --pdf, and --project-manifest together, or none")
        if args.docx:
            check_file_signature(args.docx, b"PK", "DOCX")
            check_file_signature(args.pdf, b"%PDF", "PDF")
            check_docx_layout_and_photo(args.docx, args.market)
            check_pdf_layout_and_integrity(args.pdf, args.market, expected_identity)
            check_bullet_lengths(args.project_manifest)
            check_docx_project_bold_emphasis(args.docx, args.project_manifest)
    except ResumeQAError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("OK: skill and artifacts passed quantified QA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
