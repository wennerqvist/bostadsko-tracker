"use strict";

// Reads listings.json, draws one pin per address, and greys out pins that don't
// match the filters. To colour pins by chance score later, change pinColour().

const STORAGE_KEY = "bostadsko.filters.v1";
const SOURCES = {
  boplats: { name: "Boplats", colour: "#1f6fb4" },
  homeq: { name: "HomeQ", colour: "#d9730d" },
};
const GREY = "#b8b8b8";
const SAFE_LINK = /^https:\/\/(www\.)?(boplats|homeq)\.se\//; // only link to the two sites we collect from
const FORM_FIELDS = ["maxRent", "minSize", "maxSize", "minRooms", "srcBoplats", "srcHomeq", "shortLease", "hideOthers"];

const numberFormat = new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }); // 7 200 and 52,5

const $ = (id) => document.getElementById(id);
const esc = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// --- map -------------------------------------------------------------------

const map = L.map("map").setView([57.7089, 11.9746], 11); // Göteborg
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
const pinLayer = L.layerGroup().addTo(map);

let listings = [];
let groups = []; // one per address: { lat, lon, listings: [...], marker }
let unplaced = []; // listings with no coordinates

// --- filters ---------------------------------------------------------------

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
    sources: { boplats: $("srcBoplats").checked, homeq: $("srcHomeq").checked },
    shortLease: $("shortLease").value,
    hideOthers: $("hideOthers").checked,
  };
}

// A value we don't know (null) never rules a listing out: we can't tell.
function matches(l, f) {
  if (!f.sources[l.source]) return false;
  if (f.shortLease === "exclude" && l.is_short_lease === 1) return false;
  if (f.shortLease === "only" && l.is_short_lease !== 1) return false;
  if (f.maxRent != null && l.rent_sek != null && l.rent_sek > f.maxRent) return false;
  if (f.minSize != null && l.size_m2 != null && l.size_m2 < f.minSize) return false;
  if (f.maxSize != null && l.size_m2 != null && l.size_m2 > f.maxSize) return false;
  if (f.minRooms != null && l.rooms != null && l.rooms < f.minRooms) return false;
  return true;
}

function saveFilters() {
  try {
    const state = {};
    for (const id of FORM_FIELDS) state[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) { /* private mode or blocked storage: filters just won't be remembered */ }
}

function restoreFilters() {
  try {
    const state = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!state) return;
    for (const id of FORM_FIELDS) {
      if (!(id in state)) continue;
      if ($(id).type === "checkbox") $(id).checked = Boolean(state[id]);
      else $(id).value = state[id];
    }
  } catch (e) { /* ignore a broken saved value */ }
}

// --- pins ------------------------------------------------------------------

function pinColour(hits) {
  return SOURCES[hits[0].source]?.colour ?? GREY;
}

function pinStyle(group, f) {
  const hits = group.listings.filter((l) => matches(l, f));
  const radius = 6 + Math.min(group.listings.length - 1, 4) * 1.5;
  if (hits.length === 0) {
    return { radius, color: "#9a9a9a", weight: 1, fillColor: GREY, fillOpacity: 0.5, dashArray: null, hits };
  }
  const colour = pinColour(hits);
  if (hits.every((l) => l.is_short_lease === 1)) { // hollow, dashed ring
    return { radius, color: colour, weight: 3, fillColor: colour, fillOpacity: 0.2, dashArray: "3 3", hits };
  }
  return { radius, color: "#fff", weight: 1.5, fillColor: colour, fillOpacity: 0.88, dashArray: null, hits };
}

function buildGroups() {
  const byPlace = new Map();
  for (const l of listings) {
    if (l.lat == null || l.lon == null) { unplaced.push(l); continue; }
    const key = `${l.lat.toFixed(5)},${l.lon.toFixed(5)}`; // same building = same key
    if (!byPlace.has(key)) byPlace.set(key, { lat: l.lat, lon: l.lon, listings: [], marker: null });
    byPlace.get(key).listings.push(l);
  }
  groups = [...byPlace.values()];
  for (const group of groups) {
    group.marker = L.circleMarker([group.lat, group.lon], { radius: 7 });
    group.marker.bindPopup(() => popupHtml(group), { minWidth: 270, maxWidth: 340, maxHeight: 380 });
    const n = group.listings.length;
    group.marker.bindTooltip(n === 1 ? esc(group.listings[0].address) : `${n} listings · ${esc(group.listings[0].address)}`);
  }
}

