/* Distance units. The API reports kilometres; everything shown goes through here.
 * Default is miles; the choice is remembered per browser. */
(function () {
  "use strict";

  const KM_PER_MI = 1.609344;
  const KEY = "polestar.units";

  let system = "mi";
  try { system = localStorage.getItem(KEY) === "km" ? "km" : "mi"; } catch { /* private mode */ }

  const Units = {
    get system() { return system; },
    set(next) {
      if (next !== "mi" && next !== "km" || next === system) return;
      system = next;
      try { localStorage.setItem(KEY, system); } catch { /* ignore */ }
      document.dispatchEvent(new CustomEvent("unitschange", { detail: system }));
    },
    /** km -> display distance (number, or null) */
    dist(km) {
      if (typeof km !== "number" || isNaN(km)) return null;
      return system === "mi" ? km / KM_PER_MI : km;
    },
    /** km/h -> display speed */
    speed(kmh) { return Units.dist(kmh); },
    /** kWh/100 km -> kWh/100 mi or kWh/100 km */
    consumption(kwhPer100km) {
      if (typeof kwhPer100km !== "number" || isNaN(kwhPer100km)) return null;
      return system === "mi" ? kwhPer100km * KM_PER_MI : kwhPer100km;
    },
    get distLabel() { return system === "mi" ? "mi" : "km"; },
    get speedLabel() { return system === "mi" ? "mph" : "km/h"; },
    get consumptionLabel() { return system === "mi" ? "kWh/100 mi" : "kWh/100 km"; },
  };

  window.Units = Units;
})();
