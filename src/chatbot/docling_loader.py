import logging
import os
import threading

logger = logging.getLogger("esg.ingest.docling")

# Every extension Docling can parse (lowercase, no leading dot). Grouped by
# how the file is routed, but the authoritative check is still Docling's own
# format detection at conversion time — unsupported/odd files fail gracefully.
DOCUMENT_EXTENSIONS = {
    "pdf",
    "docx", "doc", "rtf",
    "pptx", "ppt",
    "odt", "ods", "odp",
    "epub", "pages", "boxnote",
    "md", "markdown",
    "adoc", "asciidoc", "asc",
    "tex", "latex",
    "html", "htm", "xhtml", "mhtml",
    "csv", "xlsx", "xls",
    "vtt",
    "eml", "msg",
    "xml", "json", "dclx", "dclg",
}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"}
AUDIO_EXTENSIONS = {"wav", "mp3", "m4a", "aac", "ogg", "flac"}
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv", "webm"}

DOCLING_EXTENSIONS = (
    DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
)

# Extensions we can still read as plain text if Docling is unavailable/fails.
TEXT_FALLBACK_EXTENSIONS = {
    "txt", "log", "md", "markdown", "adoc", "asciidoc", "asc",
    "tex", "latex", "html", "htm", "xhtml", "csv", "vtt", "xml", "json",
}

DEFAULT_ASR_MODEL = "WHISPER_TURBO"


class ConversionError(RuntimeError):
    pass


