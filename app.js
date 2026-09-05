"use strict";

const CSV_URL = "assets/timetables/orari_eav_air.csv";
const DAY_MINUTES = 24 * 60;
const QUERY_KEYS = {
  from: "from",
  to: "to",
  time: "time",
  mode: "mode",
  line: "line",
};
const DEFAULT_FROM = "__city:Benevento";
const DEFAULT_TO = "__city:Napoli";
const DEFAULT_TIME_MODE = "departure";
const ALL_LINES = "__all";
const TIME_MODES = new Set(["departure", "arrival"]);
const TITLES = {
  beneventoToNapoli: "Quando vedrai il Vesuvio?",
  napoliToBenevento: "Quando torni a S. Colomba III?",
  fallback: "BN <> NA",
};
const THEME_STORAGE_KEY = "bus-na-bn-theme";
const THEME_COLORS = {
  light: "#0f6b4d",
  dark: "#101614",
};
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
  title: document.querySelector("#pageTitle"),
  form: document.querySelector("#searchForm"),
  from: document.querySelector("#fromStop"),
  to: document.querySelector("#toStop"),
  line: document.querySelector("#lineFilter"),
  time: document.querySelector("#timeFrom"),
  timeModes: document.querySelectorAll('input[name="timeMode"]'),
  swap: document.querySelector("#swapStops"),
  reset: document.querySelector("#resetSearch"),
  theme: document.querySelector("#themeToggle"),
  themeLabel: document.querySelector("#themeToggleLabel"),
  themeColor: document.querySelector('meta[name="theme-color"]'),
  results: document.querySelector("#results"),
  summary: document.querySelector("#resultsSummary"),
  status: document.querySelector("#dataStatus"),
};

init();

async function init() {
  initTheme();
  setCurrentTime();
  bindEvents();

  try {
    state.trips = await loadSchedule();
    state.ready = true;

    populateFromOptions();
    populateLineOptions();
    setInitialRoute();
    updateToOptions(DEFAULT_TO);
    applyQueryParams();
    updateResults({ replaceUrl: true });

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
    updateResults();
  });

  els.from.addEventListener("change", () => {
    updateToOptions();
    updateResults();
  });

  els.to.addEventListener("change", updateResults);
  els.line.addEventListener("change", updateResults);

  els.time.addEventListener("input", updateResults);

  els.timeModes.forEach((input) => {
    input.addEventListener("change", () => {
      syncTimeModeControls();
      updateResults();
    });
  });

  els.swap.addEventListener("click", swapStops);

  els.reset.addEventListener("click", () => {
    setCurrentTime();
    setTimeMode(DEFAULT_TIME_MODE);
    els.line.value = ALL_LINES;
    populateFromOptions();
    setInitialRoute();
    updateToOptions(DEFAULT_TO);
    updateResults();
  });

  els.theme.addEventListener("click", () => {
    const nextTheme = getEffectiveTheme() === "dark" ? "light" : "dark";

    writeThemePreference(nextTheme);
    applyTheme(nextTheme);
  });
}

function initTheme() {
  applyTheme(readThemePreference());

  const media = getThemeMedia();
  if (!media?.addEventListener) {
    return;
  }

  media.addEventListener("change", () => {
    if (!readThemePreference()) {
      syncThemeControls();
    }
  });
}

function applyTheme(theme) {
  if (theme === "dark" || theme === "light") {
    document.documentElement.dataset.theme = theme;
  } else {
    document.documentElement.removeAttribute("data-theme");
  }

  syncThemeControls();
}

function syncThemeControls() {
  const theme = getEffectiveTheme();
  const isDark = theme === "dark";
  const label = isDark ? "Attiva tema chiaro" : "Attiva tema scuro";

  els.theme.classList.toggle("is-dark", isDark);
  els.theme.setAttribute("aria-pressed", String(isDark));
  els.theme.setAttribute("aria-label", label);

  if (els.themeLabel) {
    els.themeLabel.textContent = label;
  }

  if (els.themeColor) {
    els.themeColor.setAttribute("content", THEME_COLORS[theme]);
  }
}

function getEffectiveTheme() {
  const explicitTheme = document.documentElement.dataset.theme;

  if (explicitTheme === "dark" || explicitTheme === "light") {
    return explicitTheme;
  }

  return getThemeMedia()?.matches ? "dark" : "light";
}

