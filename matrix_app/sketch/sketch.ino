// studIA matrix: dibuja en la matriz LED 8x13 el fotograma que le envía python/main.py.
// Toda la lógica de las animaciones está en Python; aquí solo se dibuja.
#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <vector>

Arduino_LED_Matrix matrix;

const uint8_t FRAME_SIZE = 8 * 13;
uint8_t frame[FRAME_SIZE] = {0};

void draw(std::vector<uint8_t> newFrame) {
  memcpy(frame, newFrame.data(), min(newFrame.size(), (size_t)FRAME_SIZE));
}

void setup() {
  matrix.begin();
  matrix.setGrayscaleBits(3);  // brillo 0..7
  matrix.clear();
  Bridge.begin();
  Bridge.provide("draw", draw);
}

void loop() {
  matrix.draw(frame);
  delay(10);
}
