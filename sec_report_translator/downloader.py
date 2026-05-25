from __future__ import annotations

from pathlib import Path


class DownloadError(Exception):
    pass


class EdgarDownloader:
    def __init__(self, user_agent_name: str, user_agent_email: str, output_dir: Path):
        self.user_agent_name = user_agent_name
        self.user_agent_email = user_agent_email
        self.output_dir = Path(output_dir)

    def download(
        self,
        ticker: str,
        form_type: str,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> Path:
        try:
            from sec_edgar_downloader import Downloader
        except ImportError as exc:
            raise DownloadError(
                "Missing dependency sec-edgar-downloader. Install project dependencies first."
            ) from exc

        downloader = Downloader(
            company_name=self.user_agent_name,
            email_address=self.user_agent_email,
            download_folder=str(self.output_dir),
        )
        target_path = self.output_dir / "sec-edgar-filings" / ticker / form_type
        try:
            count = downloader.get(
                form_type,
                ticker,
                after=after,
                before=before,
                limit=limit,
                download_details=True,
            )
        except Exception as exc:
            raise DownloadError(f"SEC download failed: {exc}") from exc

        validate_download_result(target_path, downloaded_count=count)
        return target_path


def validate_download_result(target_path: Path, downloaded_count: int | None) -> None:
    if downloaded_count == 0:
        raise DownloadError(f"No filings were downloaded into {target_path}.")

    if not target_path.exists():
        raise DownloadError(f"Expected download directory was not created: {target_path}")

    zero_byte_files = [path for path in target_path.rglob("*") if path.is_file() and path.stat().st_size == 0]
    if zero_byte_files:
        examples = ", ".join(str(path) for path in zero_byte_files[:3])
        raise DownloadError(
            f"Downloaded filing appears incomplete; found zero-byte files: {examples}"
        )
