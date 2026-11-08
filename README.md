# chrome-profiles

If you run several Chrome profiles with [Claude in Chrome](https://claude.ai/chrome), Claude's browser tools list them only as "Browser 1", "Browser 2", numbered in connection order. So an agent can easily act in the wrong profile.

`chrome-profiles` prints which deviceId belongs to which profile and Google account, whether the profile is open, and whether the extension is on.

```
#  PROFILE    NAME      OPEN  EXT  CLAUDE    DEVICE ID                             GOOGLE ACCOUNTS
1  Default    Personal  yes   on   1a2b3c4d  00000000-0000-4000-8000-000000000001  me@example.com
2  Profile 3  Work      yes   off  1a2b3c4d  00000000-0000-4000-8000-000000000002  me@work.example
```

macOS only. Python 3 standard library, nothing to install.

Not affiliated with or supported by Anthropic. It reads undocumented files, so a Chrome or extension update can break it. Last tested with Chrome 157, extension 1.0.105 and macOS 27.

## Install

```bash
git clone https://github.com/zamabama/chrome-profiles.git
ln -s "$PWD/chrome-profiles/chrome-profiles" ~/.local/bin/chrome-profiles
```

## Use

```bash
chrome-profiles
chrome-profiles --all
chrome-profiles --json
```

Match the DEVICE ID column against the deviceIds from `list_connected_browsers`, then pick one with `select_browser`.

## Your own numbers

Copy `labels.example.json` to `labels.json` next to the script and give each profile a label. Keys are Chrome's profile folders, which you can see with `--all`. They stay the same when a deviceId changes after signing back in to the extension.

```json
{
  "Default": "1",
  "Profile 3": "2"
}
```

Once any label exists, only labelled profiles are listed, in label order. `--all` lists the rest too.

## Where each column comes from

| Column | Source |
|---|---|
| # | `labels.json` |
| PROFILE, NAME | `Local State` → `profile.info_cache` |
| GOOGLE ACCOUNTS | Chrome's signed-in account first, then `Preferences` → `account_info` |
| OPEN | `lsof` on Chrome's main process: does it hold files in that profile's folder? |
| EXT | `Secure Preferences` → `extensions.settings.<id>`; `disable_reasons` set means off |
| CLAUDE | `accountUuid` in the extension's `chrome.storage.local`; the extension clears it on sign-out |
| DEVICE ID | `bridgeDeviceId` in the same store |

## What it reads

The extension's store is LevelDB in `<profile>/Local Extension Settings/<extension id>`, and it also holds sign-in data. The tool reads those files into memory, keeps only the three keys in `WANTED_KEYS`, and writes nothing. It never takes Chrome's LOCK file and makes no network calls.

## What it can't tell you

- **Connected right now.** Only `list_connected_browsers` knows. The stored `mcpConnected` flag is not reliable.
- **Windows, strictly.** OPEN means Chrome has the profile loaded. A profile with no window but still loaded would show yes.
- **Incognito** windows belong to no column.

A profile can be missing from `list_connected_browsers` because it's not open, EXT is off, CLAUDE is `-` (signed out), or it's signed in to a different Claude account from your session's.

## Full Disk Access

Since macOS 27, reading Chrome's folder needs Full Disk Access for whichever app runs the command. Without it the tool exits 1 and says so.

## Tests

```bash
python3 test_chrome_profiles.py
```

They run against a fake Chrome folder.
