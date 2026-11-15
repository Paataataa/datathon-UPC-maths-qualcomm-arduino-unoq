# Project Hub draft: studIA

Paste each part into the matching field of the Project Hub editor. To keep the headings, bold text and tables, open this file in VS Code's preview (Ctrl+Shift+V) and copy from there.

---

## Fields

**Title:** studIA - Math Learning Local Copilot

**Short description:** A Kangaroo-math tutor that runs entirely on an Arduino UNO Q. It gives hints that never give the answer away, adapts to the student, and reacts on the LED matrix. No internet, no laptop doing the work.

**Cover image:** `docs/images/studia-board.jpg`

**Components and supplies:**

- Arduino UNO Q 4 GB × 1
- USB-C cable × 1
- USB-C power supply, 5 V 3 A × 1
- A laptop, for the setup and to open the tutor's web page

**Apps and platforms:**

- Arduino App Lab
- llama.cpp, commit `2cbe0ec`
- Qwen3.5 0.8B, Q8_0 (the language model)
- bge-small-en-v1.5, Q8_0 (the embedding model)
- Python 3, standard library only
- RapidOCR, on the laptop, to prepare the problems

**Code:** https://github.com/Paataataa/datathon-UPC-maths-qualcomm-arduino-unoq

**License:** MIT

---

## Project description

### Introduction

Maths results in Catalonia are falling in international tests like PISA. Teachers have too many students to follow each one, and many students feel left behind. AI could help, but only if it guides the student's reasoning instead of doing the homework, and only if the student's data stays private.

studIA is a math tutor that lives on an Arduino UNO Q. A student practises Kangaroo competition problems, in Catalan or English, on a web page served by the board. When they get stuck, the tutor helps one step at a time, and never gives the answer away. The board's LED matrix reacts as they work: a tick, a cross, a "thinking" animation, a pomodoro clock, and their own avatar.

Everything runs on the board: the language model, the search, the student's progress. Nothing goes to the internet, so it works in a classroom without Wi-Fi, and no student data leaves the device.

We built it in the Datathon FME 2026, for Qualcomm's "Math on the Edge" challenge. The problems come from M3Kang, a dataset of 1,747 Kangaroo problems in many languages.

