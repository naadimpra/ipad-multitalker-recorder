# ipad-multitalker-recorder

A self-hosted web app that records several iPads at the same time and keeps them in sync. Each iPad opens a
web page and records raw audio; one admin phone or laptop starts and stops everything. Everything stays on
your own machine.

- **Record**: raw 24-bit audio per iPad (echo cancellation, noise suppression and auto-gain are off),
  uploaded live to your laptop, with a local retry if Wi-Fi drops.
- **Control**: one admin page shows every device's state and level, assigns positions and IDs, and starts and
  stops with a slide gesture.
- **Sync**: the admin device plays a chirp at the start and end of each session. Afterwards the tracks are aligned
  to the sample and each iPad's clock drift is corrected.
- **Optional labels**: a distraction-free participant screen where people tap an emotion while they speak.
- **Tools**: align and merge tracks, draft speech and overlap regions, attach labels to speech segments.

| Mode | Hardware | Output |
|---|---|---|
| **A** | 1 iPad + 4-channel lavalier receiver | `lav.wav` |
| **B** | Any number of iPads (e.g. one per speaker) + optional iPad in the center | one aligned WAV per device, named after its device name |

The number of devices is not fixed. Each iPad gets a **device name** you type (or pick from the quick chips S1–S4,
Center, Lav). Names are free text (letters, digits, `-`, `_`) and become the file names, so `spk1`, `alice` or
`table-left` all work. `center` and `lav` are treated as room/receiver mics; every other name is a speaker iPad.

## Requirements

- A laptop on the same Wi-Fi as the iPads. macOS is assumed by `make_cert.sh`; on Linux, create the
  certificate with `mkcert` yourself and put `cert.pem` / `key.pem` in `certs/`.
