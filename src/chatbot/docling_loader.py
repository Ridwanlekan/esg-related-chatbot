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
        """Convert one file to Markdown. Raises ConversionError on failure."""
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
        return result.document.export_to_markdown()


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