def clean_docling_text(text):
    """Light normalization of Docling Markdown before chunking:
    right-trim every line, collapse repeated blank lines, and strip leading
    and trailing blank lines. Markdown structure (headings, tables, fenced
    code) is untouched.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = []
    previous_blank = False
    for line in lines:
        if not line.strip():
            if previous_blank:
                continue
            previous_blank = True
            cleaned.append("")
        else:
            previous_blank = False
            cleaned.append(line)
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned) + "\n" if cleaned else ""


def extension_of(path):
    name = os.path.basename(str(path))
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].lower()


def enabled():
    return os.environ.get("DOCLING_ENABLED", "1") != "0"


def ocr_enabled():
    return os.environ.get("DOCLING_OCR", "1") != "0"


def asr_enabled():
    return os.environ.get("DOCLING_ASR", "1") != "0"


def is_audio(path):
    return extension_of(path) in AUDIO_EXTENSIONS


def is_video(path):
    return extension_of(path) in VIDEO_EXTENSIONS


def is_media(path):
    return is_audio(path) or is_video(path)


_available = None


def is_available():
    global _available
    if _available is None:
        try:
            import docling  # noqa: F401
            _available = True
        except Exception:
            logger.warning(
                "Docling is not installed; falling back to plain-text extraction. "
                "Install with: pip install docling"
            )
            _available = False
    return _available


def _asr_model_spec():
    from docling.datamodel import asr_model_specs

    name = os.environ.get("DOCLING_ASR_MODEL", DEFAULT_ASR_MODEL)
    spec = getattr(asr_model_specs, name, None)
    if spec is None:
        logger.warning("Unknown DOCLING_ASR_MODEL=%s; using %s", name, DEFAULT_ASR_MODEL)
        spec = asr_model_specs.WHISPER_TURBO
    return spec


class DoclingLoader:
    """Thin wrapper around Docling's DocumentConverter.

    Builds two converters lazily: a standard one for documents/images and an
    ASR/video one for audio and video files. Thread-safe.
    """

    def __init__(self):
        self._converter = None
        self._media_converter = None
        self._lock = threading.Lock()

    def supports(self, path):
        return extension_of(path) in DOCLING_EXTENSIONS

    def _build_standard_converter(self):
        from docling.document_converter import DocumentConverter

        if ocr_enabled():
            return DocumentConverter()

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import ImageFormatOption, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = False
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts),
            }
        )

    def _build_media_converter(self):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AsrPipelineOptions,
            VideoPipelineOptions,
        )
        from docling.document_converter import (
            AudioFormatOption,
            DocumentConverter,
            VideoFormatOption,
        )
        from docling.pipeline.asr_pipeline import AsrPipeline
        from docling.pipeline.video_pipeline import VideoPipeline

        asr_opts = AsrPipelineOptions()
        asr_opts.asr_options = _asr_model_spec()
        video_opts = VideoPipelineOptions()
        video_opts.asr_options = _asr_model_spec()

        return DocumentConverter(
            format_options={
                InputFormat.AUDIO: AudioFormatOption(
                    pipeline_cls=AsrPipeline, pipeline_options=asr_opts
                ),
                InputFormat.VIDEO: VideoFormatOption(
                    pipeline_cls=VideoPipeline, pipeline_options=video_opts
                ),
            }
        )

    def _get_converter(self, media=False):
        attr = "_media_converter" if media else "_converter"
        if getattr(self, attr) is None:
            with self._lock:
                if getattr(self, attr) is None:
                    setattr(
                        self,
                        attr,
                        self._build_media_converter()
                        if media
                        else self._build_standard_converter(),
                    )
        return getattr(self, attr)

    def convert(self, path):
        """Convert one file to Markdown. Raises ConversionError on failure.

        The raw document export; callers normalise it with clean_docling_text.
        """
        return self._convert_document(path).export_to_markdown()

    def convert_pages(self, path):
        """Convert one file to `[(page_no, text), ...]`, in page order.

        Returns None for formats with no pagination (HTML, plain markdown), where
        a page number would be a fiction rather than a citation.

        Docling's own `export_to_markdown(page_no=n)` is not used here. It
        filters on provenance but drops a merged item entirely rather than
        slicing it, which silently loses text when two pages share a text block —
        and a citation that points at the wrong page is worse than no citation.
        Slicing each item by its per-page `charspan` keeps every character and
        puts it on the page it was printed on.
        """
        return self._pages_from(self._convert_document(path))

    def convert_both(self, path):
        """Convert once and return `(markdown, [(page_no, text), ...])`.

        Conversion is by far the expensive part of ingestion — parse, layout
        analysis, and OCR — so a caller that needs both the document text and
        its page split must get them from a single pass. Calling convert() and
        convert_pages() separately would parse and OCR every paginated document
        twice for no extra information.
        """
        document = self._convert_document(path)
        markdown = document.export_to_markdown()
        try:
            pages = self._pages_from(document)
        except Exception as exc:
            # The conversion was the expensive part and it succeeded; losing the
            # page split is a smaller loss than making the caller redo the work.
            logger.debug("page split failed for %s: %s", path, exc)
            pages = None
        return markdown, pages

    @staticmethod
    def _pages_from(document):
        """Per-page text for an already-converted document, or None if unpaginated."""
        if not getattr(document, "pages", None):
            return None

        pieces = {}
        for item, _level in document.iterate_items():
            for page_no, text in DoclingLoader._page_pieces(item, document):
                pieces.setdefault(page_no, []).append(text)

        ordered = []
        for page_no in sorted(pieces):
            text = "\n\n".join(part for part in pieces[page_no] if part.strip())
            text = clean_docling_text(text)
            if text.strip():
                ordered.append((page_no, text))
        return ordered or None

    @staticmethod
    def _page_pieces(item, document):
        """Yield (page_no, text) for one Docling item, one entry per page it spans."""
        provenance = list(getattr(item, "prov", None) or [])
        if not provenance:
            return
        text = getattr(item, "text", None)
        if isinstance(text, str):
            for prov in provenance:
                span = getattr(prov, "charspan", None)
                piece = text[span[0] : span[1]] if span else text
                if piece.strip():
                    yield prov.page_no, piece
            return
        # Tables, pictures and form fields have no .text; their Markdown is
        # whole-block, so it is attributed to the page the block starts on.
        try:
            # The owning document is what resolves a table's structure; without
            # it Docling serialises a degraded table and warns about it.
            markdown = item.export_to_markdown(doc=document)
        except TypeError:
            markdown = item.export_to_markdown()
        except Exception:
            return
        if markdown and markdown.strip():
            yield provenance[0].page_no, markdown

    def _convert_document(self, path):
        media = is_media(path)
        if media and not asr_enabled():
            raise ConversionError(f"ASR disabled for {os.path.basename(path)}")
        converter = self._get_converter(media=media)
        try:
            result = converter.convert(str(path))
        except Exception as exc:
            raise ConversionError(f"Docling failed on {path}: {exc}") from exc

        from docling.datamodel.base_models import ConversionStatus

        if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
            raise ConversionError(f"Docling status {result.status} for {path}")
        return result.document


_default_loader = None


def get_loader():
    global _default_loader
    if _default_loader is None:
        _default_loader = DoclingLoader()
    return _default_loader


def extract_text(path, loader=None):
    """Return converted Markdown for `path`, or None if Docling can't help.

    Callers decide on plain-text fallback for text-like extensions.
    """
    if not (enabled() and is_available()):
        return None
    loader = loader or get_loader()
    if not loader.supports(path):
        return None
    return loader.convert(path)