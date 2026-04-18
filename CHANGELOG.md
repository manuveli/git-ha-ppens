# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/manuveli/git-ha-ppens/compare/v0.4.3...HEAD
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
