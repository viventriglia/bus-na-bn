"use strict";

const CSV_URL = "orari_eav_air.csv";
const DAY_MINUTES = 24 * 60;
const collator = new Intl.Collator("it", { sensitivity: "base" });
const CITY_OPTIONS = [
  { value: "__city:Napoli", label: "Napoli - TUTTE", city: "Napoli" },
  { value: "__city:Benevento", label: "Benevento - TUTTE", city: "Benevento" },
];

const state = {
  trips: [],
  ready: false,
};

const els = {
  form: document.querySelector("#searchForm"),
  from: document.querySelector("#fromStop"),
  to: document.querySelector("#toStop"),
  time: document.querySelector("#timeFrom"),
  line: document.querySelector("#lineFilter"),
  swap: document.querySelector("#swapStops"),
  reset: document.querySelector("#resetSearch"),
  results: document.querySelector("#results"),
  summary: document.querySelector("#resultsSummary"),
  status: document.querySelector("#dataStatus"),
};

init();

async function init() {
  setCurrentTime();
  bindEvents();

  try {
    state.trips = await loadSchedule();
    state.ready = true;

    populateLineOptions();
    populateFromOptions();
    setInitialRoute();
    updateToOptions();
    renderResults();

    els.status.textContent = `${state.trips.length} corse`;
  } catch (error) {
    console.error(error);
    els.status.textContent = "Errore dati";
    renderError("Impossibile caricare gli orari.", "Rigenera data.js dal CSV oppure avvia un server statico locale.");
  }
}

function bindEvents() {
  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    renderResults();
  });

  els.from.addEventListener("change", () => {
    updateToOptions();
    renderResults();
  });

  els.to.addEventListener("change", renderResults);

  els.time.addEventListener("input", renderResults);

  els.line.addEventListener("change", () => {
    populateFromOptions(els.from.value);
    updateToOptions(els.to.value);
    renderResults();
  });

  els.swap.addEventListener("click", swapStops);

  els.reset.addEventListener("click", () => {
    els.line.value = "";
    setCurrentTime();
    populateFromOptions();
    setInitialRoute();
    updateToOptions();
    renderResults();
  });
}

async function fetchCsv(url) {
  const response = await fetch(url);

  if (!response.ok) {
    throw new Error(`CSV request failed with ${response.status}`);
  }

  return response.text();
}

async function loadSchedule() {
  if (isEmbeddedSchedule(window.BUS_SCHEDULE_DATA)) {
    return parseEmbeddedSchedule(window.BUS_SCHEDULE_DATA);
  }

  const csv = await fetchCsv(CSV_URL);
  return parseSchedule(csv);
}

function isEmbeddedSchedule(data) {
  return data && Array.isArray(data.trips);
}

function parseEmbeddedSchedule(data) {
  return data.trips
    .map((values, index) => toTrip(data.columns, values, index + 1))
    .filter(Boolean)
    .sort((a, b) => a.departureMinutes - b.departureMinutes || a.arrivalMinutes - b.arrivalMinutes);
}

function parseSchedule(csv) {
  const rows = csv
    .trim()
    .split(/\r?\n/)
    .filter(Boolean);

  if (rows.length < 2) {
    throw new Error("CSV vuoto o senza righe dati");
  }

  const headers = parseCsvLine(rows[0]).map((header) => header.replace(/^\uFEFF/, "").trim());

  return rows
    .slice(1)
    .map((line, index) => toTrip(headers, parseCsvLine(line), index + 2))
    .filter(Boolean)
    .sort((a, b) => a.departureMinutes - b.departureMinutes || a.arrivalMinutes - b.arrivalMinutes);
}

function parseCsvLine(line) {
  const values = [];
  let current = "";
  let inQuotes = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    const nextChar = line[index + 1];

    if (char === '"' && inQuotes && nextChar === '"') {
      current += char;
      index += 1;
    } else if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === ";" && !inQuotes) {
      values.push(current.trim());
      current = "";
    } else {
      current += char;
    }
  }

  values.push(current.trim());
  return values;
}

function toTrip(headers, values, rowNumber) {
  const row = Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  const departureMinutes = timeToMinutes(row["orario partenza"]);
  const arrivalMinutes = timeToMinutes(row["orario arrivo"]);

  if (departureMinutes === null || arrivalMinutes === null) {
    console.warn(`Riga ${rowNumber} ignorata: orario non valido`);
    return null;
  }

  const durationMinutes =
    arrivalMinutes >= departureMinutes
      ? arrivalMinutes - departureMinutes
      : arrivalMinutes + DAY_MINUTES - departureMinutes;

  return {
    departureTime: row["orario partenza"],
    arrivalTime: row["orario arrivo"],
    departureStop: row["stazione partenza"],
    arrivalStop: row["stazione arrivo"],
    line: row.linea || row.line || "",
    departureMinutes,
    arrivalMinutes,
    durationMinutes,
  };
}

