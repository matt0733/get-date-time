# mcp-datetime

A local [MCP](https://modelcontextprotocol.io/) server that gives LLMs reliable
access to date, time, timezone, calendar, and financial-market information.
Designed for any MCP-aware host — **LM Studio**, Claude Desktop, Claude Code,
Open WebUI, Ollama bridges, etc. Speaks MCP over stdio.

Built for an assistant that needs to:

- reason about timestamps it sees in trading data, logs, or APIs;
- convert between time zones without DST mistakes;
- answer "how long until X" / "how long ago was Y";
- check whether a market is open before discussing intraday data;
- generate timestamps in formats other systems require.

## Install

**Requirements:** Python 3.10+ and `git`. No Homebrew or other package managers needed.

The repo is public, so cloning works with no GitHub auth:

```bash
git clone https://github.com/matt0733/get-date-time.git
cd get-date-time
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(If you already use SSH for GitHub on the target machine, you can clone with
`git clone git@github.com:matt0733/get-date-time.git` instead.)

## Run

The server is started by an MCP host, not by hand. To smoke-test it:

```bash
.venv/bin/python server.py     # waits on stdin; Ctrl-C to exit
```

## Install on another Mac (or a fresh machine)

The full sequence to install on a new machine and wire it into LM Studio:

```bash
# 1. Get the code
cd ~/Projects                                            # or wherever you keep code
git clone https://github.com/matt0733/get-date-time.git
cd get-date-time

# 2. Build the venv and install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. (Optional) confirm it boots
.venv/bin/python server.py    # Ctrl-C after a second of silence — no errors = good
```

Then add the LM Studio config below, **substituting the real absolute paths
for that machine** (run `pwd` from inside the cloned folder to confirm).
Restart LM Studio after editing `mcp.json`.

## Tools

All datetimes are ISO-8601 strings. Timezones use IANA names
(`America/New_York`, `Europe/London`, `Asia/Tokyo`, `UTC`, …).
Math is DST-correct: adding 1 day across a spring-forward preserves the wall
clock time, not 24 hours of elapsed seconds.

| Tool | Purpose |
| --- | --- |
| `get_current_datetime(timezone?)` | Current date/time in any IANA zone (defaults to host local). |
| `convert_timezone(datetime, to_timezone, from_timezone?)` | Convert any moment between zones. |
| `to_epoch(datetime, timezone?)` | ISO-8601 → Unix epoch (seconds, ms, µs). |
| `from_epoch(value, timezone?, unit?)` | Unix epoch → ISO-8601. Auto-detects s / ms / µs / ns by magnitude. |
| `parse_datetime(text, timezone?, reference?)` | Parse ISO, RFC-2822, or natural language ("next Friday 3pm", "in 2 hours"). |
| `format_datetime(datetime, format, timezone?)` | Preset (`rfc3339`, `rfc2822`, `iso8601`, `epoch`, `epoch_ms`, `human`, `date`, `time`) or `strftime` pattern. |
| `diff_datetimes(start, end, unit?)` | Difference in any unit + total seconds + human string (may be fractional). |
| `days_between(start_date, end_date, inclusive?)` | Whole calendar days between two dates, ignoring time of day. Use this for "how many days until X". |
| `add_duration(datetime, amount, unit, timezone?)` | DST-aware addition / subtraction. Units: seconds → years. |
| `list_timezones(query?, limit?)` | Search the IANA zone database (e.g. `"india"` → `Asia/Kolkata`). |
| `market_status(market, at?)` | Is a market open? Knows weekends + national holidays. |
| `business_days(start, end, country?)` | Count weekdays minus holidays between two dates. |
| `add_business_days(date, days, country?)` | Add/subtract N business days, skipping weekends + holidays. |
| `calendar_info(date)` | ISO week/year, quarter, day-of-year, leap year, days-in-month. |
| `humanize_duration(seconds, style?)` | `93824` → `"1 day, 2 hours, 3 minutes, 44 seconds"` (or compact `"1d 2h 3m 44s"`). |
| `time_until(target, timezone?)` | Seconds + human string from now to a target moment. |
| `time_since(reference, timezone?)` | Seconds + human string from a past moment to now. |

### Supported markets

`NYSE`, `NASDAQ`, `LSE` (London), `TSE` (Tokyo), `HKEX` (Hong Kong),
`CME` (E-mini futures, simplified continuous session), `CRYPTO` (always open).

Holidays come from the [`holidays`](https://pypi.org/project/holidays/) package
(US federal + NYSE-specific, UK, JP, HK). Early-close days (e.g. day after
Thanksgiving) are **not** modelled — the tool will report a normal close.

### Holiday calendars for business-day tools

`business_days` and `add_business_days` accept any ISO country code that
[`holidays`](https://python-holidays.readthedocs.io/) supports
(`US`, `GB`, `CA`, `DE`, `JP`, `HK`, `IN`, `AU`, …). Defaults to `US`.

## LM Studio configuration

LM Studio reads `~/.lmstudio/mcp.json`. Add (or merge) this entry,
**replacing the two paths with the absolute paths on the machine you are
configuring** (the values below are an example from a Mac at
`/Users/matt/Projects/get-date-time`):

```json
{
  "mcpServers": {
    "datetime": {
      "command": "/Users/matt/Projects/get-date-time/.venv/bin/python",
      "args": ["/Users/matt/Projects/get-date-time/server.py"]
    }
  }
}
```

> **Both paths must be absolute** and must match the location of the cloned
> repo on that specific machine — `~` and relative paths are not expanded.
> If the file already contains other `mcpServers`, merge the `"datetime"`
> entry into the existing object rather than replacing it.

Then in LM Studio: load a model that supports tool use, open a chat, click the
tools/MCP indicator, and confirm `datetime` is listed. (LM Studio re-reads
`mcp.json` on chat start; restart the app if a freshly-added server doesn't
appear.)

## Claude Desktop / Claude Code configuration

```json
{
  "mcpServers": {
    "datetime": {
      "command": "/Users/matt/Projects/get-date-time/.venv/bin/python",
      "args": ["/Users/matt/Projects/get-date-time/server.py"]
    }
  }
}
```

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Covers DST-safe day arithmetic, epoch round-trips, natural-language parsing,
market open/closed (incl. weekend + Memorial Day), holiday-aware business-day
math, and the rest of the tool surface.

## License

[MIT](LICENSE) © 2026 matt0733. Free to use, modify, and redistribute; keep
the copyright notice and there is no warranty.
