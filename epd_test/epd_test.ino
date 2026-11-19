// CH720N display — bench test
// XIAO nRF52840 + Waveshare e-Paper Driver HAT Rev2.3 + 1.54" B/W V2 (SSD1681, 200x200)
// Board: "Seeed nRF52 Boards" -> Seeed XIAO nRF52840 (non-mbed core)
// HAT: BS = 0 (4-line SPI), Display Config = B (0.47R), VCC = 3.3 V
//
// fallback.h is generated from ../fallback.png: run `python epaper_image.py fallback.png epd_test/fallback.h` in the project root after changing it.
//
// Sequence: white -> fallback image -> black/white checker -> fallback image -> sleep -> panel power off

#include <SPI.h>
#include "fallback.h"
#include <initializer_list>

// Pins (see wiring.md). SCK = D8, MOSI = D10 are the hardware SPI defaults.
const int PIN_BUSY = 0;   // D0  P0.02
const int PIN_CS   = 1;   // D1  P0.03
const int PIN_DC   = 2;   // D2  P0.28
const int PIN_RST  = 3;   // D3  P0.29
const int PIN_PWR  = 4;   // D4  P0.04  HIGH = panel powered

const int W = 200, H = 200;
const int BUF = W / 8 * H;  // 5000 bytes

SPISettings epdSpi(4000000, MSBFIRST, SPI_MODE0);

// ---------- low level ----------
void cmd(uint8_t c) {
  digitalWrite(PIN_DC, LOW);
  digitalWrite(PIN_CS, LOW);
  SPI.beginTransaction(epdSpi);
  SPI.transfer(c);
  SPI.endTransaction();
  digitalWrite(PIN_CS, HIGH);
}

void dat(uint8_t d) {
  digitalWrite(PIN_DC, HIGH);
  digitalWrite(PIN_CS, LOW);
  SPI.beginTransaction(epdSpi);
  SPI.transfer(d);
  SPI.endTransaction();
  digitalWrite(PIN_CS, HIGH);
}

// SSD1681: BUSY HIGH = busy. Returns false on timeout.
bool waitBusy(uint32_t timeoutMs = 5000) {
  uint32_t t0 = millis();
  while (digitalRead(PIN_BUSY) == HIGH) {
    if (millis() - t0 > timeoutMs) {
      Serial.println("!! BUSY timeout — check BUSY wire, PWR, FPC, VCC");
      return false;
    }
    delay(2);
  }
  Serial.printf("   busy %lu ms\n", millis() - t0);
  return true;
}

// ---------- panel ----------
void panelPowerOn() {
  digitalWrite(PIN_PWR, HIGH);
  delay(10);                       // let AO3401 + RT9193 settle
}

void panelPowerOff() {
  // drive everything low first so the unpowered TXB0108 isn't back-fed
  SPI.end();
  for (int p : {PIN_CS, PIN_DC, PIN_RST, 8, 10}) { pinMode(p, OUTPUT); digitalWrite(p, LOW); }
  digitalWrite(PIN_PWR, LOW);
}

void reset() {
  digitalWrite(PIN_RST, HIGH); delay(10);
  digitalWrite(PIN_RST, LOW);  delay(10);
  digitalWrite(PIN_RST, HIGH); delay(10);
}

bool init154() {
  reset();
  if (!waitBusy()) return false;
  cmd(0x12);                                  // SW reset
  if (!waitBusy()) return false;

  cmd(0x01); dat(0xC7); dat(0x00); dat(0x01); // driver output: 200 gates
  cmd(0x11); dat(0x01);                       // data entry: X+, Y-
  cmd(0x44); dat(0x00); dat(0x18);            // RAM X 0..24 (x8 = 200 px)
  cmd(0x45); dat(0xC7); dat(0x00); dat(0x00); dat(0x00); // RAM Y 199..0
  cmd(0x3C); dat(0x01);                       // border waveform
  cmd(0x18); dat(0x80);                       // internal temp sensor
  cmd(0x22); dat(0xB1); cmd(0x20);            // load temp + LUT
  if (!waitBusy()) return false;

  cmd(0x4E); dat(0x00);                       // RAM X counter
  cmd(0x4F); dat(0xC7); dat(0x00);            // RAM Y counter
  return waitBusy();
}

void refresh() {
  cmd(0x22); dat(0xF7);                       // full update
  cmd(0x20);
  waitBusy(10000);
}

void setCursor0() {
  cmd(0x4E); dat(0x00);
  cmd(0x4F); dat(0xC7); dat(0x00);
}

void showBuffer(const uint8_t *img) {
  setCursor0();
  cmd(0x24);
  digitalWrite(PIN_DC, HIGH);
  digitalWrite(PIN_CS, LOW);
  SPI.beginTransaction(epdSpi);
  for (int i = 0; i < BUF; i++) SPI.transfer(img[i]);
  SPI.endTransaction();
  digitalWrite(PIN_CS, HIGH);
  refresh();
}

void showFill(uint8_t v) {                    // 0xFF white, 0x00 black
  setCursor0();
  cmd(0x24);
  for (int i = 0; i < BUF; i++) dat(v);
  refresh();
}

void showChecker() {                          // 25 px squares
  setCursor0();
  cmd(0x24);
  for (int y = 0; y < H; y++)
    for (int xb = 0; xb < W / 8; xb++) {
      uint8_t b = 0;
      for (int bit = 0; bit < 8; bit++) {
        int x = xb * 8 + bit;
        if (((x / 25) + (y / 25)) & 1) b |= 0x80 >> bit;
      }
      dat(b);
    }
  refresh();
}

void sleepPanel() {
  cmd(0x10); dat(0x01);                       // deep sleep mode 1
  delay(100);
}

// ---------- main ----------
void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) {}  // wait for monitor (max 3 s)
  Serial.println("\nCH720N EPD test");

  pinMode(PIN_PWR, OUTPUT);  digitalWrite(PIN_PWR, LOW);
  pinMode(PIN_CS, OUTPUT);   digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_DC, OUTPUT);
  pinMode(PIN_RST, OUTPUT);
  pinMode(PIN_BUSY, INPUT);

  panelPowerOn();
  SPI.begin();

  Serial.println("init");
  if (!init154()) { panelPowerOff(); return; }

  Serial.println("white");     showFill(0xFF);
  Serial.println("fallback");  showBuffer(fallback_img);
  delay(3000);
  Serial.println("checker");   showChecker();
  delay(3000);
  Serial.println("fallback");  showBuffer(fallback_img);

  sleepPanel();
  panelPowerOff();
  Serial.println("done — image should stay with power off");
}

void loop() {}
