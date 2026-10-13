# simcoach — a live AI driving coach for iRacing

simcoach listens to iRacing's live telemetry and talks to you while you drive.

- **It coaches on its own.** After each corner it compares what you did with your
  reference lap. When a corner costs you time, it tells you why on the next straight:
  *"Braking too late into Turn 2, you overshot and lost two tenths. Try braking about
  15 meters sooner next lap."*
- **You can ask it things.** Hold a button on your wheel and ask *"How can I improve
  turn 2?"* or *"Where am I losing the most time?"* An LLM answers out loud, using your
  actual brake points, minimum speeds and throttle pickup for that corner.
- **It improves with you.** Every clean personal best becomes the new reference.
  You can also import a faster lap from Garage 61 or an iRacing `.ibt` file.

```
 iRacing SDK (60 Hz) ──► lap builder ──► corner analysis vs reference ──► callout scheduler ──► TTS
                                              │                           (only on straights)
 wheel button ─► mic ─► Whisper (local) ─► question ─► LLM + telemetry context ─────────────────► TTS
```

## Design decisions

**Callouts use rules, and questions use an LLM.** The coaching you hear while driving
comes from deterministic rules, with no LLM in that path. That keeps callouts instant,
free, and consistent, and the numbers in them are always real. The LLM handles open
questions. It gets a compact summary of the telemetry for each corner (brake point,
peak pressure, entry, minimum and exit speed, throttle pickup, coasting) for your
recent laps and your reference, so its answers are grounded in your data.

**Cloud or local LLM.** The default is Claude in the cloud, and I'd recommend keeping it:

| | Claude (default) | Local via Ollama |
|---|---|---|
| Answer quality | Best | Good with 7–14B models |
| Load on your PC | None | Competes with iRacing for GPU/VRAM, so frame drops are possible |
| Cost | Pay per question (fractions of a cent with `effort: low`) | Free |
| Offline | No | Yes |

The LLM only runs when you ask a question, and the always-on parts (Whisper speech
recognition and TTS) are already local. Speech recognition runs on the **CPU** by default
so iRacing keeps the GPU. If you have a card with 12 GB+ VRAM, or a second PC on your
network, Ollama works well: set `llm.provider: ollama`. Set `provider: none` for
rule-based answers only.

## Quick start (Windows)

You need Python 3.10+.

```powershell
git clone https://github.com/rechandler/simcoach && cd simcoach
python -m venv .venv; .venv\Scripts\activate
pip install -e ".[all]"

# Try it without iRacing: a simulated stint with mistakes on a test track
simcoach demo

# Set up your wheel button and microphone
simcoach devices          # press the button you want for push-to-talk
copy config.example.yaml simcoach.yaml   # set voice.joystick_name / joystick_button

# Claude API key (https://console.anthropic.com)
$env:ANTHROPIC_API_KEY = "sk-ant-..."

simcoach run
```

Start `simcoach run` before or after iRacing. It waits for the sim and picks up the
track and car automatically. Drive one clean lap, which becomes your reference, and
coaching starts from the next lap. You can also type questions in the terminal, which is
handy for testing.

### Things you can say

- "How can I improve turn 2?" / "What am I doing wrong in the hairpin?"
- "Where am I losing the most time?"
- "Am I braking too early anywhere?"
- "What was my last lap time?" / "What's my best lap time?" (answered instantly, no LLM)
- "Be quiet" / "Resume coaching" (mutes or unmutes automatic callouts)

## How it works

1. **Laps.** Samples are grouped into laps at the start/finish line. A lap is invalid
   if you go off track, use pit road, reset or tow, or if telemetry has gaps. Invalid
   laps are never used as a reference or coached.
2. **Corners.** iRacing doesn't say where corners are, so simcoach finds them on your
   reference lap: every significant dip in speed is a corner. The map is saved to
   `~/.simcoach/tracks/<TrackID>.yaml`. **Edit it** to use real names and numbering
   ("Turn 1", "La Source", "Corkscrew") and add aliases you might say. The real track's
   turn numbers often differ from the automatic count, because flat-out kinks aren't
   detected.