// --- popup cards -----------------------------------------------------------

const kr = (v) => (v == null ? null : `${numberFormat.format(v)} kr/mån`);
const m2 = (v) => (v == null ? null : `${numberFormat.format(v)} m²`);
const rooms = (v) => (v == null ? null : `${numberFormat.format(v)} rum`);
const days = (v) => (v == null ? null : `${numberFormat.format(v)} days`);

function cardHtml(l, f) {
  const source = SOURCES[l.source];
  const rows = [
    ["Rent", kr(l.rent_sek)],
    ["Size", m2(l.size_m2)],
    ["Rooms", rooms(l.rooms)],
    ["Floor", l.floor],
    ["Move-in", l.move_in],
    ["Apply by", l.deadline],
    ["Applicants", l.applicants],
    ["Winners' queue", days(l.winners_queue_days)], // queue time of similar flats' winners
    ["Landlord", l.landlord],
  ].filter(([, value]) => value != null && value !== "");

  const place = [l.area, l.kommun].filter((p, i, all) => p && all.indexOf(p) === i).join(", ");
  const link = SAFE_LINK.test(l.url || "")
    ? `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">Open on ${esc(source?.name ?? l.source)} ↗</a>`
    : "";
  return `
    <div class="card${matches(l, f) ? "" : " dim"}">
      <div class="title">${esc(l.address ?? "(no address)")}${l.is_short_lease === 1 ? '<span class="badge">Short lease</span>' : ""}</div>
      <div class="sub">${esc(place)}${place ? " · " : ""}<span class="source ${esc(l.source)}">${esc(source?.name ?? l.source)}</span></div>
      <dl>${rows.map(([label, value]) => `<dt>${esc(label)}</dt><dd${label === "Landlord" ? ' class="wrap"' : ""}>${esc(value)}</dd>`).join("")}</dl>
      ${link}
    </div>`;
}

function popupHtml(group) {
  const f = readFilters();
  const header = group.listings.length > 1 ? `<p class="hint">${group.listings.length} listings at this address</p>` : "";
  return header + group.listings.map((l) => cardHtml(l, f)).join("");
}

// --- drawing ---------------------------------------------------------------

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
    if (hits.length) group.marker.bringToFront(); // grey pins stay underneath
  }
  const unplacedHits = unplaced.filter((l) => matches(l, f));
  matching += unplacedHits.length;

  $("summary").textContent = `${matching} of ${listings.length} listings match`;
  $("unplaced").hidden = unplacedHits.length === 0;
  $("unplacedSummary").textContent = `${unplacedHits.length} matching ${unplacedHits.length === 1 ? "listing is" : "listings are"} not on the map`;
  $("unplacedList").innerHTML = unplacedHits
    .map((l) => {
      const label = esc(`${l.address ?? "(no address)"}, ${l.kommun ?? ""} (${SOURCES[l.source]?.name ?? l.source})`);
      return SAFE_LINK.test(l.url || "") ? `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${label}</a></li>` : `<li>${label}</li>`;
    })
    .join("");
  saveFilters();
}

function showStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

function init(data) {
  if (!Array.isArray(data)) throw new Error("listings.json is not a list");
  listings = data;
  buildGroups();

  const newest = listings.map((l) => l.last_seen).filter(Boolean).sort().pop();
  const updated = newest ? new Date(newest).toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "short" }) : "unknown";
  showStatus(`${listings.length} active listings · updated ${updated}`);

  restoreFilters();
  $("filters").addEventListener("input", apply);
  $("reset").addEventListener("click", () => {
    $("filters").reset();
    apply();
  });
  apply();
}

fetch("listings.json", { cache: "no-cache" })
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then(init)
  .catch((error) =>
    showStatus(
      `Could not load listings.json (${error.message}). If you opened index.html straight from the folder, ` +
        "run: python -m http.server -d site 8000 and open http://localhost:8000",
      true
    )
  );
