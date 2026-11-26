# Floor heating control

Works out the floor water setpoint for a Gree Versati heat pump from the indoor and outdoor temperature, and from the weather forecast when one is chosen.

## Language

### The pump

**Pump reading**:
The pump as floor heating control sees it at one moment: its water setpoint, any hold, whether it is heating the floor, and whether heat is going in.
_Avoid_: pump state, pump data, pump status

**Hold**:
A reason on the pump's side not to change the setpoint, such as the pump being off, in error, not in a heating mode, or on its own weather curve.
_Avoid_: block, lock

**Space heating**:
The pump is heating the floor, rather than defrosting or making hot water.
_Avoid_: floor mode, heating mode

### The forecast

**Look-ahead**:
Using the average of the outdoor temperature now and the forecast for the next few hours in place of today's outdoor temperature, as a floor answers hours late.
_Avoid_: prediction, forecast temperature

**Sun cut**:
How far the water is lowered because the forecast shows sun a few hours ahead, so the floor isn't full of heat when the sun arrives.
_Avoid_: solar offset, sun correction