3. **Analysis.** Each corner owns a segment of the lap, so the per-corner time deltas
   add up exactly to the lap delta. Once you leave a corner, simcoach measures it and
   compares it with the reference. It picks the single most useful finding: late or
   early braking, braking too lightly, low minimum speed, late throttle, coasting, or a
   slow exit. Corners that lose less than `min_time_loss_s` are ignored.
4. **Timing.** Findings are queued and spoken only when you're on throttle, off the
   brake, and at least `quiet_before_brake_m` from the next braking zone. There's a
   minimum gap between messages, and simcoach won't repeat the same advice about the
   same corner every lap. If you press push-to-talk, it stops talking.

Data lives in `~/.simcoach/` (override with `SIMCOACH_HOME`): reference laps, corner
maps, and every lap you drive (`sessions/`).

## Integrations

### Garage 61

[Garage 61](https://garage61.net) stores your laps and your teammates' laps with
telemetry. A faster teammate's lap, or your all-time best, makes a better reference
than today's best.

1. Create a personal access token at <https://garage61.net/developer> with the
   `driving_data` scope, and set `GARAGE61_TOKEN`. Telemetry export needs a Garage 61
   Pro plan.
2. Find the lap you want, then import it as your reference:

```powershell
simcoach g61 laps --list-tracks                    # Garage 61 track ids
simcoach g61 laps --track 123 --car 45 --drivers team
simcoach g61 import <lap-id> --track-id <iRacing TrackID> --car-id <iRacing CarPath>
```

`simcoach run` prints the `--track-id` and `--car-id` values for the session you're in.
The Garage 61 API only exposes laps by you, your teammates, and drivers you follow.
There's no global leaderboard search.

### iRacing `.ibt` files and CSV

```powershell
simcoach reference import "C:\Users\me\Documents\iRacing\telemetry\lap.ibt" --track-id 341 --car-id porsche992rgt3
simcoach replay session.ibt --ask "Where am I losing time?"   # coach a recorded session
```

CSV import accepts most exports with distance (or `LapDistPct`), speed, throttle and
brake columns. It works out units automatically (m/s or km/h, pedals 0–1 or 0–100).

### SimHub

- **Wheel button.** simcoach reads wheel buttons directly, so you don't need SimHub for
  this. If you prefer to keep all mappings in SimHub, or your button sits on a device
  pygame can't see, map it in SimHub's *Controls and events* (or JoyToKey) to a spare
  key such as F13, then set `voice.ptt: keyboard`.
- **Overlay.** Set `overlay.enabled: true`. A live page at `http://127.0.0.1:8765/`
  shows recent callouts and the delta for each corner, and `/state.json` serves the
  raw data. Use it as an OBS browser source, on a tablet, or in any dash that can show
  a web page.

### Crew Chief and other spotters

simcoach runs alongside Crew Chief. Both will talk, so leave Crew Chief's spotter on,
but consider turning off its pace and lap-time messages to reduce chatter. If the two
talk over each other, raise `callouts.min_gap_s`.

## Tuning

Everything is in [`config.example.yaml`](config.example.yaml). The settings you're most
likely to change:

- `coaching.min_time_loss_s`: raise it if the coach is too chatty, lower it if you want
  more detail.
- `callouts.pre_corner_reminders: true`: adds a short cue just before the corner on the
  next lap ("Turn 2, brake a bit earlier").
- `voice.stt_model`: `base.en` is faster, `medium.en` is more accurate.
- `voice.tts: piper`: a much more natural local voice
  ([Piper](https://github.com/rhasspy/piper) voices).

## Development

```bash
pip install -e ".[dev]"
pytest
simcoach demo --ask "How can I improve turn 3?"
```

The synthetic track (`simcoach/telemetry/synthetic.py`) generates laps with injected
mistakes (late braking, low apex speed, late throttle, and so on). The tests use it to
check the full pipeline without iRacing.

## Limitations and ideas

- Corner detection relies on speed dips, so flat-out kinks don't get a corner of their
  own. Their time still counts toward the neighbouring corner's segment.
- Racing line and steering aren't analysed yet (iRacing exposes no track position for
  the player beyond lap distance, and lat/long is only in `.ibt` files).
- Ideas: tyre and brake temperature advice, lock-up and wheelspin detection from wheel
  speeds, a post-session LLM debrief, and comparing against the 3 best laps instead of 1.
