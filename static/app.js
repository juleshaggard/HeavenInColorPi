const toggles = document.querySelectorAll(".toggle");
const rangeToggles = document.querySelectorAll(".range-toggle");
const photoView = document.getElementById("photo-view");
const colorView = document.getElementById("color-view");
const skyImage = document.getElementById("sky-image");
const colorSwatch = document.getElementById("color-swatch");
const hexValue = document.getElementById("hex-value");
const swatchHex = document.getElementById("swatch-hex");
const timeValue = document.getElementById("time-value");
const captureCount = document.getElementById("capture-count");
const selectedLabel = document.getElementById("selected-label");
const galleryGrid = document.getElementById("gallery-grid");
const galleryCardTemplate = document.getElementById("gallery-card-template");
const rangeTitle = document.getElementById("range-title");
const rangeCount = document.getElementById("range-count");

const rangeLabels = {
  week: "Showing past week",
  month: "Showing past month",
  quarter: "Showing past 3 months",
  year: "Showing past year",
  all: "Showing all captures",
};

let activeView = "photo";
let activeRange = "week";
let selectedTimestamp = null;
let latestTimestamp = null;
let galleryItems = [];

function parseTimestamp(rawTimestamp) {
  return new Date(rawTimestamp.replace(
    /(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/,
    "$1-$2-$3T$4:$5:$6Z",
  ));
}

function formatTimestamp(rawTimestamp, options) {
  return parseTimestamp(rawTimestamp).toLocaleString([], options);
}

function setView(view) {
  activeView = view;
  const showPhoto = view === "photo";
  photoView.classList.toggle("active", showPhoto);
  colorView.classList.toggle("active", !showPhoto);
  toggles.forEach((toggle) => {
    toggle.classList.toggle("active", toggle.dataset.view === view);
  });
}

function focusCapture(capture) {
  if (!capture) {
    return;
  }

  selectedTimestamp = capture.timestamp;
  skyImage.src = `${capture.image_url}?ts=${encodeURIComponent(capture.timestamp)}`;
  colorSwatch.style.background = capture.average_hex;
  hexValue.textContent = capture.average_hex;
  swatchHex.textContent = capture.average_hex;
  timeValue.textContent = formatTimestamp(capture.timestamp, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  selectedLabel.textContent = formatTimestamp(capture.timestamp, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

  document.querySelectorAll(".gallery-card").forEach((card) => {
    card.classList.toggle("selected", card.dataset.timestamp === selectedTimestamp);
  });
}

function renderEmptyState() {
  galleryGrid.innerHTML = '<div class="gallery-empty">No captures found for this range yet.</div>';
}

function renderGallery(captures) {
  galleryGrid.innerHTML = "";

  if (!captures.length) {
    renderEmptyState();
    return;
  }

  const fragment = document.createDocumentFragment();

  captures.forEach((capture) => {
    const card = galleryCardTemplate.content.firstElementChild.cloneNode(true);
    const thumb = card.querySelector(".gallery-thumb");
    const swatch = card.querySelector(".gallery-swatch");
    const time = card.querySelector(".gallery-time");
    const date = card.querySelector(".gallery-date");

    card.dataset.timestamp = capture.timestamp;
    thumb.src = `${capture.image_url}?thumb=${encodeURIComponent(capture.timestamp)}`;
    thumb.alt = `Sky capture from ${formatTimestamp(capture.timestamp, { dateStyle: "medium", timeStyle: "short" })}`;
    swatch.style.background = capture.average_hex;
    time.textContent = formatTimestamp(capture.timestamp, {
      hour: "numeric",
      minute: "2-digit",
    });
    date.textContent = formatTimestamp(capture.timestamp, {
      month: "short",
      day: "numeric",
    });

    card.addEventListener("click", () => focusCapture(capture));
    fragment.appendChild(card);
  });

  galleryGrid.appendChild(fragment);

  const nextFocus = captures.find((item) => item.timestamp === selectedTimestamp) || captures[0];
  focusCapture(nextFocus);
}

async function refreshStatus() {
  const response = await fetch("/api/status", { cache: "no-store" });
  const payload = await response.json();

  if (!payload.ready) {
    return;
  }

  latestTimestamp = payload.timestamp;
  captureCount.textContent = `${payload.capture_count} capture${payload.capture_count === 1 ? "" : "s"}`;

  if (!selectedTimestamp) {
    focusCapture(payload);
  }
}

async function refreshHistory(rangeName = activeRange) {
  activeRange = rangeName;
  rangeToggles.forEach((toggle) => {
    toggle.classList.toggle("active", toggle.dataset.range === activeRange);
  });

  const response = await fetch(`/api/history?range=${encodeURIComponent(activeRange)}`, {
    cache: "no-store",
  });
  const payload = await response.json();
  galleryItems = payload.captures;

  rangeTitle.textContent = rangeLabels[payload.range] || rangeLabels.month;
  rangeCount.textContent = `${payload.count} of ${payload.total_count} captures`;
  renderGallery(galleryItems);
}

toggles.forEach((toggle) => {
  toggle.addEventListener("click", () => setView(toggle.dataset.view));
});

rangeToggles.forEach((toggle) => {
  toggle.addEventListener("click", () => refreshHistory(toggle.dataset.range));
});

setView("photo");

Promise.all([refreshStatus(), refreshHistory(activeRange)]).then(() => {
  if (latestTimestamp && !selectedTimestamp) {
    const latestCapture = galleryItems.find((item) => item.timestamp === latestTimestamp);
    focusCapture(latestCapture);
  }
});

setInterval(() => {
  refreshStatus();
  refreshHistory(activeRange);
}, 30_000);
