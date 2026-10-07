"""Helpers for the multi-photo event editor. No database schema change."""

import io
import re
import secrets
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ticketing import BusinessError


def valid_filename(filename):
    return re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|webp)", filename) is not None


def read_gallery_selection(existing, form, validate_link):
    if form.get("gallery_editor") != "1":
        # Compatibility with old forms and clients; absent fields preserve photos.
        raw = form.get("gallery", existing)
        if len(raw) > 10000:
            raise BusinessError("现场照片地址内容过长")
        return "\n".join(validate_link(s.strip(), True) for s in raw.splitlines() if s.strip())
    originals = [s.strip() for s in existing.splitlines() if s.strip()]
    selected = form.getlist("keep_gallery")
    if any(s not in originals for s in selected):
        raise BusinessError("原有照片已发生变化，请刷新编辑页后重试")
    # Retain original order, even when a client sends checkbox values out of order.
    retained = [s for s in originals if s in selected]
    raw_links = form.get("gallery_links", "").strip()
    if len(raw_links) > 10000:
        raise BusinessError("外部图片地址内容过长")
    for line in raw_links.splitlines():
        if line.strip():
            value = validate_link(line.strip(), True)
            if value not in retained:
                retained.append(value)
    return "\n".join(retained)


def save_gallery_uploads(files, upload_dir):
    files = [f for f in files if f and f.filename]
    if len(files) > 20:
        raise BusinessError("每次最多新增 20 张照片，可以分次保存")
    paths, urls = [], []
    try:
        for number, file in enumerate(files, 1):
            data = file.stream.read(6 * 1024 * 1024 + 1)
            if len(data) > 6 * 1024 * 1024:
                raise BusinessError(f"第 {number} 张图片超过 6 MB，请压缩后重新上传")
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP"):
                    raise BusinessError(f"第 {number} 张照片格式不支持，请用 JPEG、PNG 或 WebP")
                if source.width * source.height > 20_000_000:
                    raise BusinessError(f"第 {number} 张照片超过两千万像素，请降低分辨率后再上传")
                source.load()
                # Phone portraits must honor the camera's orientation metadata.
                with ImageOps.exif_transpose(source) as oriented:
                    oriented.thumbnail((2400, 2400))
                    alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                    extension = ".png" if alpha else ".jpg"
                    path = Path(upload_dir) / (secrets.token_hex(16) + extension)
                    paths.append(path)
                    with oriented.convert("RGBA" if alpha else "RGB") as final:
                        # Do not retain EXIF/GPS metadata in publicly served photos.
                        final.info.clear()
                        if alpha:
                            final.save(path, format="PNG", optimize=True)
                        else:
                            final.save(path, format="JPEG", quality=88, optimize=True)
                    urls.append("/uploads/" + path.name)
        return urls, paths
    except Exception as exc:
        for path in paths:
            path.unlink(missing_ok=True)
        if isinstance(exc, (UnidentifiedImageError, Image.DecompressionBombError)):
            raise BusinessError("所选照片包含无法识别或尺寸过大的图片，请重新选择") from exc
        if isinstance(exc, OSError):
            raise BusinessError("照片无法读取或保存，请检查文件格式及磁盘空间") from exc
        raise