function populateLineOptions() {
  const current = els.line.value;
  const lines = uniqueSorted(state.trips.map((trip) => trip.line));
  const options = [new Option("Tutte", ""), ...lines.map((line) => new Option(line, line))];

  replaceOptions(els.line, options);
  els.line.value = lines.includes(current) ? current : "";
}

function populateFromOptions(preferredValue = "") {
  const currentLine = els.line.value;
  const stops = uniqueSorted(
    state.trips
      .filter((trip) => !currentLine || trip.line === currentLine)
      .map((trip) => trip.departureStop),
  );
  const { options, values } = buildStopOptions(stops);
  const nextValue = pickSelectValue(values, preferredValue, stops[0] ?? "");

  replaceOptions(els.from, options);
  els.from.value = nextValue;
}

function updateToOptions(preferredValue = "") {
  const currentLine = els.line.value;
  const currentFrom = els.from.value;
  const stops = uniqueSorted(
    state.trips
      .filter(
        (trip) =>
          (!currentLine || trip.line === currentLine) &&
          stopMatchesSelection(trip.departureStop, currentFrom),
      )
      .map((trip) => trip.arrivalStop),
  );
  const { options, values } = buildStopOptions(stops);
  const nextValue = pickSelectValue(values, preferredValue, stops[0] ?? "");

  replaceOptions(els.to, options);
  els.to.value = nextValue;
}

function replaceOptions(select, options) {
  select.replaceChildren(...options);
  select.disabled = options.length === 0;
}

function buildStopOptions(stops) {
  const options = [
    ...CITY_OPTIONS.map((cityOption) => new Option(cityOption.label, cityOption.value)),
    ...stops.map((stop) => new Option(stop, stop)),
  ];
  const values = options.map((option) => option.value);

  return { options, values };
}

function pickSelectValue(values, preferredValue, fallbackValue) {
  if (preferredValue && values.includes(preferredValue)) {
    return preferredValue;
  }

  if (fallbackValue && values.includes(fallbackValue)) {
    return fallbackValue;
  }

  return values[0] ?? "";
}

function setInitialRoute() {
  const firstTrip = state.trips.find((trip) => !els.line.value || trip.line === els.line.value);

  if (!firstTrip) {
    return;
  }

  if (selectHasValue(els.from, firstTrip.departureStop)) {
    els.from.value = firstTrip.departureStop;
  }
}

function swapStops() {
  if (!state.ready) {
    return;
  }

  const nextFrom = els.to.value;
  const nextTo = els.from.value;
  const currentLine = els.line.value;
  let reverseTrip = findTrip(nextFrom, nextTo, currentLine);

  if (!reverseTrip && currentLine) {
    els.line.value = "";
    populateFromOptions(nextFrom);
    reverseTrip = findTrip(nextFrom, nextTo, "");
  } else {
    populateFromOptions(nextFrom);
  }

  if (reverseTrip) {
    els.from.value = selectHasValue(els.from, nextFrom) ? nextFrom : reverseTrip.departureStop;
    updateToOptions(nextTo);
  } else {
    updateToOptions(nextTo);
  }

  renderResults();
}

function findTrip(from, to, line) {
  return state.trips.find(
    (trip) =>
      stopMatchesSelection(trip.departureStop, from) &&
      stopMatchesSelection(trip.arrivalStop, to) &&
      (!line || trip.line === line),
  );
}

