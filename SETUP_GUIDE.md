# SentinelWatch setup guide

This guide takes you from nothing to the running app, then through preparing it for a demo. You don't need to train anything, and you don't need API keys, accounts or a GPU. Both trained models and the demo data are already in this repository.

**Short version:** install Python 3.12, download the project, run one command, and open http://127.0.0.1:8000.

## What you need

- **Python 3.12.** Newer versions may work, but 3.12 is the safest choice.
- **About 2 GB of free disk space.** The project is about 150 MB and its Python packages about 1 GB, and downloaded images are added as you use it.
- **Internet access** for the first start and for downloading satellite images. Saved incidents still open without it.
- **Any recent laptop running Windows, macOS or Linux.** A GPU makes replays faster, but it isn't required.

## Step 1: Install Python 3.12

**Windows**

1. Download the Python 3.12 installer from [python.org](https://www.python.org/downloads/).
2. On the first installer screen, tick **Add python.exe to PATH**, then click **Install Now**.
3. Open a new PowerShell window and run `python --version`. It should show `Python 3.12`.

**macOS**

1. Download the Python 3.12 installer from [python.org](https://www.python.org/downloads/) and run it.
2. Open Terminal and run `python3.12 --version`. It should show `Python 3.12`.

**Linux (Ubuntu or Debian)**

Run `sudo apt install python3.12 python3.12-venv`, then check with `python3.12 --version`. If your version of Linux doesn't offer that package, install Python 3.12 another way, such as from [python.org](https://www.python.org/downloads/).

## Step 2: Download the project

Save it in an ordinary folder, not one synced by OneDrive, iCloud or Google Drive, because syncing can interfere with the database. Choose one option.

**Option A: ZIP, with no Git needed**

1. Download [the project ZIP](https://github.com/srinidhimanjunath111-ni/SentinelWatch_Project/archive/refs/heads/main.zip) (about 60 MB).
2. Extract the whole ZIP. On Windows, right-click it and choose **Extract All**. Don't run anything from inside the ZIP window.
3. You should now have a folder called `SentinelWatch_Project-main`.

**Option B: Git**

```sh
git clone https://github.com/srinidhimanjunath111-ni/SentinelWatch_Project.git
```

This creates a folder called `SentinelWatch_Project`.

## Step 3: Open a terminal in the project folder

- **Windows:** open the folder in File Explorer, click the address bar, type `powershell` and press Enter.
- **macOS:** open Terminal, type `cd ` with a space after it, drag the folder into the window and press Enter.
- **Linux:** open a terminal in the folder, or `cd` into it.

To check you're in the right place, list the files with `dir` on Windows or `ls` on macOS and Linux. You should see `README.md` and a `scripts` folder.

## Step 4: Start SentinelWatch

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run.ps1
```

**macOS and Linux**

```sh
PYTHON=python3.12 bash scripts/run.sh
```

The first start creates a private Python environment in `.venv` and installs everything, including PyTorch. It usually takes a few minutes, and longer on a slow connection. On Linux, PyTorch can be a much larger download. Later starts skip this and take a few seconds.

It's ready when the terminal shows:

```text
Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

**Keep this terminal window open.** Closing it stops the app.

## Step 5: Open the app

Go to **http://127.0.0.1:8000** in Chrome, Edge or Firefox. Check that:

- The **Operations** screen shows a satellite map with **7 sites**.
- The saved demo data has **14 cases** and **20 alerts**.
- **Models and data** shows both models as ready, not "not trained".

For a quick technical check, http://127.0.0.1:8000/api/health should say `"status":"ok"`.

On the first start, the app also checks all seven sites for new satellite images. If one really is new, you may see more alerts than 20. That's normal.

## Stop and restart

- **To stop:** click the terminal window and press **Ctrl+C**.
- **To start again:** open a terminal in the project folder (Step 3) and run the Step 4 command again. On macOS and Linux you can leave off `PYTHON=python3.12`. Your data is kept.

## Prepare for a demo

Do this at least a day before, on the laptop you'll present from.

1. **Download the images.** Images aren't included in the project, so each site's first replay has to download them. On **Operations**, open a site, leave the pre-filled dates as they are, and click **Start replay**. Wait for it to finish, then do the next site.
2. **Reset the data,** because each replay adds alerts. Follow [Return to clean demo data](#return-to-clean-demo-data) below. This keeps the downloaded images.
3. **Test the sound.** Click anywhere on the page, then check the speaker icon. Replay Wayanad and listen for the chime. Reset again afterwards.
4. **Check the projector,** using the checklist in the [demo guide](docs/SentinelWatch_Demo_Guide.md).
5. **Read the presentation material:** the [demo script](docs/SentinelWatch_Demo_Guide.md), [speaking guide](docs/SentinelWatch_Review_Speaking_Guide.md) and [viva questions](docs/SentinelWatch_Viva_Questions.md).

How long replays took on the test Mac, running on CPU only to match a laptop with no GPU:

| What | Time |
|---|---|
| All seven sites, first time, with images to download | About 11 minutes |
| One site, first time | About 1–2 minutes |
| Wayanad repeated, images already saved, CPU only | 31 seconds |
| Wayanad repeated, images already saved, Apple GPU | 11–13 seconds |

Your laptop and connection may be slower. Run it yourself and rehearse with your own timing.

## Return to clean demo data

This deletes every case, alert and review made on this laptop, then restores the original 7 sites, 14 cases and 20 alerts. Downloaded images stay, so replays stay fast.

Stop the app with **Ctrl+C**, then run this from the project folder and start the app again.

**Windows**

```powershell
Remove-Item instance\sentinelwatch.db*
Remove-Item -Recurse -Force storage\outputs, storage\outbox
```

**macOS and Linux**

```sh
rm -rf instance/sentinelwatch.db* storage/outputs storage/outbox
```

## Troubleshooting

| What you see | What to do |
|---|---|
| `Python was not found`, or `python` is not recognized | Install Python as in Step 1. On Windows, make sure **Add python.exe to PATH** was ticked, then open a new window. |
| The Microsoft Store opens when you run the command | Windows is using its Store shortcut. Install Python from python.org with **Add python.exe to PATH** ticked, then open a new window. |
| `running scripts is disabled on this system` | Use the exact Windows command in Step 4, which allows this one script to run. |
| Install fails with `No matching distribution` or a build error | Your Python may be too new or too old. Install 3.12, delete the `.venv` folder, and run Step 4 again. If Windows keeps picking another version, follow the manual Windows setup in the [README](README.md#quick-start). |
| `command not found: python3.12` on macOS or Linux | Python 3.12 isn't installed or isn't on your path. Go back to Step 1. |
| `No module named venv` or `ensurepip` on Linux | Run `sudo apt install python3.12-venv`, delete the `.venv` folder, and run Step 4 again. |
| `address already in use`, or `only one usage of each socket address` | Another program, or another copy of the app, is using port 8000. Close it, or use another port as shown below and open `http://127.0.0.1:8080`. |
| The page won't load | Check that the terminal is still open and shows no error, and that the address is exactly `http://127.0.0.1:8000`. |
| The map background is blank | The background map needs internet access, and the site outlines still appear without it. Check your connection, then reload the page. |
| **A monitoring run is active for this area** | The app is already checking that site, which often happens just after starting. Wait a minute and try again. |
| A replay or check fails with a catalogue or imagery error | The free satellite service may be busy. Wait a few minutes and try again. During a demo, open a saved incident from **Alerts** instead. |
| **Check now** finds no new alert | This is normal. Sentinel-2 passes over a place only every 2–5 days, and clouds can hide it. Use **Start replay** to show an alert. |
| **Models and data** shows a model isn't loaded, or results say spectral baseline | PyTorch didn't install. Run the matching command below, then restart the app. |
| No alert sound | Click once on the page, because browsers block sound until you do. Then check the speaker icon and the laptop volume. |
| There are more alerts than expected | Replays add alerts. [Reset the data](#return-to-clean-demo-data) when you're not presenting. |
| Something else, or the install got stuck | Stop the app, delete the `.venv` folder, and run Step 4 again. Your demo data isn't affected. |

**To use another port**

```powershell
# Windows
$env:PORT=8080; powershell -ExecutionPolicy Bypass -File scripts\run.ps1
```

```sh
# macOS and Linux
PORT=8080 bash scripts/run.sh
```

The links in the demo guide use port 8000, so change the port in those too.

**To install PyTorch by hand**

```powershell
# Windows
.venv\Scripts\python.exe -m pip install torch
```

```sh
# macOS and Linux
.venv/bin/python -m pip install torch
```

## Optional: send real emails

By default, alerts appear in the department inboxes in the app and are also saved as email files in `storage/outbox`. Nothing is actually sent, and the department addresses are placeholders ending in `.example`.

To send real emails, copy `.env.example` to `.env`, fill in these values from your email provider, and restart the app:

```ini
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_REDIRECT_TO=
```

Set `SMTP_REDIRECT_TO` to an inbox you own, because the placeholder addresses can't receive mail. Many providers require an app password rather than your normal one. Keep `.env` private: Git already ignores it, so it won't be committed.

**This has not been tested with a real mail server,** so try it well before relying on it in a demo.

## What has and hasn't been tested

| Status | What |
|---|---|
| **Tested from a clean download** on macOS | Install, first start, both models, all seven replays on CPU, images, case PDF, and reset |
| **Not tested on real Windows** | `scripts\run.ps1` and the Windows commands in this guide. The launcher's logic was only tried with stand-ins on macOS, so test it early. |
| **Not tested on Linux** | The commands are the same as on macOS, but they haven't been run on Linux. |
| **Not tested** | Real email, and the Docker and Render deployment files |
| **Not heard** | The alert chime, because testing ran without sound |

## Where everything is

| What | Where |
|---|---|
| Final project report | [`output/pdf/SentinelWatch_Final_Year_Project_Report.pdf`](output/pdf/SentinelWatch_Final_Year_Project_Report.pdf) |
| Ten-minute demo script | [`docs/SentinelWatch_Demo_Guide.md`](docs/SentinelWatch_Demo_Guide.md) |
| Speaking guide | [`docs/SentinelWatch_Review_Speaking_Guide.md`](docs/SentinelWatch_Review_Speaking_Guide.md) |
| Viva questions | [`docs/SentinelWatch_Viva_Questions.md`](docs/SentinelWatch_Viva_Questions.md) |
| Screenshots | [`docs/screenshots/`](docs/screenshots/README.md) |
| Trained models | `models/checkpoints/` |
| Original demo data | `demo_snapshot/` |
| Retraining recipe, which you don't need to run the app | [`training/README.md`](training/README.md) |
| Release notes | [`RELEASE_NOTES.md`](RELEASE_NOTES.md) |

## Before you present

SentinelWatch produces leads for a person to check, not legal findings. Its strongest verdict is "suspected unauthorised, no authorisation on record". Don't say anything is confirmed illegal, and don't accuse a named company or person. Replays use real past images and are labelled **Archive replay**, so say that out loud. No verdict has been checked on the ground, and the viva questions cover how to explain these limits.
