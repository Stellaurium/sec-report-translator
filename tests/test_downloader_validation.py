from pathlib import Path

import pytest

from sec_report_translator.downloader import DownloadError, validate_download_result


def test_validate_download_result_rejects_zero_byte_files(tmp_path):
    target = tmp_path / "sec-edgar-filings" / "BABA" / "20-F"
    target.mkdir(parents=True)
    accession = target / "0000000000-26-000001"
    accession.mkdir()
    (accession / "full-submission.txt").write_bytes(b"")

    with pytest.raises(DownloadError, match="incomplete"):
        validate_download_result(target, downloaded_count=1)


def test_validate_download_result_rejects_no_downloads(tmp_path):
    target = tmp_path / "sec-edgar-filings" / "UNKNOWN" / "10-K"
    target.mkdir(parents=True)

    with pytest.raises(DownloadError, match="No filings"):
        validate_download_result(target, downloaded_count=0)