function getThemeMedia() {
  return window.matchMedia?.("(prefers-color-scheme: dark)");
}

function readThemePreference() {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);

    return value === "dark" || value === "light" ? value : "";
  } catch {
    return "";
  }
}

function writeThemePreference(theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // The theme still changes for the current page even if storage is blocked.
  }
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
  const trips = data.trips
    .map((values, index) => toTrip(data.columns, values, index + 1))
    .filter(Boolean);
  const journeys = Array.isArray(data.journeys)
    ? data.journeys.map((journey, index) => toJourney(journey, index + 1)).filter(Boolean)
    : [];

  return [...trips, ...journeys]
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
    stops: [
      {
        name: row["stazione partenza"],
        time: row["orario partenza"],
        timeMinutes: departureMinutes,
        note: "",
      },
      {
        name: row["stazione arrivo"],
        time: row["orario arrivo"],
        timeMinutes: arrivalMinutes,
        note: "",
      },
    ],
  };
}

function toJourney(journey, journeyNumber) {
  if (!journey || !Array.isArray(journey.stops)) {
    console.warn(`Corsa articolata ${journeyNumber} ignorata: fermate mancanti`);
    return null;
  }

  const stops = journey.stops.map((stop) => ({
    name: stop.name || "",
    time: stop.time || "",
    timeMinutes: stop.time ? timeToMinutes(stop.time) : null,
    note: stop.note || "",
  }));
  const timedStops = stops.filter((stop) => stop.timeMinutes !== null);

  if (timedStops.length < 2 || stops.some((stop) => stop.time && stop.timeMinutes === null)) {
    console.warn(`Corsa articolata ${journeyNumber} ignorata: orari non validi`);
    return null;
  }

  const firstStop = timedStops[0];
  const lastStop = timedStops[timedStops.length - 1];
  const durationMinutes = getDurationMinutes(firstStop.timeMinutes, lastStop.timeMinutes);

  return {
    id: journey.id || `journey-${journeyNumber}`,
    line: journey.line || "",
    direction: journey.direction || "",
    departureTime: firstStop.time,
    arrivalTime: lastStop.time,
    departureStop: firstStop.name,
    arrivalStop: lastStop.name,
    departureMinutes: firstStop.timeMinutes,
    arrivalMinutes: lastStop.timeMinutes,
    durationMinutes,
    stops,
  };
}

function getDurationMinutes(departureMinutes, arrivalMinutes) {
  return arrivalMinutes >= departureMinutes
    ? arrivalMinutes - departureMinutes
    : arrivalMinutes + DAY_MINUTES - departureMinutes;
}

function populateFromOptions(preferredValue = "") {
  const stops = uniqueSorted(
    state.trips
      .flatMap(getJourneyDepartureStops),
  );
  const { options, values } = buildStopOptions(stops);
  const nextValue = pickSelectValue(values, preferredValue, stops[0] ?? "");

  replaceOptions(els.from, options);
  els.from.value = nextValue;
}

function updateToOptions(preferredValue = "") {
  const currentFrom = els.from.value;
  const stops = uniqueSorted(
    state.trips
      .flatMap((trip) => getJourneyArrivalStops(trip, currentFrom)),
  );
  const { options, values } = buildStopOptions(stops);
  const nextValue = pickSelectValue(values, preferredValue, stops[0] ?? "");

  replaceOptions(els.to, options);
  els.to.value = nextValue;
}

function getJourneyDepartureStops(trip) {
  return trip.stops
    .slice(0, -1)
    .filter((stop, index) =>
      stop.timeMinutes !== null && trip.stops.slice(index + 1).some((nextStop) => nextStop.timeMinutes !== null),
    )
    .map((stop) => stop.name);
}

