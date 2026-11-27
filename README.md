# Transit reliability planner

How late do buses really run, and which trip gets you there on time? This
project collects Durham Region Transit's live GTFS-realtime feeds, measures
actual delays against the schedule, and plans trips by their odds of arriving
on time.

## Notable findings

From 473,207 timed stop visits on 32 days, 19 October to 22 November 2026.

1. **Two buses in three are on time, but not in the afternoon rush.** Overall,
   67.6% of visits were on time, 27.0% were more than 5 minutes late and 5.4%
   left early. At 6 am, 86% are on time. That falls through the day to 45% at
   5 pm, when more than half (52.5%) are over 5 minutes late. By 9 pm it's back
   to 75%.
2. **The trip late most often is route 917's 5:21 pm weekday run to Oshawa
   Centre Terminal.** It reached the terminal more than 5 minutes late on all
   20 weekdays its arrival was recorded: 25 minutes late typically, 50 at
   worst. 180 of its 182 timed departures along the way were late too. At
   least 15 trips were late on every weekday they were recorded. 14 of them
   start between 2 pm and 6:15 pm.
3. **Routes 407 and 920 are the least reliable, and both serve North Campus.**
   They were on time 54% of the time, against 67.6% for the whole network, and
   more than 5 minutes late 42% of the time. 6 of the 15 trips late most often
   are on these two routes.

How these were measured:
- **On time** means leaving a timepoint between 1 minute early and 5 minutes
  late, or reaching the last stop no more than 5 minutes late. The rule is in
  [pipeline/OBSERVED_TIMES.md](pipeline/OBSERVED_TIMES.md).
- **Coverage:** about 94% of scheduled timed visits were observed.
- **Left out:** 15–17 November, when DRT's feed was down, and 1.5% of visits
  on trips starting after midnight, which are matched to the wrong night until a
  fix lands.
- **Timetable:** all of it is DRT's October 2026 timetable, which ends
  29 November.

To regenerate the numbers, run this from `pipeline/` with the VM's data copied
into `data/`:

```bash
python findings.py ../data ../data/raw/static_gtfs/<date>/<newest>.zip \
    --from 2026-10-19 --to 2026-11-22 \
    --exclude 2026-11-15,2026-11-16,2026-11-17 --min-days 15
```

| Folder | What's in it |
| --- | --- |
| [`logger/`](logger/README.md) | Polls the live feeds every 30 s and archives every snapshot. Running now. |
| `pipeline/` | Phase 1: turns snapshots into a delay for every stop visit |
| [`api/`](api/README.md), `web/` | Phase 2: the FastAPI backend and React map |
| `data/` | The collected data, on the VM only (not in git) |

Start with [logger/README.md](logger/README.md).
