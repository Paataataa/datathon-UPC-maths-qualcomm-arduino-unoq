# studIA matrix: la matriz LED del tutor

App de App Lab que dibuja en la matriz LED 8x13 lo que pasa en la web de studIA (`studia_server.py`):

| Evento | En la matriz |
| --- | --- |
| Acierto | Un tick que se dibuja |
| Fallo | Una cruz que tiembla |
| Pista o duda al LLM | Un punto que da vueltas mientras piensa (mínimo 1,5 s) |
| Pomodoro | Como un reloj: los minutos que quedan en grande y un punto con estela que da una vuelta al borde por minuto (el segundero). En pausa, a media luz. Al acabar, "00" y el borde laten 4 s |
| Gacha | Un rodillo de tragaperras que frena y se para en el objeto ganado |
| Nada | El avatar equipado en el perfil |

`python/main.py` decide cada fotograma y `sketch/sketch.ino` solo lo dibuja, por el Bridge. Nada parpadea más de tres veces por segundo.

## Instalarla en la placa

En la placa, desde el repositorio:

```bash
mkdir -p ~/ArduinoApps
cp -r matrix_app ~/ArduinoApps/studia-matrix
arduino-app-cli app start user:studia-matrix
```

La primera vez compila el sketch, y tarda un minuto. Comprueba que responde:

```bash
curl -s http://127.0.0.1:7000/state
```

Después arranca la web como siempre (`python3 studia_server.py`). Envía los eventos a `http://127.0.0.1:7000` (cámbialo con `--led-url`, o `--led-url ''` para no usar la matriz). Si la app no está en marcha, la web funciona igual y avisa una sola vez en el terminal.

Para probar las animaciones sin la web:

```bash
curl -s http://127.0.0.1:7000/led/correct/-
curl -s http://127.0.0.1:7000/led/thinking/start
curl -s http://127.0.0.1:7000/led/pomodoro/start-300-300
curl -s http://127.0.0.1:7000/led/gacha/rocket
```

Iconos disponibles: `smile`, `fox`, `owl`, `cat`, `robot`, `rocket`, `dino`, `panda`, `unicorn`, `palette`, `frame` y `crown`.
