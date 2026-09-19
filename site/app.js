"use strict";

// Läser listings.json, ritar en pin per adress (fyllningen visar chansen, ringen
// visar sajten) och gråar pins som inte matchar filtren. Chansen (bucket) räknas
// ut av score.py, inte här. queue.json ger dina ködatum till kötidsrutan.

const STORAGE_KEY = "bostadsko.filters.v2";
const SOURCES = {
  boplats: { name: "Boplats", colour: "#1f6fb4" },
  homeq: { name: "HomeQ", colour: "#d9730d" },
};
const CHANCE = {
  likely: { label: "God chans", colour: "#1e7e3a", text: "#fff" },
  possible: { label: "Möjlig", colour: "#f2b705", text: "#1c1c1c" },
  unlikely: { label: "Låg chans", colour: "#c0392b", text: "#fff" },
};
const UNKNOWN_CHANCE = { label: "Okänd chans", colour: "#eef1f4", text: "#1c1c1c" };
const CHANCE_ORDER = ["likely", "possible", "unlikely"]; // bäst först
const chanceOf = (l) => CHANCE[l.bucket] ?? UNKNOWN_CHANCE;
const GREY = "#b8b8b8";
const AREA_STYLE = { color: "#6a3fb5", weight: 2, dashArray: "6 4", fillOpacity: 0.08 };
const SAFE_LINK = /^https:\/\/(www\.)?(boplats|homeq)\.se\//; // länka bara till de två sajter vi hämtar från
const FORM_FIELDS = [
  "maxRent", "minSize", "maxSize", "minRooms", "moveFrom", "moveTo",
  "srcBoplats", "srcHomeq", "allowFirstCome", "allowLottery", "shortLease", "hideOthers",
];
const AREA_HINT = "Rita ett eller flera områden. Då matchar bara annonser inuti områdena.";
const AREA_HINT_ACTIVE = "Bara annonser inuti områdena matchar. Annonser utan position matchar inte.";

const numberFormat = new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }); // 7 200 och 52,5

const $ = (id) => document.getElementById(id);
const esc = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// --- karta -----------------------------------------------------------------

const map = L.map("map").setView([57.7089, 11.9746], 11); // Göteborg
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-bidragsgivare',
}).addTo(map);
const areaLayer = L.layerGroup().addTo(map);
const pinLayer = L.layerGroup().addTo(map);

let listings = [];
let groups = []; // en per adress: { lat, lon, listings: [...], marker }
let unplaced = []; // annonser utan koordinater
let areas = []; // ritade områden: varje område är en lista av [lat, lon]
let drawing = null; // pågående ritning: { points, preview }

// --- filter ----------------------------------------------------------------

function readFilters() {
  const number = (id) => {
    const value = $(id).value.trim();
    return value === "" || Number.isNaN(Number(value)) ? null : Number(value);
  };
  return {
    maxRent: number("maxRent"),
    minSize: number("minSize"),
    maxSize: number("maxSize"),
    minRooms: number("minRooms"),
    moveFrom: $("moveFrom").value || null, // ÅÅÅÅ-MM-DD, går att jämföra som text
    moveTo: $("moveTo").value || null,
    sources: { boplats: $("srcBoplats").checked, homeq: $("srcHomeq").checked },
    allowFirstCome: $("allowFirstCome").checked,
    allowLottery: $("allowLottery").checked,
    shortLease: $("shortLease").value,
    kommunOff: new Set(
      [...document.querySelectorAll("#kommunList input")].filter((box) => !box.checked).map((box) => box.dataset.kommun)
    ),
    areas,
    hideOthers: $("hideOthers").checked,
  };
}

// Punkt i polygon (strålmetoden): räkna hur många kanter en stråle åt öster korsar.
function inPolygon(lat, lon, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [latI, lonI] = polygon[i];
    const [latJ, lonJ] = polygon[j];
    if (latI > lat !== latJ > lat && lon < ((lonJ - lonI) * (lat - latI)) / (latJ - latI) + lonI) inside = !inside;
  }
  return inside;
}

