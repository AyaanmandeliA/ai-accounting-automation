from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageSequence
except ModuleNotFoundError:  # pragma: no cover - optional media optimization dependency
    Image = None
    ImageSequence = None

BASE_DIR = Path(__file__).resolve().parents[1]
OPTIMIZED_DIR = BASE_DIR / "data/optimized"


def _safe_name(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in path.name)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose optimized filename for {path.name}")


def file_size_mb(path: str | Path) -> float:
    try:
        return round(Path(path).stat().st_size / 1024 / 1024, 4)
    except OSError:
        return 0.0


def optimized_size_estimate_mb(path: str | Path) -> float:
    size = file_size_mb(path)
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png"}:
        return round(max(size * 0.35, min(size, 0.15)), 4)
    if suffix == ".pdf":
        return round(max(size * 0.55, min(size, 0.25)), 4)
    return size


def optimize_media_for_ai(
    file_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    max_dimension: int = 1800,
    jpeg_quality: int = 70,
    grayscale: bool = True,
) -> dict[str, Any]:
    source = Path(file_path)
    target_dir = Path(output_dir) if output_dir else OPTIMIZED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    original_size = file_size_mb(source)
    suffix = source.suffix.lower()

    if suffix in {".jpg", ".jpeg", ".png"}:
        target = _unique_path(target_dir / f"{source.stem}.optimized.jpg")
        if Image is None:
            warnings.append("Pillow not installed; image copied without optimization")
            shutil.copy2(source, target)
            return {
                "original_path": str(source.resolve()),
                "optimized_path": str(target.resolve()),
                "original_file_size_mb": original_size,
                "optimized_file_size_mb": file_size_mb(target),
                "method": "copy_fallback",
                "warnings": warnings,
            }
        with Image.open(source) as image:
            image = image.convert("L" if grayscale else "RGB")
            image.thumbnail((max_dimension, max_dimension))
            image.save(target, format="JPEG", quality=jpeg_quality, optimize=True)
        return {
            "original_path": str(source.resolve()),
            "optimized_path": str(target.resolve()),
            "original_file_size_mb": original_size,
            "optimized_file_size_mb": file_size_mb(target),
            "method": "image_downsample_grayscale" if grayscale else "image_downsample",
            "warnings": warnings,
        }

    if suffix == ".pdf":
        target = _unique_path(target_dir / f"{source.stem}.optimized.pdf")
        if Image is None or ImageSequence is None:
            warnings.append("Pillow not installed; PDF copied without optimization")
            shutil.copy2(source, target)
            return {
                "original_path": str(source.resolve()),
                "optimized_path": str(target.resolve()),
                "original_file_size_mb": original_size,
                "optimized_file_size_mb": file_size_mb(target),
                "method": "pdf_copy_fallback",
                "warnings": warnings,
            }
        try:
            with Image.open(source) as document:
                pages = []
                for page in ImageSequence.Iterator(document):
                    page = page.convert("L" if grayscale else "RGB")
                    page.thumbnail((max_dimension, max_dimension))
                    pages.append(page.copy())
                if pages:
                    pages[0].save(
                        target,
                        "PDF",
                        save_all=True,
                        append_images=pages[1:],
                        resolution=120,
                    )
                else:
                    raise ValueError("no PDF pages rendered by Pillow")
        except Exception as exc:
            warnings.append(f"PDF optimization fallback copy used: {exc}")
            shutil.copy2(source, target)
        return {
            "original_path": str(source.resolve()),
            "optimized_path": str(target.resolve()),
            "original_file_size_mb": original_size,
            "optimized_file_size_mb": file_size_mb(target),
            "method": "pdf_render_downsample" if not warnings else "pdf_copy_fallback",
            "warnings": warnings,
        }

    target = _unique_path(target_dir / _safe_name(source))
    shutil.copy2(source, target)
    warnings.append("unsupported media type copied without optimization")
    return {
        "original_path": str(source.resolve()),
        "optimized_path": str(target.resolve()),
        "original_file_size_mb": original_size,
        "optimized_file_size_mb": file_size_mb(target),
        "method": "copy_fallback",
        "warnings": warnings,
    }
