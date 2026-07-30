# zai_usage_resets

Show your [z.ai](https://z.ai) GLM coding-plan quota usage **and when each limit resets**.

The z.ai dashboard shows how much of your quota you've used, but not when it comes
back. This script reads the same monitoring endpoint the dashboard uses and prints
the `nextResetTime` values it already returns, converted to your local timezone.

```
Z.ai GLM coding plan — usage (plan: lite)

5-hour usage   [################....]  80%   resets 2026-07-30 14:13:55 CEST  (in 2h 05m)
Weekly usage   [###############.....]  76%   resets 2026-08-05 16:11:13 CEST  (in 6d 04h 02m)
Tool calls     [....................]   2%   resets 2026-08-01 16:11:13 CEST  (in 2d 04h 02m)
               2 of 100 used, 98 remaining
                 search-prime   2
                 web-reader     0
                 zread          0
```

## Setup

Requires `bash`, `curl` and `jq`.

```sh
cp .env.example .env       # then add your z.ai API key
./zai_usage_resets.sh
```

The key is read from `ZAI_API_KEY`, either from the environment or from a `.env`
file next to the script. `.env` is gitignored.

## Usage

```
./zai_usage_resets.sh            # usage report
./zai_usage_resets.sh --json     # raw API response
./zai_usage_resets.sh --probe    # also send a 1-token chat request (costs a prompt)
./zai_usage_resets.sh --help
```

Exit codes: `0` fine, `1` some limit is at 100%, `2` error — so it works in cron
or a monitor.

## Caveat

`/api/monitor/usage/quota/limit` is undocumented, so z.ai may change or remove it
at any time. Unrecognised limit types are printed with their raw fields rather
than guessed at, and a malformed response exits `2` instead of reporting
something wrong.