// Ett värde vi inte känner till (null) utesluter aldrig en annons: vi kan inte veta.
// Undantag: när du ritat områden måste annonsen ha en position inuti något av dem.
function matches(l, f) {
  if (!f.sources[l.source]) return false;
  if (f.shortLease === "exclude" && l.is_short_lease === 1) return false;
  if (f.shortLease === "only" && l.is_short_lease !== 1) return false;
  if (!f.allowFirstCome && l.allocation === "first_come") return false;
  if (!f.allowLottery && l.allocation === "lottery") return false;
  if (f.maxRent != null && l.rent_sek != null && l.rent_sek > f.maxRent) return false;
  if (f.minSize != null && l.size_m2 != null && l.size_m2 < f.minSize) return false;
  if (f.maxSize != null && l.size_m2 != null && l.size_m2 > f.maxSize) return false;
  if (f.minRooms != null && l.rooms != null && l.rooms < f.minRooms) return false;
  if (f.moveFrom && l.move_in && l.move_in < f.moveFrom) return false;
  if (f.moveTo && l.move_in && l.move_in > f.moveTo) return false;
  if (l.kommun && f.kommunOff.has(l.kommun)) return false;
  if (f.areas.length) {
    if (l.lat == null || l.lon == null) return false;
    if (!f.areas.some((polygon) => inPolygon(l.lat, l.lon, polygon))) return false;
  }
  return true;
}

function saveFilters() {
  try {
    const state = { areas, kommunOff: [...readFilters().kommunOff] };
    for (const id of FORM_FIELDS) state[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) { /* privat läge eller blockerad lagring: filtren kommer bara inte ihåg */ }
}

const isPolygon = (p) =>
  Array.isArray(p) && p.length >= 3 && p.every((pt) => Array.isArray(pt) && pt.length === 2 && pt.every(Number.isFinite));

function restoreFilters() {
  try {
    const state = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!state) return;
    for (const id of FORM_FIELDS) {
      if (!(id in state)) continue;
      if ($(id).type === "checkbox") $(id).checked = Boolean(state[id]);
      else $(id).value = state[id];
    }
    if (Array.isArray(state.kommunOff)) {
      for (const box of document.querySelectorAll("#kommunList input")) box.checked = !state.kommunOff.includes(box.dataset.kommun);
    }
    if (Array.isArray(state.areas)) areas = state.areas.filter(isPolygon);
  } catch (e) { /* strunta i ett trasigt sparat värde */ }
}

function buildKommunList() {
  const counts = new Map();
  for (const l of listings) if (l.kommun) counts.set(l.kommun, (counts.get(l.kommun) ?? 0) + 1);
  const sorted = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "sv"));
  $("kommunList").innerHTML = sorted
    .map(([name, n]) => `<label><input type="checkbox" checked data-kommun="${esc(name)}">${esc(name)} <span class="count">(${n})</span></label>`)
    .join("");
}

// --- ritade områden ---------------------------------------------------------

function renderAreas() {
  areaLayer.clearLayers();
  for (const polygon of areas) {
    L.polygon(polygon, { ...AREA_STYLE, interactive: false }).addTo(areaLayer).bringToBack();
  }
  $("areaList").innerHTML = areas
    .map((_, i) => `<li><span>Område ${i + 1}</span><button type="button" class="small" data-remove="${i}">Ta bort</button></li>`)
    .join("");
  if (!drawing) setAreaHint(areas.length ? AREA_HINT_ACTIVE : AREA_HINT);
}

function setAreaHint(text, warn = false) {
  $("areaHint").textContent = text;
  $("areaHint").classList.toggle("warn", warn);
}

function setDrawingUi(on) {
  $("drawStart").hidden = on;
  $("drawDone").hidden = !on;
  $("drawCancel").hidden = !on;
  $("map").classList.toggle("drawing", on);
}

function startDrawing() {
  if (drawing) return;
  map.closePopup();
  drawing = { points: [], preview: L.polygon([], { ...AREA_STYLE, interactive: false }).addTo(map) };
  map.doubleClickZoom.disable(); // två snabba klick ska sätta två hörn, inte zooma
  map.on("click", onDrawClick);
  setDrawingUi(true);
  setAreaHint("Klicka på kartan för att sätta ut hörn (minst 3). Tryck Klar när du är färdig, eller Esc för att avbryta.");
}

function onDrawClick(event) {
  drawing.points.push([event.latlng.lat, event.latlng.lng]);
  drawing.preview.setLatLngs(drawing.points);
  setAreaHint(`${drawing.points.length} hörn utsatta (minst 3). Tryck Klar när du är färdig, eller Esc för att avbryta.`);
}

function stopDrawing(save) {
  if (!drawing) return;
  if (save && drawing.points.length < 3) {
    setAreaHint("Ett område behöver minst 3 hörn. Klicka på kartan för att sätta ut fler.", true);
    return;
  }
  map.off("click", onDrawClick);
  map.removeLayer(drawing.preview);
  if (save) areas.push(drawing.points);
  drawing = null;
  map.doubleClickZoom.enable();
  setDrawingUi(false);
  renderAreas();
  apply();
}

// --- pins ------------------------------------------------------------------

// Pinnens fyllning = bästa chansen bland annonserna på adressen.
function pinColour(hits) {
  const best = CHANCE_ORDER.find((bucket) => hits.some((l) => l.bucket === bucket));
  return (CHANCE[best] ?? UNKNOWN_CHANCE).colour;
}

