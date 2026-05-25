import json
import base64
import binascii

from PIL import Image

from sec_report_translator.cli import main


PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_BASE64)


def uuencode_bytes(filename: str, data: bytes) -> str:
    lines = [f"begin 644 {filename}"]
    for index in range(0, len(data), 45):
        lines.append(binascii.b2a_uu(data[index : index + 45]).decode("ascii").rstrip("\n"))
    lines.append("end")
    return "\n".join(lines)


def sample_submission() -> str:
    return f"""<SEC-DOCUMENT>0000000000-26-000001.txt : 20260524
<SEC-HEADER>0000000000-26-000001.hdr.sgml : 20260524
ACCESSION NUMBER:        0000000000-26-000001
CONFORMED SUBMISSION TYPE:  20-F
PUBLIC DOCUMENT COUNT:   2
CONFORMED PERIOD OF REPORT: 20251231
FILED AS OF DATE:        20260524

FILER:

    COMPANY DATA:
        COMPANY CONFORMED NAME:         Example Holdings Inc.
        CENTRAL INDEX KEY:              0000000000
        STANDARD INDUSTRIAL CLASSIFICATION: SERVICES-BUSINESS SERVICES, NEC [7389]
        FISCAL YEAR END:                1231
</SEC-HEADER>
<DOCUMENT>
<TYPE>20-F
<SEQUENCE>1
<FILENAME>example-20251231x20f.htm
<DESCRIPTION>20-F
<TEXT>
<html><body><img src="example-logo.png"><p>Revenue</p></body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>2
<FILENAME>example-logo.png
<DESCRIPTION>GRAPHIC
<TEXT>
{PNG_BASE64}
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""


def sample_submission_with_uuencoded_image() -> str:
    encoded = uuencode_bytes("example-logo.jpg", PNG_BYTES)
    return f"""<SEC-DOCUMENT>0000000000-26-000002.txt : 20260524
<SEC-HEADER>
ACCESSION NUMBER:        0000000000-26-000002
CONFORMED SUBMISSION TYPE:  20-F
</SEC-HEADER>
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>1
<FILENAME>example-logo.jpg
<DESCRIPTION>GRAPHIC
<TEXT>
{encoded}
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""


def sample_submission_with_uuencoded_zip() -> str:
    encoded = uuencode_bytes("archive.zip", b"PK\x03\x04fake zip payload")
    return f"""<SEC-DOCUMENT>0000000000-26-000003.txt : 20260524
<SEC-HEADER>
ACCESSION NUMBER:        0000000000-26-000003
</SEC-HEADER>
<DOCUMENT>
<TYPE>ZIP
<SEQUENCE>1
<FILENAME>archive.zip
<DESCRIPTION>ZIP
<TEXT>
{encoded}
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""


def sample_submission_with_lenient_uuencoded_image() -> str:
    encoded = uuencode_bytes("example-lenient.jpg", PNG_BYTES)
    lines = encoded.splitlines()
    for index, line in enumerate(lines):
        if index > 0 and line != "end":
            lines[index] = line + "!"
            break
    encoded = "\n".join(lines)
    return f"""<SEC-DOCUMENT>0000000000-26-000004.txt : 20260524
<SEC-HEADER>
ACCESSION NUMBER:        0000000000-26-000004
</SEC-HEADER>
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>1
<FILENAME>example-lenient.jpg
<DESCRIPTION>GRAPHIC
<TEXT>
{encoded}
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""


def test_unpack_writes_flat_files_manifest_and_decodes_image(tmp_path):
    source = tmp_path / "full-submission.txt"
    output_dir = tmp_path / "output"
    source.write_text(sample_submission(), encoding="utf-8")

    status = main(["unpack", str(source), "-o", str(output_dir)])

    assert status == 0
    html = output_dir / "example-20251231x20f.htm"
    image = output_dir / "example-logo.png"
    manifest = output_dir / "manifest.json"
    assert html.read_text(encoding="utf-8").startswith("<html>")
    assert image.read_bytes().startswith(b"\x89PNG")
    with Image.open(image) as img:
        assert img.size == (1, 1)

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["filing"]["accession_number"] == "0000000000-26-000001"
    assert data["filing"]["submission_type"] == "20-F"
    assert data["filing"]["company_name"] == "Example Holdings Inc."
    assert data["documents"][0]["filename"] == "example-20251231x20f.htm"
    assert data["documents"][0]["content_kind"] == "html"
    assert data["documents"][1]["content_kind"] == "image"
    assert data["documents"][1]["decode_status"] == "base64"


def test_unpack_refuses_to_overwrite_existing_output_file(tmp_path, capsys):
    source = tmp_path / "full-submission.txt"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source.write_text(sample_submission(), encoding="utf-8")
    (output_dir / "example-20251231x20f.htm").write_text("existing", encoding="utf-8")

    status = main(["unpack", str(source), "-o", str(output_dir)])

    captured = capsys.readouterr()
    assert status != 0
    assert "--overwrite" in captured.err
    assert (output_dir / "example-20251231x20f.htm").read_text(encoding="utf-8") == "existing"


def test_unpack_overwrite_replaces_generated_files(tmp_path):
    source = tmp_path / "full-submission.txt"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source.write_text(sample_submission(), encoding="utf-8")
    (output_dir / "example-20251231x20f.htm").write_text("existing", encoding="utf-8")

    status = main(["unpack", str(source), "-o", str(output_dir), "--overwrite"])

    assert status == 0
    assert (output_dir / "example-20251231x20f.htm").read_text(encoding="utf-8").startswith("<html>")


def test_unpack_decodes_uuencoded_sec_graphic(tmp_path):
    source = tmp_path / "full-submission.txt"
    output_dir = tmp_path / "output"
    source.write_text(sample_submission_with_uuencoded_image(), encoding="utf-8")

    status = main(["unpack", str(source), "-o", str(output_dir)])

    assert status == 0
    image = output_dir / "example-logo.jpg"
    assert image.read_bytes().startswith(b"\x89PNG")
    with Image.open(image) as img:
        assert img.size == (1, 1)
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][0]["decode_status"] == "uuencode"


def test_unpack_decodes_uuencoded_binary_attachment(tmp_path):
    source = tmp_path / "full-submission.txt"
    output_dir = tmp_path / "output"
    source.write_text(sample_submission_with_uuencoded_zip(), encoding="utf-8")

    status = main(["unpack", str(source), "-o", str(output_dir)])

    assert status == 0
    archive = output_dir / "archive.zip"
    assert archive.read_bytes().startswith(b"PK\x03\x04")
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][0]["content_kind"] == "binary"
    assert manifest["documents"][0]["decode_status"] == "uuencode"


def test_unpack_decodes_sec_uuencode_with_trailing_garbage(tmp_path):
    source = tmp_path / "full-submission.txt"
    output_dir = tmp_path / "output"
    source.write_text(sample_submission_with_lenient_uuencoded_image(), encoding="utf-8")

    status = main(["unpack", str(source), "-o", str(output_dir)])

    assert status == 0
    image = output_dir / "example-lenient.jpg"
    assert image.read_bytes().startswith(b"\x89PNG")
