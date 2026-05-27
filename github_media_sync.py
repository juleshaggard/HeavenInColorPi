from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageStat


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CAPTURE_DIR = APP_ROOT / "data" / "captures"
DEFAULT_HISTORY_PATH = APP_ROOT / "data" / "history.json"
DEFAULT_CAP_BYTES = 900 * 1024 * 1024
DEFAULT_SPRITE_TILE_SIZE = 64
DEFAULT_SPRITE_COLUMNS = 16


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Capture:
    timestamp: str
    image_name: str
    average_hex: str | None

    @property
    def captured_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    @property
    def year(self) -> str:
        return self.timestamp[:4]

    @property
    def month(self) -> str:
        return self.timestamp[4:6]


@dataclass(frozen=True)
class DerivativeInfo:
    rel_path: str
    width: int
    height: int
    bytes: int


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cap_bytes_from_env() -> int:
    if os.getenv("SKY_PUBLISHED_CAP_BYTES"):
        return int(os.environ["SKY_PUBLISHED_CAP_BYTES"])
    if os.getenv("SKY_PUBLISHED_CAP_MIB"):
        return int(float(os.environ["SKY_PUBLISHED_CAP_MIB"]) * 1024 * 1024)
    return DEFAULT_CAP_BYTES


def output_dir_from_env() -> Path:
    if os.getenv("SKY_OUTPUT_DIR"):
        return Path(os.environ["SKY_OUTPUT_DIR"]).expanduser()
    if os.getenv("SKY_MEDIA_ROOT"):
        return Path(os.environ["SKY_MEDIA_ROOT"]).expanduser() / "public" / "sky"
    return APP_ROOT / "public" / "sky"


def parse_captured_at(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_captures(history_path: Path, capture_dir: Path) -> list[Capture]:
    if history_path.exists():
        raw_items = json.loads(history_path.read_text())
        captures = [
            Capture(
                timestamp=item["timestamp"],
                image_name=item["image_name"],
                average_hex=item.get("average_hex"),
            )
            for item in raw_items
        ]
    else:
        captures = [
            Capture(timestamp=p.stem, image_name=p.name, average_hex=None)
            for p in capture_dir.glob("*.jpg")
        ]

    return sorted(
        [c for c in captures if (capture_dir / c.image_name).exists()],
        key=lambda c: c.captured_at,
    )


def filter_captures(captures: list[Capture], min_captured_at: datetime | None) -> list[Capture]:
    if min_captured_at is None:
        return captures
    return [capture for capture in captures if capture.captured_at >= min_captured_at]


def probe_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def rgb_to_hex(rgb: list[int] | tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, round(channel))) for channel in rgb[:3]])


def center_square(image: Image.Image) -> Image.Image:
    size = min(image.width, image.height)
    left = (image.width - size) // 2
    top = (image.height - size) // 2
    return image.crop((left, top, left + size, top + size))


def luminance(rgb: tuple[int, int, int]) -> float:
    return rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114


