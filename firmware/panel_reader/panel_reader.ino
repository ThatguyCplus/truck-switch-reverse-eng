/*
 * Scania 2545507 steering-wheel switch panel reader
 * ---------------------------------------------------------------
 * Reverse-engineered board: SKNY_FXP1 V1.1
 *
 * Three analog resistor ladders, each 62 / 100 / 150 / 390 ohm taps
 * with a 2k pulldown.  Grounding a tap gives a distinct resistance.
 *
 * WIRING  (J1 = Molex DuraClik 560020-0620, 6-pin)
 *   J1.1  backlight +   -> 5V
 *   J1.2  ladder A      -> A0   + 1k resistor from A0 to 5V
 *   J1.3  ladder B      -> A1   + 1k resistor from A1 to 5V
 *   J1.4  ladder C      -> A2   + 1k resistor from A2 to 5V
 *   J1.5  ground        -> GND
 *   J1.6  backlight -   -> GND (always on), or MOSFET drain for PWM dim on D9
 *
 * Streams raw averaged ADC counts so the host GUI can decode and
 * re-tune thresholds without reflashing:
 *      a,b,c\n
 * Lines beginning with '#' are informational.
 */

const uint8_t PINS[3]   = {A0, A1, A2};
const uint8_t OVERSAMPLE = 4;
const uint8_t BACKLIGHT_PIN = 9;      // optional low-side MOSFET gate

// Read one channel, discarding the first conversion after the mux
// switches so the sample-and-hold has time to settle.
int readChannel(uint8_t pin) {
  analogRead(pin);
  long sum = 0;
  for (uint8_t i = 0; i < OVERSAMPLE; i++) sum += analogRead(pin);
  return (int)(sum / OVERSAMPLE);
}

void setup() {
  Serial.begin(115200);
  pinMode(BACKLIGHT_PIN, OUTPUT);
  analogWrite(BACKLIGHT_PIN, 255);    // full brightness; PWM here to dim
  delay(50);
  Serial.println(F("#SCANIA_PANEL_V1"));
}

void loop() {
  int v0 = readChannel(PINS[0]);
  int v1 = readChannel(PINS[1]);
  int v2 = readChannel(PINS[2]);

  Serial.print(v0); Serial.print(',');
  Serial.print(v1); Serial.print(',');
  Serial.println(v2);

  // Host may send a backlight level 0-255 as "B<n>\n"
  while (Serial.available()) {
    if (Serial.read() == 'B') {
      int level = Serial.parseInt();
      analogWrite(BACKLIGHT_PIN, constrain(level, 0, 255));
    }
  }

  delay(15);                          // ~60 Hz update
}
