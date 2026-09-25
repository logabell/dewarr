"""Small original synthetic book content, generated in disposable test directories."""

import io
import shutil
import subprocess
import zipfile
from xml.sax.saxutils import escape

import pytest


def cover_bytes(*, color="navy", size=(240, 360), format="PNG", **save_options):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=format, **save_options)
    return output.getvalue()


def epub(
    path, title="First Harbor", author="Alex Morgan", *, chapter=True, metadata=None, isbn=None
):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
        )
        book.writestr(
            "OEBPS/book.opf",
            metadata
            or (
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator>"
                "<dc:language>en</dc:language>"
                f"<dc:identifier>{escape(isbn or 'synthetic-fixture')}</dc:identifier>"
                '</metadata><manifest><item id="chapter" href="chapter.xhtml" '
                'media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="chapter"/></spine></package>'
            ),
        )
        if chapter:
            book.writestr(
                "OEBPS/chapter.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                "<body><p>This is original synthetic fixture content.</p></body></html>",
            )


def pdf(path, title="First Harbor", author="Alex Morgan", *, pages=2, password=None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_metadata({"/Title": title, "/Author": author})
    for _ in range(pages):
        page = writer.add_blank_page(width=360, height=540)
        contents = DecodedStreamObject()
        # Original vector page content, independent of system fonts and external images.
        contents.set_data(b"0.1 0.2 0.5 rg 30 30 280 440 re f\n")
        page[NameObject("/Contents")] = contents
    if password is not None:
        writer.encrypt(password)
    writer.write(path)


def cbz(path, title="First Harbor", author="Alex Morgan", *, pages=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as book:
        for number in range(pages):
            book.writestr(f"pages/{number + 1:03}.png", cover_bytes(color="navy"))
        book.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Title>{escape(title)}</Title><Writer>{escape(author)}</Writer>"
            "<LanguageISO>en</LanguageISO><Series>Harbor Stories</Series>"
            "<Number>1</Number></ComicInfo>",
        )


def audio(
    path, title="First Harbor", author="Alex Morgan", narrator="Jordan Lee", track=1, tags=None
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Actual audio inspection requires ffmpeg/ffprobe")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "0.2",
            "-threads",
            "1",
            "-metadata",
            f"album={title}",
            "-metadata",
            f"artist={author}",
            "-metadata",
            f"composer={narrator}",
            "-metadata",
            f"track={track}",
            *(
                argument
                for key, value in (tags or {}).items()
                for argument in ("-metadata", f"{key}={value}")
            ),
            str(path),
        ],
        check=True,
        timeout=15,
        capture_output=True,
    )
