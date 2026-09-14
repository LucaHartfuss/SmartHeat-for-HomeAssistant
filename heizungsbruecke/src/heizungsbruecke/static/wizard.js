const ROLE_LABELS = {
  entity_room_actual: "Ist-Temperatur Referenzraum (Sensor, nicht Thermostat)",
  entity_room_target: "Soll-Temperatur Referenzraum",
  entity_outdoor_temp: "Aussentemperatur",
  entity_curve_current: "Heizkurve (aktuell)",
  entity_offset_current: "Niveau/Offset (aktuell)",
  entity_heat_limit: "Heizgrenze",
};
const ROLE_DOMAINS = {
  entity_room_actual: ["sensor"],
  entity_room_target: ["sensor", "climate"],
  entity_outdoor_temp: ["sensor"],
  entity_curve_current: ["number"],
  entity_offset_current: ["number"],
  entity_heat_limit: ["number", "sensor"],
};
// A climate.* entity has no single numeric state -- room_target must reference one
// of its temperature attributes, matching ha_api.get_state()'s existing
// "entity_id::attribute" convention. Flattened directly into the option value here
// instead of a second dependent dropdown -- one fewer moving part for the same
// outcome. HA's climate entities report in the install's global unit (°C for this
// customer base), so the unit check for this role trusts that rather than reading a
// per-attribute unit_of_measurement HA doesn't expose here.
// entity_room_actual deliberately has NO climate entry (and no "climate" domain
// above): that value flows into derived_sensors.ensure_all() -> HA's statistics
// config-flow, which rejects both climate-domain sources and the "::attribute"
// suffix -- pick the room's plain temperature sensor entity instead (e.g.
// sensor.wohnzimmer_thermostat_temperatur, not climate.wohnzimmer_thermostat).
const CLIMATE_ATTRIBUTE_BY_ROLE = {
  entity_room_target: "temperature",
};

// Home Assistant serves add-on Ingress panels at a path prefix
// (/api/hassio_ingress/<token>/...) and does not rewrite this page's own
// requests to account for that -- a root-absolute "/api/..." fetch would resolve
// against the HA origin itself, not this add-on. Every API call below must be
// relative to this page's own served location instead.
const BASE = window.location.pathname.replace(/[^/]*$/, "");

let profileCatalog = [];

function showStep(id) {
  document.querySelectorAll(".step").forEach((el) => el.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}

function showError(message) {
  const el = document.getElementById("error");
  el.textContent = message;
  el.hidden = !message;
}

async function apiFetch(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `Fehler (${response.status})`);
  }
  return body;
}

document.getElementById("login-submit").addEventListener("click", async () => {
  showError("");
  try {
    await apiFetch(BASE + "api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: document.getElementById("login-email").value,
        password: document.getElementById("login-password").value,
      }),
    });
    const tenants = await apiFetch(BASE + "api/tenants");
    const select = document.getElementById("tenant-select");
    select.innerHTML = tenants.map((t) => `<option value="${t.tenant_id}">${t.tenant_id}</option>`).join("");
    showStep("step-tenant");
  } catch (error) {
    showError(error.message);
  }
});

document.getElementById("tenant-next").addEventListener("click", async () => {
  showError("");
  try {
    profileCatalog = await apiFetch(BASE + "api/profiles");
    populateHersteller();
    showStep("step-profile");
  } catch (error) {
    showError(error.message);
  }
});

function populateHersteller() {
  const herstellerSelect = document.getElementById("profile-hersteller");
  const hersteller = [...new Set(profileCatalog.map((e) => e.hersteller))];
  herstellerSelect.innerHTML = hersteller.map((h) => `<option value="${h}">${h}</option>`).join("");
  populateTyp();
}

function populateTyp() {
  const hersteller = document.getElementById("profile-hersteller").value;
  const typSelect = document.getElementById("profile-typ");
  const typen = [...new Set(profileCatalog.filter((e) => e.hersteller === hersteller).map((e) => e.erzeuger_typ))];
  typSelect.innerHTML = typen.map((t) => `<option value="${t}">${t}</option>`).join("");
  populateVerteilsystem();
}

