// CH720N display — BLE image receiver
// XIAO nRF52840 + Waveshare e-Paper Driver HAT Rev2.3 + 1.54" B/W V2 (SSD1681, 200x200)
// Board: "Seeed nRF52 Boards" -> Seeed XIAO nRF52840 (non-mbed core, uses Bluefruit)
// Wiring/HAT settings: same as epd_test.
//
// On boot: shows fallback.h, then advertises as "CH720N-EPD" and waits for images from
// ch720n_media_preview.py / epd_ble.py. The panel is powered only while it refreshes.
//
// GATT service c7201000-8f3e-4b8a-9c4d-1a2b3c4d5e6f
//   ...1001 image   (write)        [offset u16 LE][bytes...], sequential, 5000 bytes total
//   ...1002 control (write)        0x01 = begin transfer
//                                  0x02 [crc32 u32 LE] = commit: check and show the image
//   ...1003 status  (read/notify)  see ST_* below
//
// fallback.h is generated from ../fallback.png: `python epaper_image.py fallback.png epd_ble/fallback.h`.

#include <SPI.h>
#include <bluefruit.h>
#include <initializer_list>
#include "fallback.h"

// Pins (see wiring.md). SCK = D8, MOSI = D10 are the hardware SPI defaults.
const int PIN_BUSY = 0;   // D0  P0.02
const int PIN_CS   = 1;   // D1  P0.03
const int PIN_DC   = 2;   // D2  P0.28
const int PIN_RST  = 3;   // D3  P0.29
const int PIN_PWR  = 4;   // D4  P0.04  HIGH = panel powered

const int W = 200, H = 200;
const int BUF = W / 8 * H;  // 5000 bytes

SPISettings epdSpi(4000000, MSBFIRST, SPI_MODE0);

// ---------- status codes (notified on the status characteristic) ----------
enum : uint8_t {
  ST_IDLE       = 0x00,
  ST_ACCEPTED   = 0x01,   // image complete + CRC ok, refresh starting
  ST_DONE       = 0x02,   // refresh finished, ready for the next image
  ST_RECEIVING  = 0x03,   // begin received
  ST_ERR_ORDER  = 0xE1,   // chunk offset out of sequence / overflow
  ST_ERR_SHORT  = 0xE2,   // commit before 5000 bytes arrived
  ST_ERR_CRC    = 0xE3,
  ST_ERR_BUSY   = 0xE4,   // commit while a refresh is still running
  ST_ERR_PANEL  = 0xE5,   // BUSY timeout from the panel
};

// ---------- BLE ----------
const uint8_t UUID_SVC[16]  = {0x6F,0x5E,0x4D,0x3C,0x2B,0x1A,0x4D,0x9C,0x8A,0x4B,0x3E,0x8F,0x00,0x10,0x20,0xC7};
const uint8_t UUID_IMG[16]  = {0x6F,0x5E,0x4D,0x3C,0x2B,0x1A,0x4D,0x9C,0x8A,0x4B,0x3E,0x8F,0x01,0x10,0x20,0xC7};
const uint8_t UUID_CTL[16]  = {0x6F,0x5E,0x4D,0x3C,0x2B,0x1A,0x4D,0x9C,0x8A,0x4B,0x3E,0x8F,0x02,0x10,0x20,0xC7};
const uint8_t UUID_STAT[16] = {0x6F,0x5E,0x4D,0x3C,0x2B,0x1A,0x4D,0x9C,0x8A,0x4B,0x3E,0x8F,0x03,0x10,0x20,0xC7};

BLEService        epdSvc(UUID_SVC);
BLECharacteristic imgChr(UUID_IMG);
BLECharacteristic ctlChr(UUID_CTL);
BLECharacteristic statChr(UUID_STAT);

uint8_t rxBuf[BUF];             // filled by BLE writes
uint8_t frame[BUF];             // copy that the panel is drawn from
volatile int  rxLen = 0;
volatile bool rxOk = false;     // false after an out-of-order chunk until the next begin
volatile bool framePending = false;
volatile bool refreshing = false;

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
  return true;
}

// ---------- panel ----------
void panelPowerOn() {
  pinMode(PIN_CS, OUTPUT);   digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_DC, OUTPUT);
  pinMode(PIN_RST, OUTPUT);
  digitalWrite(PIN_PWR, HIGH);
  delay(10);                       // let AO3401 + RT9193 settle
  SPI.begin();
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

bool refresh() {
  cmd(0x22); dat(0xF7);                       // full update
  cmd(0x20);
  return waitBusy(10000);
}

void setCursor0() {
  cmd(0x4E); dat(0x00);
  cmd(0x4F); dat(0xC7); dat(0x00);
}