function getJourneyArrivalStops(trip, fromSelection) {
  const departureIndex = trip.stops.findIndex(
    (stop) => stop.timeMinutes !== null && stopMatchesSelection(stop.name, fromSelection),
  );

  if (departureIndex < 0) {
    return [];
  }

  return trip.stops
    .slice(departureIndex + 1)
    .filter((stop) => stop.timeMinutes !== null)
    .map((stop) => stop.name);
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

function populateLineOptions() {
  const lines = uniqueSorted(state.trips.map((trip) => trip.line));
  const options = [
    new Option("Tutte le linee", ALL_LINES),
    ...lines.map((line) => new Option(line, line)),
  ];

  replaceOptions(els.line, options);
  els.line.value = ALL_LINES;
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
  if (selectHasValue(els.from, DEFAULT_FROM)) {
    els.from.value = DEFAULT_FROM;
  }
}

function applyQueryParams() {
  const params = new URLSearchParams(window.location.search);
  const from = params.get(QUERY_KEYS.from) ?? "";
  const to = params.get(QUERY_KEYS.to) ?? "";
  const time = params.get(QUERY_KEYS.time) ?? "";
  const mode = params.get(QUERY_KEYS.mode) ?? "";
  const line = params.get(QUERY_KEYS.line) ?? "";

  if (selectHasValue(els.from, from)) {
    els.from.value = from;
  }

  updateToOptions(els.to.value);

  if (selectHasValue(els.to, to)) {
    els.to.value = to;
  }

  if (timeToMinutes(time) !== null) {
    els.time.value = time;
  }

  setTimeMode(isTimeMode(mode) ? mode : DEFAULT_TIME_MODE);

  if (selectHasValue(els.line, line)) {
    els.line.value = line;
  }
}

function updateQueryParams() {
  if (!window.history?.replaceState) {
    return;
  }

  const params = new URLSearchParams(window.location.search);
  setQueryValue(params, QUERY_KEYS.from, els.from.value);
  setQueryValue(params, QUERY_KEYS.to, els.to.value);
  setQueryValue(params, QUERY_KEYS.time, els.time.value);
  setQueryValue(params, QUERY_KEYS.mode, getTimeMode() === DEFAULT_TIME_MODE ? "" : getTimeMode());
  setQueryValue(params, QUERY_KEYS.line, els.line.value === ALL_LINES ? "" : els.line.value);

  const queryString = params.toString();
  const nextUrl = `${window.location.pathname}${queryString ? `?${queryString}` : ""}${window.location.hash}`;

  window.history.replaceState(null, "", nextUrl);
}

function setQueryValue(params, key, value) {
  if (value) {
    params.set(key, value);
  } else {
    params.delete(key);
  }
}

function getTimeMode() {
  return [...els.timeModes].find((input) => input.checked)?.value ?? DEFAULT_TIME_MODE;
}

function setTimeMode(mode) {
  const nextMode = isTimeMode(mode) ? mode : DEFAULT_TIME_MODE;

  els.timeModes.forEach((input) => {
    input.checked = input.value === nextMode;
  });
  syncTimeModeControls();
}

function syncTimeModeControls() {
  const isArrivalMode = getTimeMode() === "arrival";
  els.time.setAttribute("aria-label", isArrivalMode ? "Arrivo entro" : "Partenza dalle");
}

function isTimeMode(mode) {
  return TIME_MODES.has(mode);
}

function swapStops() {
  if (!state.ready) {
    return;
  }

  const nextFrom = els.to.value;
  const nextTo = els.from.value;
  const reverseTrip = findTrip(nextFrom, nextTo);

  populateFromOptions(nextFrom);

  if (reverseTrip) {
    els.from.value = selectHasValue(els.from, nextFrom) ? nextFrom : reverseTrip.departureStop;
    updateToOptions(nextTo);
  } else {
    updateToOptions(nextTo);
  }

  updateResults();
}

function findTrip(from, to) {
  return state.trips
    .map((trip) => selectJourneySegment(trip, from, to))
    .find(Boolean);
}

function selectJourneySegment(trip, fromSelection, toSelection) {
  const departureIndex = trip.stops.findIndex(
    (stop) => stop.timeMinutes !== null && stopMatchesSelection(stop.name, fromSelection),
  );

  if (departureIndex < 0) {
    return null;
  }

  let arrivalIndex = -1;
  for (let index = departureIndex + 1; index < trip.stops.length; index += 1) {
    const stop = trip.stops[index];
    if (stop.timeMinutes !== null && stopMatchesSelection(stop.name, toSelection)) {
      arrivalIndex = index;
    }
  }

  if (arrivalIndex < 0) {
    return null;
  }

  const departureStop = trip.stops[departureIndex];
  const arrivalStop = trip.stops[arrivalIndex];

  return {
    ...trip,
    departureTime: departureStop.time,
    arrivalTime: arrivalStop.time,
    departureStop: departureStop.name,
    arrivalStop: arrivalStop.name,
    departureMinutes: departureStop.timeMinutes,
    arrivalMinutes: arrivalStop.timeMinutes,
    durationMinutes: getDurationMinutes(departureStop.timeMinutes, arrivalStop.timeMinutes),
  };
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
    .map((trip) => selectJourneySegment(trip, els.from.value, els.to.value))
    .filter(Boolean)
    .filter((trip) => els.line.value === ALL_LINES || trip.line === els.line.value)
    .sort((a, b) => a.departureMinutes - b.departureMinutes || a.durationMinutes - b.durationMinutes);

  const timeMode = getTimeMode();
  const matches = routeTrips
    .filter((trip) =>
      timeMode === "arrival"
        ? trip.arrivalMinutes <= selectedMinutes
        : trip.departureMinutes >= selectedMinutes,
    )
    .sort((a, b) =>
      timeMode === "arrival"
        ? b.arrivalMinutes - a.arrivalMinutes || b.departureMinutes - a.departureMinutes
        : a.departureMinutes - b.departureMinutes || a.durationMinutes - b.durationMinutes,
    );
  els.summary.textContent = makeSummary(matches.length, selectedMinutes, timeMode);

  if (routeTrips.length === 0) {
    renderEmpty("Nessuna corsa per questa tratta.", "Prova a cambiare linea o fermata.");
    return;
  }

  if (matches.length === 0) {
    const hintTimes =
      timeMode === "arrival"
        ? [...routeTrips]
            .sort((a, b) => a.arrivalMinutes - b.arrivalMinutes)
            .slice(0, 3)
            .map((trip) => trip.arrivalTime)
            .join(", ")
        : routeTrips.slice(0, 3).map((trip) => trip.departureTime).join(", ");
    const title =
      timeMode === "arrival"
        ? `Nessuna corsa con arrivo entro le ${formatTime(selectedMinutes)}.`
        : `Nessuna corsa dopo le ${formatTime(selectedMinutes)}.`;
    const hint =
      timeMode === "arrival"
        ? `Primi arrivi disponibili: ${hintTimes}.`
        : `Prime corse disponibili: ${hintTimes}.`;

    renderEmpty(title, hint);
    return;
  }

  const fragment = document.createDocumentFragment();
  matches.forEach((trip) => fragment.append(createTripElement(trip)));
  els.results.replaceChildren(fragment);
}

function updateResults({ replaceUrl = true } = {}) {
  updatePageTitle();
  renderResults();

  if (replaceUrl) {
    updateQueryParams();
  }
}

function updatePageTitle() {
  const fromCity = getSelectionCity(els.from.value);
  const toCity = getSelectionCity(els.to.value);
  document.body.dataset.destinationCity = toCity === "Benevento" ? "benevento" : "napoli";

  if (fromCity === "Benevento" && toCity === "Napoli") {
    els.title.textContent = TITLES.beneventoToNapoli;
    return;
  }

  if (fromCity === "Napoli" && toCity === "Benevento") {
    els.title.textContent = TITLES.napoliToBenevento;
    return;
  }

  els.title.textContent = TITLES.fallback;
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

  if (trip.stops.length > 2) {
    const journeyStops = document.createElement("small");
    journeyStops.className = "journey-stops";
    journeyStops.textContent = trip.stops.map(formatJourneyStop).join(" → ");
    route.append(journeyStops);
  }

  const aside = document.createElement("div");
  aside.className = "trip-aside";

  const badge = document.createElement("span");
  const lineClass = trip.line ? trip.line.toLowerCase().replace(/[^a-z0-9]+/g, "-") : "unknown";
  badge.className = `badge ${lineClass}`;
  badge.textContent = trip.line || "Linea";

  const duration = document.createElement("span");
  duration.className = "meta";
  duration.textContent = formatDuration(trip.durationMinutes);

  aside.append(badge, duration);
  article.append(timeStack, route, aside);

  return article;
}

function formatJourneyStop(stop) {
  const timedStop = stop.time ? `${stop.time} ${stop.name}` : stop.name;
  return stop.note ? `${timedStop} — ${stop.note}` : timedStop;
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

function makeSummary(count, selectedMinutes, timeMode = DEFAULT_TIME_MODE) {
  const label = count === 1 ? "corsa" : "corse";
  const timeLabel = timeMode === "arrival" ? "entro le" : "dalle";

  return `${count} ${label} ${timeLabel} ${formatTime(selectedMinutes)}`;
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

function getSelectionCity(selection) {
  return getCityOption(selection)?.city ?? getStopCity(selection);
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
