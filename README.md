# Singapore Haze Monitor

Hourly Singapore air quality updates — PSI, PM2.5, temperature, humidity, and 2-hour rain forecast — pulled from [data.gov.sg](https://data.gov.sg) and posted to Telegram when the data changes.

Runs on GitHub Actions. No server, no hosting fees.

---

## Example output

```

⛈️ Next 2hrs in West area ☔
━━━━━━━━━━━━━━━━━━
🟠 PSI Updated: 13 Oct, 12:30 am
━━━━━━━━━━━━━━━━━━
┃ Reg  | 1h 2.5 | 24h 2.5 | 24h PSI
┃      |   🟠   |   🟢    |   🟠
┃ -----+--------+---------+--------
┃  C   |  100 ↑ |   102   |  149 ↑
┃  W   |   98 ↓ |   104   |  151 ↓
┃  S   |   98   |    69 ↑ |  115
┃  E   |   84 ↓ |    90   |  136 ↓
┃  N   |   68 ↑ |    71 ↓ |  118 ↑
🌡️ 29.1-30.9°c | 💧 70-85%

```

If no region is forecast to get rain, the rain line and its ruler are omitted.

Regions: **C**entral · **W**est · **S**outh · **E**ast · **N**orth

Columns ordered by reactivity — fastest-changing on the left:
**1h PM2.5** → **24h PM2.5** → **24h PSI**

Trend arrows: `↑` rose · `↓` fell · blank = unchanged

### Colour code

One circle per column, based on the worst region in that column.

**PM2.5 (µg/m³):**
| Range | Circle |
|---|---|
| 0 – 55 | 🟢 |
| 56 – 100 | 🟡 |
| 101 – 150 | 🟠 |
| 151 – 250 | 🔴 |
| ≥ 251 | 💀 |

**PSI:**
| Range | Circle |
|---|---|
| 0 – 50 | 🟢 |
| 51 – 100 | 🟡 |
| 101 – 200 | 🟠 |
| 201 – 300 | 🔴 |
| ≥ 301 | 💀 |

### Rain emoji

| Forecast | Emoji | Umbrella ☔? |
|---|---|---|
| Thundery showers | ⛈️ | ✅ Yes |
| Heavy rain / heavy showers | 🌧️ | ✅ Yes |
| Light / moderate / passing | 🌦️ | ❌ No |

The umbrella is appended only for heavy or thundery rain — a light drizzle gets the rain emoji alone.

---

## Setup

**1. Create a Telegram bot**
- Message **@BotFather** → `/newbot` → copy the token
- Send `/start` to your new bot

**2. Create a Telegram channel**
- Add the bot as an **Administrator** with **Post Messages** permission

**3. Add secrets to GitHub**
Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Channel `@username` |
| `TELEGRAM_CHAT_ID_TEST` | *(optional)* your personal chat ID for testing |

**4. Test**
Actions → Singapore Haze Monitor → Run workflow

---

## Scheduling

GitHub's native cron is best-effort. For reliable hourly triggers, use **cron-job.org**:

1. Create a **GitHub PAT** (Fine-grained, `Actions: Read and write`)
2. In cron-job.org, create a job:
   - **URL:** `https://api.github.com/repos/<user>/<repo>/actions/workflows/psi_monitor.yml/dispatches`
   - **Method:** `POST`
   - **Body:** `{"ref":"main"}`
   - **Headers:**
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your-PAT>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - **Schedule:** hourly, minute 5, timezone Asia/Singapore
3. Successful trigger returns **204 No Content**
4. **Renew the PAT every ~90 days**

---

## Configuration

**Toggle metrics** — edit `psi_monitor.py`:

```python
SEND_PSI_24H  = True
SEND_PM25_24H = True
SEND_PM25_1H  = True
```

Alert gating — only send when conditions warrant it:

```python
SEND_ONLY_ON_ALERT      = False   # set True to enable
ALERT_1H_PM25_THRESHOLD = 0       # µg/m³ — any region above this triggers
```

When SEND_ONLY_ON_ALERT = True, the script sends only if:

· any region's 1-hour PM2.5 exceeds ALERT_1H_PM25_THRESHOLD, OR
· any region's forecast includes heavy rain or thundery showers

Useful thresholds:

· 0 — effectively always (PM2.5 is never zero)
· 56 — any region into the "Elevated" band or worse
· 151 — any region into the "High" band or worse
· 251 — only "Very High" triggers

--force bypasses gating for testing.

Ruler width:

```python
HR = "━" * 18    # 18 chars ≈ 50% width
```

Region order:

```python
REGION_ORDER = ["central", "west", "south", "east", "north"]
```

---

Files

File Purpose
psi_monitor.py Main script
regions.py Town → NEA region mapping (used for rain forecast)
requirements.txt Dependencies (requests)
.github/workflows/psi_monitor.yml Workflow

Note: If regions.py is missing, the script still runs — it just skips the rain line. All other features work normally. A warning appears in the Actions log:

```
WARNING regions.py not found — rain forecast will be skipped
```

---

CLI

Flag Effect
(none) Send only if timestamp changed (and alert conditions, if enabled)
--preview Print only, no send, no state
--force Send even if unchanged; bypasses alert gating
--to CHAT_ID Send to a different chat (testing)

```bash
python psi_monitor.py --preview
python psi_monitor.py --force --to 123456789
```

---

Test branch

The workflow targets different chats based on branch:

Branch Target Force send?
main Channel Only on change
test Your DM (TELEGRAM_CHAT_ID_TEST) Yes

---

Troubleshooting

Bad Request: chat not found — Bot not added to channel, or wrong TELEGRAM_CHAT_ID

Workflow missing from Actions tab — File must be at .github/workflows/<name>.yml (leading dot)

Failed to save: Unable to reserve cache — Use a fresh run (Actions → Run workflow), not "Re-run all jobs"

Rain line never appears — Check that regions.py is present on the branch being run, and that at least one town has rain forecast. The log will show WARNING regions.py not found if the file is missing.

SyntaxError: '(' was never closed — A merge conflict left broken code. Replace the affected file with the full version from the repo history.

No messages despite SEND_ONLY_ON_ALERT = True — Either no region exceeded the PM2.5 threshold, or no region had heavy/thundery rain. Check the Actions log — it prints alert gating enabled and no alert condition met; skipping.

Scheduled runs not firing — Check Actions tab for a disabled banner; confirm workflow is on main; use cron-job.org for reliability

---

Data sources

· PSI + 24h PM2.5: api-open.data.gov.sg/v2/real-time/api/psi
· 1h PM2.5: api-open.data.gov.sg/v2/real-time/api/pm25
· Temperature: api-open.data.gov.sg/v2/real-time/api/air-temperature
· Humidity: api-open.data.gov.sg/v2/real-time/api/relative-humidity
· 2-hour forecast: api-open.data.gov.sg/v2/real-time/api/two-hr-forecast
· Town list: https://www.bcs.org.sg/form/region

Source: NEA. Readings older than 3 hours are rejected.

---

Cost

Free. GitHub Actions is unlimited for public repos. ~30 seconds per run.

License

MIT

```

---

## 📱 Updated welcome message

```
