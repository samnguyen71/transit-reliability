# Transit reliability planner

How late do buses really run, and which trip gets you there on time? This
project collects a transit agency's live GTFS-realtime feeds, measures actual
delays against the schedule, and plans trips by their odds of arriving on time.

| Folder | What's in it |
| --- | --- |
| [`logger/`](logger/README.md) | Polls the live feeds every 30 s and archives every snapshot. Running now. |
| `pipeline/` | Phase 1: turns snapshots into a delay for every stop visit |
| `api/`, `web/` | Phase 2: the FastAPI backend and React map |
| `data/` | The collected data, on the VM only (not in git) |

Start with [logger/README.md](logger/README.md).
