/**
 * Floor heating card for the Gree Versati integration.
 *
 * Shows what floor heating control does, or would do in advisory mode: the
 * controller's water target against the pump's setpoint and the measured
 * water, and the room against its target and the outdoor temperature. It is
 * built from Home Assistant's own history graph cards and finds the
 * controller's entities through the thermostat, so that is all it needs:
 *
 *   type: custom:gree-versati-floor-heating-card
 *   entity: climate.gree_versati_<device_id>_floor_heating
 *   hours_to_show: 72  # optional
 *
 * The integration serves this file and loads it on every dashboard.
 */

const DOMAIN = "gree_versati";
const CARD_TYPE = "gree-versati-floor-heating-card";
const THERMOSTAT_KEY = "floor_heating";

// Lines on the water graph: [domain, translation key, label]. They are looked
// up on the thermostat's device, so renamed entity IDs don't matter.
const WATER_LINES = [
  ["sensor", "heating_target_water_temperature", "Controller target"],
  ["sensor", "heating_curve_water_temperature", "Curve only"],
  ["number", "he_wat_out_tem_set", "Pump setpoint"],
  ["sensor", "all_out_wat_tem", "Water out (measured)"],
];
const OUTDOOR_LINE = ["sensor", "heating_outdoor_temperature", "Outdoor (smoothed)"];

const LABELS = {
  entity: "Floor heating thermostat",
  title: "Title",
  hours_to_show: "Hours to show",
};

function deviceLine(entities, deviceId, [domain, translationKey, name]) {
  const entry = Object.values(entities).find(
    (candidate) =>
      candidate.device_id === deviceId &&
      candidate.platform === DOMAIN &&
      candidate.translation_key === translationKey &&
      candidate.entity_id.startsWith(`${domain}.`)
  );
  return entry ? [{ entity: entry.entity_id, name }] : [];
}

/** Config for the built-in cards to show, or null if the thermostat is unknown. */
function stackConfig(entities, config) {
  const thermostat = entities[config.entity];
  if (!thermostat) {
    return null;
  }
  const deviceId = thermostat.device_id;
  return {
    type: "vertical-stack",
    cards: [
      {
        type: "history-graph",
        title: config.title ?? "Floor heating water",
        hours_to_show: config.hours_to_show,
        entities: WATER_LINES.flatMap((line) => deviceLine(entities, deviceId, line)),
      },
      {
        type: "history-graph",
        title: "Room and outdoor",
        hours_to_show: config.hours_to_show,
        entities: [
          { entity: config.entity, name: "Room" },
          ...deviceLine(entities, deviceId, OUTDOOR_LINE),
        ],
      },
    ],
  };
}

class FloorHeatingCard extends HTMLElement {
  setConfig(config) {
    if (typeof config?.entity !== "string" || !config.entity.startsWith("climate.")) {
      throw new Error("Choose the Floor heating thermostat of a Gree Versati");
    }
    this._config = { hours_to_show: 72, ...config };
    this._entities = undefined;
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  async _update() {
    if (!this._config || !this._hass) {
      return;
    }
    // Look the entities up again only when the entity registry changes.
    if (this._hass.entities !== this._entities) {
      this._entities = this._hass.entities;
      const stack = stackConfig(this._entities, this._config);
      const key = JSON.stringify(stack);
      if (key !== this._key) {
        this._key = key;
        await this._show(stack, key);
      }
    }
    if (this._card) {
      this._card.hass = this._hass;
    }
  }

  async _show(stack, key) {
    let card;
    if (stack) {
      const helpers = await window.loadCardHelpers();
      if (key !== this._key) {
        return; // The configuration changed while the helpers loaded.
      }
      card = helpers.createCardElement(stack);
    } else {
      card = document.createElement("ha-card");
      const message = document.createElement("div");
      message.className = "card-content";
      message.textContent = `Floor heating thermostat not found: ${this._config.entity}`;
      card.append(message);
    }
    this._card = card;
    this.replaceChildren(card);
  }

  getCardSize() {
    return this._card?.getCardSize ? this._card.getCardSize() : 8;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  static getStubConfig(hass) {
    const thermostat = Object.values(hass.entities).find(
      (entry) => entry.platform === DOMAIN && entry.translation_key === THERMOSTAT_KEY
    );
    return { entity: thermostat?.entity_id ?? "" };
  }

  static getConfigForm() {
    return {
      schema: [
        {
          name: "entity",
          required: true,
          selector: { entity: { domain: "climate", integration: DOMAIN } },
        },
        { name: "title", selector: { text: {} } },
        {
          name: "hours_to_show",
          selector: { number: { min: 1, max: 336, mode: "box", unit_of_measurement: "h" } },
        },
      ],
      computeLabel: (schema) => LABELS[schema.name],
    };
  }
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, FloorHeatingCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Gree Versati floor heating",
    description:
      "The floor heating controller's water target against the pump, and the room against its target.",
    preview: true,
  });
}