def color_distance_sq(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum((a[index] - b[index]) ** 2 for index in range(3))


def average_rgb(colors: list[tuple[int, int, int]]) -> tuple[int, int, int] | None:
    if not colors:
        return None
    return tuple(round(sum(color[index] for color in colors) / len(colors)) for index in range(3))


def unique_colors(colors: list[tuple[int, int, int]], min_distance: int = 18) -> list[tuple[int, int, int]]:
    selected: list[tuple[int, int, int]] = []
    threshold = min_distance * min_distance
    for color in colors:
        if all(color_distance_sq(color, existing) >= threshold for existing in selected):
            selected.append(color)
    return selected


def pick_evenly_by_luminance(colors: list[tuple[int, int, int]], count: int) -> list[tuple[int, int, int]]:
    if count <= 0:
        return []
    ordered = sorted(colors, key=luminance)
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    return [ordered[round(index * (len(ordered) - 1) / (count - 1))] for index in range(count)]


def highlight_colors(pixels: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    if not pixels:
        return []

    highlights: list[tuple[int, int, int]] = []
    bright_count = max(12, len(pixels) // 64)
    brightest = sorted(pixels, key=luminance, reverse=True)[:bright_count]
    bright_average = average_rgb(brightest)
    if bright_average and luminance(bright_average) >= 210:
        highlights.append(bright_average)

    warm_pixels = [
        pixel
        for pixel in pixels
        if luminance(pixel) >= 155 and pixel[0] >= pixel[2] + 8 and pixel[1] >= pixel[2] + 4
    ]
    if warm_pixels:
        warm_pixels.sort(key=lambda pixel: ((pixel[0] + pixel[1]) * 0.5 - pixel[2] + luminance(pixel) * 0.15), reverse=True)
        warm_average = average_rgb(warm_pixels[: max(12, len(pixels) // 80)])
        if warm_average and luminance(warm_average) >= 160:
            highlights.append(warm_average)

    return unique_colors(highlights, min_distance=20)


def extract_palette(image: Image.Image, count: int = 5) -> list[str]:
    sample_size = 64
    sample = image.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
    quantize_method = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT", getattr(Image, "MEDIANCUT", 0))
    quantized = sample.quantize(colors=count + 2, method=quantize_method)
    raw_palette = quantized.getpalette() or []
    raw_counts = quantized.getcolors(maxcolors=sample_size * sample_size) or []
    pixels = list(sample.getdata())

    colors: list[tuple[int, int, int]] = []
    seen: set[str] = set()
    for _, index in raw_counts:
        start = index * 3
        rgb = tuple(raw_palette[start : start + 3])
        if len(rgb) != 3:
            continue
        hex_color = rgb_to_hex(rgb)
        if hex_color in seen:
            continue
        seen.add(hex_color)
        colors.append(rgb)

    specials = highlight_colors(pixels)
    base = [
        color
        for color in unique_colors(sorted(colors, key=luminance), min_distance=14)
        if all(color_distance_sq(color, special) >= 16 * 16 for special in specials)
    ]
    selected = pick_evenly_by_luminance(base, count - len(specials)) + specials
    if len(selected) < count:
        selected = unique_colors(selected + sorted(colors, key=luminance), min_distance=12)

    selected = pick_evenly_by_luminance(unique_colors(selected, min_distance=12), count)
    return [rgb_to_hex(color) for color in sorted(selected[:count], key=luminance)]


def compute_visible_crop_colors(path: Path) -> tuple[str, list[str]]:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        crop = center_square(image)
        average_hex = rgb_to_hex(ImageStat.Stat(crop).mean[:3])
        palette = extract_palette(crop)
    return average_hex, palette


def write_derivative(
    source_path: Path,
    output_dir: Path,
    rel_path: str,
    max_width: int,
    quality: int,
    dry_run: bool,
) -> DerivativeInfo:
    dest = output_dir / rel_path
    if dry_run:
        if dest.exists():
            width, height = probe_image(dest)
            return DerivativeInfo(rel_path=rel_path, width=width, height=height, bytes=dest.stat().st_size)
        width, height = probe_image(source_path)
        if width > max_width:
            height = round(height * (max_width / width))
            width = max_width
        return DerivativeInfo(rel_path=rel_path, width=width, height=height, bytes=source_path.stat().st_size)

    if dest.exists() and dest.stat().st_mtime >= source_path.stat().st_mtime:
        width, height = probe_image(dest)
        return DerivativeInfo(rel_path=rel_path, width=width, height=height, bytes=dest.stat().st_size)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.width > max_width:
            ratio = max_width / image.width
            image = image.resize((max_width, round(image.height * ratio)), Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(prefix=dest.name, suffix=".tmp", dir=dest.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            image.save(tmp_path, format="JPEG", quality=quality, optimize=True, progressive=True)
            tmp_path.replace(dest)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    width, height = probe_image(dest)
    return DerivativeInfo(rel_path=rel_path, width=width, height=height, bytes=dest.stat().st_size)


def remove_empty_dirs(root: Path) -> None:
    for child in sorted(root.rglob("*"), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass


def prune_unretained_files(output_dir: Path, retained_paths: set[Path], dry_run: bool) -> list[str]:
    removed: list[str] = []
    for folder in (output_dir / "images", output_dir / "thumbs", output_dir / "sprites"):
        if not folder.exists():
            continue
        for path in folder.rglob("*.jpg"):
            if path not in retained_paths:
                removed.append(str(path.relative_to(output_dir)))
                if not dry_run:
                    path.unlink()
    if not dry_run:
        remove_empty_dirs(output_dir)
    return removed


def write_manifest(output_dir: Path, manifest: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "manifest.json"
    with tempfile.NamedTemporaryFile("w", prefix="manifest", suffix=".tmp", dir=output_dir, delete=False) as tmp:
        json.dump(manifest, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)


def comparable_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "generatedAt"}


def preserve_existing_manifest_if_equivalent(
    output_dir: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    target = output_dir / "manifest.json"
    if not target.exists():
        return manifest, False
    try:
        existing = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return manifest, False
    if comparable_manifest(existing) == comparable_manifest(manifest):
        return existing, True
    return manifest, False


def sprite_key(captured_at: datetime) -> str:
    iso_year, iso_week, _ = captured_at.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def write_sprite_sheet(
    output_dir: Path,
    key: str,
    entries: list[dict[str, Any]],
    tile_size: int,
    columns: int,
    quality: int,
    dry_run: bool,
) -> dict[str, Any]:
    rows = (len(entries) + columns - 1) // columns
    width = columns * tile_size
    height = rows * tile_size
    year = key.split("-", 1)[0]
    rel_path = f"sprites/{year}/{key}.jpg"
    dest = output_dir / rel_path

    for index, entry in enumerate(entries):
        entry["sprite"] = {"key": key, "index": index}

    if dry_run:
        bytes_written = dest.stat().st_size if dest.exists() else 0
        if dest.exists():
            width, height = probe_image(dest)
        return {
            "key": key,
            "url": rel_path,
            "tileSize": tile_size,
            "columns": columns,
            "rows": rows,
            "width": width,
            "height": height,
            "count": len(entries),
            "bytes": bytes_written,
        }

    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet = Image.new("RGB", (width, height), (0, 0, 0))
    for index, entry in enumerate(entries):
        source_path = output_dir / entry["imageUrl"]
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            frame = ImageOps.fit(
                image,
                (tile_size, tile_size),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
        col = index % columns
        row = index // columns
        sheet.paste(frame, (col * tile_size, row * tile_size))

    with tempfile.NamedTemporaryFile(prefix=dest.name, suffix=".tmp", dir=dest.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        sheet.save(tmp_path, format="JPEG", quality=quality, optimize=True, progressive=True)
        tmp_path.replace(dest)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return {
        "key": key,
        "url": rel_path,
        "tileSize": tile_size,
        "columns": columns,
        "rows": rows,
        "width": width,
        "height": height,
        "count": len(entries),
        "bytes": dest.stat().st_size,
    }


def build_weekly_sprites(
    entries: list[dict[str, Any]],
    output_dir: Path,
    tile_size: int,
    columns: int,
    quality: int,
    dry_run: bool,
) -> tuple[dict[str, Any], set[Path], int]:
    for entry in entries:
        entry.pop("sprite", None)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        captured_at = parse_captured_at(entry.get("capturedAt"))
        if captured_at is None:
            continue
        key = sprite_key(captured_at)
        grouped.setdefault(key, []).append(entry)

    weeks: list[dict[str, Any]] = []
    sprite_paths: set[Path] = set()
    sprite_bytes = 0
    for key in sorted(grouped):
        week = write_sprite_sheet(
            output_dir=output_dir,
            key=key,
            entries=grouped[key],
            tile_size=tile_size,
            columns=columns,
            quality=quality,
            dry_run=dry_run,
        )
        weeks.append(week)
        sprite_paths.add(output_dir / week["url"])
        sprite_bytes += week["bytes"]

    return {
        "tileSize": tile_size,
        "columns": columns,
        "weeks": weeks,
    }, sprite_paths, sprite_bytes


def retained_file_paths(
    output_dir: Path,
    entries: list[dict[str, Any]],
    sprite_paths: set[Path],
) -> set[Path]:
    paths = set(sprite_paths)
    for entry in entries:
        paths.add(output_dir / entry["imageUrl"])
        if entry.get("thumbUrl"):
            paths.add(output_dir / entry["thumbUrl"])
    return paths


def build_manifest_from_existing_output(
    output_dir: Path,
    tile_size: int,
    sprite_columns: int,
    sprite_quality: int,
    dry_run: bool,
) -> tuple[dict[str, Any], list[str]]:
    target = output_dir / "manifest.json"
    if not target.exists():
        raise RuntimeError(f"No manifest found at {target}")

    manifest = json.loads(target.read_text())
    if manifest.get("version") != 1 or not isinstance(manifest.get("images"), list):
        raise RuntimeError("Existing manifest has an unsupported shape")

    entries = manifest["images"]
    sprite_manifest, sprite_paths, sprite_bytes = build_weekly_sprites(
        entries=entries,
        output_dir=output_dir,
        tile_size=tile_size,
        columns=sprite_columns,
        quality=sprite_quality,
        dry_run=dry_run,
    )
    manifest["sprites"] = sprite_manifest
    manifest["generatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    retention = manifest.setdefault("retention", {})
    derivative_bytes = sum(int(entry.get("bytes") or 0) + int(entry.get("thumbBytes") or 0) for entry in entries)
    retention["derivativeBytes"] = derivative_bytes
    retention["spriteBytes"] = sprite_bytes
    retention["retainedBytes"] = derivative_bytes + sprite_bytes
    retention["retainedCount"] = len(entries)

    removed_files = prune_unretained_files(output_dir, retained_file_paths(output_dir, entries, sprite_paths), dry_run)
    return manifest, removed_files


def build_manifest(
    captures: list[Capture],
    capture_dir: Path,
    output_dir: Path,
    cap_bytes: int,
    max_width: int,
    thumb_width: int,
    quality: int,
    thumb_quality: int,
    sprite_tile_size: int,
    sprite_columns: int,
    sprite_quality: int,
    sprites_enabled: bool,
    prune: bool,
    dry_run: bool,
) -> tuple[dict[str, Any], list[str], list[Capture]]:
    retained: list[dict[str, Any]] = []
    pruned: list[Capture] = []
    retained_bytes = 0
    capture_by_id = {capture.timestamp: capture for capture in captures}

    for capture in reversed(captures):
        source_path = capture_dir / capture.image_name
        image_rel = f"images/{capture.year}/{capture.month}/{capture.timestamp}.jpg"
        thumb_rel = f"thumbs/{capture.year}/{capture.month}/{capture.timestamp}.jpg"
        image_info = write_derivative(source_path, output_dir, image_rel, max_width, quality, dry_run)
        thumb_info = write_derivative(source_path, output_dir, thumb_rel, thumb_width, thumb_quality, dry_run)
        color_source = output_dir / image_rel
        if dry_run and not color_source.exists():
            color_source = source_path
        crop_average_hex, crop_palette = compute_visible_crop_colors(color_source)
        pair_bytes = image_info.bytes + thumb_info.bytes

        if pair_bytes > cap_bytes:
            raise RuntimeError(f"{capture.image_name} derivatives are larger than the configured cap")

        if prune and retained_bytes + pair_bytes > cap_bytes:
            pruned.append(capture)
            if not dry_run:
                for rel in (image_rel, thumb_rel):
                    path = output_dir / rel
                    if path.exists():
                        path.unlink()
            continue

        retained_bytes += pair_bytes
        retained.append(
            {
                "id": capture.timestamp,
                "capturedAt": capture.captured_at.isoformat().replace("+00:00", "Z"),
                "imageUrl": image_info.rel_path,
                "thumbUrl": thumb_info.rel_path,
                "averageHex": capture.average_hex,
                "cropAverageHex": crop_average_hex,
                "cropPalette": crop_palette,
                "width": image_info.width,
                "height": image_info.height,
                "bytes": image_info.bytes,
                "thumbBytes": thumb_info.bytes,
            }
        )

    retained.reverse()
    sprite_manifest: dict[str, Any] | None = None
    sprite_paths: set[Path] = set()
    sprite_bytes = 0
    if sprites_enabled:
        while True:
            sprite_manifest, sprite_paths, sprite_bytes = build_weekly_sprites(
                entries=retained,
                output_dir=output_dir,
                tile_size=sprite_tile_size,
                columns=sprite_columns,
                quality=sprite_quality,
                dry_run=dry_run,
            )
            if not prune or not retained or retained_bytes + sprite_bytes <= cap_bytes:
                break

            dropped = retained.pop(0)
            retained_bytes -= int(dropped.get("bytes") or 0) + int(dropped.get("thumbBytes") or 0)
            dropped_capture = capture_by_id.get(dropped["id"])
            if dropped_capture:
                pruned.insert(0, dropped_capture)
            if not dry_run:
                for rel in (dropped["imageUrl"], dropped.get("thumbUrl")):
                    if not rel:
                        continue
                    path = output_dir / rel
                    if path.exists():
                        path.unlink()

    removed_files = prune_unretained_files(output_dir, retained_file_paths(output_dir, retained, sprite_paths), dry_run)
    total_retained_bytes = retained_bytes + sprite_bytes
    manifest = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retention": {
            "capBytes": cap_bytes,
            "retainedBytes": total_retained_bytes,
            "derivativeBytes": retained_bytes,
            "spriteBytes": sprite_bytes,
            "retainedCount": len(retained),
            "sourceCount": len(captures),
            "prunedCount": len(pruned),
            "oldestCapturedAt": retained[0]["capturedAt"] if retained else None,
            "newestCapturedAt": retained[-1]["capturedAt"] if retained else None,
        },
        "images": retained,
    }
    if sprite_manifest is not None:
        manifest["sprites"] = sprite_manifest
    return manifest, removed_files, pruned


def find_git_root(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def pull_git_root(output_dir: Path, branch: str) -> None:
    root = find_git_root(output_dir)
    if root is None:
        raise RuntimeError(f"No git checkout found above {output_dir}")
    subprocess.run(["git", "pull", "--ff-only", "origin", branch], cwd=root, check=True)


def run_git_publish(output_dir: Path, branch: str, message: str) -> None:
    root = find_git_root(output_dir)
    if root is None:
        raise RuntimeError(f"No git checkout found above {output_dir}")

    rel_output = output_dir.relative_to(root)
    subprocess.run(["git", "add", str(rel_output)], cwd=root, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
    if diff.returncode == 0:
        print("No media changes to commit")
        return
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True)
    subprocess.run(["git", "push", "origin", branch], cwd=root, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish sky captures into the GitHub-hosted media folder.")
    parser.add_argument("--capture-dir", type=Path, default=Path(os.getenv("SKY_CAPTURE_DIR", DEFAULT_CAPTURE_DIR)))
    parser.add_argument("--history-path", type=Path, default=Path(os.getenv("SKY_HISTORY_PATH", DEFAULT_HISTORY_PATH)))
    parser.add_argument("--output-dir", type=Path, default=output_dir_from_env())
    parser.add_argument("--cap-bytes", type=int, default=cap_bytes_from_env())
    parser.add_argument("--max-width", type=int, default=int(os.getenv("SKY_IMAGE_MAX_WIDTH", "128")))
    parser.add_argument("--thumb-width", type=int, default=int(os.getenv("SKY_THUMB_MAX_WIDTH", "64")))
    parser.add_argument("--quality", type=int, default=int(os.getenv("SKY_IMAGE_QUALITY", "76")))
    parser.add_argument("--thumb-quality", type=int, default=int(os.getenv("SKY_THUMB_QUALITY", "68")))
    parser.add_argument("--sprite-tile-size", type=int, default=int(os.getenv("SKY_SPRITE_TILE_SIZE", str(DEFAULT_SPRITE_TILE_SIZE))))
    parser.add_argument("--sprite-columns", type=int, default=int(os.getenv("SKY_SPRITE_COLUMNS", str(DEFAULT_SPRITE_COLUMNS))))
    parser.add_argument("--sprite-quality", type=int, default=int(os.getenv("SKY_SPRITE_QUALITY", "72")))
    parser.add_argument("--no-sprites", action="store_true", default=not env_flag("SKY_SPRITES_ENABLED", True))
    parser.add_argument("--sprites-from-manifest", action="store_true", default=env_flag("SKY_SPRITES_FROM_MANIFEST"))
    parser.add_argument("--no-prune", action="store_true", default=not env_flag("SKY_PRUNE_ENABLED", True))
    parser.add_argument("--dry-run", action="store_true", default=env_flag("SKY_SYNC_DRY_RUN"))
    parser.add_argument("--commit", action="store_true", default=env_flag("SKY_GIT_COMMIT"))
    parser.add_argument("--branch", default=os.getenv("SKY_GIT_BRANCH", "main"))
    parser.add_argument("--message", default=os.getenv("SKY_GIT_MESSAGE", "Update sky media"))
    parser.add_argument("--min-captured-at", default=os.getenv("SKY_MIN_CAPTURED_AT"))
    return parser.parse_args()


def main() -> None:
    load_env_file(APP_ROOT / ".env")
    args = parse_args()
    if args.sprite_tile_size <= 0:
        raise RuntimeError("--sprite-tile-size must be positive")
    if args.sprite_columns <= 0:
        raise RuntimeError("--sprite-columns must be positive")

    min_captured_at = parse_captured_at(args.min_captured_at)
    if args.commit and not args.dry_run:
        pull_git_root(args.output_dir, args.branch)

    if args.sprites_from_manifest:
        manifest, removed_files = build_manifest_from_existing_output(
            output_dir=args.output_dir,
            tile_size=args.sprite_tile_size,
            sprite_columns=args.sprite_columns,
            sprite_quality=args.sprite_quality,
            dry_run=args.dry_run,
        )
        manifest, manifest_unchanged = preserve_existing_manifest_if_equivalent(args.output_dir, manifest)
        write_manifest(args.output_dir, manifest, args.dry_run)
        retention = manifest.get("retention", {})
        sprites = manifest.get("sprites", {})
        print(
            "retained={retainedCount} retained_bytes={retainedBytes} sprite_bytes={spriteBytes} sprite_weeks={weeks}".format(
                retainedCount=retention.get("retainedCount", 0),
                retainedBytes=retention.get("retainedBytes", 0),
                spriteBytes=retention.get("spriteBytes", 0),
                weeks=len(sprites.get("weeks", [])),
            )
        )
        if removed_files:
            print(f"removed stale files: {len(removed_files)}")
        if manifest_unchanged:
            print("manifest unchanged")
        if args.dry_run:
            print("dry run: no files written")
            return
        if args.commit:
            run_git_publish(args.output_dir, args.branch, args.message)
        return

    source_captures = load_captures(args.history_path, args.capture_dir)
    captures = filter_captures(source_captures, min_captured_at)
    manifest, removed_files, pruned = build_manifest(
        captures=captures,
        capture_dir=args.capture_dir,
        output_dir=args.output_dir,
        cap_bytes=args.cap_bytes,
        max_width=args.max_width,
        thumb_width=args.thumb_width,
        quality=args.quality,
        thumb_quality=args.thumb_quality,
        sprite_tile_size=args.sprite_tile_size,
        sprite_columns=args.sprite_columns,
        sprite_quality=args.sprite_quality,
        sprites_enabled=not args.no_sprites,
        prune=not args.no_prune,
        dry_run=args.dry_run,
    )
    manifest["retention"]["sourceCount"] = len(source_captures)
    manifest["retention"]["excludedBeforeCount"] = len(source_captures) - len(captures)
    manifest["retention"]["minCapturedAt"] = min_captured_at.isoformat().replace("+00:00", "Z") if min_captured_at else None
    manifest, manifest_unchanged = preserve_existing_manifest_if_equivalent(args.output_dir, manifest)
    write_manifest(args.output_dir, manifest, args.dry_run)

    retention = manifest["retention"]
    print(
        "retained={retainedCount} pruned={prunedCount} retained_bytes={retainedBytes} sprite_bytes={spriteBytes} cap_bytes={capBytes}".format(
            **retention
        )
    )
    if pruned:
        print(f"oldest pruned: {pruned[-1].timestamp}; newest pruned: {pruned[0].timestamp}")
    if removed_files:
        print(f"removed stale files: {len(removed_files)}")
    if manifest_unchanged:
        print("manifest unchanged")
    if args.dry_run:
        print("dry run: no files written")
        return
    if args.commit:
        run_git_publish(args.output_dir, args.branch, args.message)


if __name__ == "__main__":
    main()