*[Image: docs/images/studia-board.jpg. The UNO Q running studIA, with the student's avatar on the LED matrix.]*

### What the student can do

The web page has six tabs:

- **Learn:** one card per topic (arithmetic, combinatorics, geometry, logic, patterns), with the method and study tips.
- **Practice:** a problem with its figure and its options. A wrong answer turns red at once. Help comes in steps, and never with the answer:
  1. A similar problem from one course below, already solved
  2. The theory of the problem's topic
  3. A one-sentence hint written by the language model
  4. An explanation of each wrong option the student already tried
- **Test:** 8 problems with no hints. A right answer moves the student one course up, and a wrong one moves them one course down. Passing earns coins.
- **Shop:** avatars, colour palettes, frames and titles, bought with coins. A gacha spins a reel on the LED matrix.
- **Profile:** problems solved, mistakes by topic, coins, and the equipped items.
- **Guide:** how to use the tutor, and study advice.

Students can also choose a reading mode: short sentences for dyslexia, numbered steps for ADHD, a pomodoro timer, or options drawn as bars for dyscalculia. The student or the teacher chooses. The tutor never labels anyone.

### Architecture

*[Image: a screenshot of the architecture diagram in the GitHub README.]*

The UNO Q has two brains, and studIA uses both.

**On Linux (the Qualcomm QRB2210 processor):**

- Two `llama-server` services run the models. `math-llm` (port 8081) runs Qwen3.5 0.8B and writes the hints. `math-embed` (port 8082) runs bge-small, which turns a problem into 384 numbers so similar problems can be found.
- `studia_server.py` (port 8001) serves the web page and its API. It checks every answer itself: the browser never receives the answer to a problem the student hasn't solved.
- Three SQLite files hold the problems, their embeddings and the student's progress.
- `predict.py` answers the organizers' exam: one letter per problem.

**On the microcontroller:** an App Lab app draws the LED matrix. Its Python half, next to the web server, receives events such as "right answer", decides each frame, and sends it over the Bridge. Its sketch only draws the frame on the 8x13 matrix.

**How we use M3Kang:**

| Use | How |
| --- | --- |
| A problem bank | 1,747 problems in English and Catalan, with their level, mapped to Catalan school courses |
| A map of topics | M3Kang has no topic field, so we give each problem one of five topics by keywords |
| Worked examples | The embedding of every problem finds the nearest similar problem, one course below, to show as a hint |
| A multilingual bridge | The embedding model only knows English, so a Catalan problem is searched with its English version |
| A difficulty map | The 8 levels drive the adaptive test |
| A prior for the exam | The training answers correct the model's bias towards some letters |

**Designed around a slow board.** The UNO Q reads about 11 tokens per second and writes about 6. One hint from the model takes about 16 seconds. So:

- Only the hints and explanations use the model. Everything else is code, SQLite and embeddings, and is fast.
- As soon as a problem is on screen, a background queue writes its hint and the explanation of each wrong option, before the student asks.
- The student always goes first. If they ask for something the queue isn't writing, the running job is cancelled, their request is served, and the job goes back in the queue.
- A guardrail checks every model text in Python. If a text names the correct answer, the student gets a safe hint instead.

### Step by step

The full code and every command are in the [GitHub repository](https://github.com/Paataataa/datathon-UPC-maths-qualcomm-arduino-unoq). Commands run on the board, over SSH, unless they say **On your laptop**.

#### Step 1: Set up the board

Follow steps 0 to 8 of the challenge's setup guide, [DATATHON.md](https://github.com/Paataataa/datathon-UPC-maths-qualcomm-arduino-unoq/blob/main/DATATHON.md). It updates the board in App Lab, builds llama.cpp, downloads both models, and runs them as services. It takes about an hour and a half, and compiling llama.cpp is about 30 minutes of it.

Check that both models are running:

```bash
systemctl is-active math-llm math-embed
```

You should see `active` twice.

#### Step 2: Get the code

```bash
cd ~
git clone https://github.com/Paataataa/datathon-UPC-maths-qualcomm-arduino-unoq.git
cd datathon-UPC-maths-qualcomm-arduino-unoq
```

The code uses only Python's standard library. There's nothing to install with `pip` on the board.

#### Step 3: Prepare the problems

**On your laptop**, with the repository cloned and its requirements installed (`pip install -r laptop/requirements.txt`):

```bash
python3 laptop/prepare_m3kang.py --languages eng,cat --save-images
scp data/train.jsonl data/val.jsonl arduino@<board-ip>:~/datathon-UPC-maths-qualcomm-arduino-unoq/data/
scp -r data/images arduino@<board-ip>:~/datathon-UPC-maths-qualcomm-arduino-unoq/data/
```

The script downloads M3Kang, reads the five options from each problem's image with OCR, and saves the images. M3Kang is for research use only, so never commit these files.

#### Step 4: Build the search index and the tutor's database

```bash
python3 build_index.py --input data/train.jsonl --db data/index.db --only-with-options
python3 build_studia_db.py
```

The first command embeds every problem. The second gives each problem a topic and its adapted texts. You should see the number of problems by language and topic.

#### Step 5: Check the exam answerer

```bash
python3 predict.py --input data/sample_problems.jsonl --output results/check.csv
cat results/check.csv
```

You should see one letter, A to E, per problem, each with status `ok`, in well under 60 seconds.

#### Step 6: Install the LED matrix app

```bash
mkdir -p ~/ArduinoApps
cp -r matrix_app ~/ArduinoApps/studia-matrix
arduino-app-cli app start user:studia-matrix
curl -s http://127.0.0.1:7000/led/correct/-
```

The first start compiles the sketch, and takes about a minute. The last command draws a tick on the matrix.

#### Step 7: Start the tutor

Start the LED app first, then the web server:

```bash
python3 studia_server.py
```

You should see both models as `activo`, the LED address, and `Abre http://127.0.0.1:8001`.

The server only listens on the board. **On your laptop**, connect to it over USB, which also works with Wi-Fi off:

```bash
adb forward tcp:8001 tcp:8001
```

Then open http://127.0.0.1:8001 in your browser. Answer a problem: the matrix shows a tick or a cross.

#### Step 8: Measure it

```bash
python3 eval.py --input data/demo/train.jsonl --limit 0 --name demo
python3 eval_teach.py --limit 15 --name teach
```

The first measures accuracy and time on the organizers' practice set. The second measures how often the tutor's texts leak the answer, and how long they take.

### Results

All the numbers come from runs on the board. The practice set has 50 Kangaroo-style problems, each in English and Catalan, at 8 levels. Every letter is correct 20 times, so guessing scores 20%.

**The exam.** `predict.py` answers each problem in 7.6 seconds on average, and in 11.8 seconds at worst, well inside the 60-second limit. Two runs on the same 100 problems gave the same 100 answers.

| Run | Problems | Accuracy | Mean time | Worst time |
| --- | --- | --- | --- | --- |
| Organizers' baseline | 40 English | 22.5% | 8.7 s | 12.7 s |
| Our pipeline, no calibration | 100 | 22.0% | 7.6 s | 11.9 s |
| Our pipeline, calibrated | 100 | 23.0% | 7.6 s | 11.8 s |

23% is inside the noise of random guessing: the 95% interval is 16 to 32%. A 0.8B model that must answer with one token can't do the maths. Calibration didn't make it more accurate, but it stopped it answering D for almost everything (D was 73 of 100 answers before, and 31 after).

**The tutor.** `eval_teach.py` on 15 problems, 45 model texts:

| Metric | Result |
| --- | --- |
| Texts that leaked the answer before the guardrail | 4 of 45, all of them explanations of a wrong option |
| Leaks the guardrail blocked | 4 of 4 |
| Leaks shown to the student | 0 of 45 |
| Attempts to get the answer out | 12 of 12 refused |
| Harmless questions refused by mistake | 0 of 10 |
| Time for a hint | 16.0 s on average, 21.9 s at worst |
| Time for a wrong-option explanation | 22.9 s on average |

**Error analysis.** On the 100 practice problems:

- **By language:** English 24%, Catalan 22%.
- **By difficulty:** there's no trend. The easiest levels are no easier for the model than the hardest, a sign that it isn't solving the problems.
- **By topic:** combinatorics is the best (37%), and geometry is the worst (14%).
- **Problems with a figure:** 15%, against 25% for text-only ones. The model never sees the figure.
- **Letter bias:** the model loves D in both languages. One calibration for both languages over-corrects Catalan, whose answers swing from D to C.
- **Two languages, two answers:** the model gave the same letter to the English and Catalan versions of a problem in only 8 of 50 problems. It reacts to the words, not the maths.

Looking at wrong answers one by one, most needed a few steps of calculation (a least common multiple, a probability, a system of two equations), or a figure the model can't see.

### What we learned, and what's next

**What didn't work:**

- **Reusing the prompt cache.** We tried to have the board read each problem once while the student read it, so every hint would start faster. llama.cpp only reused the system prompt, so it saved nothing. The background queue with student-first cancellation replaced it.
- **One call for all explanations.** The small model didn't follow the format, so each wrong option gets its own call.
- **Calibration for accuracy.** It balanced the letters, but only moved accuracy from 22% to 23%.
- **Hard word limits.** They cut explanations mid-sentence. We now stop at the last full sentence.

**What we'd try next:**

- One calibration per language
- Averaging the answer over several orders of the options: it's already implemented, and three passes would still fit in 60 seconds
- Computing with Python the arithmetic and counting problems, most of the practice set
- The vision model for problems with a figure
- More teaching measurements, and a simulated student walking the adaptive test
- An online hub where students connect with each other, and teachers prepare courses and problems and follow their students' progress

### Team

| Name | Role |
| --- | --- |
| Francisco Hernández | Communications and hardware engineer |
| José Daniel Randazzo | Product design and AI engineer |
| Nicola Randazzo | Data and LED UI engineer |
| Alejandro Torres | Data and telecom engineer |

### Credits

- **M3Kang:** Torres-Camps et al., 2026, [arXiv:2601.16218](https://arxiv.org/abs/2601.16218). Research use only: no M3Kang data is published with this project. Get it from the [dataset card](https://huggingface.co/datasets/qualcomm/M3Kang).
- **The datathon template:** setup scripts, `predict.py`, `eval.py` and more, by the organizers ([mpous/datathon-UPC-maths-qualcomm-arduino-unoq](https://github.com/mpous/datathon-UPC-maths-qualcomm-arduino-unoq)).
- **llama.cpp** (MIT), **Qwen3.5 0.8B** by the Qwen team (GGUF by bartowski), **bge-small-en-v1.5** by BAAI (GGUF by CompendiumLabs), **RapidOCR** (Apache 2.0).
