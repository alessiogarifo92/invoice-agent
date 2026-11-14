"""Upload validation for POST /invoices. Every rejection carries an HTTP status and a code."""

from app.ingestion.pdf import UnreadablePdf, open_pdf

PDF_CONTENT_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"


class UploadRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def check_content_type(content_type: str | None) -> None:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != PDF_CONTENT_TYPE:
        raise UploadRejected(415, "not_pdf", f"expected {PDF_CONTENT_TYPE}, got {media_type!r}")


def check_size(data: bytes, limit: int) -> None:
    """`data` is at most limit + 1 bytes: the caller never reads further than that."""
    if len(data) > limit:
        raise UploadRejected(413, "too_large", f"the file is larger than {limit} bytes")


def check_magic(data: bytes) -> None:
    if not data.startswith(PDF_MAGIC):
        raise UploadRejected(415, "not_pdf", "the file does not start with a PDF header")


def inspect_pdf(data: bytes, max_pages: int) -> int:
    """Parse the PDF and return its page count. CPU-bound: run it off the event loop."""
    try:
        with open_pdf(data) as pdf:
            page_count = len(pdf.pages)
            # Pages are parsed only when the count is acceptable.
            has_text = page_count <= max_pages and any(
                c["text"].strip() for page in pdf.pages for c in page.chars
            )
    except UnreadablePdf as exc:
        # pdfminer's error text stays server-side; the client gets a stable message.
        raise UploadRejected(422, "unreadable_pdf", "the PDF is corrupt or unreadable") from exc
    if page_count > max_pages:
        raise UploadRejected(
            422, "page_limit", f"the PDF has {page_count} pages; at most {max_pages} are allowed"
        )
    if not has_text:
        raise UploadRejected(
            422, "no_text_layer", "the PDF has no text layer (scanned or image-only documents)"
        )
    return page_count
