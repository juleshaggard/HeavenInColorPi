from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import fcntl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from flask import Flask, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageStat
import cloudinary
import cloudinary.api
import cloudinary.uploader


APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
CAPTURE_DIR = DATA_DIR / "captures"
STATE_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
CAPTURE_INTERVAL_SECONDS = 30 * 60
CLOUDINARY_MAX_IMAGES = 1000
RANGE_WINDOWS = {
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "quarter": timedelta(days=90),
    "year": timedelta(days=365),
    "all": None,
}
MEDIA_SYNC_LOCK_PATH = Path(os.getenv("SKY_MEDIA_SYNC_LOCK", "/tmp/skywatcher-media-sync.lock"))
MEDIA_SYNC_TIMEOUT_SECONDS = int(os.getenv("SKY_MEDIA_SYNC_TIMEOUT_SECONDS", "600"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def configure_cloudinary() -> bool:
    cloudinary_url = os.getenv("CLOUDINARY_URL")
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    if cloudinary_url:
        cloudinary.config(cloudinary_url=cloudinary_url, secure=True)
        logger.info("Cloudinary uploads enabled via CLOUDINARY_URL")
        return True

    if cloud_name and api_key and api_secret:
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        logger.info("Cloudinary uploads enabled via explicit credentials")
        return True

    logger.info("Cloudinary uploads disabled because no credentials were found")
    return False


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def media_sync_enabled() -> bool:
    default_enabled = bool(os.getenv("SKY_MEDIA_ROOT") or os.getenv("SKY_OUTPUT_DIR"))
    return env_flag("SKY_SYNC_AFTER_CAPTURE", default_enabled) and (APP_ROOT / "github_media_sync.py").exists()


def media_sync_command() -> list[str]:
    if os.getenv("SKY_MEDIA_SYNC_COMMAND"):
        return shlex.split(os.environ["SKY_MEDIA_SYNC_COMMAND"])
    return [sys.executable, str(APP_ROOT / "github_media_sync.py")]


def run_media_sync(reason: str) -> None:
    if not media_sync_enabled():
        return

    MEDIA_SYNC_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MEDIA_SYNC_LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("GitHub media sync already running; skipping %s sync", reason)
            return

        command = media_sync_command()
        logger.info("Running GitHub media sync after %s", reason)
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=MEDIA_SYNC_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.exception("GitHub media sync timed out after %s seconds", MEDIA_SYNC_TIMEOUT_SECONDS)
            return
        except Exception:
            logger.exception("GitHub media sync failed to start")
            return
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    for line in completed.stdout.splitlines():
        logger.info("media sync: %s", line)
    for line in completed.stderr.splitlines():
        logger.warning("media sync: %s", line)
    if completed.returncode != 0:
        logger.error("GitHub media sync failed with exit code %s", completed.returncode)


@dataclass
class CaptureState:
    timestamp: str
    image_name: str
    average_rgb: list[int]
    average_hex: str
    cloudinary_public_id: str | None = None
    cloudinary_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "image_name": self.image_name,
            "average_rgb": self.average_rgb,
            "average_hex": self.average_hex,
            "cloudinary_public_id": self.cloudinary_public_id,
            "cloudinary_url": self.cloudinary_url,
        }


def ensure_directories() -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def parse_timestamp(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def image_name_to_timestamp(image_name: str) -> str:
    return Path(image_name).stem


def camera_commands(output_path: Path) -> Iterable[list[str]]:
    common_args = [
        "--output",
        str(output_path),
        "--nopreview",
        "--timeout",
        "2000",
        "--width",
        "2304",
        "--height",
        "1296",
        "--quality",
        "85",
        "--lens-position",
        "0",
        "--awb",
        "daylight",
        "--ev",
        "0",
        "--hdr",
        "sensor",
    ]
    yield ["rpicam-still", *common_args]
    yield ["libcamera-still", *common_args]


def capture_photo(output_path: Path) -> None:
    last_error: Exception | None = None
    for command in camera_commands(output_path):
        try:
            logger.info("Capturing photo with %s", command[0])
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return
        except FileNotFoundError as exc:
            last_error = exc
        except subprocess.CalledProcessError as exc:
            last_error = RuntimeError(exc.stderr.decode("utf-8", errors="replace"))

    raise RuntimeError(f"Unable to capture a sky photo: {last_error}")


def compute_average_color(image_path: Path) -> tuple[list[int], str]:
    with Image.open(image_path) as image:
        rgb_image = image.convert("RGB")
        stat = ImageStat.Stat(rgb_image)
        avg = [round(channel) for channel in stat.mean[:3]]
    average_hex = "#{:02x}{:02x}{:02x}".format(*avg)
    return avg, average_hex


def save_state(state: CaptureState) -> None:
    STATE_PATH.write_text(json.dumps(state.to_dict(), indent=2))


def load_state() -> CaptureState | None:
    if not STATE_PATH.exists():
        return None

    raw = json.loads(STATE_PATH.read_text())
    return CaptureState(
        timestamp=raw["timestamp"],
        image_name=raw["image_name"],
        average_rgb=list(raw["average_rgb"]),
        average_hex=raw["average_hex"],
        cloudinary_public_id=raw.get("cloudinary_public_id"),
        cloudinary_url=raw.get("cloudinary_url"),
    )


def load_history() -> list[CaptureState]:
    if not HISTORY_PATH.exists():
        return []

    raw_items = json.loads(HISTORY_PATH.read_text())
    return [
        CaptureState(
            timestamp=item["timestamp"],
            image_name=item["image_name"],
            average_rgb=list(item["average_rgb"]),
            average_hex=item["average_hex"],
            cloudinary_public_id=item.get("cloudinary_public_id"),
            cloudinary_url=item.get("cloudinary_url"),
        )
        for item in raw_items
    ]


def save_history(history: list[CaptureState]) -> None:
    HISTORY_PATH.write_text(json.dumps([item.to_dict() for item in history], indent=2))


def upsert_history_item(state: CaptureState) -> list[CaptureState]:
    history = {item.timestamp: item for item in load_history()}
    history[state.timestamp] = state
    ordered_history = sorted(history.values(), key=lambda item: item.timestamp, reverse=True)
    save_history(ordered_history)
    return ordered_history


def prune_cloudinary_history(history: list[CaptureState]) -> list[CaptureState]:
    if not cloudinary_enabled:
        return history

    uploaded_history = [item for item in history if item.cloudinary_public_id]
    excess_items = uploaded_history[CLOUDINARY_MAX_IMAGES:]
    if not excess_items:
        return history

    public_ids = [item.cloudinary_public_id for item in excess_items if item.cloudinary_public_id]
    cleared_public_ids: set[str] = set()

    for start in range(0, len(public_ids), 100):
        batch = public_ids[start : start + 100]
        try:
            result = cloudinary.api.delete_resources(batch, resource_type="image", type="upload")
            deleted = result.get("deleted", {})
            for public_id, status in deleted.items():
                if status in {"deleted", "not_found"}:
                    cleared_public_ids.add(public_id)
            logger.info("Pruned %s older Cloudinary images", len(batch))
        except Exception:
            logger.exception("Cloudinary prune failed for batch starting at %s", start)

    if not cleared_public_ids:
        return history

    updated_history: list[CaptureState] = []
    for item in history:
        if item.cloudinary_public_id in cleared_public_ids:
            item.cloudinary_public_id = None
            item.cloudinary_url = None
        updated_history.append(item)

    save_history(updated_history)
    return updated_history


def hydrate_history_from_captures() -> list[CaptureState]:
    ensure_directories()
    indexed = {item.image_name: item for item in load_history()}

    changed = False
    for image_path in sorted(CAPTURE_DIR.glob("*.jpg")):
        if image_path.name in indexed:
            continue
        average_rgb, average_hex = compute_average_color(image_path)
        indexed[image_path.name] = CaptureState(
            timestamp=image_name_to_timestamp(image_path.name),
            image_name=image_path.name,
            average_rgb=average_rgb,
            average_hex=average_hex,
        )
        changed = True

    history = sorted(indexed.values(), key=lambda item: item.timestamp, reverse=True)
    if changed or not HISTORY_PATH.exists():
        save_history(history)

    if history and (load_state() is None or load_state().timestamp != history[0].timestamp):
        save_state(history[0])

    return history


def perform_capture() -> CaptureState:
    ensure_directories()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    image_name = f"{timestamp}.jpg"
    image_path = CAPTURE_DIR / image_name

    capture_photo(image_path)
    average_rgb, average_hex = compute_average_color(image_path)

    state = CaptureState(
        timestamp=timestamp,
        image_name=image_name,
        average_rgb=average_rgb,
        average_hex=average_hex,
    )
    state = upload_to_cloudinary(image_path, state)
    save_state(state)
    history = upsert_history_item(state)
    prune_cloudinary_history(history)
    logger.info("Saved capture %s with average color %s", image_name, average_hex)
    run_media_sync("capture")
    return state


def upload_to_cloudinary(image_path: Path, state: CaptureState) -> CaptureState:
    if not cloudinary_enabled:
        return state

    try:
        result = cloudinary.uploader.upload(
            str(image_path),
            folder="skywatcher",
            public_id=state.timestamp,
            overwrite=True,
            resource_type="image",
            tags=["skywatcher", "raspberry-pi", "sky"],
            context={
                "captured_at": state.timestamp,
                "average_hex": state.average_hex,
            },
        )
        state.cloudinary_public_id = result.get("public_id")
        state.cloudinary_url = result.get("secure_url") or result.get("url")
        logger.info("Uploaded %s to Cloudinary as %s", state.image_name, state.cloudinary_public_id)
    except Exception:
        logger.exception("Cloudinary upload failed for %s", state.image_name)

    return state


def serialize_capture(state: CaptureState) -> dict[str, object]:
    payload = state.to_dict()
    payload["image_url"] = f"/captures/{state.image_name}"
    return payload


def filter_history(history: list[CaptureState], range_name: str) -> list[CaptureState]:
    if range_name not in RANGE_WINDOWS:
        range_name = "month"

    window = RANGE_WINDOWS[range_name]
    if window is None:
        return history

    cutoff = datetime.now(timezone.utc) - window
    return [item for item in history if parse_timestamp(item.timestamp) >= cutoff]


def capture_loop() -> None:
    while True:
        if stop_event.wait(CAPTURE_INTERVAL_SECONDS):
            return
        try:
            perform_capture()
        except Exception:
            logger.exception("Scheduled capture failed")


app = Flask(__name__)
stop_event = threading.Event()
cloudinary_enabled = configure_cloudinary()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    state = load_state()
    history = load_history()
    if state is None:
        return jsonify({"ready": False})

    payload = serialize_capture(state)
    payload["ready"] = True
    payload["capture_count"] = len(history)
    return jsonify(payload)


@app.route("/api/history")
def history():
    range_name = request.args.get("range", "month")
    all_history = load_history()
    filtered = filter_history(all_history, range_name)
    return jsonify(
        {
            "range": range_name if range_name in RANGE_WINDOWS else "month",
            "count": len(filtered),
            "total_count": len(all_history),
            "captures": [serialize_capture(item) for item in filtered],
        }
    )


@app.route("/captures/<path:filename>")
def captures(filename: str):
    return send_from_directory(CAPTURE_DIR, filename)


def bootstrap_capture() -> None:
    history = hydrate_history_from_captures()
    if not history:
        logger.info("No prior capture found, taking an initial photo")
        perform_capture()


def main() -> None:
    ensure_directories()
    bootstrap_capture()
    threading.Thread(target=capture_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