function pinStyle(group, f) {
  const hits = group.listings.filter((l) => matches(l, f));
  const radius = 6 + Math.min(group.listings.length - 1, 4) * 1.5;
  if (hits.length === 0) {
    return { radius, color: "#9a9a9a", weight: 1, fillColor: GREY, fillOpacity: 0.5, dashArray: null, hits };
  }
  const ring = SOURCES[hits[0].source]?.colour ?? "#555"; // ringen visar sajten
  const dashArray = hits.every((l) => l.is_short_lease === 1) ? "3 3" : null; // streckad ring = korttidskontrakt
  return { radius, color: ring, weight: 3, fillColor: pinColour(hits), fillOpacity: 0.95, dashArray, hits };
}

function buildGroups() {
  const byPlace = new Map();
  for (const l of listings) {
    if (l.lat == null || l.lon == null) { unplaced.push(l); continue; }
    const key = `${l.lat.toFixed(5)},${l.lon.toFixed(5)}`; // samma byggnad = samma nyckel
    if (!byPlace.has(key)) byPlace.set(key, { lat: l.lat, lon: l.lon, listings: [], marker: null });
    byPlace.get(key).listings.push(l);
  }
  groups = [...byPlace.values()];
  for (const group of groups) {
    group.marker = L.circleMarker([group.lat, group.lon], { radius: 7 });
    group.marker.bindPopup(() => popupHtml(group), { minWidth: 270, maxWidth: 340, maxHeight: 380 });
    const n = group.listings.length;
    group.marker.bindTooltip(n === 1 ? esc(group.listings[0].address) : `${n} annonser · ${esc(group.listings[0].address)}`);
  }
}

// --- kort i popupen ---------------------------------------------------------

const kr = (v) => (v == null ? null : `${numberFormat.format(v)} kr/mån`);
const m2 = (v) => (v == null ? null : `${numberFormat.format(v)} m²`);
const rooms = (v) => (v == null ? null : `${numberFormat.format(v)} rum`);
const days = (v) => (v == null ? null : `${numberFormat.format(v)} dagar`);

function allocationLabel(l) {
  switch (l.allocation) {
    case "queue": return l.source === "homeq" ? "Köpoäng" : "Kötid";
    case "queue_guidance": return "Köpoäng (vägledande)";
    case "points_landlord": return "Hyresvärdens poäng";
    case "first_come": return "Först till kvarn";
    case "lottery": return "Lottning";
    default: return null; // okänd
  }
}

function cardHtml(l, f) {
  const source = SOURCES[l.source];
  const rows = [
    ["Hyra", kr(l.rent_sek)],
    ["Storlek", m2(l.size_m2)],
    ["Rum", rooms(l.rooms)],
    ["Våning", l.floor],
    ["Inflyttning", l.move_in],
    ["Sök senast", l.deadline],
    ["Publicerad", l.published],
    ["Sökande", l.applicants],
    ["Tilldelning", allocationLabel(l)],
    ["Vinnarnas kötid", days(l.winners_queue_days)], // kötid för vinnarna av liknande lägenheter
    ["Hyresvärd", l.landlord],
  ].filter(([, value]) => value != null && value !== "");

  const place = [l.area, l.kommun].filter((p, i, all) => p && all.indexOf(p) === i).join(", ");
  const link = SAFE_LINK.test(l.url || "")
    ? `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">Öppna hos ${esc(source?.name ?? l.source)} ↗</a>`
    : "";
  return `
    <div class="card${matches(l, f) ? "" : " dim"}">
      <div class="title">${esc(l.address ?? "(adress saknas)")}${l.is_short_lease === 1 ? '<span class="badge">Korttidskontrakt</span>' : ""}</div>
      <div class="sub">${esc(place)}${place ? " · " : ""}<span class="source ${esc(l.source)}">${esc(source?.name ?? l.source)}</span></div>
      <div class="chance">
        <span class="chip" style="background:${chanceOf(l).colour};color:${chanceOf(l).text}">${esc(chanceOf(l).label)}</span>
        <span class="note">${esc(l.bucket_note ?? "")}</span>
      </div>
      <dl>${rows.map(([label, value]) => `<dt>${esc(label)}</dt><dd${label === "Hyresvärd" ? ' class="wrap"' : ""}>${esc(value)}</dd>`).join("")}</dl>
      ${link}
    </div>`;
}

function popupHtml(group) {
  const f = readFilters();
  const header = group.listings.length > 1 ? `<p class="hint">${group.listings.length} annonser på denna adress</p>` : "";
  return header + group.listings.map((l) => cardHtml(l, f)).join("");
}