function populateVerteilsystem() {
  const hersteller = document.getElementById("profile-hersteller").value;
  const typ = document.getElementById("profile-typ").value;
  const verteilSelect = document.getElementById("profile-verteilsystem");
  const systeme = profileCatalog.filter((e) => e.hersteller === hersteller && e.erzeuger_typ === typ);
  verteilSelect.innerHTML = systeme.map((e) => `<option value="${e.verteilsystem}">${e.verteilsystem}</option>`).join("");
  updateProfileWarning();
}

function currentProfileEntry() {
  const hersteller = document.getElementById("profile-hersteller").value;
  const typ = document.getElementById("profile-typ").value;
  const verteilsystem = document.getElementById("profile-verteilsystem").value;
  return profileCatalog.find(
    (e) => e.hersteller === hersteller && e.erzeuger_typ === typ && e.verteilsystem === verteilsystem
  );
}

function updateProfileWarning() {
  const entry = currentProfileEntry();
  const warning = document.getElementById("profile-warning");
  const nextButton = document.getElementById("profile-next");
  const blocked = !entry || !entry.verified;
  warning.hidden = !blocked;
  nextButton.disabled = blocked;
}

document.getElementById("profile-hersteller").addEventListener("change", populateTyp);
document.getElementById("profile-typ").addEventListener("change", populateVerteilsystem);
document.getElementById("profile-verteilsystem").addEventListener("change", updateProfileWarning);

document.getElementById("profile-next").addEventListener("click", async () => {
  showError("");
  try {
    const container = document.getElementById("sensor-fields");
    container.innerHTML = "";
    for (const [role, label] of Object.entries(ROLE_LABELS)) {
      let rawEntities = [];
      for (const domain of ROLE_DOMAINS[role]) {
        rawEntities = rawEntities.concat(await apiFetch(`${BASE}api/entities?domain=${domain}`));
      }
      if (rawEntities.length === 0) {
        showError(`Keine passenden Entities gefunden fuer '${label}' (Domain(n): ${ROLE_DOMAINS[role].join(", ")})`);
        return;
      }
      const climateAttribute = CLIMATE_ATTRIBUTE_BY_ROLE[role];
      const options = rawEntities.map((e) => {
        if (e.entity_id.startsWith("climate.") && climateAttribute) {
          return { value: `${e.entity_id}::${climateAttribute}`, label: e.friendly_name, unit: "°C" };
        }
        return { value: e.entity_id, label: e.friendly_name, unit: e.unit_of_measurement || "" };
      });
      const wrapper = document.createElement("label");
      wrapper.textContent = label;
      const select = document.createElement("select");
      select.dataset.role = role;
      select.innerHTML = options
        .map((o) => `<option value="${o.value}" data-unit="${o.unit}">${o.label}</option>`)
        .join("");
      wrapper.appendChild(select);
      container.appendChild(wrapper);
    }
    showStep("step-sensors");
  } catch (error) {
    showError(error.message);
  }
});

document.getElementById("sensors-next").addEventListener("click", async () => {
  showError("");
  const entities = {};
  document.querySelectorAll("#sensor-fields select").forEach((select) => {
    const chosenOption = select.options[select.selectedIndex];
    entities[select.dataset.role] = {
      entity_id: select.value,
      unit_of_measurement: chosenOption ? chosenOption.dataset.unit || null : null,
    };
  });

  const entry = currentProfileEntry();
  try {
    const result = await apiFetch(BASE + "api/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: document.getElementById("tenant-select").value,
        profile_id: entry.profile_id,
        entities,
      }),
    });
    document.getElementById("done-message").textContent = result.message;
    document.getElementById("done-mqtt-username").textContent = result.mqtt_username;
    document.getElementById("done-mqtt-password").textContent = result.mqtt_password;
    document.getElementById("done-mosquitto-command").textContent = result.mosquitto_passwd_command;
    document.getElementById("done-acl-snippet").textContent = result.acl_snippet;
    showStep("step-done");
  } catch (error) {
    showError(error.message);
  }
});
