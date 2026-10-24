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

### Changed
- A team can send its own map result only while its group is playing: from the group's start
  time (in the event's time zone) until its maps are done, 30 minutes a map, plus 3 hours. Outside
  that the server refuses with `claims_not_open_yet` or `claims_closed`. Reading what the team sent
  stays open. New `GET events/team-map-results/open/?event=<slug>` lists the maps a team may claim
  right now, only from its own groups; the event page builds its picker from it.

## v7.3.0 - 2026-10-22

### Added
- Each organizer has its own memory of how player names come out of screenshots, and it is removed
  with the organizer. Every committed or applied review teaches it: the player a reviewer kept or
  picked is remembered for that spelling, and a suggestion they turned down (changed, or marked "not
  on the platform") is remembered as wrong for it and is not picked again. The next read of that
  spelling, or a very close one, is matched from memory, only to a player on that roster (#334, #335).
- Each organizer has its own line for leaderboard screenshot reads: two maps are read at a time and
  organizers take turns, so one organizer's big batch no longer holds up another's maps. A map waiting
  in line reports its place and the expected wait; a read stuck for 10 minutes is stopped with a
  sentence and frees its place (#334).

### Changed
- The OCR name memory used to be one table for everyone, written only when an admin changed a match
  and read by exact spelling; it held nothing on production. It is now per organizer and learns from
  every review. The AI prompt only uses the event organizer's own memory.

## v7.2.0 - 2026-10-22

### Changed
- Result screenshots are now read on the server first, for free, by PaddleOCR's PP-OCRv6 reader;
  only a screenshot it is not sure about goes to the AI engine. The reader was built in June but
  its packages were never installed on the server, so every read went to Gemini (#327, #328).
  Measured on the 12 admin-checked screenshots: 98% of player rows right, every kill count it
  found right, about 3 seconds a screenshot on the server.
- An organizer with no AI key can use OCR: a sure read is used as it is, and an unsure one comes
  back as a draft marked "check every row" instead of the "connect your own AI key" refusal.
  Uploads that save results straight away, with no review step, still need a sure read or a key.
  An organization AFC switched OCR off for stays off, on-site reader included.
- A screenshot uploaded sideways or upside down is turned the right way up before it is read.

### Fixed
- Leaderboard OCR batches no longer refuse to start for an organizer whose free read was used:
  seven batches made on 24 and 26 Sept 2026 never ran.
- A reopened OCR session now says which reader read it.
- A map read from several screenshots says "check every row" when any one of them needed it, not
  only when the last one did.

### Internal
- The known-bugs check for packages the server code needs no longer exempts the OCR reader's
  packages, which is what let them stay off the server unnoticed.

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