bool showBuffer(const uint8_t *img) {
  setCursor0();
  cmd(0x24);
  digitalWrite(PIN_DC, HIGH);
  digitalWrite(PIN_CS, LOW);
  SPI.beginTransaction(epdSpi);
  for (int i = 0; i < BUF; i++) SPI.transfer(img[i]);
  SPI.endTransaction();
  digitalWrite(PIN_CS, HIGH);
  return refresh();
}

void sleepPanel() {
  cmd(0x10); dat(0x01);                       // deep sleep mode 1
  delay(100);
}

// Full cycle: power on, init, draw, deep sleep, power off. The image stays without power.
bool drawImage(const uint8_t *img) {
  uint32_t t0 = millis();
  panelPowerOn();
  bool ok = init154() && showBuffer(img);
  if (ok) sleepPanel();
  panelPowerOff();
  Serial.printf("draw %s in %lu ms\n", ok ? "ok" : "FAILED", millis() - t0);
  return ok;
}

// ---------- transfer ----------
uint32_t crc32(const uint8_t *d, int n) {     // same as Python zlib.crc32
  uint32_t c = 0xFFFFFFFF;
  for (int i = 0; i < n; i++) {
    c ^= d[i];
    for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320 & (0 - (c & 1)));
  }
  return ~c;
}

void setStatus(uint8_t s) {
  statChr.write8(s);
  if (Bluefruit.connected()) statChr.notify8(s);
}

void onImage(uint16_t, BLECharacteristic *, uint8_t *data, uint16_t len) {
  if (len < 2 || !rxOk) return;
  int off = data[0] | (data[1] << 8);
  int n = len - 2;
  if (off != rxLen || off + n > BUF) {
    rxOk = false;
    Serial.printf("chunk out of order: off %d, expected %d\n", off, rxLen);
    setStatus(ST_ERR_ORDER);
    return;
  }
  memcpy(rxBuf + off, data + 2, n);
  rxLen = off + n;
}

void onControl(uint16_t, BLECharacteristic *, uint8_t *data, uint16_t len) {
  if (len < 1) return;
  if (data[0] == 0x01) {                      // begin
    rxLen = 0;
    rxOk = true;
    setStatus(ST_RECEIVING);
  } else if (data[0] == 0x02 && len >= 5) {   // commit
    uint32_t want = data[1] | (data[2] << 8) | (data[3] << 16) | ((uint32_t)data[4] << 24);
    if (!rxOk || rxLen != BUF) { setStatus(ST_ERR_SHORT); return; }
    if (crc32(rxBuf, BUF) != want) { setStatus(ST_ERR_CRC); return; }
    if (refreshing || framePending) { setStatus(ST_ERR_BUSY); return; }
    memcpy(frame, rxBuf, BUF);
    rxOk = false;
    framePending = true;
    setStatus(ST_ACCEPTED);
  }
}

void onConnect(uint16_t conn) {
  BLEConnection *c = Bluefruit.Connection(conn);
  char name[32] = {0};
  c->getPeerName(name, sizeof(name));
  Serial.printf("connected: %s\n", name);
  c->requestConnectionParameter(12);          // 15 ms interval for faster transfers (central may refuse)
}

void onDisconnect(uint16_t, uint8_t reason) {
  Serial.printf("disconnected, reason 0x%02X\n", reason);
  rxOk = false;
}

void setupBle() {
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);  // MTU up to 247 -> 244-byte writes
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName("CH720N-EPD");
  Bluefruit.Periph.setConnectCallback(onConnect);
  Bluefruit.Periph.setDisconnectCallback(onDisconnect);

  epdSvc.begin();

  imgChr.setProperties(CHR_PROPS_WRITE);
  imgChr.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  imgChr.setMaxLen(244);
  imgChr.setWriteCallback(onImage);
  imgChr.begin();

  ctlChr.setProperties(CHR_PROPS_WRITE);
  ctlChr.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  ctlChr.setMaxLen(8);
  ctlChr.setWriteCallback(onControl);
  ctlChr.begin();

  statChr.setProperties(CHR_PROPS_READ | CHR_PROPS_NOTIFY);
  statChr.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  statChr.setFixedLen(1);
  statChr.begin();
  statChr.write8(ST_IDLE);

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(epdSvc);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);  // 20 ms fast, 152.5 ms slow (units of 0.625 ms)
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);              // advertise forever
}

// ---------- main ----------
void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) {}  // wait for monitor (max 2 s)
  Serial.println("\nCH720N EPD BLE");

  pinMode(PIN_PWR, OUTPUT);  digitalWrite(PIN_PWR, LOW);
  pinMode(PIN_BUSY, INPUT);

  drawImage(fallback_img);
  setupBle();
  Serial.println("advertising as CH720N-EPD");
}

void loop() {
  if (framePending) {
    refreshing = true;
    framePending = false;
    bool ok = drawImage(frame);
    refreshing = false;
    setStatus(ok ? ST_DONE : ST_ERR_PANEL);
  }
  delay(20);
}
