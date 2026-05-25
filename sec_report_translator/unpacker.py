from __future__ import annotations

import base64
import binascii
import io
import json
import re
import shutil
import uu
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UnpackError(Exception):
    pass


@dataclass(frozen=True)
class SubmissionDocument:
    sequence: int | None
    doc_type: str
    filename: str
    description: str
    text: str
    content_kind: str


HEADER_FIELDS = {
    "ACCESSION NUMBER": "accession_number",
    "CONFORMED SUBMISSION TYPE": "submission_type",
    "PUBLIC DOCUMENT COUNT": "public_document_count",
    "CONFORMED PERIOD OF REPORT": "period_of_report",
    "FILED AS OF DATE": "filed_as_of_date",
    "COMPANY CONFORMED NAME": "company_name",
    "CENTRAL INDEX KEY": "cik",
    "STANDARD INDUSTRIAL CLASSIFICATION": "sic",
    "FISCAL YEAR END": "fiscal_year_end",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
HTML_EXTENSIONS = {".htm", ".html", ".xhtml"}
XML_EXTENSIONS = {".xml"}
XBRL_EXTENSIONS = {".xsd"}
TEXT_EXTENSIONS = {".txt"}
BINARY_EXTENSIONS = {".zip", ".xlsx", ".xls", ".pdf", ".doc", ".docx"}


def parse_submission(text: str) -> tuple[dict[str, str], list[SubmissionDocument]]:
    header = parse_header(text)
    documents = [parse_document(block) for block in find_document_blocks(text)]
    return header, documents


def parse_header(text: str) -> dict[str, str]:
    match = re.search(r"<SEC-HEADER[^>]*>(.*?)</SEC-HEADER>", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return {}

    header_text = match.group(1)
    result: dict[str, str] = {}
    for line in header_text.splitlines():
        field_match = re.match(r"\s*([^:]+):\s*(.*?)\s*$", line)
        if not field_match:
            continue
        raw_key = field_match.group(1).strip().upper()
        value = field_match.group(2).strip()
        key = HEADER_FIELDS.get(raw_key)
        if key and key not in result:
            result[key] = value
    return result


def find_document_blocks(text: str) -> list[str]:
    return re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", text, re.IGNORECASE | re.DOTALL)


def parse_document(block: str) -> SubmissionDocument:
    filename = extract_document_field(block, "FILENAME")
    if not filename:
        raise UnpackError("Encountered DOCUMENT without FILENAME.")

    sequence_text = extract_document_field(block, "SEQUENCE")
    try:
        sequence = int(sequence_text) if sequence_text else None
    except ValueError:
        sequence = None

    doc_type = extract_document_field(block, "TYPE")
    description = extract_document_field(block, "DESCRIPTION")
    body = extract_text_body(block)
    return SubmissionDocument(
        sequence=sequence,
        doc_type=doc_type,
        filename=filename,
        description=description,
        text=body,
        content_kind=classify_content(filename, doc_type),
    )


def extract_document_field(block: str, field_name: str) -> str:
    pattern = rf"^\s*<{re.escape(field_name)}>\s*(.*?)\s*$"
    match = re.search(pattern, block, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_text_body(block: str) -> str:
    match = re.search(r"<TEXT>\s*(.*?)\s*</TEXT>", block, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip("\r\n") if match else ""


def classify_content(filename: str, doc_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    doc_type_upper = doc_type.upper()
    if suffix in HTML_EXTENSIONS:
        return "html"
    if suffix in IMAGE_EXTENSIONS or doc_type_upper == "GRAPHIC":
        return "image"
    if suffix in XBRL_EXTENSIONS or doc_type_upper.startswith("EX-101"):
        return "xbrl"
    if suffix in XML_EXTENSIONS:
        return "xml"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in BINARY_EXTENSIONS:
        return "binary"
    return "unknown"


def unpack_submission(source_path: Path, output_dir: Path, overwrite: bool = False) -> Path:
    if not source_path.exists():
        raise UnpackError(f"Input file does not exist: {source_path}")

    source_text = source_path.read_text(encoding="utf-8", errors="replace")
    filing, documents = parse_submission(source_text)
    if not documents:
        raise UnpackError("No DOCUMENT blocks found in submission.")

    output_dir.mkdir(parents=True, exist_ok=True)
    planned_paths = [output_dir / source_path.name, output_dir / "manifest.json"]
    planned_paths.extend(output_dir / doc.filename for doc in documents)
    for path in planned_paths:
        if path.exists() and not overwrite:
            raise UnpackError(f"Output file already exists: {path}. Add --overwrite to replace it.")

    shutil.copyfile(source_path, output_dir / source_path.name)
    manifest_documents: list[dict[str, Any]] = []

    for doc in documents:
        written_path = output_dir / doc.filename
        decode_status, size = write_document(written_path, doc)
        manifest_documents.append(
            {
                "sequence": doc.sequence,
                "type": doc.doc_type,
                "filename": doc.filename,
                "description": doc.description,
                "content_kind": doc.content_kind,
                "written_path": doc.filename,
                "size": size,
                "decode_status": decode_status,
            }
        )

    manifest = {
        "source_file": source_path.name,
        "filing": filing,
        "documents": manifest_documents,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_dir


def write_document(path: Path, doc: SubmissionDocument) -> tuple[str, int]:
    uu_decoded = decode_possible_uuencode(doc.text)
    if uu_decoded is not None:
        path.write_bytes(uu_decoded)
        return "uuencode", len(uu_decoded)

    if doc.content_kind in {"image", "binary"}:
        payload, status = decode_possible_base64(doc.text)
        path.write_bytes(payload)
        return status, len(payload)

    path.write_text(doc.text, encoding="utf-8")
    return "text", len(doc.text)


def decode_possible_base64(text: str) -> tuple[bytes, str]:
    compact = re.sub(r"\s+", "", text)
    try:
        decoded = base64.b64decode(compact, validate=True)
    except Exception:
        return text.encode("utf-8", errors="replace"), "raw"
    return decoded, "base64"


def decode_possible_uuencode(text: str) -> bytes | None:
    lines = [line.rstrip("\r\n") for line in text.splitlines() if line.strip()]
    begin_index = None
    for index, line in enumerate(lines):
        if re.match(r"^begin\s+\d{3,4}\s+\S+", line):
            begin_index = index
            break
    if begin_index is None:
        return None

    raw = ("\n".join(lines[begin_index:]) + "\n").encode("ascii", errors="strict")
    output = io.BytesIO()
    try:
        uu.decode(io.BytesIO(raw), output, quiet=True)
    except Exception:
        return None
    return output.getvalue()
