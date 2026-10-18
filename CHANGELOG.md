# Changelog

The AFC server's releases, newest first. AFC has ONE version number for the whole product, this
server and the AFC_Frontend website together: a release is tagged `vX.Y.Z` in every repo it changed
(this repo on its merge commit to main), and the number is counted across both (`release.json`).

Rule R89 (owner, 17 Oct 2026). Write the bullets in the PR, under `## Unreleased`, in plain words
about what a person or a partner notices. The headings decide the kind of release:

- `### Breaking` makes it a **major** (v7.4.2 to v8.0.0): something stops working for people or
  systems outside. A partner API field or address removed or renamed is Breaking (Game Evo reads
  it); so is anything that makes every user sign in again. Only on the owner's word.
- `### Added` or `### Changed` makes it a **minor** (v7.4.2 to v7.5.0): a new feature, endpoint or
  API field (additive), or a deliberate change to how a feature behaves.
- `### Fixed`, `### Security` or `### Internal` alone makes it a **patch** (v7.4.2 to v7.4.3).

At release time `node ~/.claude/skills/versioning/scripts/release.mjs next` prints the number;
rename `## Unreleased` to it, merge, then tag the merge commit. A version tag pushed with the wrong
number is blocked before the push.

## Unreleased

## v7.1.129 - 2026-10-18

### Fixed
- A team can no longer get a 7th player by any door. An admin adding someone now meets the
  6-player limit and the one-coach / one-manager / one-analyst rule (the override only lifts the
  9-person total), and two people joining the same team at the same moment can no longer both take
  the last playing seat (#136).
- An admin "move from their current team" that the new team refuses no longer leaves the player on
  no team: every check runs before they are taken off the old one (#136).
- An SES event posted by an SNS subscription with "Raw message delivery" switched on is refused as
  `sns_raw_delivery` (it was `sns_wrong_topic`, which pointed at the wrong setting), and every
  refused SES event is written to the server log with its code (#135).

### Internal
- known-bugs `be-team-seat-unlocked` (new kind `call-outside-with`): a team member created outside
  the team's seat lock fails the checks and the deploy (#136).

## v7.1.128 - 2026-10-17

### Fixed
- The API's own coded refusals (payments, payouts, Help bot offline or timeout, OCR provider errors,
  results import, email news) reach the screen: nginx no longer swaps every API 502/503/504 for the
  maintenance page (`deploy/vps/nginx-afc.conf`, applied to the box at the ship) (#134).
- An email news test send refused by Amazon SES says why (`mail_ses_not_authorized`,
  `mail_ses_unverified`, `mail_ses_throttled`, `mail_ses_login_refused`, answered 424); "sending is
  not set up yet" is 409 (#134).

### Internal
- Releases follow rule R89: this changelog and `release.json` (one number with AFC_Frontend).
- known-bugs `be-nginx-swallows-api-errors` keeps the API block from intercepting again (#134).

## v7.1.127 - 2026-10-17

The release this server's tags start from: email news (AFC-B #132, merge 29e4bc4f) shipped with the
website's v7.1.127. The server had no tags before; its history is the merged pull requests.
