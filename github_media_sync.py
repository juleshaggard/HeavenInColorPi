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

from PIL import Image, ImageOps


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CAPTURE_DIR = APP_ROOT / "data" / "captures"
DEFAULT_HISTORY_PATH = APP_ROOT / "data" / "history.json"
DEFAULT_CAP_BYTES = 900 * 1024 * 1024


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


def probe_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


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
    for folder in (output_dir / "images", output_dir / "thumbs"):
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


def build_manifest(
    captures: list[Capture],
    capture_dir: Path,
    output_dir: Path,
    cap_bytes: int,
    max_width: int,
    thumb_width: int,
    quality: int,
    thumb_quality: int,
    prune: bool,
    dry_run: bool,
) -> tuple[dict[str, Any], list[str], list[Capture]]:
    retained: list[dict[str, Any]] = []
    pruned: list[Capture] = []
    retained_paths: set[Path] = set()
    retained_bytes = 0

    for capture in reversed(captures):
        source_path = capture_dir / capture.image_name
        image_rel = f"images/{capture.year}/{capture.month}/{capture.timestamp}.jpg"
        thumb_rel = f"thumbs/{capture.year}/{capture.month}/{capture.timestamp}.jpg"
        image_info = write_derivative(source_path, output_dir, image_rel, max_width, quality, dry_run)
        thumb_info = write_derivative(source_path, output_dir, thumb_rel, thumb_width, thumb_quality, dry_run)
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
        retained_paths.add(output_dir / image_rel)
        retained_paths.add(output_dir / thumb_rel)
        retained.append(
            {
                "id": capture.timestamp,
                "capturedAt": capture.captured_at.isoformat().replace("+00:00", "Z"),
                "imageUrl": image_info.rel_path,
                "thumbUrl": thumb_info.rel_path,
                "averageHex": capture.average_hex,
                "width": image_info.width,
                "height": image_info.height,
                "bytes": image_info.bytes,
                "thumbBytes": thumb_info.bytes,
            }
        )

    retained.reverse()
    removed_files = prune_unretained_files(output_dir, retained_paths, dry_run)
    manifest = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retention": {
            "capBytes": cap_bytes,
            "retainedBytes": retained_bytes,
            "retainedCount": len(retained),
            "sourceCount": len(captures),
            "prunedCount": len(pruned),
            "oldestCapturedAt": retained[0]["capturedAt"] if retained else None,
            "newestCapturedAt": retained[-1]["capturedAt"] if retained else None,
        },
        "images": retained,
    }
    return manifest, removed_files, pruned


def find_git_root(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


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
    parser.add_argument("--no-prune", action="store_true", default=not env_flag("SKY_PRUNE_ENABLED", True))
    parser.add_argument("--dry-run", action="store_true", default=env_flag("SKY_SYNC_DRY_RUN"))
    parser.add_argument("--commit", action="store_true", default=env_flag("SKY_GIT_COMMIT"))
    parser.add_argument("--branch", default=os.getenv("SKY_GIT_BRANCH", "main"))
    parser.add_argument("--message", default=os.getenv("SKY_GIT_MESSAGE", "Update sky media"))
    return parser.parse_args()


def main() -> None:
    load_env_file(APP_ROOT / ".env")
    args = parse_args()
    captures = load_captures(args.history_path, args.capture_dir)
    manifest, removed_files, pruned = build_manifest(
        captures=captures,
        capture_dir=args.capture_dir,
        output_dir=args.output_dir,
        cap_bytes=args.cap_bytes,
        max_width=args.max_width,
        thumb_width=args.thumb_width,
        quality=args.quality,
        thumb_quality=args.thumb_quality,
        prune=not args.no_prune,
        dry_run=args.dry_run,
    )
    write_manifest(args.output_dir, manifest, args.dry_run)

    retention = manifest["retention"]
    print(
        "retained={retainedCount} pruned={prunedCount} retained_bytes={retainedBytes} cap_bytes={capBytes}".format(
            **retention
        )
    )
    if pruned:
        print(f"oldest pruned: {pruned[-1].timestamp}; newest pruned: {pruned[0].timestamp}")
    if removed_files:
        print(f"removed stale files: {len(removed_files)}")
    if args.dry_run:
        print("dry run: no files written")
        return
    if args.commit:
        run_git_publish(args.output_dir, args.branch, args.message)


if __name__ == "__main__":
    main()