- Python 3 (developed with 3.14) and [mkcert](https://github.com/FiloSottile/mkcert).
- iPads running iPadOS 14.5 or newer (Safari needs AudioWorklet).

## Setup (laptop)

```bash
brew install mkcert
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./make_cert.sh            # run again if the laptop's Wi-Fi IP changes
.venv/bin/python server.py
```

The server prints three URLs. Every device (iPads and the admin device) must be on the same Wi-Fi.
Use `--out <folder>` to store recordings somewhere other than `recordings/`.

## Setup (each iPad and the admin device)

Safari only allows the microphone over trusted HTTPS, so each device must trust the laptop's certificate:

1. Open `http://<laptop-ip>:8080/` and tap **Download rootCA.pem**.
2. Settings → General → VPN & Device Management → install the profile.
3. Settings → General → About → Certificate Trust Settings → turn on full trust.
4. On the iPads, set Settings → Display & Brightness → **Auto-Lock → Never**, and turn on
   Settings → Accessibility → **Guided Access** so the page can't be left by accident.

## Running a session

1. On each iPad, open `https://<laptop-ip>:8443/record`, type a device name (or tap a quick chip: S1–S4, Center, Lav),
   tap **Arm microphone** and allow mic access. Then triple-click the Home button to start Guided Access.
2. On the admin device (a phone or a Mac), open `https://<laptop-ip>:8443/control`. The big status shows
   whether everything is ready ("Ready", or "N to fix" with the reasons underneath).
   - **Tap a device row** to expand it: device name, participant ID, label language, participant view,
     Identify (flashes that iPad's screen). A coloured dot and a few flags show its state; a flag only
     appears when something is wrong.
   - **Disarm** (in the expanded row, or **Disarm all** in the Speaker iPads card) releases an iPad's microphone
     between sessions. It is disabled while recording.
   - **Swipe an offline device left** to remove it (or use Remove in its expanded row).
3. **Slide to start** (drag the handle all the way right). About 1.5 s later the admin device plays the
   **sync chirp**: hold it near the middle of the table, with media volume at about 70%.
   While recording, **♪ Chirp** plays an extra chirp and **⚑ Marker** adds a marker (tap) or a labelled
   marker (hold).
4. **Slide to stop**. The admin device plays an end chirp, then every iPad uploads what is left.
   Wait for "Session saved".

If a banner appears at the top of the admin page, an iPad is running an old copy of the page or the server
needs a restart after a code change: reload that iPad's page (close the tab if needed) or restart
`python server.py`.

Each device needs a unique name when a session starts; any number of armed devices can record at once.

Each session is saved in `recordings/<date>_<name>/`: one WAV per device plus `session.json` (devices,
participant IDs, chirp and marker times, interruptions, labels).

## Updates

If you installed with `git clone`, the server checks GitHub every 6 hours for new commits on `main`. When there
are any, it prints a notice in the terminal and shows a banner on the admin page. Update with `git pull` and
restart the server. It only reads the public GitHub API and sends nothing about your setup. Use
`--no-update-check` to turn it off, or `--repo owner/name --branch name` if you run a fork. A downloaded zip
(no `.git` folder) can't check for updates.

## Participant view and labels (optional)

Each speaker iPad can switch to a **participant view**: a simple screen with eight emotion buttons (Neutral,
Happy, Sad, Angry, Surprised, Fear, Disgust, Contempt), a recording timer, a level bar and a status chip.
No device settings are shown. The layout adapts to the orientation (4 × 2 in landscape, 2 × 4 in portrait).

- Switch it from the admin device (**Speaker iPads → Participant view**, or per device), or tap
  **Participant view** on the iPad. The **‹** button returns to setup; it never stops a recording.
- A participant taps the emotion that matches how they feel while speaking. It stays selected until they tap
  another one, and every recording starts with the selected emotion.
- Labels can be shown in Japanese or English (the **日本語 | English** switch decides which label is big). The
  admin device can set it for all speaker iPads or per device. The buttons are defined in
  `static/emotions.js` (edit labels or add and remove emotions).

Labels are saved per device in `session.json` as an emotion plus an audio frame position.

## Post-processing

```bash
.venv/bin/python -m tools.align      recordings/<session>   # align tracks, correct clock drift
.venv/bin/python -m tools.draft_rttm recordings/<session>   # draft speech + overlap regions
.venv/bin/python -m tools.utterances recordings/<session>   # attach labels to speech segments
```

- **`align`** finds the start and end chirp in every track, corrects each iPad's clock drift (typically
  ±10–60 ppm, which grows to 100+ ms per hour) and shifts all tracks onto one timeline. It writes
  `aligned/*.wav`, `alignment.json`, `markers.tsv` and `emotions.tsv` (labels on the aligned timeline), and
  merges the speaker tracks into `aligned/combined.wav` (one channel per iPad, order in `combined.json`) and
  `aligned/mix_mono.wav`. Use `python -m tools.combine recordings/<session> --devices spk1,alice,center` to pick
  the channels. If a chirp is missing it warns, and that track falls back to network timestamps (accurate to
  roughly 20–100 ms).
- **`draft_rttm`** compares the close-talk channels' energy to produce `aligned/draft.rttm`, rejecting crosstalk
  and keeping real overlaps. It reports overlap as a share of speech time and lists each region in
  `aligned/overlap.tsv` (`--min-overlap` sets the minimum length, default 0.1 s). Treat it as a starting point
  for manual correction, not ground truth.
- **`utterances`** cuts each speech segment where the selected emotion changed and writes
  `aligned/utterances.tsv` (`speaker, device, start_s, end_s, dur_s, emotion`). Pass your own corrected segments
  with `--rttm yours.rttm`; `--shift 0.5` moves emotion changes 0.5 s earlier; `--min-piece` merges slivers.

If the server crashed mid-session, rebuild the WAVs from the raw files with
`python -m tools.common recordings/<session>`.

## Project layout

```
server.py        FastAPI + WebSocket server: receives audio, runs sessions
static/          recorder page (/record), admin page (/control), shared assets
tools/           post-processing: align, combine, draft_rttm, utterances
make_cert.sh     local HTTPS certificate for the laptop's IP
```

## Known limits

- **Multichannel input (mode A):** Safari on iPad usually delivers only **mono** from external interfaces. If the
  receiver mixes its mics down to one output you get one mixed track, and `draft_rttm` cannot split speakers
  from it. To keep separate channels, record them with a native multitrack app (or on the receiver itself) and
  play a chirp at the start and end so `align` can still sync them.
- The 7th-gen iPad has a Lightning port: a USB receiver needs the powered *Lightning to USB 3 Camera Adapter*.
- If Safari goes to the background (Home button, a tapped notification, a call), audio stops. The page fills
  the gap with silence and logs it in `session.json`. Guided Access prevents this.
- Uploads are acknowledged chunk by chunk, so a Wi-Fi drop only delays them as long as the recorder page stays
  open. Reloading an iPad's page during a recording loses that iPad's take.

## Tips

- Get consent from everyone you record. `recordings/` and `certs/` are git-ignored; never commit them.
- Keep the speaker iPads at about the same distance from their speakers (30–50 cm, facing them) so
  loudness-based tools stay comparable.
