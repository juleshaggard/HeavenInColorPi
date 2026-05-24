# Skywatcher

Small Flask app for Raspberry Pi that:

- captures a sky photo every 30 minutes with `rpicam-still`
- computes the overall average color of the latest image
- optionally uploads each new capture to Cloudinary
- publishes GitHub-hosted site images with a rolling storage cap
- lets the viewer switch between the photo and a full-screen color swatch
- starts automatically on boot via `systemd`

The app listens on port `8080`.

To enable Cloudinary uploads, add credentials to `/home/haggy/skywatcher/.env`.
You can use either `CLOUDINARY_URL` or the three explicit variables shown in [.env.example](/Users/haggylap/Documents/Codex/2026-04-20-skywatcher-app-write-me-a-web/.env.example).

## GitHub media sync

`github_media_sync.py` converts captures into tiny web images, writes `public/sky/manifest.json`, and removes the oldest retained captures before adding new ones when the published image set would exceed the configured cap. By default, hosted site images are capped at 128px wide so the project can keep running for years.

Recommended settings in `/home/haggy/skywatcher/.env`:

```bash
SKY_MEDIA_ROOT=/home/haggy/heavenincolor
SKY_GIT_BRANCH=main
SKY_GIT_COMMIT=true
SKY_PUBLISHED_CAP_MIB=900
SKY_PRUNE_ENABLED=true
```

Run a dry check:

```bash
/home/haggy/skywatcher/.venv/bin/python /home/haggy/skywatcher/github_media_sync.py --dry-run
```

Run and commit from the configured site checkout:

```bash
/home/haggy/skywatcher/.venv/bin/python /home/haggy/skywatcher/github_media_sync.py --commit
```