function renderResults() {
  if (!state.ready) {
    return;
  }

  const selectedMinutes = timeToMinutes(els.time.value);

  if (selectedMinutes === null) {
    els.summary.textContent = "";
    renderEmpty("Scegli un orario valido.", "");
    return;
  }

  const routeTrips = state.trips
    .filter(
      (trip) =>
        stopMatchesSelection(trip.departureStop, els.from.value) &&
        stopMatchesSelection(trip.arrivalStop, els.to.value) &&
        (!els.line.value || trip.line === els.line.value),
    )
    .sort((a, b) => a.departureMinutes - b.departureMinutes || a.durationMinutes - b.durationMinutes);

  const matches = routeTrips.filter((trip) => trip.departureMinutes >= selectedMinutes);
  els.summary.textContent = makeSummary(matches.length, selectedMinutes);

  if (routeTrips.length === 0) {
    renderEmpty("Nessuna corsa per questa tratta.", "Prova a cambiare linea o fermata.");
    return;
  }

  if (matches.length === 0) {
    const firstDepartures = routeTrips.slice(0, 3).map((trip) => trip.departureTime).join(", ");
    renderEmpty(`Nessuna corsa dopo le ${formatTime(selectedMinutes)}.`, `Prime corse disponibili: ${firstDepartures}.`);
    return;
  }

  const fragment = document.createDocumentFragment();
  matches.forEach((trip) => fragment.append(createTripElement(trip)));
  els.results.replaceChildren(fragment);
}

function createTripElement(trip) {
  const article = document.createElement("article");
  article.className = "trip";

  const timeStack = document.createElement("div");
  timeStack.className = "time-stack";

  const departure = document.createElement("span");
  departure.className = "time";
  departure.textContent = trip.departureTime;

  const arrival = document.createElement("span");
  arrival.className = "time arrival";
  arrival.textContent = trip.arrivalTime;

  timeStack.append(departure, arrival);

  const route = document.createElement("div");
  route.className = "route";

  const from = document.createElement("strong");
  from.textContent = trip.departureStop;

  const to = document.createElement("span");
  to.textContent = trip.arrivalStop;

  route.append(from, to);

  const aside = document.createElement("div");
  aside.className = "trip-aside";

  const badge = document.createElement("span");
  const lineClass = trip.line ? trip.line.toLowerCase() : "unknown";
  badge.className = `badge ${lineClass}`;
  badge.textContent = trip.line || "Linea";

  const duration = document.createElement("span");
  duration.className = "meta";
  duration.textContent = formatDuration(trip.durationMinutes);

  aside.append(badge, duration);
  article.append(timeStack, route, aside);

  return article;
}

function renderEmpty(title, hint) {
  const wrapper = document.createElement("div");
  wrapper.className = "empty-state";

  const strong = document.createElement("strong");
  strong.textContent = title;
  wrapper.append(strong);

  if (hint) {
    const paragraph = document.createElement("p");
    paragraph.className = "hint";
    paragraph.textContent = hint;
    wrapper.append(paragraph);
  }

  els.results.replaceChildren(wrapper);
}

function renderError(title, hint) {
  els.summary.textContent = "";

  const wrapper = document.createElement("div");
  wrapper.className = "error-state";

  const strong = document.createElement("strong");
  strong.textContent = title;

  const paragraph = document.createElement("p");
  paragraph.textContent = hint;

  wrapper.append(strong, paragraph);
  els.results.replaceChildren(wrapper);
}

function makeSummary(count, selectedMinutes) {
  const label = count === 1 ? "corsa" : "corse";
  return `${count} ${label} dalle ${formatTime(selectedMinutes)}`;
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort(collator.compare);
}

function selectHasValue(select, value) {
  return [...select.options].some((option) => option.value === value);
}

function stopMatchesSelection(stop, selection) {
  const cityOption = getCityOption(selection);

  if (cityOption) {
    return getStopCity(stop) === cityOption.city;
  }

  return stop === selection;
}

function getCityOption(value) {
  return CITY_OPTIONS.find((option) => option.value === value);
}

function getStopCity(stop) {
  if (/^Napoli\b/i.test(stop)) {
    return "Napoli";
  }

  if (/^Benevento\b/i.test(stop)) {
    return "Benevento";
  }

  return "";
}

function setCurrentTime() {
  const now = new Date();
  els.time.value = formatTime(now.getHours() * 60 + now.getMinutes());
}

function timeToMinutes(value) {
  if (!/^\d{2}:\d{2}$/.test(value)) {
    return null;
  }

  const [hours, minutes] = value.split(":").map(Number);

  if (hours > 23 || minutes > 59) {
    return null;
  }

  return hours * 60 + minutes;
}

function formatTime(minutes) {
  const normalizedMinutes = ((minutes % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
  const hours = Math.floor(normalizedMinutes / 60);
  const mins = normalizedMinutes % 60;

  return `${hours.toString().padStart(2, "0")}:${mins.toString().padStart(2, "0")}`;
}

function formatDuration(minutes) {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;

  if (hours === 0) {
    return `${mins} min`;
  }

  return mins === 0 ? `${hours} h` : `${hours} h ${mins} min`;
}
