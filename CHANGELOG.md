# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-07-14

### Fixed
- Improve AI commit-message context by distributing the bounded diff across changed files and compacting long structured-data lines (#77)

### Security
- Redact common credentials, token formats, authorization headers, credential-bearing URLs, and private keys from AI commit-message prompts on a best-effort basis (#77)

## [1.0.1] - 2026-07-12

### Fixed
- Show the current HEAD above the configuration restore selector while keeping it non-selectable
- Clarify in all translations and documentation that selecting the first earlier commit restores the state before the latest change

## [1.0.0] - 2026-07-11

> 🎉 **git-ha-ppens 1.0.0 is here!**
>
> Version 1.0 marks the first stable major release of git-ha-ppens. The new Configuration Restore workflow completes the GitOps lifecycle in Home Assistant, making it possible to safely return to any previous tracked configuration directly from the UI while preserving Git history.

### Added
- Add a native, confirmation-based UI for restoring the tracked configuration tree from a recent commit or an older SHA without rewriting Git history
- Add bounded commit and file previews, mandatory Home Assistant validation for live-configuration restores, optional remote push, and the `git_ha_ppens_restore` event
- Security: Block historical restores while staged, unstaged, or untracked changes exist and roll back failed restores to the exact original `HEAD`

## [0.9.1] - 2026-07-08

### Fixed
- Make stable entity IDs multi-entry safe by using repo-based IDs for additional git-ha-ppens config entries (#75)
- Make the #74 button entity ID migration entry-aware so multiple config entries are not migrated to the same button IDs (#75)

## [0.9.0] - 2026-06-28

### Fixed
- Use stable, language-independent entity IDs for git-ha-ppens entities so button IDs are no longer derived from Home Assistant areas, device names, or translations (#74)
- BREAKING: Automatically migrate existing git-ha-ppens button entity IDs to button.git_ha_ppens_push, button.git_ha_ppens_pull, button.git_ha_ppens_fetch, and button.git_ha_ppens_discard_changes when those target IDs are free. Existing automations, dashboards, scripts and other external references to the old button entity IDs must be updated manually (#74)

## [0.8.3] - 2026-06-11

### Added
- Include changed file paths in successful commit and pull events (#74)

## [0.8.2] - 2026-06-10

### Added
- Add a service and optional button to discard tracked local changes (#72)

### Fixed
- Update the button platform device version during release preparation

## [0.8.1] - 2026-06-10

### Fixed
- Make the Push button commit pending changes using the configured standard or AI commit message before pushing (#73)

## [0.8.0] - 2026-06-10

### Added
- Add native Home Assistant buttons for Push, Pull, and Fetch (#73)

## [0.7.1] - 2026-06-09

### Fixed
- Persist last fetch, pull, and push timestamps across Home Assistant restarts (#71)

## [0.7.0] - 2026-05-31

### Added
- Optional pre-deploy check: when enabled, a Home Assistant configuration check runs after a pull/auto-pull. If the check fails, the pull is rolled back (`git reset --hard`) to the last working state, a `git_ha_ppens_check_failed` event is fired, and a persistent notification surfaces the errors — blocking broken remote changes from reaching the live system (#69)
- New `pre_deploy_check` option in setup and configuration UI (default off)

## [0.6.5] - 2026-05-13

### Fixed
- Moved blocking `.gitignore` file read out of the Home Assistant event loop to prevent `Detected blocking call to read_text` warnings (#68)

## [0.6.4] - 2026-05-11

### Fixed
- Corrected misleading `ai_agent_id` field description — leaving it empty disables AI commit messages instead of using a default agent (#66)
- Added missing AI-related field translations for German (de.json)

## [0.6.3] - 2026-05-11

### Fixed
- File watcher now reads ignore patterns from `.gitignore` on disk instead of using a hardcoded list — removing an entry from `.gitignore` (e.g. `.storage`) now correctly enables auto-commit for those files

## [0.6.2] - 2026-05-10

### Fixed
- `.gitignore` defaults were re-applied on every HA restart, overwriting manual user edits and potentially removing user-added files from tracking

## [0.6.1] - 2026-05-10

### Added
- Periodic `git fetch` with configurable `fetch_interval` (default 300s)
- New sensors: Last Fetch Time, Last Pull Time, Last Push Time, Commits Behind, Commits Ahead
- New `git_ha_ppens.fetch` service for manual fetch without merge
- `fetch_interval` option in setup and configuration UI

### Fixed
- Auto-pull never triggered because status polling did not fetch from remote first
- JSON syntax error in `strings.json` (duplicate `ssh_key_path` key)

## [0.5.0] - 2026-04-18

### Added
- Added Hindi (`hi`), Portuguese (`pt`) and Turkish (`tr`) translations for the config and options flows, entity names and error messages.

## [0.4.4] - 2026-04-18

### Added
- Added Changelog Functionality

## [0.4.3] - 2026-04-06

### Added
- Spanish, French, and Italian translations (i18n)

### Fixed
- Aligned `WATCHER_IGNORE_PATTERNS` with `DEFAULT_GITIGNORE_ENTRIES`
- Excluded `CLAUDE.md` from repository tracking

## [0.4.2] - 2026-04-03

### Added
- HACS integration icon added to repository root

### Fixed
- AI commit message failures caused by Jinja2 syntax in diffs
- Icon path corrected for HACS logo display

## [0.4.1] - 2026-04-01

### Fixed
- Missing comma in configuration caused parse error

## [0.4.0] - 2026-04-01

### Added
- AI-powered commit message generation via Home Assistant conversation service
- German translation

## [0.3.1] - 2026-03-31

### Added
- Configurable `.gitignore` entries editable through the options flow after setup

## [0.3.0] - 2026-03-31

### Added
- Configurable `.gitignore` entries editable during setup
- `.ssh/` added to default gitignore and file watcher exclusions
- Editor swap files (`*.swp`, `*.swo`) added to default gitignore entries

## [0.2.5] - 2026-03-30

### Added
- Repository link displayed on the service info page

## [0.2.4] - 2026-03-28

### Fixed
- Tracked files correctly untracked when `.gitignore` is updated
- `__pycache__/` and `*.pyc` excluded via gitignore
- `www/snapshots/` exclusion corrected

## [0.2.3] - 2026-03-27

### Fixed
- "Not in a git directory" error on Docker installations

## [0.2.2] - 2026-03-26

### Added
- `zigbee2mqtt/coordinator_backup.json` added to default gitignore entries
- MIT LICENSE file

## [0.2.1] - 2026-03-26

### Changed
- Release artifacts now include both a versioned zip and a static `git_ha_ppens.zip`

## [0.2.0] - 2026-03-26

### Changed
- Updated integration logo

## [0.1.0] - 2026-03-25

### Added
- Initial release
- Automatic commit/push on file changes via file watcher
- Auto-pull from remote repository
- Sensors: `last_commit`, `last_commit_time`, `uncommitted_changes`, `branch`, `remote_status`, `dirty`
- Remote configuration with HTTPS token and SSH key auth
- Default `.gitignore` entries for common HA files
- Automated release workflow for HACS

[Unreleased]: https://github.com/manuveli/git-ha-ppens/compare/v1.0.0...HEAD
[1.1.0]: https://github.com/manuveli/git-ha-ppens/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/manuveli/git-ha-ppens/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.9.1...v1.0.0
[0.9.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.8.3...v0.9.0
[0.8.3]: https://github.com/manuveli/git-ha-ppens/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/manuveli/git-ha-ppens/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.6.5...v0.7.0
[0.6.5]: https://github.com/manuveli/git-ha-ppens/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/manuveli/git-ha-ppens/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/manuveli/git-ha-ppens/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/manuveli/git-ha-ppens/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.5.0...v0.6.1
[0.5.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/manuveli/git-ha-ppens/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/manuveli/git-ha-ppens/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/manuveli/git-ha-ppens/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.2.5...v0.3.0
[0.2.5]: https://github.com/manuveli/git-ha-ppens/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/manuveli/git-ha-ppens/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/manuveli/git-ha-ppens/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/manuveli/git-ha-ppens/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/manuveli/git-ha-ppens/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/manuveli/git-ha-ppens/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/manuveli/git-ha-ppens/releases/tag/v0.1.0
