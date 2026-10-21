"""The floor heating dashboard card, run in an embedded JavaScript engine.

Home Assistant's frontend is replaced by small stand-ins: when the card asks
for built-in cards it gets plain objects that record what it asked for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

quickjs = pytest.importorskip("quickjs")

CARD = (
    Path(__file__).parents[1]
    / "custom_components"
    / "gree_versati"
    / "heating"
    / "www"
    / "floor-heating-card.js"
)

FRONTEND_STAND_INS = """
globalThis.window = globalThis;
globalThis.HTMLElement = class {
  replaceChildren(...children) { this.children = children; }
};
const elements = new Map();
globalThis.customElements = {
  get: (name) => elements.get(name),
  define: (name, element) => elements.set(name, element),
};
globalThis.document = {
  createElement: (tag) => ({ tag, children: [], append(child) { this.children.push(child); } }),
};
globalThis.created = [];
globalThis.loadCardHelpers = async () => ({
  createCardElement: (config) => {
    const card = { config };
    created.push(card);
    return card;
  },
});
globalThis.Card = () => new (customElements.get("gree-versati-floor-heating-card"))();
// Let every pending promise in the card run.
globalThis.settle = async () => { for (let turn = 0; turn < 20; turn++) await null; };
"""


def entry(entity_id: str, translation_key: str, **overrides: str) -> dict[str, str]:
    return {
        "entity_id": entity_id,
        "device_id": "pump",
        "platform": "gree_versati",
        "translation_key": translation_key,
        **overrides,
    }


ENTITIES = {
    item["entity_id"]: item
    for item in (
        entry("climate.floor", "floor_heating"),
        entry("sensor.target", "heating_target_water_temperature"),
        entry("sensor.curve", "heating_curve_water_temperature"),
        # Sensor and number share this key; the graph wants the number.
        entry("sensor.setpoint_reading", "he_wat_out_tem_set"),
        entry("number.setpoint", "he_wat_out_tem_set"),
        entry("sensor.water_out", "all_out_wat_tem"),
        entry("sensor.outdoor", "heating_outdoor_temperature"),
        entry("sensor.outdoor_ahead", "heating_forecast_outdoor_temperature"),
        entry("sensor.sun_cut", "heating_sun_cut"),
        entry("sensor.learned", "heating_learned_offset"),
        # Same key on another device or from another integration: not ours.
        entry("sensor.other_pump_target", "heating_target_water_temperature", device_id="other"),
        entry("sensor.lookalike", "heating_curve_water_temperature", platform="other"),
    )
}


@pytest.fixture
def js() -> Any:
    context = quickjs.Context()
    context.eval(FRONTEND_STAND_INS)
    context.eval(CARD.read_text())
    context.set("entities_json", json.dumps(ENTITIES))
    context.eval("globalThis.ENTITIES = JSON.parse(entities_json);")
    return context


def run(js: Any, script: str) -> Any:
    """Run a script, let its promises settle, and return its JSON result."""
    js.eval(f"globalThis.result = undefined; (async () => {{ {script} }})();")
    while js.execute_pending_job():
        pass
    return json.loads(js.eval("JSON.stringify(result ?? null)"))


def test_shows_the_controllers_water_and_room(js: Any) -> None:
    result = run(
        js,
        """
        const card = Card();
        card.setConfig({ entity: "climate.floor" });
        const hass = { entities: ENTITIES };
        card.hass = hass;
        await settle();
        result = { config: created[0].config, hassPassed: created[0].hass === hass };
        """,
    )

    water, room, adjustments = result["config"]["cards"]
    assert result["config"]["type"] == "vertical-stack"
    assert water == {
        "type": "history-graph",
        "title": "Floor heating water",
        "hours_to_show": 72,
        "entities": [
            {"entity": "sensor.target", "name": "Controller target"},
            {"entity": "sensor.curve", "name": "Curve only"},
            {"entity": "number.setpoint", "name": "Pump setpoint"},
            {"entity": "sensor.water_out", "name": "Water out (measured)"},
        ],
    }
    assert room["entities"] == [
        {"entity": "climate.floor", "name": "Room"},
        {"entity": "sensor.outdoor", "name": "Outdoor used by the curve"},
        {"entity": "sensor.outdoor_ahead", "name": "Outdoor, next hours (forecast)"},
    ]
    assert adjustments == {
        "type": "history-graph",
        "title": "Adjustments",
        "hours_to_show": 72,
        "entities": [
            {"entity": "sensor.sun_cut", "name": "Sun cut"},
            {"entity": "sensor.learned", "name": "Learned offset"},
        ],
    }
    assert result["hassPassed"] is True


def test_adjustments_graph_only_when_there_is_something_to_show(js: Any) -> None:
    result = run(
        js,
        """
        const card = Card();
        card.setConfig({ entity: "climate.floor" });
        const entities = { ...ENTITIES };
        delete entities["sensor.sun_cut"];
        delete entities["sensor.learned"];
        card.hass = { entities };
        await settle();
        result = created[0].config.cards.map((graph) => graph.title);
        """,
    )

    assert result == ["Floor heating water", "Room and outdoor"]


def test_options_are_passed_on(js: Any) -> None:
    result = run(
        js,
        """
        const card = Card();
        card.setConfig({ entity: "climate.floor", title: "Floor", hours_to_show: 24 });
        card.hass = { entities: ENTITIES };
        await settle();
        result = created[0].config.cards.map((graph) => [graph.title, graph.hours_to_show]);
        """,
    )

    assert result == [["Floor", 24], ["Room and outdoor", 24], ["Adjustments", 24]]


def test_rebuilds_only_when_its_entities_change(js: Any) -> None:
    result = run(
        js,
        """
        const card = Card();
        card.setConfig({ entity: "climate.floor" });
        card.hass = { entities: ENTITIES };
        await settle();
        const later = { entities: ENTITIES };
        card.hass = later;
        card.hass = { entities: { ...ENTITIES } };
        await settle();
        const afterStateUpdates = created.length;
        const withoutOutdoor = { ...ENTITIES };
        delete withoutOutdoor["sensor.outdoor"];
        card.hass = { entities: withoutOutdoor };
        await settle();
        result = {
          afterStateUpdates,
          latestHassPassed: created[0].hass !== undefined,
          rebuilt: created.length,
          roomLines: created[1].config.cards[1].entities.length,
        };
        """,
    )

    assert result == {
        "afterStateUpdates": 1,
        "latestHassPassed": True,
        "rebuilt": 2,
        "roomLines": 2,
    }


def test_unknown_thermostat_shows_a_message(js: Any) -> None:
    result = run(
        js,
        """
        const card = Card();
        card.setConfig({ entity: "climate.gone" });
        card.hass = { entities: ENTITIES };
        await settle();
        const shown = card.children[0];
        result = { tag: shown.tag, text: shown.children[0].textContent, created: created.length };
        """,
    )

    assert result == {
        "tag": "ha-card",
        "text": "Floor heating thermostat not found: climate.gone",
        "created": 0,
    }


@pytest.mark.parametrize("config", ["{}", '{ entity: "sensor.indoor" }', "null"])
def test_refuses_anything_but_a_thermostat(js: Any, config: str) -> None:
    result = run(
        js,
        f"""
        try {{ Card().setConfig({config}); result = "accepted"; }}
        catch (error) {{ result = error.message; }}
        """,
    )

    assert result == "Choose the Floor heating thermostat of a Gree Versati"


def test_card_picker(js: Any) -> None:
    result = run(
        js,
        """
        const Element = customElements.get("gree-versati-floor-heating-card");
        result = {
          listed: window.customCards.map((card) => card.type),
          stub: Element.getStubConfig({ entities: ENTITIES }),
          form: Element.getConfigForm().schema.map((field) => field.name),
        };
        """,
    )

    assert result == {
        "listed": ["gree-versati-floor-heating-card"],
        "stub": {"entity": "climate.floor"},
        "form": ["entity", "title", "hours_to_show"],
    }
