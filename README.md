<p align="center">
  <img src="img/logo@2x.png" alt="git-ha-ppens logo" width="200">
</p>

<h1 align="center">git-ha-ppens</h1>

<p align="center">
  <strong>Git version control for Home Assistant — right from your UI.</strong>
</p>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge" alt="HACS Custom"></a>
  <a href="https://github.com/manuveli/git-ha-ppens/releases"><img src="https://img.shields.io/github/v/release/manuveli/git-ha-ppens?style=for-the-badge" alt="GitHub Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://www.home-assistant.io/"><img src="https://img.shields.io/badge/HA-2024.1+-blue?style=for-the-badge" alt="Home Assistant 2024.1+"></a>
</p>

<p align="center">
  <strong>GitOps for Home Assistant.</strong> Edit your config in VS Code or directly on GitHub, push the changes, and your HA instance pulls them automatically. Roll back any breaking change by reverting a commit. Review config changes in a pull request before they ever reach your live system.<br><br>
  git-ha-ppens brings native git directly into Home Assistant — auto-commit on file change, auto-push to your remote, and now <strong>auto-pull</strong> when new commits arrive. All configured through the UI, no command line needed.
</p>

---

## 📑 Table of Contents

- [✨ Features](#-features)
- [🔁 GitOps Workflow](#-gitops-workflow)
- [📥 Installation](#-installation)
- [⚙️ Configuration](#️-configuration)
- [🤖 AI Commit-Messages](#-ai-commit-messages)
- [🚀 Services](#-services)
- [📊 Sensors & Entities](#-sensors--entities)
- [⚡ Events](#-events)
- [💡 Example Automations](#-example-automations)
- [🛡️ Auto-Generated .gitignore](#️-auto-generated-gitignore)
- [🔧 Troubleshooting](#-troubleshooting)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)

---

## ✨ Features

### 🤖 Automatic Version Control
- 👁️ **File watcher** detects config changes in real time (powered by [watchdog](https://github.com/gorakhargosh/watchdog))
- ⏱️ **Configurable debounce interval** (default 5 min) to batch changes and avoid excessive commits
- 📝 **Auto-generated commit messages** listing the changed files

### 🔁 Auto-Sync (GitOps)
- 🔄 **Periodic git fetch** checks the remote on a configurable interval (default 5 min, range 60–3600s)
- ⬇️ **Auto-pull** when the integration detects your instance is behind the remote
- ⬆️ **Auto-push** after every auto-commit to keep the remote up to date

### 🔧 Manual Control
- **6 services** callable from automations, scripts, or Developer Tools:
  - `git_ha_ppens.commit` — create a commit with an optional custom message
  - `git_ha_ppens.push` — push commits to the configured remote
  - `git_ha_ppens.pull` — pull from remote (auto-backs up uncommitted changes first)
  - `git_ha_ppens.fetch` — fetch from remote without merging (updates ahead/behind counts)
  - `git_ha_ppens.sync` — commit + push in one step
  - `git_ha_ppens.diff` — get the current diff of uncommitted changes

### 🛡️ Security & Secrets
- 🚫 **Automatic `.gitignore`** for `secrets.yaml`, `.storage/`, databases, logs, and more
- 🔍 **Secret detection** scans tracked files for API keys, tokens, and passwords
- 🔔 Fires a `git_ha_ppens_secret_detected` event when potential secrets are found

### ☁️ Remote Support
- Push and pull from **GitHub**, **GitLab**, **Bitbucket**, or any git remote
- **HTTPS** with personal access token or **SSH key** authentication

### 📊 Visibility & Monitoring
- **10 sensors** + **1 binary sensor** for real-time git status
- **Events** for commit, push, pull, fetch, errors, and secret detection
- Build dashboards, notifications, and automations around your config history

### 🩺 Diagnostics
- Full diagnostics support via **Settings → Devices & Services → git-ha-ppens → Diagnostics**
- Sensitive values are **automatically redacted**

---

## 🔁 GitOps Workflow

GitOps means your git repository is the **single source of truth** for your Home Assistant configuration. Instead of editing files directly on your HA instance, you manage changes through git — and your instance automatically stays in sync.

With git-ha-ppens you get a full GitOps loop without any extra tooling:

```
Edit config in VS Code / GitHub  →  push to remote
         ↓
git-ha-ppens fetches periodically (default: every 5 min)
         ↓
Detects your instance is behind  →  auto-pull
         ↓
Home Assistant is up to date ✓
```

### Why this matters

- **Edit from anywhere** — use your local editor, the GitHub web UI, or any other git client. Changes reach HA automatically.
- **Review before it goes live** — open a pull request for config changes and merge only when you're ready.
- **Instant rollback** — revert a commit on GitHub and your HA instance pulls the rollback automatically.
- **Full history** — every config change is a commit. Know exactly what changed, when, and why.

### Enabling the GitOps loop

1. Configure a remote repository (GitHub, GitLab, etc.) in the integration setup
2. Enable **Auto-Pull** in the commit settings
3. Set a **Fetch Interval** (default 5 min) so the integration checks for remote changes periodically
4. Optionally enable **Auto-Push** to send local changes upstream automatically

That's it. From this point on, your HA config and your git remote stay in sync automatically.

---

## 📥 Installation

### Prerequisites

> **Requirements:**
> - 🏠 Home Assistant **2024.1** or later
> - 🔧 **Git** installed on the host system
>
> **HA OS users:** Git may not be available by default. You may need a dedicated add-on or container with git pre-installed.

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=manuveli&repository=git-ha-ppens&category=integration)

1. Make sure [HACS](https://hacs.xyz/) is installed
2. Click the badge above — or go to **HACS → Integrations → ⋮ → Custom repositories** and add:
   ```
   https://github.com/manuveli/git-ha-ppens
   ```
   with category **Integration**
3. Search for **git-ha-ppens** and click **Install**
4. **Restart** Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** and search for **git-ha-ppens**

### Manual Installation

1. Download the latest release from the [Releases](https://github.com/manuveli/git-ha-ppens/releases) page
2. Copy the `custom_components/git_ha_ppens` folder into your `config/custom_components/` directory
3. **Restart** Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for **git-ha-ppens**

---

## ⚙️ Configuration

The integration is configured entirely through the UI. The setup flow has **3 steps**:

### Step 1: 📁 Repository Settings

| Option | Description | Default |
|--------|-------------|---------|
| `repo_path` | Path to the HA configuration directory | `/config` |
| `git_user` | Git author name for commits | *(required)* |
| `git_email` | Git author email for commits | *(required)* |

### Step 2: 🔄 Auto-Commit Settings

| Option | Description | Default |
|--------|-------------|---------|
| `auto_commit` | Automatically commit when files change | `true` |
| `auto_push` | Push to remote after each auto-commit | `true` |
| `auto_pull` | Pull automatically when the instance is behind the remote | `false` |
| `commit_interval` | Debounce interval in seconds (30–86400) | `300` |
| `scan_interval` | Status polling interval in seconds (10–3600) | `30` |
| `fetch_interval` | How often to fetch from remote in seconds (60–3600) | `300` |

### Step 3: ☁️ Remote Repository *(optional)*

| Option | Description | Default |
|--------|-------------|---------|
| `remote_url` | Git remote URL (HTTPS or SSH) | *(empty)* |
| `auth_method` | `none` / `token` / `ssh` | `none` |
| `auth_token` | Personal access token (for HTTPS) | *(empty)* |
| `ssh_key_path` | Path to SSH private key file | *(empty)* |

> 💡 **Tip:** All settings can be changed later via **Settings → Devices & Services → git-ha-ppens → Configure**. The options menu provides two sections: **General Settings** (all configuration options above) and **Edit .gitignore** (a built-in editor for customizing ignored files).

---

## 🤖 AI Commit-Messages

By default, **git-ha-ppens** generates simple commit messages listing the changed files — for example `Auto: config.yaml changed` or `Auto: 3 files changed`. This works great out of the box and **does not require any AI setup**.

If you want more descriptive, context-aware commit messages, you can optionally enable **AI-generated commit messages**. The integration uses Home Assistant's built-in [Conversation](https://www.home-assistant.io/integrations/conversation/) service to analyze the git diff and generate a meaningful commit message — powered by whichever AI agent you have configured in HA (OpenAI, Google Generative AI, Ollama, etc.).

### ✅ Enabling AI Commit-Messages

AI commit messages can be enabled during the initial setup (Step 2: Auto-Commit Settings) or at any time later via **Settings → Devices & Services → git-ha-ppens → Configure**.

| Option | Description | Default |
|--------|-------------|---------|
| `ai_commit_messages` | Enable AI-generated commit messages | `false` |
| `ai_agent_id` | Entity ID of the conversation agent to use (e.g. `conversation.chatgpt`) | *(empty)* |

> 💡 **Tip:** Leave `ai_agent_id` empty to use Home Assistant's default conversation agent. If you have multiple AI agents configured, you can specify exactly which one should generate your commit messages.

### 🛡️ Fallback Behavior

AI commit messages are designed to **never interfere** with normal operation:

- 🔒 If AI is **disabled** (default), the integration works exactly as before — no AI code is executed at all
- ⚠️ If AI is **enabled** but the conversation agent is **unavailable** or returns an error, the integration silently falls back to the standard auto-generated message
- ✅ **Commits are never blocked** by AI failures — your configuration changes are always saved regardless of AI availability

> 📌 **No AI? No problem.** The integration is fully functional without any AI agent configured. The AI feature is a purely optional enhancement.

---

## 🚀 Services

| Service | Description | Parameters |
|---------|-------------|------------|
| `git_ha_ppens.commit` | Stage all changes and create a commit | `message` *(optional)* — custom commit message |
| `git_ha_ppens.push` | Push commits to the configured remote | — |
| `git_ha_ppens.pull` | Pull from remote (backs up uncommitted changes first) | — |
| `git_ha_ppens.fetch` | Fetch from remote without merging — updates the ahead/behind counts | — |
| `git_ha_ppens.sync` | Commit + push in one step | `message` *(optional)* — custom commit message |
| `git_ha_ppens.diff` | Get the current diff of uncommitted changes | — *(returns response data)* |

### Example: Call sync from an automation

```yaml
action:
  - service: git_ha_ppens.sync
    data:
      message: "Manual sync from automation"
```

### Example: Get the current diff

The `diff` service returns response data containing the diff content and a status summary. Use it in scripts or automations to inspect changes before committing.

```yaml
action:
  - service: git_ha_ppens.diff
    response_variable: git_diff
```

---

## 📊 Sensors & Entities

### Sensors

| Entity | Description | Attributes |
|--------|-------------|------------|
| `sensor.git_ha_ppens_last_commit` | Last commit hash (short) | `message`, `author`, `full_hash` |
| `sensor.git_ha_ppens_last_commit_time` | Timestamp of last commit | — |
| `sensor.git_ha_ppens_uncommitted_changes` | Number of changed files | `changed_files`, `untracked_files`, `staged_files` |
| `sensor.git_ha_ppens_branch` | Current branch name | — |
| `sensor.git_ha_ppens_remote_status` | Sync status (e.g. "in sync", "ahead 3") | `ahead`, `behind`, `remote_configured`, `has_upstream`, `total_commits` |
| `sensor.git_ha_ppens_commits_ahead` | Number of local commits not yet pushed | — |
| `sensor.git_ha_ppens_commits_behind` | Number of remote commits not yet pulled | — |
| `sensor.git_ha_ppens_last_fetch_time` | Timestamp of last successful fetch | — |
| `sensor.git_ha_ppens_last_pull_time` | Timestamp of last successful pull | — |
| `sensor.git_ha_ppens_last_push_time` | Timestamp of last successful push | — |

### Binary Sensors

| Entity | Description | Device Class |
|--------|-------------|--------------|
| `binary_sensor.git_ha_ppens_dirty` | `on` when there are uncommitted changes | `problem` |

---

## ⚡ Events

Use these events as automation triggers to build notifications, dashboards, or recovery workflows.

| Event | Fired when | Data |
|-------|-----------|------|
| `git_ha_ppens_commit` | A commit is created | `hash`, `message`, `author` |
| `git_ha_ppens_push` | Commits are pushed | `commits_pushed` |
| `git_ha_ppens_pull` | Commits are pulled | `commits_pulled` |
| `git_ha_ppens_fetch` | A fetch completes | — |
| `git_ha_ppens_error` | A git operation fails | `operation`, `error` |
| `git_ha_ppens_secret_detected` | Potential secrets found in tracked files | `findings`, `count` |

---

## 💡 Example Automations

### 🔔 Notify when your instance is behind the remote

A useful GitOps signal: let HA notify you when new commits are available on the remote but haven't been pulled yet (e.g. if auto-pull is disabled).

```yaml
automation:
  - alias: "Git: Notify when behind remote"
    trigger:
      - platform: numeric_state
        entity_id: sensor.git_ha_ppens_commits_behind
        above: 0
    action:
      - service: notify.mobile_app
        data:
          title: "git-ha-ppens"
          message: "{{ states('sensor.git_ha_ppens_commits_behind') }} new commit(s) available on the remote."
```

### ⬆️ Auto-push after every commit

```yaml
automation:
  - alias: "Git: Push after commit"
    trigger:
      - platform: state
        entity_id: sensor.git_ha_ppens_last_commit
    action:
      - service: git_ha_ppens.push
```

### 🔔 Notify when changes are uncommitted for over an hour

```yaml
automation:
  - alias: "Git: Remind to commit"
    trigger:
      - platform: state
        entity_id: binary_sensor.git_ha_ppens_dirty
        to: "on"
        for: "01:00:00"
    action:
      - service: notify.mobile_app
        data:
          title: "git-ha-ppens"
          message: "You have uncommitted configuration changes."
```

### 📅 Weekly configuration snapshot

```yaml
automation:
  - alias: "Git: Weekly snapshot"
    trigger:
      - platform: time
        at: "02:00:00"
    condition:
      - condition: time
        weekday:
          - sun
    action:
      - service: git_ha_ppens.commit
        data:
          message: "Weekly config snapshot"
```

### 🚨 Alert on secret detection

```yaml
automation:
  - alias: "Git: Secret detected alert"
    trigger:
      - platform: event
        event_type: git_ha_ppens_secret_detected
    action:
      - service: notify.mobile_app
        data:
          title: "⚠️ git-ha-ppens Security Alert"
          message: "Found {{ trigger.event.data.count }} potential secret(s) in tracked files!"
```

---

## 🛡️ Auto-Generated .gitignore

The integration automatically creates or updates `.gitignore` with sensible defaults for Home Assistant.

You can customize the `.gitignore` entries at any time via **Settings → Devices & Services → git-ha-ppens → Configure → Edit .gitignore**. The built-in editor lets you add, remove, or modify entries directly from the UI.

| Category | Entries |
|----------|---------|
| **Sensitive files** | `secrets.yaml`, `.storage/`, `.cloud/`, `tls/`, `.ssh/`, `.jwt_secret`, `SERVICE_ACCOUNT.json` |
| **Databases & logs** | `*.db`, `*.db-shm`, `*.db-wal`, `*.log`, `home-assistant_v2.db`, `home-assistant.log*`, `zigbee.db`, `OZW_Log.txt` |
| **System files** | `.HA_VERSION`, `known_devices.yaml`, `ip_bans.yaml` |
| **Python cache** | `__pycache__/`, `*.pyc`, `*.pyo` |
| **Runtime & other** | `.git/`, `deps/`, `tts/`, `.venv/`, `.cache/`, `.claude/`, `custom_components/`, `www/snapshots/`, `.ha_run.lock`, `.exports`, `.timeline`, `.vacuum` |
| **Zigbee2MQTT** | `zigbee2mqtt/state.json`, `zigbee2mqtt/coordinator_backup.json` |
| **Editor swap files** | `*.swp`, `*.swo` |

> 📌 Existing `.gitignore` entries are preserved — only missing defaults are appended.

---

## 🔧 Troubleshooting

<details>
<summary><strong>❌ "Git is not installed"</strong></summary>

Home Assistant OS does not include git by default. Options:
- Use a container/add-on that includes git
- Install git via the SSH & Web Terminal add-on: `apk add git`
</details>

<details>
<summary><strong>❌ Push fails with "permission denied" or "403"</strong></summary>

- Verify your personal access token has the `repo` scope
- Check that the remote URL is correct and the repository exists
- For SSH: ensure the key path is valid and the key is added to your git provider
</details>

<details>
<summary><strong>❌ "Remote origin is not configured"</strong></summary>

Go to **Settings → Devices & Services → git-ha-ppens → Configure** and set a remote URL in the options flow.
</details>

<details>
<summary><strong>❌ Auto-commit not triggering</strong></summary>

- Verify `auto_commit` is enabled in the integration options
- Check that the changed files are not in `.gitignore` or the watcher's ignore patterns (`.git`, `.storage`, `.ssh`, `__pycache__`, `*.db`, `*.log`, etc.)
- Review HA logs for file watcher errors
</details>

<details>
<summary><strong>❌ "Secrets detected" warning</strong></summary>

- Review the flagged files and move sensitive values to `secrets.yaml`
- Ensure `secrets.yaml` is listed in `.gitignore` (it is by default)
- The detection uses regex patterns for common key formats (API keys, tokens, passwords)
</details>

---

## 🤝 Contributing

Contributions are welcome! Whether it's bug reports, feature requests, or pull requests — feel free to get involved.

- 🐛 **Bug reports & feature requests:** [GitHub Issues](https://github.com/manuveli/git-ha-ppens/issues)
- 🔀 **Pull requests:** Fork, create a branch, and submit a PR

### Development Setup

1. Clone this repository
2. Symlink or copy `custom_components/git_ha_ppens` into your HA dev instance's `custom_components/` directory
3. Restart Home Assistant
4. Follow Home Assistant's coding conventions (`ruff`, `mypy`)

---

## Star History

<a href="https://www.star-history.com/?repos=manuveli%2Fgit-ha-ppens&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=manuveli/git-ha-ppens&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=manuveli/git-ha-ppens&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/image?repos=manuveli/git-ha-ppens&type=date&legend=top-left" />
 </picture>
</a>

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

---

<p align="center">
  Made with ❤️ for the Home Assistant community<br>
  <a href="https://github.com/manuveli/git-ha-ppens">github.com/manuveli/git-ha-ppens</a>
</p>