// --- ritning ------------------------------------------------------------------

function apply() {
  const f = readFilters();
  pinLayer.clearLayers();
  let matching = 0;
  for (const group of groups) {
    const { hits, ...style } = pinStyle(group, f);
    matching += hits.length;
    if (hits.length === 0 && f.hideOthers) continue;
    group.marker.setStyle(style);
    pinLayer.addLayer(group.marker);
    if (hits.length) group.marker.bringToFront(); // grå pins ligger under
  }
  const unplacedHits = unplaced.filter((l) => matches(l, f));
  matching += unplacedHits.length;

  $("summary").textContent = `${matching} av ${listings.length} annonser matchar`;
  $("unplaced").hidden = unplacedHits.length === 0;
  $("unplacedSummary").textContent =
    unplacedHits.length === 1 ? "1 matchande annons finns inte på kartan" : `${unplacedHits.length} matchande annonser finns inte på kartan`;
  $("unplacedList").innerHTML = unplacedHits
    .map((l) => {
      const label = esc(`${l.address ?? "(adress saknas)"}, ${l.kommun ?? ""} (${SOURCES[l.source]?.name ?? l.source})`);
      return SAFE_LINK.test(l.url || "") ? `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${label}</a></li>` : `<li>${label}</li>`;
    })
    .join("");
  saveFilters();
}

// --- din kötid ----------------------------------------------------------------

// Datum som dagnummer (UTC), så att skillnaden blir hela dagar oavsett tidszon och sommartid.
const dayNumber = (iso) => {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d) / 86400000;
};
const todayNumber = () => {
  const now = new Date();
  return Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86400000;
};

function renderQueue(queue) {
  const queues = [
    { name: "Boplats", start: queue.boplats_start, unit: "dagar" },
    { name: "HomeQ", start: queue.homeq_start, unit: "poäng" },
  ];
  const on = $("queueDate").value ? dayNumber($("queueDate").value) : null;

  $("queueNow").innerHTML = queues
    .map((q) =>
      q.start
        ? `<li>${esc(q.name)}: <strong>${numberFormat.format(todayNumber() - dayNumber(q.start))} ${q.unit}</strong> <span class="count">(sedan ${esc(q.start)})</span></li>`
        : `<li>${esc(q.name)}: datum saknas (fyll i me.local.json)</li>`
    )
    .join("");

  if (on == null) {
    $("queueLater").textContent = "";
  } else if (on < todayNumber()) {
    $("queueLater").textContent = "Välj ett datum som inte har passerat.";
  } else {
    $("queueLater").textContent = queues
      .filter((q) => q.start)
      .map((q) => `${q.name} ${numberFormat.format(on - dayNumber(q.start))} ${q.unit}`)
      .join(" · ");
  }
}

function initQueue() {
  fetch("queue.json", { cache: "no-cache" })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((queue) => {
      renderQueue(queue);
      $("queueDate").addEventListener("input", () => renderQueue(queue));
    })
    .catch((error) => {
      $("queueNow").innerHTML = `<li class="warn">Kunde inte läsa queue.json (${esc(error.message)}).</li>`;
    });
}

function showStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

function init(data) {
  if (!Array.isArray(data)) throw new Error("listings.json är ingen lista");
  listings = data;
  buildGroups();
  buildKommunList();

  const newest = listings.map((l) => l.last_seen).filter(Boolean).sort().pop();
  const updated = newest ? new Date(newest).toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "short" }) : "okänt";
  showStatus(`${listings.length} aktiva annonser · uppdaterad ${updated}`);

  restoreFilters();
  renderAreas();

  $("filters").addEventListener("input", apply);
  $("drawStart").addEventListener("click", startDrawing);
  $("drawDone").addEventListener("click", () => stopDrawing(true));
  $("drawCancel").addEventListener("click", () => stopDrawing(false));
  document.addEventListener("keydown", (event) => event.key === "Escape" && stopDrawing(false));
  $("areaList").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-remove]");
    if (!button) return;
    areas.splice(Number(button.dataset.remove), 1);
    renderAreas();
    apply();
  });
  $("resetFilters").addEventListener("click", () => {
    stopDrawing(false);
    $("filters").reset(); // sätter tillbaka fälten och kommunrutorna (knappen får inte heta "reset": då skuggar den metoden)
    areas = [];
    renderAreas();
    apply();
  });
  apply();
}

initQueue();
fetch("listings.json", { cache: "no-cache" })
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then(init)
  .catch((error) =>
    showStatus(
      `Kunde inte läsa listings.json (${error.message}). Om du öppnade index.html direkt från mappen, ` +
        "kör: python -m http.server -d site 8000 och öppna http://localhost:8000",
      true
    )
  );
