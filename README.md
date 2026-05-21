# Skywatcher

Small Flask app for Raspberry Pi that:

- captures a sky photo every 30 minutes with `rpicam-still`
- computes the overall average color of the latest image
- optionally uploads each new capture to Cloudinary
- lets the viewer switch between the photo and a full-screen color swatch
- starts automatically on boot via `systemd`

The app listens on port `8080`.

To enable Cloudinary uploads, add credentials to `/home/haggy/skywatcher/.env`.
You can use either `CLOUDINARY_URL` or the three explicit variables shown in [.env.example](/Users/haggylap/Documents/Codex/2026-04-20-skywatcher-app-write-me-a-web/.env.example).
