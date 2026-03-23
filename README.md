# git-ha-ppens

**Version-control your Home Assistant configuration with git — directly from the HA UI.**

![Status: In Development](https://img.shields.io/badge/status-in%20development-orange)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

> **Note:** This project is in early development. The features described below represent the planned scope. Contributions and feedback are welcome!

## Overview

Home Assistant users regularly modify YAML configurations, automations, scripts, and dashboards. One wrong edit can break an entire setup — and without version control, there's no easy way to see what changed or roll back.

**git-ha-ppens** brings git version control into Home Assistant as a native custom integration. Commit, diff, push, and browse your configuration history without leaving the HA interface or touching the command line.

### Design Goals

- **Simple** — Set up via the HA config flow, no manual git init required
- **Secure** — Automatic `.gitignore` management to keep secrets out of your repository
- **Flexible** — Automatic or manual commits, optional remote push

## Planned Features

### Automatic Version Control

- Auto-commit when configuration files change (file watcher on the config directory)
- Configurable commit interval / debounce to avoid excessive commits
- Auto-generated commit messages (e.g., *"Auto: automations.yaml changed"*)

### Manual Control

- Service `git_ha_ppens.commit` — create a commit with a custom message
- Service `git_ha_ppens.push` — push commits to a configured remote
- Service `git_ha_ppens.pull` — pull from remote

### Security & Secrets

- Automatic `.gitignore` for `secrets.yaml`, `.storage/`, and other sensitive files
- Warning if secrets are detected in tracked files
- Pre-commit validation to prevent accidental secret exposure

### Visibility

- Sensor entities exposing git status: last commit, uncommitted changes, current branch, remote sync status
- Binary sensor for dirty working tree

### Remote Integration

- Push to GitHub, GitLab, Bitbucket, or any git remote
- SSH key or token-based authentication

## Installation

### Prerequisites

- Home Assistant 2024.1 or later
- Git installed on the host system

> **HA OS users:** Git availability may require a dedicated add-on or a container with git pre-installed.

### HACS (Recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. Go to HACS → Integrations → three-dot menu → **Custom repositories**.
3. Add `https://github.com/manuveli/git-ha-ppens` with category **Integration**.
4. Search for *git-ha-ppens* and install.
5. Restart Home Assistant.

### Manual

1. Download the latest release from the [Releases](https://github.com/manuveli/git-ha-ppens/releases) page.
2. Copy the `custom_components/git_ha_ppens` folder into your `config/custom_components/` directory.
3. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **git-ha-ppens**.
3. The config flow will guide you through:
   - **Repository path** (defaults to `/config`)
   - **Git user name and email** for commits
   - **Auto-commit** on/off and interval
   - **Remote URL** (optional)
   - **Authentication method** (SSH key / personal access token) if a remote is configured

All options can be changed later via the integration's Options flow.

## Services

| Service | Description | Parameters |
|---|---|---|
| `git_ha_ppens.commit` | Create a git commit | `message` (optional) — custom commit message |
| `git_ha_ppens.push` | Push commits to remote | — |
| `git_ha_ppens.pull` | Pull from remote | — |

## Entities

| Entity | Type | Description |
|---|---|---|
| `sensor.git_ha_ppens_last_commit` | Sensor | Last commit hash (short), message as attribute |
| `sensor.git_ha_ppens_last_commit_time` | Sensor | Timestamp of last commit |
| `sensor.git_ha_ppens_uncommitted_changes` | Sensor | Number of uncommitted file changes |
| `sensor.git_ha_ppens_branch` | Sensor | Current branch name |
| `sensor.git_ha_ppens_remote_status` | Sensor | Ahead/behind count vs. remote |
| `binary_sensor.git_ha_ppens_dirty` | Binary Sensor | On if there are uncommitted changes |

## Example Automations

### Auto-push after every commit

```yaml
automation:
  - alias: "Git: Push after commit"
    trigger:
      - platform: state
        entity_id: sensor.git_ha_ppens_last_commit
    action:
      - service: git_ha_ppens.push
```

### Notify when changes are uncommitted for over an hour

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

### Weekly configuration snapshot

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

## Roadmap

- [ ] Core integration scaffold (config flow, basic sensors)
- [ ] Git commit and push services
- [ ] Auto-commit on file change
- [ ] `.gitignore` management and secrets protection
- [ ] Remote authentication (SSH / token)
- [ ] Diff view panel
- [ ] Commit history panel
- [ ] HACS default repository submission

## Contributing

Contributions are welcome! Whether it's bug reports, feature requests, or pull requests — feel free to get involved.

Development setup:

1. Clone this repository.
2. Symlink or copy `custom_components/git_ha_ppens` into your HA dev instance's `custom_components/` directory.
3. Restart Home Assistant.

Please follow Home Assistant's coding conventions (ruff, black, mypy).

## License

This project is licensed under the [MIT License](LICENSE).
