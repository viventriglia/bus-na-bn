const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "..");
const context = vm.createContext({
  window: {}, console, Intl, URLSearchParams,
  document: { querySelector: () => ({}), querySelectorAll: () => [] },
  Option: function(text, value) { this.text = text; this.value = value; },
});
vm.runInContext(fs.readFileSync(path.join(root, "data.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(path.join(root, "app.js"), "utf8").replace(/\r?\ninit\(\);/, ""), context);
const api = vm.runInContext("({parseEmbeddedSchedule, toJourney, selectJourneySegment, buildStopOptions, getSelectionCity})", context);
const data = context.window.BUS_SCHEDULE_DATA;
const trips = api.parseEmbeddedSchedule(data);

test("city-only filters retain the existing URL values and migrate old stop links", () => {
  assert.deepEqual(Array.from(api.buildStopOptions().options, o => o.text).sort(), ["Benevento", "Napoli"]);
  assert.deepEqual(Array.from(api.buildStopOptions().values), ["__city:Napoli", "__city:Benevento"]);
  assert.equal(api.getSelectionCity("Benevento, Bar 2000"), "Benevento");
  assert.equal(api.getSelectionCity("Napoli Centrale - via G. Ferraris (di fronte Ramada Hotel)"), "Napoli");
});

test("expanding city stop data keeps service counts, endpoint previews and time filters stable", () => {
  assert.equal(trips.length, data.trips.length + data.journeys.length);
  for (const trip of trips) {
    const from = `__city:${api.getSelectionCity(trip.departureStop)}`;
    const to = `__city:${api.getSelectionCity(trip.arrivalStop)}`;
    const selected = api.selectJourneySegment(trip, from, to);
    for (const field of ["departureStop", "arrivalStop", "departureTime", "arrivalTime", "durationMinutes"]) {
      assert.equal(selected[field], trip[field]);
    }
    assert.ok(selected.stopVariants.length);
    for (const stops of selected.stopVariants) {
      assert.ok(stops.every(stop => /^(Benevento|Napoli)\b/.test(stop.name)));
    }
  }
});

test("same departure and service number keep separate BUS 1 and BUS 2 stop details", () => {
  const origin = "Benevento Stazione - via P. Diacono", destination = "Napoli Centrale - via G. Ferraris";
  const rows = [["05:44", "07:22", origin, destination, "EAV", "BUS 1", "3775", ""],
    ["05:44", "07:03", origin, destination, "EAV", "BUS 2 (veloce)", "3775", ""]];
  const details = {};
  for (const row of rows) {
    const key = JSON.stringify([row[4], ...row.slice(0, 4), row[5], row[6]]);
    const stops = [{ name: origin, time: row[0] }];
    if (row[5] === "BUS 1") stops.push({ name: "Benevento Appia", time: "05:50" }, { name: "Benevento, Rione Libertà", time: "05:52" });
    stops.push({ name: destination, time: row[1] });
    details[key] = [stops];
  }
  const pair = api.parseEmbeddedSchedule({ columns: data.columns, trips: rows, stopDetails: details });
  assert.equal(pair.length, 2);
  assert.equal(pair.find(t => t.busCategory === "BUS 1").stopVariants[0].length, 4);
  assert.equal(pair.find(t => t.busCategory === "BUS 2 (veloce)").stopVariants[0].length, 2);
});

test("MOT.TAM retains untimed De Pretis in the details and Garibaldi in the outbound preview", () => {
  const trip = api.toJourney({ line: "MOT.TAM", stops: [
    { name: "Benevento, Terminal bus - via Mustilli", time: "06:45" },
    { name: "Benevento, Bar 2000", time: "06:50" },
    { name: "Napoli, Piazza Garibaldi", time: "08:00" },
    { name: "Napoli, via De Pretis (Piazza Municipio)", time: "" },
  ] }, 1);
  assert.equal(trip.arrivalStop, "Napoli, Piazza Garibaldi");
  assert.equal(trip.stopVariants[0].at(-1).name, "Napoli, via De Pretis (Piazza Municipio)");
  assert.equal(trip.stopVariants[0].at(-1).time, "");
});
