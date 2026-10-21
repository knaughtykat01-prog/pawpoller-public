# Where the sounds come from

The operator picked every sound by ear (2026-10-20/21). `deploy/make_sounds.py` renders them. Each one is built
from voices in the [earshot](https://github.com/Leonxlnx/earshot) sound bank (MIT). The bank synthesises each voice
from a code recipe with no model involved. No recorded or third-party audio is in these files. No AI made or chose
them.

| File | Event | Built from |
|---|---|---|
| `post_up.mp3` | a post went up | `swish/zing` + `notify/m5` at A6 |
| `post_failed.mp3` | a post failed | `notify/m5` at D5 → Db5 |
| `error.mp3` | something went wrong | `pluck/kalimba_soft` at A3, twice |
| `news.mp3` | a poll brought comments or favourites | `pro_pop/bloop` |
| `batch_done.mp3` | a batch finished | `pro_shimmer/m3` |
| `medal.mp3` | a medal was earned | `logo_sting/zip_ping` |
| `level_up.mp3` | level up (spec 050) | `notify/m5` G5 ×3 → C6, with `pluck/kalimba_soft` E6 + G6 |
| `connected.mp3` | an account connected or its Test passed | `pluck/kalimba_soft` C6 → G6 |
| `scheduled.mp3` | a post was scheduled | `toggle/latch` |
| `connection_lost.mp3` | the connection to the server dropped | `pluck/kalimba_soft` G5 → D5 → G4 |
| `connection_back.mp3` | the connection came back | `pluck/kalimba_soft` G4 → D5 → G5 |
| `job_done.mp3` | an import, render or backup finished | `pro_logo_sting/taps_tone` |
| `commission.mp3` | a commission request arrived | `notify/m5` B6 → E7 |

Licence: the earshot code and bank recipes are MIT (© Leon Lin). The rendered sounds ship under PawPoller's own
licence.
